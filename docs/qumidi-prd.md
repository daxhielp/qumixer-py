# PRD — `qumidi`: Python control library for Allen & Heath Qu mixers

**Version:** 1.1 (implementation spec)
**Target implementer:** Claude Code
**Source of truth:** *Qu MIDI Protocol, Firmware V1.9+, Oct 2021* (referred to below as "the spec"). Where this PRD and the spec disagree, this PRD wins — the disagreements are deliberate and documented in §14 (Errata).

**Revision 1.1** corrects several internal contradictions and adds the edge cases and requirements listed in §19. Items that changed carry a `▲` marker in the body.

---

## 1. Summary

Build a Python library that provides complete programmatic control and state observation of Allen & Heath Qu-series digital mixers (Qu-16, Qu-24, Qu-32, Qu-Pac, Qu-SB) over MIDI-on-TCP.

The library is **table-driven**: a declarative parameter registry describes every controllable parameter, and both the flat command API and the ergonomic object API are generated from it. Protocol knowledge lives in exactly one place.

### Goals

- Full read/write coverage of the NRPN parameter set: faders, mutes, pan, sends, assigns, pre/post, preamp, PEQ, HPF, GEQ, gate, compressor, delay, trim, polarity, insert, scene recall, channel naming.
- Accurate local state cache, populated by the Sysex system-state handshake and kept current by unsolicited MIDI from the mixer.
- Change notification so callers can react to surface moves, not just issue writes.
- Experimental live metering with correct 7-bit unpacking and dB decoding.
- Async-first, with a fully supported synchronous facade.
- Testable end-to-end without hardware.

### Non-goals for v1

- USB MIDI transport (the transport interface must accommodate it; no implementation).
- DAW emulation (HUI/Mackie translation). The raw MIDI-strip layer is exposed; translation is not.
- Audio streaming of any kind.
- FX unit parameter editing beyond delay time/link (the spec does not document it).
- Qu-Pad protocol compatibility beyond what the spec describes.

---

## 2. Package and project conventions

- **Package name:** `qumidi` — this is the official name, both as the import package and the PyPI distribution name. **Before first publish, confirm `qumidi` is actually free on PyPI** (`pip install qumidi` should fail with "no matching distribution," or check `https://pypi.org/project/qumidi/` directly) — this has not been independently verified against the live index. If it turns out to be taken, fall back to a distribution name like `qumidi-mixer` or `qu-midi` while keeping `qumidi` as the import package, and update the README install instructions accordingly.
- **Python:** 3.10+.
- **Core dependencies:** none. Standard library only (`asyncio`, `socket`, `struct`, `dataclasses`, `enum`).
- **Dev dependencies:** `pytest`, `pytest-asyncio`, `pytest-cov`, `hypothesis`, `mypy`, `ruff`.
- **Optional extra** (reserved, not implemented): `qumidi[usb]` → `mido`, `python-rtmidi`.
- **Typing:** fully annotated, `mypy --strict` clean.
- **Style:** `ruff` defaults; line length 100.
- **License:** MIT.

### Repository layout

```
qumidi/
  __init__.py            # public exports
  transport/
    base.py              # Transport ABC
    tcp.py               # TcpTransport
  midi/
    parser.py            # incremental MIDI stream parser
    messages.py          # message dataclasses + builders
    nrpn.py              # NRPN assembly/disassembly
  protocol/
    constants.py         # sysex headers, command bytes, box IDs
    channels.py          # ChannelId, channel classes, model capability maps
    registry.py          # THE parameter registry
    params.py            # ParamSpec dataclass, VX modes, domains
    units.py             # dB/Hz/time conversion tables + interpolation
    sysex.py             # sysex encode/decode incl. 7-bit packing
    meters.py            # meter layout maps + frame decoding
  state.py               # StateCache, tri-state values
  events.py              # change event bus
  client_async.py        # AsyncQuMixer
  client_sync.py         # QuMixer (threaded facade)
  model/                 # object facade (InputChannel, Mix, Group, ...)
  testing/
    fake_qu.py           # FakeQu server
    fixtures/            # recorded byte streams, golden vectors
  cli.py                 # qumidi record / monitor / dump-state
tests/
docs/
```

---

## 3. Architecture

Five layers, each depending only on the one below it.

```
5. Object facade      mixer.input(6).fader.db = -6.0
4. Client             AsyncQuMixer / QuMixer  — state, sync, events, lifecycle
3. Registry           declarative ParamSpec table → encode/decode any parameter
2. MIDI codec         stream parser, message builders, NRPN assembly
1. Transport          TcpTransport — bytes in/out, keepalive, reconnect
```

**Critical invariant:** layers 1–2 know nothing about mixers. Layer 3 knows nothing about connections. Layer 5 contains zero protocol constants — it is a thin proxy over layer 4.

---

## 4. Layer 1 — Transport

### 4.1 Interface

```python
class Transport(ABC):
    async def connect(self) -> None: ...
    async def close(self) -> None: ...
    async def send(self, data: bytes) -> None: ...
    def messages(self) -> AsyncIterator[MidiMessage]: ...
    @property
    def is_connected(self) -> bool: ...
    def on_state_change(self, cb: Callable[[ConnectionState], None]) -> None: ...
```

`ConnectionState` ∈ `{DISCONNECTED, CONNECTING, CONNECTED, SYNCING, READY, RECONNECTING}`.

The transport yields **parsed messages**, not bytes. `TcpTransport` owns a `MidiStreamParser` internally; a future `UsbTransport` will get pre-framed messages from mido and skip the parser. This asymmetry is the reason the parser is not part of the client.

### 4.2 `TcpTransport`

- Connects to `host:51325` (default port constant `QU_TCP_PORT = 51325`).
- Connect timeout configurable, default 5s.
- **The mixer permits exactly one TCP MIDI connection at a time.** If a Qu-Pad session or another client holds it, connect will fail or hang. Surface this as `ConnectionRefusedError` wrapped in `QuConnectionError` with a message that explicitly names the single-connection limit — this will be the #1 user support question.
- Reads into a buffer and feeds the parser incrementally. Never assume a TCP read boundary equals a message boundary.

### 4.3 Active sensing / keepalive

The spec's behaviour, restated precisely because it is asymmetric:

- The Qu sends `FE` on connect and roughly every 300ms during outbound idle.
- Once the Qu **receives** an `FE` from the client, it begins expecting regular inbound data and will close the connection after **12 seconds** of silence.
- If the system-state request is sent with `iPadFlag = 1`, the Qu expects an `FE` within **5 seconds** or it closes the connection.

Implementation:

- Send `FE` every `keepalive_interval` (default **2.0s**) whenever the connection is up. Sending unconditionally is safe under both regimes and satisfies the 5s iPad-flag deadline.
- Track `last_inbound_at`. If no bytes of any kind arrive for `inbound_timeout` (default **10.0s**), declare the link dead and trigger reconnect. Do not wait for the socket to notice.
- `FE` bytes are consumed by the transport for liveness accounting and **not** forwarded to upper layers by default (expose `forward_active_sense: bool = False` for diagnostics).

### 4.4 Reconnect

- `auto_reconnect: bool = True`.
- Exponential backoff: 0.5s → 30s cap, ×2 per attempt, ±20% jitter. `max_reconnect_attempts: int | None = None` (unlimited by default); on exhaustion, transition to `DISCONNECTED` and emit a terminal event rather than spinning forever.
- On every successful reconnect: mark the **entire** state cache `STALE` before re-running the full sync. Never serve pre-disconnect values as current.
- Emit `ConnectionState` transitions on the event bus so applications can grey out UI.
- If metering was enabled before the drop, re-enable it after sync completes.
- ▲ **An explicit `close()` disables reconnect.** A user-initiated close must not race with a pending backoff timer and resurrect the connection. `close()` is idempotent and cancels any in-flight reconnect task.
- ▲ **Drop all pending write expectations (§10.3) and the outbound queue (§4.5) on disconnect.** Echoes for writes issued before the drop will never arrive; leaving expectations live causes the first post-reconnect surface move to be misclassified as an echo and swallowed.
- ▲ **Re-learn `N`, BoxID and firmware on every sync, never cache them across a reconnect.** The operator may have changed the mixer's MIDI channel, or the host may now be a different console at the same IP. If the BoxID differs from the previously observed one, log a warning and rebuild the cache from empty rather than reusing keys for a different model.
- ▲ If `sync_names` is enabled, re-run the name sweep after reconnect — names are not in the state push (§9.4) and a scene may have been recalled while disconnected.

### 4.5 ▲ Outbound pacing

MIDI-over-TCP has no flow control back to the console's MIDI parser. A caller doing a 128-step fade, a bulk scene push, or a `set_all_mutes()` loop can emit thousands of bytes in one event-loop tick, and the mixer will drop what it cannot parse — silently, exactly like a wrong `VX`.

- All outbound traffic passes through a single **ordered send queue**. Writes are transmitted in the order they were issued; concurrent `set()` calls from different tasks never interleave the four CCs of an NRPN triplet.
- `max_send_rate` (default **1000 bytes/s**, i.e. roughly the 31.25 kbaud DIN-MIDI rate the console's parser is designed around) throttles the queue. Configurable; `None` disables throttling for users who have measured their console's tolerance.
- The keepalive `FE` and meter enable/disable bypass the throttle — liveness must never be starved by a bulk write.
- `await mixer.set(...)` resolves when the bytes have been handed to the socket **and drained**, not when they were queued. Expose `mixer.pending_writes` so applications can observe backlog.
- Sysex writes (name set) and NRPN writes share the queue so that a name sweep cannot be reordered ahead of a fader move.

---

## 5. Layer 2 — MIDI codec

### 5.1 Stream parser (`MidiStreamParser`)

A byte-at-a-time incremental parser. It must correctly handle, because all of these occur in real Qu traffic:

1. **Running status** — status byte omitted on repeated same-status messages. The Qu sends long NRPN bursts during state sync; running status is likely.
2. **Sysex spanning arbitrary TCP boundaries** — a single meter frame will not arrive in one read.
3. **System real-time bytes interleaved anywhere**, including *inside* a sysex payload and *between* the four CC messages of an NRPN sequence. `FE` arrives every 300ms; this is not a hypothetical. Real-time bytes must be extracted and dispatched without disturbing the in-progress message, and must not clear running status.
4. **Truncated sysex** — a new status byte before `F7` aborts the sysex. Discard and log; do not raise.
5. Unknown/undocumented status bytes — skip gracefully.
6. ▲ **System *common* messages (`F1`–`F6`) clear running status; system *real-time* (`F8`–`FF`) does not.** This is the standard MIDI rule and the two classes must not be conflated — item 3 above applies only to `F8`–`FF`.
7. ▲ **A bounded sysex buffer.** A meter frame on a Qu-32 is ~1.8 kB (§12.3), but a corrupted stream with a lost `F7` would otherwise grow the buffer without limit. Cap at `max_sysex_bytes` (default **65536**); on overflow, discard the in-progress sysex, log a warning, and resynchronise at the next status byte.
8. ▲ **A lone `F7` with no open sysex is ignored,** not treated as an error.
9. ▲ **Data bytes with the high bit set never reach a value field.** Any byte ≥ `0x80` encountered where a data byte is expected terminates the current message; the parser re-dispatches it as a status byte.

The parser is a pure function of its byte input with no I/O. `feed(data: bytes) -> list[MidiMessage]`. It must never raise on any input; malformed input yields fewer messages and a log line.

### 5.2 Message types

Dataclasses: `NoteOn`, `NoteOff`, `ControlChange`, `ProgramChange`, `SysEx`, `SystemRealtime`. All carry `channel` where applicable and are frozen/hashable.

### 5.3 NRPN assembly

The Qu's NRPN usage is idiosyncratic and must be modelled exactly:

```
BN 63 CH    NRPN MSB   = channel id
BN 62 ID    NRPN LSB   = parameter id
BN 06 VA    Data MSB   = parameter value
BN 26 VX    Data LSB   = index / discriminator  ← NOT a fine value
```

`VX` is **not** a value refinement. It is either a filler constant (`07` or `00`, varying per parameter) or a genuine index (send destination, GEQ band, dSNAKE socket, FX tap). This is registry data; see §7.

**Encoder** emits all four CCs in the order above, always, on the mixer's MIDI channel `N`.

**Decoder** maintains a per-MIDI-channel accumulator holding the most recent `(CH, ID)` and emits a `ParamUpdate` when a Data MSB (`06`) arrives. Notes:

- Emit on `06`, not on `26`. Some senders omit the LSB. Buffer briefly (or emit on `06` and revise on a following `26` within the same burst) — prefer: hold the pending update for at most one subsequent CC; if it is `26`, attach `VX` and emit; otherwise emit with `VX = None`.
- ▲ **The held update must also be flushed on three other triggers, or the last parameter of a burst is lost forever:** (a) the sysex `14` end-of-sync marker, (b) any non-CC message, and (c) a `pending_flush_timeout` (default **50 ms**) of inbound silence. Test explicitly that a stream ending in `... B0 06 62` with nothing after it still yields one `ParamUpdate`.
- ▲ Real-time bytes (`FE` every 300 ms) must **not** flush or disturb the held update — they arrive constantly and would break every NRPN pair if treated as "a subsequent message".
- The `(CH, ID)` pair persists across messages under running status; do not reset it after each value.
- An unrecognised `(CH, ID)` must not raise. Emit a `RawParamUpdate` and log at debug. Undocumented parameters exist.

▲ **Registry resolution for inbound decode.** A raw update carries `(CH, ID, VX)`. Resolve in this order, and stop at the first hit:

1. Exact `(channel_class(CH), ID, VX)` — needed for the FX-delay `48` collision (§14.5).
2. `(channel_class(CH), ID)` — the normal case; `VX` is then either the spec'd filler or a genuine index.
3. `(ID,)` across all classes, with a debug log noting the class was unexpected.

Keying on `ID` alone is insufficient: the same ID means different things on different channel classes (e.g. GEQ `70` exists only on mixes/LR, Local Gain `19` only on inputs), and a future firmware could reuse an ID. ▲ **If a match is found at step 2 whose `VxMode` is `Fixed(k)` and the observed `VX ≠ k`, decode it anyway but log at warning** — this is the signature of a transcription error in the registry and is exactly what the golden vectors (§13.2) exist to catch.

---

## 6. Layer 3 — Channels and models

### 6.1 Channel identifiers

`ChannelId` is a typed value (int subclass or frozen dataclass) constructed through factories, never a bare hex literal at call sites.

▲ `ChannelClass` (referenced by `ParamSpec.applies_to`, §7.1) is an explicit enum with one member per row of the table below: `FX_SEND, FX_RETURN, DCA, INPUT, STEREO_INPUT, MUTE_GROUP, MIX_MONO, MIX_STEREO, LR, GROUP, MATRIX`. Every `ChannelId` exposes `.cls`. Splitting `MIX_MONO` from `MIX_STEREO` and `LR` from both matters: pan and delay behave differently on them (§7.2), and the meter layout treats them as separate blocks (§12.3).

| Class | Range | Notes |
|---|---|---|
| FX Send 1–4 | `0x00`–`0x03` | Qu-16: FX 1–2 only |
| FX Return 1–4 | `0x08`–`0x0B` | Qu-16: FX 1–2 only |
| DCA Group 1–4 | `0x10`–`0x13` | |
| Input 1–32 | `0x20`–`0x3F` | count is model-dependent |
| Stereo Input ST1–ST3 | `0x40`–`0x42` | |
| Mute Group 1–4 | `0x50`–`0x53` | |
| Mix 1,2,3,4 | `0x60`–`0x63` | mono mixes |
| Mix 5-6, 7-8, 9-10 | `0x64`–`0x66` | stereo pairs |
| Main LR | `0x67` | |
| Group 1-2 … 7-8 | `0x68`–`0x6B` | not Qu-16 |
| Matrix 1-2, 3-4 | `0x6C`, `0x6D` | not Qu-16 |

Note that "Mix 1 to 10" occupies only seven ids because 5-6/7-8/9-10 are stereo. Model this explicitly: `Mix.MIX1 … Mix.MIX4, Mix.MIX56, Mix.MIX78, Mix.MIX910, Mix.LR`.

### 6.2 Model capabilities

▲ Booleans are not sufficient here. The meter layout (§12.3) gives Qu-24 **two** stereo group blocks against Qu-32's four, while §6.1 assigns four group channel ids (`0x68`–`0x6B`) unconditionally. A `has_groups: bool` cannot express that difference and would let the library address Group 5-6 on a console that has none. Model counts, not flags:

```python
@dataclass(frozen=True)
class ModelCaps:
    name: str
    box_id: int
    mono_inputs: int
    stereo_inputs: int      # ST1-ST3
    mono_mixes: int         # Mix 1-4
    stereo_mixes: int       # Mix 5-6, 7-8, 9-10
    groups: int             # stereo groups
    matrices: int           # stereo matrices
    fx_units: int
```

| Model | BoxID | Mono inputs | Stereo in | Mono mix | Stereo mix | Groups | Matrix | FX |
|---|---|---|---|---|---|---|---|---|
| Qu-16 | 1 | 16 | 3 | 4 | 3 | 0 | 0 | 2 |
| Qu-24 | 2 | 24 | 3 | 4 | 3 | 2 † | 2 | 4 |
| Qu-32 | 3 | 32 | 3 | 4 | 3 | 4 | 2 | 4 |
| Qu-Pac | 4 | 32 | 3 | 4 | 3 | 4 | 2 | 4 |
| Qu-SB | 5 | 32 | 3 | 4 | 3 | 4 | 2 | 4 |

† ▲ **Unverified and contradictory.** The `2` for Qu-24 is inferred from the meter block table; the channel-id table implies 4. This is an M6 hardware-verification item (§13.5): write to Group 5-6 on a Qu-24 and observe whether it responds. Until verified, `groups` for Qu-24 must be marked provisional in code and capability checks on Qu-24 groups should **warn, not raise**, so a wrong guess here cannot break working code.

▲ Note that mixes and matrices are *stereo pair* counts, matching the channel-id table (§6.1): `mono_mixes=4, stereo_mixes=3` is seven channel ids covering "Mix 1 to 10", not ten.

Model is learned from the state-sync reply. Before sync completes, model is `None` and capability checks are permissive with a warning. After sync, requesting an unavailable channel raises `UnsupportedOnModelError` with a clear message ("Qu-16 has no audio groups"). Callers may pass `model=` at construction to enforce capabilities before connecting.

▲ **An unknown BoxID must not be fatal.** A future console, or a Qu-SB firmware variant, may report a BoxID outside the table. Fall back to the most permissive capability set, set `mixer.model = None` with `mixer.raw_box_id` populated, log a warning naming the unrecognised id, and continue. Refusing to connect to hardware that would otherwise work is the worse failure.

▲ **Guard the firmware floor.** The `11` reply carries `<Major> <Minor>`. This spec is V1.9+. If the console reports below 1.9, log a prominent warning that parameter coverage is unverified below the documented firmware; do not raise, and do not silently pretend the registry applies.

### 6.3 Layers, custom layers, and surface position

**Core invariant: the Qu MIDI protocol is entirely identity-addressed and never position-addressed.** No message in the spec carries a physical strip position or a layer index. Implement to this invariant and do not build layer awareness into the core.

Consequences the implementer must not get wrong:

- **`CH` is always a channel identity, never a strip number.** Input 6 is `0x25` whether it sits on Layer A strip 6, Custom Layer strip 3, or nowhere on the surface at all. Writes are unaffected by which layer is currently selected on the console.
- **Inbound traffic is also identity-addressed.** When an operator moves a physical fader, the Qu transmits NRPN Fader with `CH` set to the channel's identity, not the strip it was moved on. A listener therefore always knows *which channel* changed and can never know *which strip* was touched. This is the correct behaviour for state tracking and requires no mapping.
- **DCA masters, mute group masters, mixes and groups are channels too** (`0x10`–`0x13`, `0x50`–`0x53`, `0x60`–`0x6D`). A DCA master placed on a custom layer is addressed by its DCA identity like anything else.
- **Qu-Pac and Qu-SB have no physical faders.** Layers there are purely an app-side concept. Another reason surface geometry must not leak into the core.

**MIDI strips are a separate namespace, not channels on a layer.** The "MIDI 1–32" strips described in §7.2 are distinct virtual entities that can be *placed* on the Custom Layer; they are not audio channels. They are numbered `0x00`–`0x1F` by their own identity (MIDI strip 5 sends `0x04` regardless of where it is placed), they live on MIDI channel `N+1`, and moving one emits **no** channel NRPN at all. Code listening only on channel `N` will not see MIDI strip activity. See erratum §14.7 for the type-safety requirement.

**What the library cannot do, and must say so plainly.** There is no MIDI message to query the layer configuration, to read which channels are assigned to which Custom Layer positions, or to learn which layer is currently selected. Applications that need to mirror the physical surface — a companion remote, a "what did the operator just touch" display — cannot obtain that mapping from the mixer. Document this limitation explicitly in the README rather than letting users discover it.

**Optional `SurfaceMap` helper.** Provide a small, purely client-side utility in `qumidi/layout.py` for applications that need a position↔identity mapping:

```python
custom = SurfaceMap.from_dict({1: Input(6), 2: Input(7), 3: Dca(1), 4: Mix.MIX3})
custom.channel_at(3)          # Dca(1)
custom.position_of(Input(7))  # 2
```

Requirements: hand-authored or loaded from JSON/YAML; no protocol involvement whatsoever; no attempt to infer the mapping from traffic. Its docstring must state that the map is user-supplied configuration, that the library cannot validate it against the console, and that **recalling a scene can change the console's custom layer assignment without any notification**, silently invalidating the map. Applications that care should re-load their map after a scene recall.

---

## 7. Layer 3 — The parameter registry

This is the core asset. Everything else is generic machinery over it.

### 7.1 `ParamSpec`

```python
@dataclass(frozen=True)
class ParamSpec:
    name: str                      # "fader", "peq.hf.gain"
    nrpn_id: int
    vx: VxMode                     # see below
    domain: Domain                 # value semantics
    applies_to: frozenset[ChannelClass]
    direction: Direction = BIDIRECTIONAL
    tier: Tier = SUPPORTED
    notes: str = ""
```

`VxMode` is one of:
- `Fixed(0x07)` / `Fixed(0x00)` — constant filler byte
- `SendTarget` — index selects mix/group/matrix/FX destination
- `GeqBand` — 0x00–0x1B
- `DsnakeSocket` — 0x00–0x27
- `FxTap` — 0x05 left, 0x07 right, 0x06 link

`Domain` is one of: `Boolean(off=0x00, on=0x01)`, `Raw7Bit(lo=0x00, hi=0x7F)`, `Interpolated(table)`, `Enum(mapping)`, `FlaggedIndex(on_flag=0x40, count=4)`.

▲ **`Direction` and `Tier` must be defined, not implied.** `Direction ∈ {TX_ONLY, RX_ONLY, BIDIRECTIONAL}`; writing to an `RX_ONLY` parameter (Group Mode `5E`, dSNAKE Patch `5D`) raises `ParameterRangeError` rather than emitting bytes the mixer ignores. `Tier ∈ {VERIFIED, SUPPORTED, EXPERIMENTAL}` — `VERIFIED` means confirmed against hardware in M6 and is the only tier that may be set by the calibration run, never by hand.

▲ **`Raw7Bit` carries explicit bounds and every write is range-checked before encoding.** Several parameters do not use the full `00`–`7F` span — Pan stops at `4A`, input Delay Time at `40`, GEQ band index at `1B`, dSNAKE socket at `27`. Out-of-range writes raise `ParameterRangeError`; out-of-range *reads* from the console are clamped and logged, never rejected.

▲ **`FlaggedIndex` is a set-valued parameter and needs a `count`.** DCA and Mute Group assign encode "channel X ∈ group i" as `0x40 | i` for on and `0x00 | i` for off, with the group index living in the **value**, not in `VX`. One NRPN message therefore carries one bit of a four-bit set. See §10.1 for the consequence on cache keying, which is the single easiest thing to get wrong in this document.

**The `VX` filler value is inconsistent across parameters and must be transcribed literally, not inferred.** Getting this wrong produces messages the mixer silently ignores, which is nearly impossible to debug in the field. Golden-vector tests (§13.2) exist primarily to guard this table.

### 7.2 Registry contents

All values hexadecimal. `VX` column is the Data Entry LSB.

#### Level, pan, routing

| Parameter | ID | VX | Value |
|---|---|---|---|
| Fader | `17` | `07` | `00`–`7F`, interpolated dB (§8.1) |
| Pan | `16` | send target | `00` full L, `25` centre, `4A` full R |
| LR Assign | `18` | `07` | `00` off, `01` on |
| Mix Assign | `55` | send target | `00` off, `01` on |
| Mix Pre/Post | `50` | send target | `00` post, `01` pre |
| Send Level | `20` | send target | `00`–`7F`, same taper as fader |
| Mute Group Assign | `5C` | `07` | `00`–`03` off grp 1–4; `40`–`43` on |
| DCA Group Assign | `40` | `07` | `00`–`03` off grp 1–4; `40`–`43` on |
| PAFL Select | `51` | `07` | `00` off, `01` on |
| Group Mode | `5E` | `00` | `00` group mode, `01` mix mode — **RX only** |

▲ **Pan is bounded at `4A`, not `7F`,** and is *not* symmetric about `0x40`. Centre is `0x25` (37), full right is `0x4A` (74), so the two halves are 37 steps each and a naive `(raw - 0x40)` normalisation is wrong. Model as `Raw7Bit(lo=0x00, hi=0x4A)` with a `.position` property in −1.0…+1.0 computed piecewise around `0x25`. Values above `4A` arriving from the console are clamped with a warning.

▲ **There is no separate "channel pan" parameter.** A channel's LR pan is Pan (`16`) with `VX = 07` (the LR send target). The object facade should surface `ch.pan` as sugar for `ch.send(Mix.LR).pan` and say so in the docstring, rather than implying two independent parameters exist.

▲ **`LR Assign` (`18`) and `Mix Assign` (`55`) with `VX = 07` are not interchangeable.** The send-target table maps `07` to LR, which makes `55/07` look like a second way to assign to LR. Use `18` exclusively for LR; treat `55` as valid only for `VX` in `00`–`06`, `08`–`0D`, `10`–`13`. Encoding `55/07` is a `ParameterRangeError`. On *receive*, accept either and normalise both to the `lr_assign` cache key. Flag for M6 verification.

▲ **`Group Mode` (`5E`) has no documented `CH`.** It is a console-global setting, but every NRPN carries a channel byte. Record the `CH` observed on hardware in M6 and encode that literally; until then, store it in the cache under a synthetic `GLOBAL` channel id and accept it from any `CH`. Do not guess at `00`.

▲ **Mute Group and DCA masters are muted like any other channel** — a Note On to `0x50`–`0x53` fires the whole mute group, and to `0x10`–`0x13` mutes the DCA. This is the *only* way to fire a mute group; there is no dedicated message. Make it discoverable: `mixer.mute_group(2).fire(True)` as an alias for the mute write, documented as identical.

**Send target `VX` values** (single canonical table, see Errata §14.1):

| Target | VX |
|---|---|
| Mix 1, 2, 3, 4 | `00`, `01`, `02`, `03` |
| Mix 5-6, 7-8, 9-10 | `04`, `05`, `06` |
| Main LR | `07` |
| Group 1-2 … 7-8 | `08`, `09`, `0A`, `0B` |
| Matrix 1-2, 3-4 | `0C`, `0D` |
| FX Send 1–4 | `10`, `11`, `12`, `13` |

Groups accept level/pan/pre-post only when the console is in Mix mode; Matrix is unavailable on Qu-16.

▲ Note the gap at `0E`–`0F`: send targets are **not** a dense range, so `VX` for a send must come from this table by lookup, never from arithmetic on a mix index. `SendTarget` validates against the table and against `ModelCaps` (FX 3–4 and all groups/matrices are rejected on Qu-16).

#### Input source and preamp

| Parameter | ID | VX | Value |
|---|---|---|---|
| Ch USB Source | `12` | `00` | `00` preamp, `01` USB |
| Ch Preamp Source | `57` | `00` | `00` local, `01` dSNAKE |
| dSNAKE Patch | `5D` | `00` | socket `00`–`27` — **RX only** |
| Local Gain | `19` | `07` | `00`–`7F`, interpolated −5…+60 dB (§8.2) |
| Local 48V | `69` | `07` | `00` off, `01` on |
| dSNAKE Gain | `58` | socket idx | `00`–`7F`, interpolated +5…+60 dB (§8.3) |
| dSNAKE Pad | `59` | socket idx | `00` out, `01` in |
| dSNAKE 48V | `5A` | socket idx | `00` off, `01` on |
| Digital Trim (USB src) | `52` | `07` | −24…+24 dB, `00`–`7F`, 0 dB = `40` |
| Stereo Trim (ST1–3) | `54` | `07` | −24…+24 dB, `00`–`7F`, 0 dB = `40` |
| Polarity | `6A` | `07` | `00` normal, `01` reversed |
| Insert In/Out | `6B` | `07` | `00` out, `01` in |

Note: local preamp has **no pad parameter**; only dSNAKE does. Do not invent one.

▲ **The dSNAKE preamp family (`58`, `59`, `5A`) is socket-addressed in `VX`, which makes `CH` ambiguous.** Two readings are possible: `CH` is the input channel the socket is patched to (making `VX` redundant), or `CH` is a fixed/ignored byte and the socket in `VX` is the sole address. The distinction decides whether the cache key is per-channel or per-socket, and getting it wrong means dSNAKE gain writes land on the wrong preamp. Resolve on hardware in M6. Until then: encode `CH` as the input channel, key the cache on `(socket,)` under a synthetic `DSNAKE` channel id so a later re-patch does not strand stale values, and mark the family `EXPERIMENTAL`.

▲ **`dSNAKE Patch` (`5D`) is RX-only and is the map from channel to socket.** Cache it — it is the only way to know which socket a channel's remote preamp lives on, and therefore the only way to make `ch.preamp` do the right thing when `Ch Preamp Source` (`57`) is `01`. `ch.preamp.set_gain_db()` must dispatch to `19` (local) or `58` (dSNAKE) based on the cached `57` value, and raise a clear error if `57` is `UNKNOWN` rather than guessing.

#### PEQ and HPF

| Parameter | ID | VX | Value |
|---|---|---|---|
| LF Gain | `01` | `07` | −12…+12 dB, 0 dB = `40` |
| LF Freq | `02` | `07` | 20 Hz…20 kHz, log |
| LF Width | `03` | `07` | 1.5…1/9 oct |
| LF Type | `04` | `07` | `00` bell, `06` shelf |
| LM Gain | `05` | `07` | −12…+12 dB |
| LM Freq | `06` | `07` | 20 Hz…20 kHz |
| LM Width | `07` | `07` | 1.5…1/9 oct |
| HM Gain | `09` | `07` | −12…+12 dB |
| HM Freq | `0A` | `07` | 20 Hz…20 kHz |
| HM Width | `0B` | `07` | 1.5…1/9 oct |
| HF Gain | `0D` | `07` | −12…+12 dB |
| HF Freq | `0E` | `07` | 20 Hz…20 kHz |
| HF Width | `0F` | `07` | 1.5…1/9 oct |
| HF Type | `10` | `07` | `00` bell, `06` shelf |
| PEQ In/Out | `11` | `00` | `00` out, `01` in |
| HPF Freq | `13` | `07` | 20 Hz…20 kHz |
| HPF In/Out | `14` | `00` | `00` out, `01` in |

IDs `08` and `0C` are unassigned (the band blocks are 4 wide with the fourth slot used only by LF/HF for Type).

#### GEQ

| Parameter | ID | VX | Value |
|---|---|---|---|
| GEQ Gain | `70` | band `00`–`1B` | −12…+12 dB, `00`–`7F` |
| GEQ In/Out | `71` | `00` | `00` out, `01` in |

28 bands, index `00`–`1B`: 31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630, 800 Hz, 1k, 1k25, 1k6, 2k, 2k5, 3k15, 4k, 5k, 6k3, 8k, 10k, 12k5, 16k.

#### Gate

| Parameter | ID | VX | Value |
|---|---|---|---|
| Attack | `41` | `07` | 50 µs…300 ms |
| Release | `42` | `07` | 10 ms…1 s |
| Hold | `43` | `07` | 10 ms…5 s |
| Threshold | `44` | `07` | −72…+18 dB |
| Depth | `45` | `07` | 0…60 dB |
| Gate In/Out | `46` | `00` | `00` out, `01` in |

#### Compressor

| Parameter | ID | VX | Value |
|---|---|---|---|
| Type | `61` | `07` | `00` Manual RMS, `01` Auto Slow Opto, `02` Auto Punchbag, `03` Manual Peak |
| Attack | `62` | `07` | 300 µs…300 ms |
| Release | `63` | `07` | 100 ms…2 s |
| Knee | `64` | `07` | `00` hard, `01` soft |
| Ratio | `65` | `07` | 1:1…∞, `00`–`7F`; 2.6:1 = `50` |
| Threshold | `66` | `07` | −46…+18 dB |
| Makeup Gain | `67` | `07` | 0…+18 dB |
| Comp In/Out | `68` | `00` | `00` out, `01` in |

#### Delay

| Parameter | ID | VX | Value |
|---|---|---|---|
| Delay Time | `6C` | `07` | Input: 0–85 ms = `00`–`40`; Mix/Group/Matrix: 0–170 ms = `00`–`7F`. **Linear.** |
| Delay In/Out | `6D` | `00` | `00` out, `01` in |

The input-channel range is half-scale. The domain must be channel-class-aware or the dB/ms helper will be wrong by 2× on inputs.

▲ This is the one place where a single `ParamSpec` needs two domains. Either carry `domain_by_class: dict[ChannelClass, Domain]`, or register `delay_time` twice with disjoint `applies_to` sets. Prefer the latter — it keeps `ParamSpec` uniform and makes the golden vectors cover both explicitly. ▲ Writing raw `41`–`7F` to an input channel is a `ParameterRangeError`; receiving it is clamped to `40` with a warning.

#### FX delay (Tier: experimental)

| Parameter | ID | VX | Value |
|---|---|---|---|
| FX Delay Time, fine (LSB) | `49` | `05` L / `07` R | `00`–`7F` |
| FX Delay Time, coarse (MSB) | `48` | `05` L / `07` R | `00`–`7F` |
| FX Delay Link | `48` | `06` | `00` unlinked, `7F` linked |

For fine resolution, send **LSB first, then MSB**. Sending MSB alone gives coarse resolution. Note that `48` serves double duty as coarse-time and link, discriminated only by `VX` — the registry key must therefore be `(id, vx)` for this family, not `id` alone. Reference points (fine/coarse): 5 ms = `00`/`00`; 100 ms = `31`/`44`; 200 ms = `22`/`54`; 400 ms = `05`/`64`; 800 ms = `70`/`73`; 1.36 s = `7F`/`7F`.

#### Non-NRPN controls

**Mute** — Note On/Off, not NRPN:
```
Mute on:  9N CH 7F   then  9N CH 00
Mute off: 9N CH 3F   then  9N CH 00
```
On receive: velocity `00` and Note Off are **ignored**; `01`–`3F` = unmute; `40`–`7F` = mute.

**Scene recall** — Bank Select + Program Change. Only Bank 1 is used, and the Qu ignores program changes if Bank 1 is not currently selected, so **always send the bank select immediately before the program change**:
```
BN 00 00   BN 20 00   CN SS
```
`SS` = `00`–`63` for scenes 1–100. ▲ The API takes a **1-based** scene number to match the console's own display; `recall_scene(1)` sends `SS = 00`. Out-of-range raises `ParameterRangeError`.

▲ **A scene recall invalidates essentially the whole cache and must be handled as a state event, not as a fire-and-forget write.** A scene changes hundreds of parameters at once, and the spec does not promise the console echoes every one of them. `recall_scene()` must therefore, by default (`resync=True`):

1. Send bank select + program change.
2. Mark the entire parameter cache `STALE`.
3. Wait a settling delay (default **500 ms**, configurable — recalls are not instantaneous on the console) and then run a full `resync()`, followed by a name sweep if `sync_names` is on. Channel names are scene data and will have changed.
4. Emit a `SceneRecalled` event *before* the resync, so applications can grey out their UI, and `SyncComplete` after.

Also invalidate any `SurfaceMap` the application registered (§6.3) — the custom layer is scene data.

▲ **There is no way to read the current scene number, scene names, or the scene list.** `mixer.last_recalled_scene` reflects only what *this client* recalled and is `None` after connect or after a recall from the console's own surface. Document this in the README limitations list; do not expose it as if it were console state.

**MMC transport** — `F0 7F 7F 06 TC F7`, `TC` ∈ {`01` stop, `02` play, `04` FF, `05` RW, `06` record strobe, `09` pause}.

**Remote shutdown** — `B0 63 00  B0 62 5F  B0 06 00  B0 26 00`. Note this is hardcoded to MIDI channel 0 and `CH = 00` in the spec, *not* the mixer's configured channel `N`. Implement it literally as specified.

> **Safety requirement.** Recovery requires a physical power cycle. This must be guarded twice: `AsyncQuMixer(..., allow_shutdown=True)` at construction **and** `await mixer.shutdown(confirm=True)` at the call site. Default `allow_shutdown=False` raises `ShutdownNotPermittedError`. Do not expose it in the object facade.

**MIDI fader strips (DAW layer)** — separate MIDI channel `N+1`. Tier: experimental, exposed as a raw sub-API only. Strip numbers are strip *identities* (MIDI 1–32), not surface positions, and are unrelated to channel ids — see §6.3 and §14.7.
- Strip fader: `B(N+1) FD VA`, `FD` = strip `00`–`1F`, `VA` = `00`–`7F`.
- Strip keys: Note On `7F` then Note Off `00` on channel `N+1`. `KY` = Mute `00`–`1F`, Sel `20`–`3F`, PAFL `40`–`5F`.
- Bank Up `7E`, Bank Down `7F`.

▲ **`N + 1` overflows when the console's MIDI channel is 16.** MIDI channels are `0`–`15` on the wire; if the operator sets the Qu to channel 16, `N = 0x0F` and there is no channel `N+1`. The spec does not say whether the console wraps to 0 or forbids the setting. Handle it explicitly: on sync, if `N == 15`, disable the MIDI-strip sub-API, set `mixer.strips_available = False`, and log a warning naming the cause. Never emit a status byte of `B0 | 16`, which would corrupt into a system message. Verify the console's actual behaviour in M6.

▲ This also means **every derived channel must be computed once at sync time and validated**, not recomputed at each call site.

---

## 8. Unit conversion

The spec gives only sparse anchor points. Implement monotonic piecewise-linear interpolation over raw MIDI values, with lookup in both directions.

**Contract:** the raw 0–127 value is always the ground truth. `.raw` is exact and lossless. `.db` is a convenience property, documented as approximate (~±0.5 dB, limited by 128 steps across the range). Round-tripping `db → raw → db` is not guaranteed to be exact and tests must assert tolerance, not equality.

### 8.1 Fader / send level taper

| dB | raw |
|---|---|
| +10 | `7F` |
| +5 | `72` |
| 0 | `62` |
| −5 | `4F` |
| −10 | `3F` |
| −15 | `36` |
| −20 | `2F` |
| −25 | `27` |
| −30 | `1F` |
| −35 | `17` |
| −40 | `10` |
| −45 | `0C` |
| −inf | `00` |

`00` is a discrete −∞, not a point on the curve. Do not interpolate between `00` and `0C`; treat raw `00` as `float("-inf")` and clamp any request below −45 dB to `00`.

▲ **This leaves raw `01`–`0B` undefined, and the console will send those values** — the physical fader passes through them on its way to the bottom. Decoding them must be specified, not left to whatever the interpolator happens to do:

- `.db` for raw `01`–`0B` returns a **linear extrapolation of the `0C`→`10` segment**, floored at −60 dB, and the returned value is flagged approximate. Do not return `-inf` (it would make raw `0B` and raw `00` indistinguishable) and do not raise.
- The **inverse** direction is not surjective onto this band: `db_to_raw()` for anything below −45 dB returns `00`. This asymmetry is deliberate and must be stated in the docstring, because it means `raw → db → raw` is **not** an identity for `01`–`0B`. Tests assert the asymmetry explicitly rather than asserting a round trip that cannot hold.
- ▲ The same treatment applies to Send Level (`20`), which shares this taper.

### 8.2 Local preamp gain (ID `19`)

| dB | raw |
|---|---|
| +60 | `7F` |
| +50 | `6B` |
| +40 | `57` |
| +30 | `44` |
| +20 | `30` |
| +10 | `1D` |
| +5 | `13` |
| 0 | `0A` |
| −5 | `00` |

### 8.3 dSNAKE preamp gain (ID `58`)

Different curve — do not share the table with §8.2.

| dB | raw |
|---|---|
| +60 | `7F` |
| +50 | `67` |
| +40 | `50` |
| +35 | `45` |
| +30 | `39` |
| +25 | `2E` |
| +20 | `22` |
| +10 | `0B` |
| +5 | `00` |

### 8.4 Others

- **Trim** (`52`, `54`): linear, −24…+24 dB across `00`–`7F`, 0 dB = `40`. ▲ Note `00`–`7F` is 128 steps around a centre of `40`, so the span is asymmetric: 64 steps below zero and 63 above. Anchor the interpolation on `40 = 0 dB` and treat `00` and `7F` as the endpoints; do not assume `+24 dB` lands exactly on `7F` by symmetry.
- **PEQ/GEQ gain**: linear, −12…+12 dB across `00`–`7F`, 0 dB = `40`. Same asymmetry caveat.
- **Delay time**: linear; range depends on channel class (§7.2).
- **Frequencies, widths, gate/comp times**: the spec gives endpoints only. Expose `.raw` as primary; provide best-effort log-interpolated helpers marked clearly as unverified in docstrings. **These are prime candidates for hardware calibration** (§13.5) — build a `tools/calibrate.py` that sweeps a parameter, reads back the console display, and emits a table.

---

## 9. Layer 4 — Sysex protocol

### 9.1 Headers

```
Standard:  F0 00 00 1A 50 11 01 00 0N     (N = mixer MIDI channel)
All-call:  F0 00 00 1A 50 11 01 00 7F
```

The all-call form is required for the initial state request, because the mixer's MIDI channel is unknown until it replies.

▲ `N` is the wire channel `0`–`15`, and the header byte is `0x00 | N`. Users think in 1–16; the public API and all log messages use the 1-based number the console displays, and the conversion happens in exactly one place. ▲ Reject a `11` reply whose header byte is not in `00`–`0F` (or the all-call `7F`) rather than deriving a nonsense channel from it.

### 9.2 Command bytes

| Byte | Direction | Meaning |
|---|---|---|
| `01 CH` | → mixer | Get channel name |
| `02 CH <name>` | ← mixer | Name reply |
| `03 CH <name>` | → mixer | Set channel name |
| `10 <iPadFlag>` | → mixer | Get system state (all-call header) |
| `11 <BoxID> <Major> <Minor>` | ← mixer | System state reply |
| `12 <MeterOnOff>` | → mixer | Meters on (`01`) / off (`00`) |
| `13 <MeterData>` | ← mixer | Meter frame |
| `14` | ← mixer | End of state sync |

`<Name>` is ASCII bytes. All payload bytes must be 7-bit; validate and reject/strip anything ≥ `0x80` on set, and be tolerant on receive.

### 9.3 State sync handshake

```
→ F0 00 00 1A 50 11 01 00 7F  10 <iPadFlag>  F7
← F0 00 00 1A 50 11 01 00 0N  11 <BoxID> <Maj> <Min>  F7
← ... burst of NRPN messages carrying current state ...
← F0 00 00 1A 50 11 01 00 0N  14  F7
```

Implementation:

1. Send request with `iPadFlag` (config, default `1`).
2. On the `11` reply: record MIDI channel `N`, model (from BoxID), firmware version. **Everything subsequently sent must use channel `N`** — do not assume 1.
3. Start keepalive immediately if not already running (5-second deadline under `iPadFlag=1`).
4. Ingest the NRPN burst into the state cache. Do not emit external-change events for these; emit a single `SyncComplete` at the end.
5. The `14` marker resolves the `sync_complete` future. Also apply an overall timeout (default 15s) in case it never arrives.
6. `iPadFlag` is configurable so users can experiment with `0` to avoid the watchdog. Document that `0` is untested and may not trigger the state push.
7. ▲ **Flush the NRPN decoder's held update when `14` arrives** (§5.3) — otherwise the last parameter in the burst is stranded in the accumulator and the cache is silently one value short after every sync.
8. ▲ **A resync on a live connection must suppress `external_change` for the burst, but not lose real concurrent moves.** While `state = SYNCING`, tag every ingested update `source=SYNC` and do not emit `external_change`. An operator moving a fader *during* a resync will have that move overwritten by the burst or swallowed — this is unavoidable and must be documented. Keep the sync window short (do not interleave the name sweep with it) to minimise the exposure.
9. ▲ **Timeouts must distinguish "no reply at all" from "reply but no `14`".** No `11` within `reply_timeout` (default 5s) means the host is not a Qu, or another client holds the session: raise `QuConnectionError` naming the single-connection limit (§4.2). A `11` but no `14` within `sync_timeout` means the state push failed: raise `SyncTimeoutError` and, if `partial_sync_ok=True` (default `False`), keep whatever arrived, marked `KNOWN`, and proceed to `READY` with a warning.
10. ▲ **Ignore a second `11` reply** arriving in the same sync (all-call on a network with two consoles). Log a warning naming both BoxIDs and bind to the first; two consoles on one TCP socket is impossible, but a stray reply must not silently re-point the client.

### 9.4 Channel names

Names are **not** included in the state push. A "complete" sync is:

```
state sync  →  paced sweep of name requests over all valid channels
```

Requirements:
- Sweep only channels valid for the detected model.
- Pace requests: default **20 ms** between requests, configurable. Do not fire 100 sysex requests at once.
- Per-request timeout with limited retry; a missing name yields ▲ `None`, never an exception that aborts the sweep.
- `sync_names: bool = True` on connect; make it skippable for fast startup.
- ▲ **`None`, not a sentinel string.** A missing name must not be the string `"UNKNOWN"` — a channel can legitimately be *named* "UNKNOWN", and the two must stay distinguishable. `ch.name` is `str | None`; use the `CachedValue` freshness machinery (§10.1) for names as for everything else.
- ▲ **Bound the name length and character set on write.** The console's strip display is short (six characters on Qu hardware — ▲ verify in M6 and encode the measured limit as a constant, not a guess). Over-long names must raise `ParameterRangeError` rather than being silently truncated by the console, because a truncated name then round-trips differently than it was set and will oscillate any state-mirroring application. Reject non-printable and non-ASCII characters explicitly; on receive, replace bytes ≥ `0x80` or below `0x20` with `?` and log.
- ▲ **A `03` name-set is not acknowledged.** There is no reply. Confirm by issuing a `01` get after the set (default `verify_name_writes=True`), or document that the write is fire-and-forget. Without this, `set_name()` cannot report failure at all.
- ▲ **Enumerate which channel classes actually have names.** Inputs, stereo inputs, mixes, LR, groups, matrices, DCAs and FX are expected to; mute groups may not. The sweep must be driven by an explicit `NAMEABLE_CLASSES` set, and a channel that returns no name after retries is recorded as "has no name" rather than retried on every resync.

---

## 10. Layer 4 — State cache

### 10.1 Value freshness

▲ Four states, not three — see §10.3 for why `UNCONFIRMED` is needed.

```python
class Freshness(Enum):
    UNKNOWN     = auto()   # never received
    KNOWN       = auto()   # confirmed by the mixer
    UNCONFIRMED = auto()   # ▲ we wrote it; the mixer never confirmed it
    STALE       = auto()   # was known, connection dropped since

@dataclass(frozen=True)
class CachedValue:
    raw: int | None
    freshness: Freshness
    updated_at: float | None
```

**Never synthesize defaults.** "I have not been told the value" is categorically different from "the fader is at −∞", and conflating them will cause a user to unmute something they shouldn't.

▲ `updated_at` is a **monotonic** clock reading (`time.monotonic()`), not wall clock — echo windows and staleness must not break when NTP steps the system clock. Expose a separate wall-clock field only if a user-facing display needs one.

### 10.1a ▲ Cache key

```
(ChannelId, param_name, sub_index | None)
```

`sub_index` is **not** simply `vx`. It is the parameter's *addressing* index, which comes from one of three places depending on the `VxMode`/`Domain`:

| Parameter shape | `sub_index` | Example |
|---|---|---|
| `Fixed` filler `VX` | `None` | fader, HPF freq |
| `SendTarget` / `GeqBand` / `DsnakeSocket` / `FxTap` | the `VX` byte | send level to Mix 3 → `0x02` |
| `FlaggedIndex` domain | **the low bits of the value**, not `VX` | DCA assign, mute group assign |

**The third row is the correction.** Keying `(ChannelId, param_name, vx_index)` as originally written collapses all four DCA memberships of a channel into one entry, because their `VX` is the constant `07` and the group index lives in the value. Input 6 assigned to DCA 1 *and* DCA 3 is two independent facts; a single-slot cache would report the second write as having turned the first off. The same bug applies to mute group assign. Concretely:

```
set(Input(6), "dca_assign", 0x40)  → key (0x25, "dca_assign", 0) = True
set(Input(6), "dca_assign", 0x42)  → key (0x25, "dca_assign", 2) = True
```

and the decoded value is the flag bit alone. `ch.dca_assign` returns a `frozenset[int]` of group numbers, and a membership whose bit has never been reported is `UNKNOWN` independently of the others.

▲ **Cache reads must not fabricate a set from partial knowledge.** If DCA 1 is `KNOWN` and DCA 2–4 are `UNKNOWN`, `ch.dca_assign` raises or returns a partial-set wrapper — it must never return `{1}` as though 2–4 were known to be off.

### 10.2 Reads and writes

- Reads are cache reads and are synchronous/cheap in both APIs: `mixer.get(ch, "fader")` → `CachedValue`.
- `await mixer.refresh(ch, param)` — no targeted get exists in the protocol, so this must be implemented as a full `resync()`. Document that clearly rather than faking granular refresh.
- Writes apply an **optimistic local update immediately**, then reconcile against the mixer's echo.

### 10.3 Echo handling

The Qu transmits on surface changes and will likely echo commanded changes. Every write records a pending expectation `(key, value, timestamp)`. An inbound update matching a pending expectation within a window (default 1s) is treated as an **echo**: it updates the cache but emits no `external_change` event. Non-matching updates emit `external_change`. Expectations expire after the window.

This matters for any application that mirrors state — without it, a fader-follow script sees its own writes and can oscillate.

▲ Four cases the simple rule gets wrong:

- **Whether the Qu echoes at all is unverified.** If it does not, every expectation expires unmatched and the design still works; if it does, echoes are suppressed. Both must be exercised — `FakeQu` has an echo toggle (§13.1) and the test suite runs the state tests under both settings. Do not build anything that *requires* an echo to arrive.
- **Duplicate expectations.** Two writes of the same value to the same key within the window produce two expectations and possibly one echo. Match and consume expectations **FIFO, one echo per expectation**; a surplus echo after the queue is empty is treated as external.
- **An expectation that expires unmatched means the optimistic cache value was never confirmed.** The mixer may have ignored the write — wrong `VX`, parameter invalid on that channel class, console in Group mode (§14.6). Do not leave the value looking `KNOWN`. Downgrade it to a distinct `Freshness.UNCONFIRMED` and emit a `write_unconfirmed` event carrying the key. ▲ This adds a fourth member to `Freshness`; it is the only signal a user gets that the library sent bytes into the void, and it is what turns the "silently ignored message" failure mode from undebuggable into visible.
- **A genuine surface move during the echo window** that happens to land on the commanded value is indistinguishable from an echo and will be suppressed. This is inherent to the protocol. Document it; keep the window short (1s) rather than generous.

### 10.4 Events

```python
mixer.on_change(callback, channel=None, param=None)   # filtered subscription
mixer.on_connection_state(callback)
mixer.on_sync_complete(callback)
```

Event payload: channel, param name, ▲ sub-index (§10.1a), old `CachedValue`, new `CachedValue`, `source ∈ {LOCAL, MIXER, SYNC}`. Callbacks are dispatched on the event loop; exceptions in user callbacks are caught and logged, never allowed to kill the read loop.

▲ Additional subscriptions: `on_write_unconfirmed`, `on_scene_recalled`. ▲ A slow callback blocks the read loop; document that callbacks must not await long operations, and log a warning when one exceeds `slow_callback_warn` (default 100 ms).

### 10.5 ▲ Snapshot and restore

The cache is the only complete picture of console state the library holds, and it is expensive to rebuild (a full sync plus a paced name sweep). Make it serialisable:

```python
snap = mixer.snapshot()             # plain dict, JSON-serialisable
mixer.export_snapshot(path)
```

- Includes model, BoxID, firmware, every `CachedValue` with freshness and age, and names.
- `UNKNOWN` and `UNCONFIRMED` entries are included **with their freshness**, not omitted — a snapshot that silently drops unknowns is indistinguishable from one taken before sync.
- ▲ **Restoring is explicitly *not* "push this snapshot to the console".** Offer `mixer.diff(snapshot)` returning the parameters that differ, and let the application decide what to write. A blind `restore()` that replays hundreds of writes at an unpaced rate is the single most dangerous operation this library could offer, and a partial failure halfway through leaves the console in a state that is in neither snapshot. If an `apply_snapshot()` is added later it must be paced through the §4.5 queue, resumable, and dry-runnable.
- This is what the `qumidi dump-state` CLI emits, and what a regression fixture is made of.

---

## 11. Public API

### 11.1 Async core

```python
async with AsyncQuMixer("192.168.1.60") as mixer:
    await mixer.wait_synced()

    print(mixer.model, mixer.firmware)          # Model.QU32, (1, 9)

    # Flat API — complete, always available
    await mixer.set(Input(6), "fader", raw=0x62)
    await mixer.set(Input(6), "send_level", raw=0x50, vx=Mix.MIX3)
    v = mixer.get(Input(6), "fader")

    # Object facade — ergonomic, generated from registry
    ch = mixer.input(6)
    ch.name                                     # "Kick"
    await ch.set_name("Kick In")
    await ch.mute(True)
    await ch.fader.set_db(-6.0)
    await ch.send(Mix.MIX3).set_level_db(-12.0)
    await ch.send(Mix.MIX3).set_pre(True)
    await ch.peq.hf.set_gain_db(3.0)
    await ch.gate.set_enabled(True)
    await ch.preamp.set_gain_db(35.0)
    await ch.preamp.set_phantom(True)

    await mixer.recall_scene(12)
    await mixer.lr.geq.set_band_db(band=15, db=-2.0)   # band 15 (0-based) = 1 kHz
```

▲ The GEQ example was wrong in v1.0: with 28 bands indexed `00`–`1B` starting at 31.5 Hz, 1 kHz is index **15**, not 14 (index 14 is 800 Hz). Band indices are 0-based throughout to match the wire encoding, and `geq.set_band_db` additionally accepts `freq=1000` resolved through the band table, which is what most callers actually want.

The facade must expose both `set_db()` and `set_raw()` everywhere a taper exists.

▲ **Reads mirror writes.** Every `set_db()`/`set_raw()` has a matching `.db` / `.raw` read returning the tri-state `CachedValue` semantics — a read of an `UNKNOWN` value raises rather than returning a plausible-looking number. `ch.fader.value` returns the `CachedValue` itself for callers that want to inspect freshness.

▲ **Ramp helper.** Given the outbound queue (§4.5), a fade is safe to offer and users will otherwise write a bad one:

```python
await ch.fader.ramp_to_db(-10.0, duration=2.0, steps=None)   # steps default: one per raw step
await mixer.all_fade_out(duration=3.0)                        # LR only, convenience
```

Ramps are cancellable, respect the send-rate limit, and are documented as approximate in timing. A second ramp on the same parameter cancels the first rather than interleaving with it.

▲ **`await mixer.wait_synced()` is mandatory before any read.** Reading before sync returns `UNKNOWN` for everything; make that obvious rather than surprising. Writes before sync are queued and flushed after the `11` reply establishes `N` — they cannot be sent earlier because the MIDI channel is not yet known. ▲ Document that a write issued before sync is transmitted *after* the state burst and will therefore appear to "win" over the console's own state.

### 11.2 Sync facade

`QuMixer` runs an event loop in a daemon thread and proxies every coroutine method to it via `run_coroutine_threadsafe`. **One implementation only** — the sync class contains no protocol logic. Property reads (cache lookups) do not cross the thread boundary; they read the cache under a lock.

```python
with QuMixer("192.168.1.60") as mixer:
    mixer.input(6).fader.db = -6.0
```

Callbacks registered through the sync API are invoked on the loop thread; document this and offer `thread_safe=True` to marshal them onto a queue the caller drains.

▲ **Property-setter semantics must be pinned down.** `fader.db = -6.0` performs blocking I/O behind an assignment, which is surprising and cannot return a value. Specify: the setter **blocks until the write is flushed** (same guarantee as `await` in the async API) and raises the same exceptions; it does not return early. Provide `fader.set_db(-6.0)` as the explicit form and present the property as sugar. ▲ Both APIs otherwise expose the *same* method names — the sync facade is the async one minus `await`, generated from the same registry, never a hand-maintained parallel surface.

▲ **`QuMixer` must be usable from multiple caller threads.** Cache reads take a lock; writes are marshalled with `run_coroutine_threadsafe` and are ordered by the §4.5 queue, not by which thread called first. Document that ordering between threads is not guaranteed. Shutdown joins the loop thread with a timeout and never leaves a non-daemon thread behind.

### 11.3 Exceptions

```
QuError
├── QuConnectionError          (incl. single-connection-in-use)
│   └── QuConnectionLostError  ▲ raised on writes after an unrecoverable drop
├── SyncTimeoutError
├── NotSyncedError             ▲ read/write requiring model or channel N before sync
├── UnsupportedOnModelError
├── ParameterRangeError
├── UnknownParameterError
├── ReadOnlyParameterError     ▲ write to a Direction.RX_ONLY parameter
├── ValueUnknownError          ▲ read of an UNKNOWN/STALE value without a default
└── ShutdownNotPermittedError
```

▲ Every exception message must name the channel, parameter and value involved. "Invalid value" with no context is useless when debugging a 40-channel console.

---

## 12. Metering (Tier: experimental)

Marked experimental in v1. API may change; documented as such in the README and in every relevant docstring.

### 12.1 Control

```python
async with mixer.meters.enabled():       # sends 12 01 on entry, 12 00 on exit
    async for frame in mixer.meters.stream():
        print(frame.db(MeterSlot.input(6, Meter.POST_PREAMP)))
```

▲ `MeterSlot.input(6).POST_PREAMP` as written in v1.0 mixes an instance with a class attribute and cannot be typed cleanly. A slot is one value: `MeterSlot.input(6, Meter.POST_PREAMP)`, `MeterSlot.mix(Mix.MIX3, Meter.POST_FADER)`, `MeterSlot.rta(channel="L", band=14)`. `Meter` is an enum whose members are validated against the block type — asking for `POST_PREAMP` on a mix block is a compile-visible error, not a silent wrong index.

Meters are off by default. The context manager exists so a firehose cannot be left running by accident — including on exception paths. `mixer.meters.enable()/disable()` also available for long-lived apps.

▲ **Metering must survive a reconnect and a nested enable.** Track an enable *count*, not a flag, so two overlapping context managers do not disable metering when the inner one exits. §4.4 already requires re-enabling after resync; that re-enable is driven by this counter.

▲ **Meters do not run before sync.** The model determines the frame layout, so `enable()` before `wait_synced()` raises `NotSyncedError`.

### 12.2 Decoding

**7-bit → 8-bit unpacking.** Every 7 payload bytes were encoded as 8 MIDI bytes, with a leading byte carrying the high bits:

```
AAAAaaaa BBBBbbbb ... GGGGgggg
  →  0ABCDEFG 0AAAaaaa 0BBBbbbb ... 0GGGgggg
```

The final group may be short. With 2 remaining bytes it is `0AB00000 0AAAaaaa 0BBBbbbb` — the high-bit byte is **left-aligned**, padded with zeros on the right. Handle every partial-group length 1–6, not just the example.

**Value decoding.** Each meter is an unsigned 16-bit big-endian 7Q8 value with offset `0x8000`:

```python
db = (raw_u16 - 0x8000) / 256.0    # arithmetic on int, result may be negative
```

Verify against the spec's worked example: packed `20 7C 00` → unpacked `7C 80` → `0x7C80 − 0x8000 = −896` → `/256.0 = −3.5 dB`. Make this an explicit unit test.

▲ **Bit order, stated unambiguously,** because the example only exercises two bytes and an implementer can satisfy it with the wrong convention: in the leading byte `0ABCDEFG`, `A` is **bit 6** (the most significant data bit) and belongs to the *first* following byte; `G` is bit 0 and belongs to the seventh. Check against the example: `0x20 = 0b0100000`, so `A = 0`, `B = 1`, giving `7C | 0x00 = 0x7C` and `00 | 0x80 = 0x80`. A reversed convention also produces valid-looking output and would corrupt every meter above the first.

▲ **The unpacked payload length must be even** — meters are 16-bit. An odd length means the frame was truncated or the packing is misunderstood; discard the frame, count it, and log at warning rather than decoding a misaligned array.

▲ dB values are unbounded above by the encoding: `(0xFFFF − 0x8000)/256 ≈ +128 dB`. Do not clamp to a plausible-looking range — an out-of-range reading is diagnostic information about a layout error.

### 12.3 Layout

Frames are a flat array of meters laid out per model, ▲ each meter two bytes, big-endian, in the order the blocks are listed. **Decode lazily** — unpack to a `bytes`/`memoryview` buffer and decode individual slots on access. A Qu-32 frame contains ~790 meters including two 31-band RTAs; eagerly building 790 floats at frame rate is wasteful when the caller wants six of them. Provide an optional `subscribe(slots)` filter.

▲ **"Unused" counts are meter slots, not blocks.** This is load-bearing and was ambiguous in v1.0: read as blocks, the Qu-16 frame would be nonsensically large. Every count below multiplies out to individual 2-byte meters.

**Block order (to be verified against hardware):**

*Qu-16* — 16 mono input, 80 unused, 3 stereo input, 20 unused, 4 mono mix, 4 stereo mix, 1 stereo monitor, 4 stereo FX.

*Qu-24* — 24 mono input, 3 stereo input, 180 unused, 4 mono mix, 4 stereo mix, 2 stereo group, 2 stereo matrix, 1 stereo monitor, 4 stereo FX.

*Qu-32 / Qu-Pac / Qu-SB* — 24 mono input (CH1–24), 3 stereo input, 20 unused, 8 mono input (CH25–32), 4 mono mix, 4 stereo mix, 4 stereo group, 2 stereo matrix, 1 stereo monitor, 4 stereo FX.

Note the Qu-32 layout is **not contiguous** for inputs — channels 25–32 appear after the stereo inputs and a 20-meter gap.

▲ **The stated counts do not add up, and one of them is almost certainly wrong.** Working the arithmetic through (mono input 10, stereo input 20, mono mix 10, stereo mix/group/matrix 20, monitor 78, FX 18):

| Model | Input region | Mix/grp/mtx | Monitor | FX | Total meters | Payload bytes | Encoded bytes |
|---|---|---|---|---|---|---|---|
| Qu-16 | 160 + 80 + 60 + 20 = **320** | 40 + 80 = 120 | 78 | 72 | **590** | 1180 | 1349 |
| Qu-24 as written | 240 + 60 + 180 = **480** | 40 + 80 + 40 + 40 = 200 | 78 | 72 | **830** | 1660 | 1898 |
| Qu-32 | 240 + 60 + 20 + 80 = **400** | 40 + 80 + 80 + 40 = 240 | 78 | 72 | **790** | 1580 | 1806 |

A Qu-24 frame cannot plausibly be **larger** than a Qu-32 frame. The `180 unused` figure for Qu-24 is the suspect number. The hypothesis to test first on hardware: it is **100**, not 180 — which makes the input region a uniform 400 meters on Qu-24/32/Pac/SB (24 mono + 3 stereo + 20 pad + the 80 slots that carry CH25–32 on a Qu-32 and are unused on a Qu-24), and gives Qu-24 a total of **750** meters. Note the same padding logic explains Qu-16's `80 unused`: it pads 16 mono inputs out to a 24-input stride.

▲ Do not encode a guess. Encode both candidate layouts, and have the frame-size assertion below select or reject them against a real capture.

▲ **"4 stereo mix" means Mix 5-6, 7-8, 9-10 *and* LR.** There are only three stereo mixes in the channel-id table; the fourth block is the main LR, which is stereo and is metered here rather than in the monitor block. Getting this wrong shifts every subsequent block by 20 meters.

▲ **The Qu-24 group-block count (2) contradicts §6.2's four group channel ids.** Same open question as the `†` note there; resolve both with one hardware test.

**Block contents:**

- *Mono input* (10): Post Preamp, Post PEQ, Post Comp, Post Delay, Gate SC, Comp SC, Direct Out, Gate GR, Comp GR, Ducker GR.
- *Stereo input* (20): the above ×2, all L then all R.
- *Mono mix* (10): TB/SigGen, Pre-Insert, Post-PEQ, Post-GEQ, Post Comp, Post Fader, Post Insert, Comp SC, Comp GR, Ducker GR.
- *Stereo mix/group/matrix* (20): the above ×2, all L then all R.
- *Stereo monitor* (78): PAFL L, PAFL R, PAFL mono sum, Talkback, SigGen, Main Pre Fader L/R, Main Post Fader L/R, Main Mono Sum Pre/Post Fader, USB A Record Out L/R, 3 unused, RTA 31 bands L, RTA 31 bands R.
- *Stereo FX* (18): Send L, Send R, Send mono sum, Pre PEQ L/R, Tap Tempo L/R, Post PEQ L/R, 9 unused.

Total frame sizes must be computed from these counts and **asserted against a real captured frame** during hardware verification. If the computed size does not match the observed payload, the layout table is wrong — fail loudly rather than silently mis-indexing.

▲ **"Fail loudly" means at runtime, not only in tests.** On the first meter frame after enabling, compare the unpacked length against the computed layout for the detected model. On mismatch: disable metering, emit a `MeterLayoutError` event naming both lengths, and do **not** deliver frames. A silently mis-indexed meter reads a plausible dB value from the wrong channel, which is worse than no meter at all — a user will trust it.

▲ **A layout override hook.** Because these numbers are unverified, let users supply a corrected layout (`mixer.meters.set_layout(...)`) and dump the observed frame length, so a user with a console we have never tested can fix their own installation without waiting for a release. `qumidi record` captures a raw frame for exactly this purpose.

### 12.4 Isolation

Meter frames must not delay control traffic or the keepalive. Decode off the read path (hand the raw payload to a bounded queue). If consumers fall behind, **drop old frames** — meters are a sampled signal; a stale frame has no value. Log drops at debug with a counter exposed as `mixer.meters.dropped_frames`.

---

## 13. Testing

Hardware is available (Qu-24 and/or Qu-32), so the plan has both offline and on-console phases. Offline tests must remain the primary gate — CI cannot reach a mixer.

### 13.1 `FakeQu` server (`qumidi.testing.fake_qu`)

An asyncio TCP server implementing the real state machine. This is both the primary test fixture and a shipped dev tool. It must:

- Accept exactly one connection; refuse the second (mirroring real behaviour, which tests must cover).
- Emit `FE` on connect and every 300ms while idle.
- After receiving an `FE`, close the connection if no inbound data arrives for 12s (configurable, so tests can shrink it).
- If the state request carried `iPadFlag=1`, close if no `FE` within 5s.
- Answer the all-call `10` with `11 <BoxID> <Maj> <Min>` for a configurable model.
- Push a canned scene as NRPN, then the `14` end marker. Configurable delay and configurable "never send `14`" mode for timeout tests.
- Serve name get/set with an in-memory name table.
- Apply inbound NRPN writes to internal state and echo them (echo toggleable, to test §10.3 both ways).
- Stream meter frames from a recorded or synthesized buffer at a configurable rate when metering is on.
- Support fault injection: split writes at arbitrary boundaries, insert `FE` mid-sysex, drop the connection mid-sync, send malformed sysex.
- ▲ Additional fault modes the client must survive: a `11` reply with an unknown BoxID; a firmware below 1.9; a state burst that ends without `14`; a truncated meter frame (odd payload length); a meter frame of the wrong size for the model; a second `11` reply; `N = 15` (the MIDI-strip overflow case, §7.2); a name reply containing bytes ≥ `0x80`; running-status NRPN bursts with `FE` inserted between every pair of bytes.
- ▲ Replay mode: `--replay capture.bin` feeds a recorded hardware session (§13.5) instead of synthesised state, so CI can regress against real traffic shapes.

Expose as a pytest fixture on an ephemeral port and as `python -m qumidi.testing.fake_qu --model qu32 --port 51325`.

### 13.2 Golden vectors

The highest-value test set. A checked-in fixture file (`tests/fixtures/golden_params.yaml`) maps every registry entry to its expected byte sequence:

```yaml
- param: fader
  channel: 0x25      # Input 6
  value: 0x62
  vx: null
  expected: "B0 63 25 B0 62 17 B0 06 62 B0 26 07"
```

**Transcribe this fixture independently from the PDF, not generated from the registry** — a fixture generated from the code under test proves nothing. It exists specifically to catch a wrong `VX` filler or a transposed NRPN ID, which are the failure modes that produce silently-ignored messages.

Cover: every parameter at least once; every `VxMode`; all send targets; GEQ band boundaries (`00`, `1B`); dSNAKE socket boundaries (`00`, `27`); mute on/off; scene recall including the bank select; the FX delay `48`/`VX` collision.

▲ Also cover: every `FlaggedIndex` value (`00`–`03` and `40`–`43` for both DCA and mute-group assign); pan at `00`/`25`/`4A`; input delay at its `40` ceiling versus mix delay at `7F`; a non-zero MIDI channel `N` for at least one vector of each family, since `B0` is the easy case and `B5` is where a hardcoded zero shows up; the remote-shutdown sequence with its hardcoded channel 0; and the MIDI-strip messages on channel `N+1`.

▲ **A completeness test, not just a correctness test.** Assert that the set of `(param, vx_mode)` pairs in the fixture covers the registry exhaustively, and fail if a registry entry has no vector. Otherwise a parameter added later is silently untested — which is precisely the failure this fixture exists to prevent.

### 13.3 Parser robustness

- **Boundary fuzzing:** take a representative byte stream and feed it split at every possible position (and at random multi-cut positions); assert the parsed message list is identical every time.
- **Real-time injection:** insert `FE`/`F8` at every index in a stream containing sysex and NRPN bursts; assert messages parse identically and running status survives.
- Running status sequences, truncated sysex, unknown status bytes.
- Hypothesis-based property test: arbitrary well-formed message lists → encode → chunk randomly → parse → identity.

### 13.4 Round-trip and unit tests

- `encode → parse → same (channel, param, value, vx)` for the whole registry.
- `db → raw → db` within tolerance for each taper; assert monotonicity across the full 0–127 range; assert the `−inf`/`00` discontinuity is handled. ▲ Assert the deliberate `raw → db → raw` **non**-identity for fader raw `01`–`0B` (§8.1) rather than asserting a round trip that cannot hold.
- Meter decode: the spec's worked example, all partial-group lengths 1–6, and a full frame whose size matches the computed layout. ▲ Plus: odd-length payload rejected; wrong-size frame disables metering rather than mis-indexing; bit-order regression test that a reversed high-bit convention fails.
- State cache: freshness transitions, echo suppression, stale-on-reconnect. ▲ Plus: `FlaggedIndex` multi-membership (assign one channel to DCA 1 and 3, assert both are independently `KNOWN` and neither write cleared the other — the §10.1a bug); `UNCONFIRMED` after an expectation expires with echo disabled; FIFO consumption of duplicate expectations; expectations cleared on disconnect.
- ▲ NRPN decoder: held-update flush on `14`, on timeout, and at end of stream; `FE` interleaving does not flush; running status across an entire burst.
- ▲ Scene recall: cache goes `STALE`, resync is issued, `SceneRecalled` precedes `SyncComplete`, names are re-swept.
- ▲ Outbound pacing: a 128-step ramp does not exceed `max_send_rate`; keepalive is still emitted on time while the queue is saturated; concurrent `set()` calls never interleave NRPN triplets.
- ▲ Lifecycle: `close()` during backoff does not reconnect; `close()` twice is a no-op; the sync facade's loop thread exits; no task or socket is leaked (assert with `asyncio.all_tasks()` at teardown).

### 13.5 Hardware conformance suite

Opt-in, `pytest -m hardware --qu-host=...`, excluded from CI.

- **Must save the current scene to a spare slot before running and restore it after**, including on failure. Wrap in a fixture with `try/finally`.
- Verify: model detection, firmware, real state-sync content, name get/set round-trip, echo behaviour, actual meter frame size vs. computed layout, single-connection refusal.
- **Calibration runs** (`tools/calibrate.py`): sweep raw `00`–`7F` for each interpolated parameter, record the value shown on the console, emit a table. Use this to replace the "unverified" frequency/time/width curves with measured ones. This is the main thing hardware access buys that nothing else can.
- Record every hardware session with the `qumidi record` CLI and commit the captures as replay fixtures — they become the regression corpus and let CI test against real traffic shapes forever after.

### 13.6 Coverage target

≥90% line coverage on `protocol/`, `midi/`, and `state.py`. Transport and client may be lower given timing-dependent paths.

---

## 14. Errata — deliberate deviations from the spec

Implement these as written here and cite the reason in a code comment, or the next reader will "fix" them back.

**14.1 Mix Assign `VX` is self-contradictory.** The spec lists `VX 00 to 0B = Mix1 to 9-10, LR` and then, on the next line, `VX 08, 09, 0A, 0B, 0C, 0D = Grp1-2 to 7-8, MTX1-2 to 3-4`. `08`–`0B` cannot be both. The Pan and Mix Pre/Post entries resolve it: Pan gives `04, 05, 06, 07 = Mix 5-6, 7-8, 9-10, LR`, and Pre/Post gives `00 to 06 = Mix1 to 9-10`. Therefore the correct mapping is `00`–`06` = Mix 1…9-10, `07` = LR, `08`–`0B` = groups, `0C`–`0D` = matrix, `10`–`13` = FX. Use the single table in §7.2 for all four send-family parameters.

**14.2 DCA Group Assign describes itself as "Mute Grp".** The value line reads `Off Mute Grp 1-4 = 00 to 03` under the DCA Grp Assign heading — a copy-paste from the row above. The encoding is correct as given (`0x40 | index` for on); only the label is wrong.

**14.3 Meter block table column headers are wrong.** The table's first two columns are both headed "Qu-24". The first column contains 16 mono input blocks and is unambiguously Qu-16. Treat columns as Qu-16 / Qu-24 / Qu-32+Pac+SB.

**14.4 Delay Time ID `6C` collides with Matrix channel ID `6C`.** These are different namespaces (parameter ID vs. channel ID) and there is no actual conflict, but the type system should make it impossible to pass one where the other is expected. `ChannelId` and `ParamId` must not be interchangeable ints.

**14.5 `48` is both FX Delay coarse time and FX Delay Link,** discriminated only by `VX` (`05`/`07` vs `06`). Registry lookup for inbound decode must key on `(id, vx)` for this family, not `id` alone.

**14.6 Group level/pan/pre-post are conditional on console mode.** Groups accept these only in Mix mode. Mode is reported via the RX-only Group Mode parameter (`5E`). Cache it, and warn (do not raise) when a group send parameter is set while in Group mode.

**14.7 MIDI strip numbers and channel ids overlap numerically but are unrelated.** Strip `0x00`–`0x1F` and Input channels `0x20`–`0x3F` do not collide by luck of range, but strip key numbers (`0x00`–`0x5F` across Mute/Sel/PAFL) *do* overlap input channel ids. They are different namespaces on different MIDI channels (`N+1` vs `N`). `StripId` must be a distinct type from `ChannelId`, mutually non-assignable, exactly as required for `ChannelId` vs `ParamId` in §14.4. A function accepting a `ChannelId` must not compile when handed a `StripId`.

**▲ 14.8 `Mix Assign` (`55`) with `VX = 07` duplicates `LR Assign` (`18`).** The unified send-target table (§14.1) maps `07` to LR, which makes `55/07` a syntactically valid second encoding for LR assignment. Only one of them can be what the console listens to. Encode LR assignment exclusively as `18`; reject `55/07` on write; accept and normalise both on read. Resolve which the console actually emits in M6.

**▲ 14.9 Pan is not centred on `0x40`.** Centre is `0x25`. Any code that treats pan as a signed offset from `0x40` — the reflex for a 7-bit bipolar parameter — is wrong by 27 steps at centre and will pan every channel hard left. Pan's usable range ends at `0x4A`.

**▲ 14.10 `FlaggedIndex` parameters carry their index in the value, not in `VX`.** DCA and mute-group assign are the only set-valued parameters in the registry, and the naive cache key loses three quarters of the information. See §10.1a. This is listed as errata because the shape of the spec's tables actively invites the wrong reading.

**▲ 14.11 The console's MIDI channel `N` can make `N+1` invalid.** The MIDI-strip layer occupies channel `N+1`, which does not exist when `N = 15` (channel 16 on the console's display). The spec does not address this. Disable the strip sub-API rather than wrapping to channel 0, which would send strip traffic on a channel a different device may own. See §7.2.

**▲ 14.12 The Qu-24 meter layout is internally inconsistent with the Qu-32 layout.** Taken literally, a Qu-24 frame is larger than a Qu-32 frame (830 vs 790 meters). The `180 unused` figure is the probable error; `100` makes the input region uniform across Qu-24/32/Pac/SB. See §12.3. Until resolved on hardware, the runtime frame-size check — not the table — is the authority.

**▲ 14.13 The scene recall path has no completion signal.** Program Change is unacknowledged, and the console gives no "recall finished" message. The 500 ms settle-then-resync in §7.2 is a heuristic, not a protocol guarantee; calibrate the delay in M6 and document that a recall of a large scene may need longer.

---

## 15. Milestones

**M1 — Codec foundation.** Transport ABC, `TcpTransport` with keepalive and reconnect, `MidiStreamParser`, message types, NRPN assembly, ▲ the ordered outbound queue and send-rate limiter (§4.5). Boundary/real-time fuzz tests passing, ▲ including decoder-flush and lifecycle/leak tests. `FakeQu` skeleton (active sensing + connect/disconnect only).

**M2 — Registry and encoding.** `ParamSpec`, `VxMode`, `Domain`, channel ids, model caps, full registry transcription, unit conversion tables. Golden-vector fixture written independently and passing. This milestone is complete when every parameter in §7.2 encodes correctly.

**M3 — Client and state.** `AsyncQuMixer`, state sync handshake, state cache with ▲ four-state freshness and the §10.1a sub-indexed key, echo suppression ▲ including `UNCONFIRMED` handling, event bus, name get/set and paced sweep, ▲ scene recall with invalidation and resync, ▲ snapshot/diff. `FakeQu` completed ▲ with the §13.1 fault modes. Flat API usable end to end.

**M4 — Object facade and sync API.** Model classes generated from the registry, `QuMixer` threaded facade, exception hierarchy, docs and examples.

**M5 — Metering (experimental).** 7→8-bit unpacking, 7Q8 decoding, per-model layout maps ▲ (both Qu-24 candidates), lazy frame access, ▲ runtime frame-size validation and layout override, backpressure/drop policy, context-managed enable ▲ with nesting.

**M6 — Hardware verification.** Run the conformance suite against Qu-24 and Qu-32. Calibrate the unverified tapers. Assert meter frame sizes. Commit recorded fixtures. Promote or demote tiers in the docs based on what actually verified. ▲ Close every open question listed at the end of §19 — that list is this milestone's definition of done.

---

## 16. Acceptance criteria

1. Connects to a Qu over TCP, completes state sync, reports correct model and firmware.
2. Every parameter in §7.2 round-trips: set via API → observed bytes match the golden vector → parsed back to the same logical value.
3. Moving a fader on the physical console updates the cache and fires an `external_change` event; a fader set via the API does **not** fire `external_change`.
4. Pulling the network cable transitions to `RECONNECTING`, marks the cache `STALE`, and on restore re-syncs automatically and returns to `READY`.
5. The library never sends a message the mixer silently ignores due to a wrong `VX` — enforced by the golden-vector suite.
6. Full test suite passes with no hardware present, against `FakeQu`.
7. `mypy --strict` and `ruff` clean.
8. A meter frame from a real Qu-32 decodes to a payload whose length matches the computed layout, and known-silent channels read as very low dB.
9. Remote shutdown cannot be triggered without both the constructor flag and `confirm=True`.
10. ▲ Assigning one input to two DCA groups leaves both memberships independently readable, and neither write clears the other.
11. ▲ A write whose echo never arrives (echo disabled on `FakeQu`) leaves the value `UNCONFIRMED` and fires `write_unconfirmed` — the library never reports a value as `KNOWN` on the strength of its own optimistic write alone.
12. ▲ `recall_scene()` marks the cache `STALE`, resyncs, re-sweeps names, and emits `SceneRecalled` then `SyncComplete` in that order.
13. ▲ A meter frame whose length does not match the computed layout disables metering and surfaces an error; no frames with a wrong-sized payload are ever delivered to a consumer.
14. ▲ A 128-step fader ramp stays within `max_send_rate` and does not delay a keepalive past its deadline.
15. ▲ The full suite passes with the console's MIDI channel set to a non-zero `N` on `FakeQu`, not only the default.
16. ▲ `close()` during reconnect backoff terminates cleanly, leaving no pending tasks, threads, or sockets.

---

## 17. Documentation deliverables

- README: install, 10-line quickstart, the single-TCP-connection caveat prominently placed.
- `docs/protocol-notes.md`: the errata in §14 with reasoning, so future maintainers don't regress them.
- `docs/tiers.md`: parameter tier table (verified / supported / experimental), updated after M6.
- Runnable examples: fader follow, scene automation, meter monitor, state dump.
- ▲ `docs/limitations.md` — one page, linked from the README, listing everything the protocol *cannot* do, because every one of these will otherwise arrive as a bug report: no layer or custom-layer readback (§6.3); no current-scene, scene-name or scene-list query; no targeted parameter get (only full resync, §10.2); no acknowledgement for name writes or scene recalls; one TCP client at a time; no USB transport in v1; no FX parameter editing beyond delay; unverified tapers for frequency, width and time parameters.
- ▲ `docs/troubleshooting.md` — the "my write did nothing" flow: check `write_unconfirmed`, check Group mode (§14.6), check the model capability, check `Direction`, enable the wire log.

▲ **CLI.** `qumidi record` / `monitor` / `dump-state`, plus `replay` (feed a capture to `FakeQu`), `set` (one-shot parameter write for shell scripting), and `probe` (connect, report model/firmware/MIDI channel, disconnect — the first thing to run when something is wrong).

---

## 18. ▲ Operational constraints

**Security.** The protocol is unauthenticated and unencrypted. Anyone who can reach port 51325 can mute the PA, recall a scene mid-show, or trigger the remote shutdown. The library must not pretend otherwise: the README states plainly that the mixer belongs on a trusted, isolated network, and that this library provides no access control. Do not add a "password" parameter — there is nothing to authenticate against.

**Logging.** One logger per module under the `qumidi` namespace. The library configures no handlers and never calls `basicConfig`. A separate `qumidi.wire` logger emits hex dumps at `DEBUG` for both directions; it must be cheap to leave disabled (guard the formatting behind `isEnabledFor`) because it would otherwise format every meter frame.

**Performance targets** (with metering off): steady-state CPU negligible; a full state sync under 2 s excluding the name sweep; a name sweep of a Qu-32 under 4 s at the default 20 ms pacing. With metering on at a typical frame rate, decoding six subscribed slots per frame must not become the dominant cost — this is what the lazy decode in §12.3 buys, and it should be measured, not assumed.

**Resource discipline.** No unbounded queues anywhere. The meter queue drops old frames (§12.4); the event dispatch queue for `thread_safe=True` callbacks is bounded and drops with a warning; the outbound queue is bounded and applies backpressure to `set()` rather than growing.

---

## 19. ▲ Revision 1.1 change log

Corrections to v1.0 (each marked `▲` in place):

1. **§10.1a — cache key.** `(ChannelId, param, vx)` collapses all four DCA/mute-group memberships into one slot. Set-valued parameters carry their index in the *value*. This was a functional bug, not a documentation gap.
2. **§11.1 — GEQ example.** Band 14 is 800 Hz; 1 kHz is band 15.
3. **§6.2 — `ModelCaps`.** Booleans cannot express Qu-24's group count differing from Qu-32's; changed to counts, with the contradiction flagged for hardware resolution.
4. **§12.3 — meter arithmetic.** The stated counts make a Qu-24 frame larger than a Qu-32 frame. Totals computed, the suspect figure identified, and a runtime size check made authoritative over the table.
5. **§8.1 — fader taper.** Raw `01`–`0B` were undefined but are transmitted by the console; decode behaviour and the deliberate round-trip asymmetry now specified.
6. **§7.2 — Pan.** Range is `00`–`4A` with centre at `25`, not a symmetric `00`–`7F` about `40`.
7. **§14.8 — `55/07` vs `18`.** Two encodings for LR assign; one canonical form chosen.
8. **§14.11 — `N+1` overflow.** The MIDI-strip channel does not exist when the console is on MIDI channel 16.
9. **§5.3 — decoder flush.** The held NRPN update was never flushed at end of burst, losing the last parameter of every sync.

Additions:

10. §4.5 outbound pacing and an ordered send queue.
11. §7.2 scene recall as a state-invalidating operation with resync and name re-sweep.
12. §10.3 `UNCONFIRMED` freshness and the `write_unconfirmed` event — the only visible signal that the console ignored a write.
13. §10.5 snapshot/diff, with blind restore deliberately excluded.
14. §9.4 name length, character set, `None` sentinel, and write verification.
15. §11.1 ramp helper; §11.2 sync-facade blocking semantics.
16. §12.3 runtime layout validation and a user-supplied layout override.
17. §18 security, logging, performance and resource-discipline constraints.
18. §17 limitations and troubleshooting docs; `replay`, `set` and `probe` CLI commands.

Open questions for M6 (each blocks promoting a parameter to `VERIFIED`): Qu-24 group count; Qu-24 meter layout; dSNAKE `CH`/`VX` addressing; Group Mode's `CH`; whether the console echoes writes; channel-name length limit; console behaviour at MIDI channel 16; scene-recall settling time.
- Every experimental API carries an `⚠️ Experimental` docstring banner.