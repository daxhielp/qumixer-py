# qumidi

Control and observe Allen & Heath Qu-series digital mixers (Qu-16, Qu-24,
Qu-32, Qu-Pac, Qu-SB) from Python, over MIDI-on-TCP. No runtime dependencies.

## Install

```bash
pip install qumidi
```

Python 3.10 or newer. The library has zero runtime dependencies.

## ⚠️ One connection at a time

**The mixer permits exactly one TCP MIDI connection.** If Qu-Pad, Qu-Control,
or another script holds the session, this library cannot connect. This is the
first thing to check when something does not work.

## ⚠️ Security

The Qu MIDI protocol is **unauthenticated and unencrypted**. Anyone who can
reach port 51325 can mute the PA, recall a scene mid-show, or trigger a remote
shutdown that requires a physical power cycle to recover from. Put the mixer
on a trusted, isolated network. This library provides no access control and
cannot: there is nothing to authenticate against.

There is deliberately no `password` parameter anywhere in this API. If you see
one in a fork, it is decoration.

Remote shutdown is double-guarded here — it needs both `allow_shutdown=True`
on the constructor and `confirm=True` on the call — but that is this library
being careful, not the protocol being safe.

## Quickstart

```python
import asyncio
from qumidi import AsyncQuMixer, Mix

async def main():
    async with AsyncQuMixer("192.168.1.60") as mixer:
        await mixer.wait_synced()
        print(mixer.model, mixer.firmware)

        ch = mixer.input(6)
        print(ch.name)
        await ch.fader.set_db(-6.0)
        await ch.send(Mix.MIX3).set_level_db(-12.0)
        await ch.mute(False)

asyncio.run(main())
```

Or synchronously:

```python
from qumidi import QuMixer

with QuMixer("192.168.1.60") as mixer:
    mixer.wait_synced()
    mixer.input(6).fader.db = -6.0
```

Everything the async API offers is reachable this way, sub-APIs included —
`mixer.meters` and `mixer.strips` block like every other call, and the two
async-only shapes have plain synchronous equivalents:

```python
with QuMixer("192.168.1.60") as mixer:
    mixer.wait_synced()
    with mixer.meters.enabled(), mixer.meters.stream() as frames:
        for frame in frames:
            print(frame.db(slot))
```

## Command line

```bash
qumidi probe      --host 192.168.1.60     # model, firmware, MIDI channel
qumidi monitor    --host 192.168.1.60     # print changes as they happen
qumidi dump-state --host 192.168.1.60     # sync and print everything
qumidi set        --host 192.168.1.60 fader input:6 -6db
qumidi set-name   --host 192.168.1.60 input:6 Kick
qumidi recall     --host 192.168.1.60 12
qumidi record     --host 192.168.1.60 --out capture.bin
qumidi replay     capture.bin             # serve a capture back through the fake console
```

Add `--wire` to any of them to hex-dump every byte in both directions.

## Without hardware

A complete fake console ships with the library:

```bash
python -m qumidi.testing.fake_qu --model qu32 --port 51325
qumidi probe --host 127.0.0.1
```

The entire test suite runs against it; no hardware is needed to develop
against this library.

## Examples

Runnable, and all four run against the fake console:

| Script | What it shows |
|---|---|
| [`examples/fader_follow.py`](examples/fader_follow.py) | Watch surface moves and mirror one channel onto another |
| [`examples/scene_automation.py`](examples/scene_automation.py) | Recall a scene, wait for the resync, then apply an overlay |
| [`examples/meter_monitor.py`](examples/meter_monitor.py) | Subscribe to meter slots and print live dB |
| [`examples/state_dump.py`](examples/state_dump.py) | Sync and export the whole cache as JSON |

```bash
python -m qumidi.testing.fake_qu --port 51325 &
python examples/state_dump.py --host 127.0.0.1 --once
```

## Reading values

The raw 0-127 value is always the truth; `.db` is a convenience and is
approximate. A value the console has never reported reads as `UNKNOWN` and
raises rather than returning a plausible-looking number — see
[docs/limitations.md](docs/limitations.md).

The library never synthesises a default. If it has not been told, it says so.

## Logging

One logger per module under the `qumidi` namespace, plus a separate
`qumidi.wire` logger that hex-dumps both directions at `DEBUG`. The library
installs no handlers and never calls `basicConfig` — that is your
application's decision.

## Documentation

- [What this library cannot do](docs/limitations.md) — read this first
- [Troubleshooting](docs/troubleshooting.md) — "my write did nothing"
- [Parameter tiers](docs/tiers.md) — what is verified against hardware
- [Protocol notes](docs/protocol-notes.md) — deliberate deviations from the vendor spec

## Status

Pre-1.0. Every parameter in the registry is encoded and round-tripped against
a golden-vector fixture, and the whole suite runs green against the fake
console — but **no parameter has yet been verified against physical
hardware**, so nothing is at `Tier.VERIFIED` and several tapers are
best-effort interpolations. See [docs/tiers.md](docs/tiers.md) for exactly
which, and for the open questions a hardware run would close.

## License

MIT.
