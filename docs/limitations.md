# What this library cannot do

Every item here is a limitation of the Qu MIDI protocol itself, not of this
implementation. They are listed because otherwise each one arrives as a bug
report.

## No layer or custom-layer readback

The protocol is entirely identity-addressed. No message carries a physical
strip position or a layer index. You cannot query the layer configuration,
read which channels are assigned to which Custom Layer positions, or learn
which layer is currently selected.

When an operator moves a physical fader, the console transmits the *channel's*
identity, not the strip that was touched. A listener therefore always knows
which channel changed and can never know which strip was moved. `SurfaceMap`
in `qumixer_py.layout` lets you supply a mapping by hand; the library cannot
validate it, and a scene recall can change the console's custom layer without
any notification. `mixer.register_surface_map(...)` makes the library
invalidate a registered map when *this client* recalls a scene — a recall made
from the console's own surface cannot be detected at all.

## No current-scene, scene-name or scene-list query

There is no message to read the current scene number, a scene's name, or the
list of scenes. `mixer.last_recalled_scene` reflects only what *this client*
recalled; it is `None` after connecting and after any recall made from the
console's own surface.

There is also no scene *store* message. The library can recall a scene and
cannot save one, which is why the hardware conformance suite asks a human to
save a spare scene by hand before it runs (`tests/hardware/conftest.py`).

## No targeted parameter get

There is no "read this one parameter" message. `mixer.refresh()` is
implemented as a full `resync()` and is documented as such — its `ch` and
`param` arguments exist only to document intent at the call site. A resync is
cheap enough to do occasionally and too expensive to do per parameter.

## No acknowledgement for name writes or scene recalls

A `03` name-set gets no reply; the library issues a read-back to confirm
(`verify_name_writes=True`). A Program Change scene recall gets no reply and
the console gives no "recall finished" signal; the library waits a settling
delay (`scene_settle_delay`, 500 ms by default) and resyncs, which is a
heuristic rather than a guarantee. A recall of a large scene may need longer.

## No acknowledgement for parameter writes either

NRPN parameter writes are equally unacknowledged. The library infers success
from the console's *echo* of the write, and downgrades a value to
`UNCONFIRMED` (firing `write_unconfirmed`) when no echo arrives inside
`echo_window`. That is an inference, not a protocol receipt: a genuine surface
move that happens to land on exactly the value just commanded is
indistinguishable from an echo.

## One TCP client at a time

The mixer permits exactly one TCP MIDI connection. If Qu-Pad, Qu-Control or
another script holds the session, this library cannot connect. This is the
single most common support question.

## No USB transport in v1

The `Transport` interface in `qumixer_py.transport.base` accommodates one, and the
`usb` extra reserves the dependencies; there is no implementation. `TcpTransport`
is the only transport that ships.

## No FX parameter editing beyond delay

The spec documents FX delay time (coarse `48`, fine `49`) and delay link
(`48` with `VX 06`), and nothing else about the FX units — no FX type
selection, no reverb or modulation parameters, no FX library recall. FX
*returns* are ordinary channel strips and are fully supported; the FX
processors themselves are not addressable.

## Group level, pan and pre/post depend on a console mode you cannot set

Groups accept level, pan and pre/post only when the console is in Mix mode.
The mode is reported by the receive-only Group Mode parameter (`5E`) and there
is no message to change it — you can read `mixer.get(GLOBAL, "group_mode")`
and you cannot write it. In Group mode those writes are silently ignored by
the console and surface here only as a `write_unconfirmed` event.

## Two parameters are receive-only

`dsnake_patch` (`5D`) and `group_mode` (`5E`) are broadcast by the console and
cannot be written. The library raises `ReadOnlyParameterError` rather than
emitting bytes the console ignores.

## No MIDI-strip layer when the console is on MIDI channel 16

The MIDI-strip (DAW-layer) sub-API lives on MIDI channel `N+1`, which does not
exist when the console's MIDI channel is 16. In that configuration
`mixer.strips` is `None` and `mixer.strips_available` is `False`; the library
will not wrap to channel 0 and send strip traffic on a channel another device
may own.

## MIDI strips are not translated

The strip sub-API is a raw pass-through: raw notes, raw CC, raw inbound
events. There is no HUI or Mackie Control translation, and a strip event is
never ingested into the channel state cache, because a strip number carries no
channel identity.

## No layer-aware or position-aware metering

Meters are identity-addressed like everything else, and the frame layout is
model-specific. The Qu-24 layout is still unresolved between two candidates
(see [`docs/protocol-notes.md`](protocol-notes.md), protocol note 12); the library
validates every frame's length against the computed layout, and on a mismatch
it disables metering and fires `on_meter_layout_error` with a diagnostic
rather than delivering mis-indexed data. `mixer.meters.stream()` never yields
a frame of the wrong size.

## Unverified tapers

The dB tapers for faders, sends, preamp gain, trim and EQ/GEQ gain come from
measured tables in the spec and are accurate to about ±0.5 dB (the limit of
128 steps). Frequency, filter width and the gate/compressor times and
thresholds have only endpoints in the spec; the helpers for those are
best-effort interpolations and are marked as unverified in
[`docs/tiers.md`](tiers.md). `.raw` is always exact.

No parameter is at `Tier.VERIFIED` in this release: hardware verification
 has not been run. See `docs/tiers.md` for the full list and for the
open questions that a hardware run would close.

## No access control

The protocol is unauthenticated and unencrypted. See the security section of
the README.
