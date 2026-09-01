# Protocol notes — deliberate deviations from the vendor spec

The Allen & Heath *Qu MIDI Protocol* document is the source for everything
this library encodes. In thirteen places it is wrong, self-contradictory, or
actively misleading, and this library deliberately does *not* follow it.

Each entry below names the file and function that implements the deviation,
so that a maintainer reading the vendor PDF and reaching for the "obvious
fix" finds this page first. **Do not regress these.** Every one of them is
also cited in a code comment at the site.

Numbering matches PRD §14.

---

## 14.1 — Mix Assign's `VX` table is self-contradictory

**The spec says** `VX 00 to 0B = Mix1 to 9-10, LR` and then, on the next
line, `VX 08, 09, 0A, 0B, 0C, 0D = Grp1-2 to 7-8, MTX1-2 to 3-4`. `08`–`0B`
cannot be both a mix and a group.

**We do** resolve it against the two entries that are internally consistent:
Pan gives `04, 05, 06, 07 = Mix 5-6, 7-8, 9-10, LR`, and Mix Pre/Post gives
`00 to 06 = Mix1 to 9-10`. That pins the table as `00`–`06` = Mix 1…9-10,
`07` = LR, `08`–`0B` = groups, `0C`–`0D` = matrix, `10`–`13` = FX, with a
**gap at `0E`–`0F`**.

**Why** the second line is the copy-paste error: it is the only reading under
which all four send-family parameters (`pan` `16`, `send_level` `20`,
`mix_pre` `50`, `mix_assign` `55`) share one table, which is what the spec's
own structure implies.

**Where:** `SEND_TARGET_VX` and `send_target_vx()` in
`qumidi/protocol/registry.py`; consumed by `AsyncQuMixer._resolve_vx()` in
`qumidi/client_async.py`. The table is a `dict[ChannelId, int]` precisely so
that no call site can compute a target byte arithmetically from a mix index
and silently walk into the `0E`–`0F` gap.

---

## 14.2 — DCA Group Assign describes itself as "Mute Grp"

**The spec says**, under the *DCA Grp Assign* heading, `Off Mute Grp 1-4 =
00 to 03` — a copy-paste from the row above.

**We do** keep the encoding exactly as given (`index` for off, `0x40 | index`
for on) and ignore the label. Only the wording is wrong.

**Why:** the numbers are consistent with the Mute Group Assign row and with
the general `0x40`-is-on convention used elsewhere; there is no plausible
reading under which the DCA row's *values* are wrong too.

**Where:** the `dca_assign` `ParamSpec` in `qumidi/protocol/registry.py`
(which carries this note inline), encoded by `FlaggedIndex.encode()` in
`qumidi/protocol/params.py`.

---

## 14.3 — The meter block table's column headers are wrong

**The spec says** the meter block table's first two columns are both headed
"Qu-24".

**We do** treat the columns as Qu-16 / Qu-24 / Qu-32+Pac+SB.

**Why:** the first column contains 16 mono input blocks. Only the Qu-16 has
16 inputs, so that column is unambiguously Qu-16 regardless of what the
header says.

**Where:** `QU16`, `QU24_AS_WRITTEN`, `QU32` and the `LAYOUTS` map in
`qumidi/protocol/meters.py`.

---

## 14.4 — Delay Time id `6C` collides with the Matrix channel id `6C`

**The spec** uses `6C` for two unrelated things: the FX/channel *delay time*
parameter id and the *Matrix 1-2* channel id.

**We do** treat this as no conflict at all — they are different namespaces —
but make it impossible to pass one where the other is expected.

**Why:** the collision is harmless on the wire (a `CH` byte and an `ID` byte
never occupy the same position) and dangerous in code, where both are small
ints.

**Where:** `ChannelId` in `qumidi/protocol/channels.py` is a frozen dataclass
and deliberately **not** an `int` subclass, so it cannot be passed where a
parameter id (`ParamSpec.nrpn_id`, a plain `int`) is expected, nor the
reverse. Parameters are addressed by *name* throughout the public API
(`mixer.set(ch, "delay_time", ...)`), never by their id. `mypy --strict`
enforces this statically; there is no runtime check because there does not
need to be one.

---

## 14.5 — `48` is both FX Delay coarse time and FX Delay Link

**The spec** assigns id `48` twice, discriminated only by `VX`: `05`/`07`
(delay time, left/right tap) versus `06` (link).

**We do** key inbound decode on `(channel class, id, vx)` for this family,
not on `id` alone.

**Why:** a decoder keyed on `id` alone resolves every `48` to whichever spec
was registered last, so half of all FX-delay traffic would be decoded as the
wrong parameter with no error anywhere.

**Where:** `Registry.register_exact()` and `Registry.resolve()` in
`qumidi/protocol/registry.py`, plus the three `register_exact()` calls at the
bottom of that module that index `fx_delay_coarse` under `FX_TAP_LEFT`/
`FX_TAP_RIGHT` and `fx_delay_link` under `FX_TAP_LINK`. The tap constants
live at module level in `qumidi/protocol/params.py` (`FX_TAP_LEFT`,
`FX_TAP_LINK`, `FX_TAP_RIGHT`) so the registry can reference them without
instantiating `FxTap`.

---

## 14.6 — Group level, pan and pre/post are conditional on console mode

**The spec** documents these as ordinary parameters. They are accepted only
when the console is in Mix mode; in Group mode the console silently ignores
them.

**We do** cache the mode from the receive-only Group Mode parameter (`5E`)
and surface an ignored write as a `write_unconfirmed` event whose log line
names Group mode as a likely cause. We do **not** raise, and do not pre-empt
the write.

**Why:** the mode is not readable synchronously at write time on a console
that has never broadcast `5E` (it is not guaranteed to appear in the state
push), and refusing a write on the strength of a value we may not have would
be worse than sending it and reporting honestly that it was not confirmed.

**Where:** the `group_mode` `ParamSpec` in `qumidi/protocol/registry.py`
(`Direction.RX_ONLY`, `ChannelClass.GLOBAL`, `Tier.EXPERIMENTAL`); the
GLOBAL-channel redirect in `AsyncQuMixer._ingest()` — which is what makes
`5E` broadcasts on arbitrary `CH` bytes converge on one cache entry instead
of scattering — and the diagnostic in `AsyncQuMixer._expire_expectation()`,
both in `qumidi/client_async.py`.

**Known gap:** the spec's "warn when a group send parameter is set while in
Group mode" is *not* implemented as a pre-write check. The signal a caller
gets is the post-hoc `write_unconfirmed` event. Read
`mixer.get(GLOBAL, "group_mode")` yourself if you need to check first.

---

## 14.7 — MIDI strip numbers and channel ids overlap but are unrelated

**The spec** presents strip key numbers (`00`–`5F` across Mute/Sel/PAFL) and
input channel ids (`20`–`3F`) in the same document with no warning that they
overlap numerically. They live on different MIDI channels (`N+1` versus `N`)
and mean entirely different things.

**We do** give strips their own type, `StripId`, which is not assignable to
`ChannelId` — exactly as required for `ChannelId` versus parameter ids in
14.4 — and route strip-channel traffic before it can reach the channel
decoder at all.

**Why:** a strip carries no channel identity (strip 5 sends `0x04` wherever
it sits on the custom layer), so a strip event must never enter the channel
state cache. Beyond the cache, the routing has to happen *early*:
`NrpnDecoder._held` is a single slot that is **not** per-channel, so letting
a strip-channel CC reach `NrpnDecoder.feed()` at all risks force-flushing a
channel-`N` NRPN that is still mid-assembly, without its `VX`.

**Where:** `StripId`, `Strip()` and `StripApi` in
`qumidi/protocol/strips.py`; the early return at the top of
`AsyncQuMixer._dispatch()` in `qumidi/client_async.py` (which fires *before*
`self._decoder.feed(msg)`, not after); and the belt-and-braces MIDI-channel
guard at the top of `AsyncQuMixer._ingest()`.

---

## 14.8 — `mix_assign` (`55`) with `VX 07` duplicates `lr_assign` (`18`)

**The spec**, once 14.1 is resolved, maps send target `07` to LR — which
makes `55/07` a second, syntactically valid encoding of LR assignment
alongside the dedicated `18`. Only one of them can be what the console
listens to.

**We do** emit LR assignment exclusively as `18`, refuse `55/07` on write
with a `ParameterRangeError` that names the alternative, and **normalise an
inbound `55/07` onto the `lr_assign` cache key** on read.

**Why:** emitting whichever encoding the console ignores would be a silent
no-op, so the write side picks one and sticks to it. The read side has to
accept both, because which one the console *emits* is still unresolved (see
the open questions in [tiers.md](tiers.md)) — and if the console speaks
`55/07`, an un-normalised read would leave `lr_assign` `UNKNOWN` for ever
while quietly filling a `mix_assign` sub-key that nothing reads, and would
strand this client's own `lr_assign` write expectation into a spurious
`UNCONFIRMED`.

**Where:** the write refusal in `AsyncQuMixer._resolve_vx()` and the read
normalisation in `AsyncQuMixer._ingest()`, both in
`qumidi/client_async.py`; the `lr_assign` and `mix_assign` `ParamSpec`s in
`qumidi/protocol/registry.py`. Regression tests:
`tests/test_state_sync.py::test_an_inbound_5507_normalises_onto_the_lr_assign_key`
and `::test_an_inbound_mix_assign_to_a_real_mix_is_left_alone`.

---

## 14.9 — Pan is not centred on `0x40`

**The spec** gives pan as a 7-bit value, which invites the reflex reading of
a bipolar parameter centred on `0x40`.

**We do** encode centre as `0x25`, full left as `0x00` and full right as
`0x4A`. The domain is `Raw7Bit(lo=0x00, hi=0x4A)`; `0x4B`–`0x7F` are not
valid values.

**Why:** this is the single most damaging error a reader can make here. Code
that treats pan as a signed offset from `0x40` is wrong by 27 steps at
centre and pans **every channel hard left** — an error that is loud on stage
and silent in a test suite that only round-trips its own assumption.

**Where:** the `pan` `ParamSpec` in `qumidi/protocol/registry.py`, with the
reasoning in a comment directly above the domain. There is no separate
"channel pan" parameter: a channel's LR pan is `pan` with `VX 07`, which is
what `SendSource.pan` in `qumidi/model/channel.py` resolves to.

---

## 14.10 — Set-valued parameters carry their index in the *value*, not in `VX`

**The spec's** tables for DCA Group Assign and Mute Group Assign look like
every other parameter's table, which invites keying the cache on
`(channel, param, vx)` like everything else. Their `VX` is a fixed filler;
the group index lives in the low bits of the *value*.

**We do** take the sub-index from the value for any `FlaggedIndex` domain,
before the `VxMode` dispatch, so that each of the four memberships gets its
own cache slot.

**Why:** the naive key collapses all four DCA memberships into one slot and
loses three quarters of the information — assigning an input to DCA 2 would
appear to clear its DCA 1 membership. This was a functional bug, not a
documentation gap (PRD revision 1.1, item 1).

**Where:** `FlaggedIndex` (`encode()`, `is_on()`, `sub_index_from_value()`)
in `qumidi/protocol/params.py`; `sub_index_for()` in `qumidi/state.py`,
where the domain check deliberately takes priority over the `VxMode`
dispatch; `StateCache.key()` and `MembershipSet` in the same module.
`MembershipSet` also refuses to fabricate a set from partial knowledge: if
DCA 1 is `KNOWN` and 2–4 are `UNKNOWN`, `require()` raises rather than
returning `{1}`.

---

## 14.11 — The console's MIDI channel `N` can make `N+1` invalid

**The spec** does not address this at all: the MIDI-strip layer occupies
channel `N+1`, which does not exist when `N = 15` (channel 16 on the
console's display).

**We do** disable the strip sub-API entirely in that configuration —
`mixer.strips` is `None`, `mixer.strips_available` is `False`, and a warning
is logged naming the fix (move the console to channel 1–15).

**Why:** the alternative reflex, wrapping to channel 0, would send strip
traffic on a MIDI channel a completely different device may own. Four bits
cannot hold 16 either: a status byte of `0xB0 | 16` corrupts into a system
message.

**Where:** `AsyncQuMixer._on_state_reply()` in `qumidi/client_async.py`,
which is the only place `StripApi` is ever constructed. The guard is
airtight at a second, independent layer as well: `NoteOn`, `NoteOff` and
`ControlChange.__post_init__` in `qumidi/midi/messages.py` reject any
channel above 15 outright.

---

## 14.12 — The Qu-24 meter layout is inconsistent with the Qu-32 layout

**The spec's** figures make a Qu-24 frame *larger* than a Qu-32 frame —
830 meters against 790 — which cannot be right.

**We do** encode both candidates and let the wire decide. `QU24_AS_WRITTEN`
transcribes the spec literally (`180 unused`, 830 meters). `QU24_ALTERNATE`
applies the hypothesis that the figure should be `100`, which makes the
input region a uniform 400 meters across Qu-24/32/Pac/SB (750 total).
`LAYOUTS["qu24"]` currently points at the literal transcription; the
**runtime frame-size check, not the table, is the authority**.

**Why:** picking one on paper and silently mis-indexing every meter on the
other is exactly the failure mode metering must not have. A frame whose
length does not match the computed layout disables metering with a
diagnostic instead of being delivered.

**Where:** `QU24_AS_WRITTEN`, `QU24_ALTERNATE` and `QU24_CANDIDATES` in
`qumidi/protocol/meters.py`; the runtime length check in
`MeterController._decode()` in `qumidi/meters.py` and in
`MeterFrame.__init__()` in `qumidi/protocol/meters.py`;
`MeterController.set_layout()` for the user override.

This is still open. It is the one erratum whose resolution needs a physical
Qu-24 — see the open questions in [tiers.md](tiers.md).

---

## 14.13 — The scene recall path has no completion signal

**The spec** documents scene recall as a Program Change. Program Change is
unacknowledged, and the console emits nothing to say a recall has finished.

**We do** mark the cache `STALE`, invalidate any registered `SurfaceMap`,
emit `SceneRecalled`, wait `scene_settle_delay` (500 ms by default), then
run a full resync and name re-sweep and finally emit `SyncComplete`.

**Why:** there is nothing better available. This is a heuristic, not a
protocol guarantee, and it is documented as one everywhere it is visible to
a user. A recall of a large scene may need longer than 500 ms; the delay is
a constructor argument for exactly that reason.

**Where:** `AsyncQuMixer.recall_scene()` in `qumidi/client_async.py`;
`SCENE_SETTLE_DELAY` in `qumidi/protocol/constants.py`. Note that
`recall_scene()` does *not* call `sweep_names()` a second time — `resync()`
→ `_sync()` already sweeps unconditionally, and a second explicit sweep
would silently double the cost of every recall.

---

## A note on what is *not* an erratum

The spec offers no way to read the current scene, no targeted parameter get,
no layer readback, and no acknowledgement for name writes or parameter
writes. Those are not errors in the document — they are things the protocol
genuinely cannot do, and they are listed in
[limitations.md](limitations.md) instead.
