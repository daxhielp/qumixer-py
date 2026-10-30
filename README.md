# qumixer-py

Control and observe Allen & Heath Qu-series digital mixers (Qu-16, Qu-24,
Qu-32, Qu-Pac, Qu-SB) from Python, over MIDI-on-TCP. No runtime dependencies.

## Install

```bash
pip install qumixer-py
```

Python 3.10 or newer. The library has zero runtime dependencies.

## ⚠️ One connection at a time

**The mixer permits exactly one TCP MIDI connection.** If Qu-Pad, Qu-Control,
or another script holds the session, this library cannot connect. This is the
first thing to check when something does not work.

## ⚠️ Security

The Qu MIDI protocol is **unauthenticated and unencrypted**, so anyone who can
reach port 51325 can interact witht the mixer and potentially perform
disruptive behavior commands. Make sure to put the mixer on a trusted, isolated
network. 

## Quickstart

```python
import asyncio
from qumixer_py import AsyncQuMixer, Mix

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
from qumixer_py import QuMixer

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
qumixer-py probe      --host 192.168.1.60     # model, firmware, MIDI channel
qumixer-py monitor    --host 192.168.1.60     # print changes as they happen
qumixer-py dump-state --host 192.168.1.60     # sync and print everything
qumixer-py set        --host 192.168.1.60 fader input:6 -6db
qumixer-py set-name   --host 192.168.1.60 input:6 Kick
qumixer-py recall     --host 192.168.1.60 12
qumixer-py record     --host 192.168.1.60 --out capture.bin
qumixer-py replay     capture.bin             # serve a capture back through the fake console
```

Add `--wire` to any of them to hex-dump every byte in both directions.

## Without hardware

This library ships with a complete, emulated console that simulates the qumidi
protocol. This is helpful for if developing without access to the 
physical board. To start the fake console, run:

```bash
python -m qumixer_py.testing.fake_qu --model qu32 --port 51325
qumixer-py probe --host 127.0.0.1
```

The entire test suite runs against it, so no hardware is needed to develop
against this library.

## Examples

Here are some runnable examples that use this library. These work in both the 
fake console and live mixer.

| Script | What it shows |
|---|---|
| [`examples/fader_follow.py`](examples/fader_follow.py) | Watch surface moves and mirror one channel onto another |
| [`examples/scene_automation.py`](examples/scene_automation.py) | Recall a scene, wait for the resync, then apply an overlay |
| [`examples/meter_monitor.py`](examples/meter_monitor.py) | Subscribe to meter slots and print live dB |
| [`examples/state_dump.py`](examples/state_dump.py) | Sync and export the whole cache as JSON |

```bash
python -m qumixer_py.testing.fake_qu --port 51325 &
python examples/state_dump.py --host 127.0.0.1 --once
```

## Reading values

The raw 0-127 value is always the truth; `.db` is a convenience and is
approximate. A value the console has never reported reads as `UNKNOWN` and
raises rather than returning a plausible-looking number — see
[docs/limitations.md](docs/limitations.md).

The library never synthesises a default. If it has not been told, it says so.

## Logging

One logger per module under the `qumixer_py` namespace, plus a separate
`qumixer_py.wire` logger that hex-dumps both directions at `DEBUG`. The library
installs no handlers and never calls `basicConfig` — that is your
application's decision.

## Documentation

- [What this library cannot do](docs/limitations.md) — read this first
- [Troubleshooting](docs/troubleshooting.md) — "my write did nothing"
- [Parameter tiers](docs/tiers.md) — what is verified against hardware
- [Protocol notes](docs/protocol-notes.md) — deliberate deviations from the vendor spec
- [Hardware verification checklist](docs/hardware-verification.md) — what still needs checking on a real console before anything is `VERIFIED`

## Status

Pre-1.0. Every parameter in the registry is encoded and round-tripped against
a golden-vector fixture, and the whole suite runs green against the fake
console — but **no parameter has yet been verified against physical
hardware**, so nothing is at `Tier.VERIFIED` and several tapers are
best-effort interpolations. See [docs/tiers.md](docs/tiers.md) for exactly
which, and for the open questions a hardware run would close.

## Development

Python 3.10+. From a clone:

```bash
python -m venv .venv
source .venv/bin/activate        # fish: source .venv/bin/activate.fish
pip install -e '.[dev]'
```

Checks (the same ones the release gate runs):

```bash
python -m pytest -m "not hardware" --cov-fail-under=90
ruff check .
mypy qumixer_py
```

No mixer is needed: the suite runs against the bundled fake console. Individual
focused runs such as `pytest tests/test_cli.py` work too.

`docs/tiers.md` is generated, so don't edit it by hand. After changing the
registry, regenerate it with `python tools/gen_tiers.py > docs/tiers.md`.
`tests/test_docs.py` fails if you forget.

The opt-in hardware suite (`tests/hardware/`) writes to a real console. Read
`tests/hardware/conftest.py` before passing `--qu-host`.

## License

MIT.
