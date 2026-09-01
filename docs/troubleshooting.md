# Troubleshooting

## "I connected but nothing happened"

Run `qumidi probe --host <ip>` first. It connects, reports the model,
firmware and MIDI channel, and disconnects.

If it hangs or refuses: **the Qu permits exactly one TCP MIDI connection at a
time.** Close Qu-Pad, Qu-Control, and any other script.

If you have no console to hand, run the bundled fake one and probe that first,
to prove the client half works:

```bash
python -m qumidi.testing.fake_qu --model qu32 --port 51325 &
qumidi probe --host 127.0.0.1
```

## "My write did nothing"

Work down this list in order.

1. **Check `write_unconfirmed`.** Subscribe with
   `mixer.on_write_unconfirmed(print)`. If the write fires this event, the
   console did not echo it inside `echo_window` and the value is now
   `UNCONFIRMED` rather than `KNOWN`. That is the library telling you it sent
   bytes into the void.

2. **Check Group mode.** Group level, pan and pre/post are accepted only when
   the console is in Mix mode. Read `mixer.get(GLOBAL, "group_mode")` —
   `0x00` is Group mode, `0x01` is Mix mode. There is no way to change the
   mode over MIDI; do it on the console.

3. **Check the model capability.** `mixer.caps` reports what the detected
   console actually has. A Qu-16 has no groups, no matrices, and only FX 1-2,
   and writing to a channel it does not have raises `UnsupportedOnModelError`
   rather than emitting bytes. The one exception is the Qu-24's group count,
   which is unverified (`caps.groups_provisional`) and therefore warns and
   permits rather than refusing — see the open questions in [tiers.md](tiers.md).

4. **Check `Direction`.** `dsnake_patch` (`5D`) and `group_mode` (`5E`) are
   receive-only; writing them raises `ReadOnlyParameterError` rather than
   emitting bytes the console ignores.

5. **Check the send destination.** Send-family parameters (`send_level` `20`,
   `pan` `16`, `mix_pre` `50`, `mix_assign` `55`) need `vx=` to be a
   destination channel, and the destination table is not a dense range — there
   is a gap at `0x0E`-`0x0F`. Pass `vx=Mix.MIX3` or `vx=Group(1)`, never a
   hand-computed byte. Assigning to LR is `lr_assign` (`18`), and
   `mix_assign` with an LR destination is deliberately refused — see
   erratum 14.8 in [protocol-notes.md](protocol-notes.md).

6. **Enable the wire log.** `qumidi <command> --wire`, or in code:

   ```python
   import logging
   logging.basicConfig(level=logging.DEBUG)
   logging.getLogger("qumidi.wire").setLevel(logging.DEBUG)
   ```

   Compare the emitted bytes against `tests/fixtures/golden_params.yaml`. A
   wrong `VX` filler byte produces a message the console silently ignores, and
   is the most common cause of a write that does nothing. Note that the
   library itself installs no logging handlers and never calls `basicConfig` —
   your application must, or nothing is printed.

## "The value I read back is wrong"

`.raw` is the ground truth. `.db` is a convenience and is approximate —
about ±0.5 dB, limited by 128 steps across the range. Round-tripping
`db -> raw -> db` is not exact by design. For fader raw values `0x01`-`0x0B`,
`raw -> db -> raw` deliberately does *not* round-trip: those values are below
the lowest anchor the spec documents, decode by extrapolation, and encode back
to `0x00`. See [tiers.md](tiers.md).

On the object facade, check `mixer.input(6).fader.is_approximate` if you need
to know whether a particular reading was extrapolated beyond the documented
anchors, or came from a taper that has not been verified against hardware.

## "My meter numbers look plausible but are from the wrong channel"

They should not be: the library validates every frame's length against the
computed layout and, on a mismatch, disables metering and fires
`on_meter_layout_error` with a diagnostic rather than delivering mis-indexed
data. `mixer.meters.stream()` never yields a wrong-sized frame.

If metering disabled itself, capture a frame and supply a corrected layout:

```bash
qumidi record --host <ip> --out frame.bin --seconds 2 --meters
```

then `mixer.meters.set_layout(...)`. Please open an issue with the observed
frame length and your console model — the Qu-24 layout in particular is still
unresolved between two candidates (erratum 14.12).

## "Reading a value raises ValueUnknownError"

The console has never reported that value. Await `wait_synced()` first, and
note that not every parameter appears in the state push. This is deliberate:
the library never invents a plausible-looking number for something it has not
been told. `mixer.get(...)` itself never raises — it returns a `CachedValue`
whose `freshness` is `UNKNOWN`; it is `.raw`/`.db` on the object facade that
raise rather than synthesise.

## "It reconnected but my values are stale"

On a link drop the whole cache is marked `STALE` and the library re-runs the
full state handshake (and the name sweep) on restore, emitting `SyncComplete`
when it is done. Values read between the drop and that event are the last ones
the console reported, flagged `STALE` — they are not refreshed in place.

If the BoxID changes across a reconnect (a different console answered), the
library rebuilds its capability set from scratch.

## "A scene recall left the cache half-right"

`recall_scene()` sends the Program Change, marks the cache `STALE`, waits
`scene_settle_delay` (500 ms by default), then resyncs and re-sweeps names,
emitting `SceneRecalled` and then `SyncComplete`. The settle delay is a
heuristic — the console gives no "recall finished" signal at all (erratum
14.13). A large scene may need longer; pass a bigger `scene_settle_delay` to
the constructor if you see stale values immediately after a recall.

Any `SurfaceMap` you registered is invalidated by a recall, because a scene can
change the custom layer with no notification.

## "The strip sub-API is None"

`mixer.strips` is `None` and `mixer.strips_available` is `False` when the
console is on MIDI channel 16, because the strip layer lives on channel `N+1`
and there is no channel 17. Move the console to MIDI channel 1-15.

## "I need to reproduce a problem without the console"

Record the traffic once, then replay it:

```bash
qumidi record --host <ip> --out capture.bin --seconds 30
qumidi replay capture.bin --port 51325
qumidi monitor --host 127.0.0.1
```
