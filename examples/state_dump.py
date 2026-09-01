#!/usr/bin/env python3
"""Sync the console and dump everything the client knows, as JSON.

This is the shape of a show-file exporter. Two things about it are deliberate
and worth copying:

* every entry carries its **freshness**, so a consumer can tell "the console
  said 0x62" from "we wrote 0x62 and it was never confirmed" from "we have
  never been told". The library never invents a plausible value;
* a channel with no name is exported as `null`, not omitted and not replaced
  with a placeholder string.

There is deliberately no blind restore. Writing a whole snapshot back would
mean writing hundreds of parameters the console may have changed underneath
you, and some of them (group sends in Group mode, read-only parameters) would
be silently dropped. Use `diff()` and decide what to send.

Run it against the bundled fake console::

    python -m qumidi.testing.fake_qu --port 51325 &
    python examples/state_dump.py --host 127.0.0.1 --once
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import contextlib
import json

from qumidi import AsyncQuMixer


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=51325)
    ap.add_argument("--out", help="write the snapshot JSON here instead of stdout")
    ap.add_argument("--full", action="store_true", help="print the whole snapshot to stdout")
    ap.add_argument("--once", action="store_true", help="run one dump, then exit")
    ap.add_argument("--interval", type=float, default=60.0, help="seconds between dumps")
    return ap.parse_args()


def summarise(snapshot: dict[str, object]) -> None:
    values = snapshot.get("values")
    names = snapshot.get("names")
    counts: collections.Counter[str] = collections.Counter()
    if isinstance(values, list):
        for entry in values:
            if isinstance(entry, dict):
                counts[str(entry.get("freshness"))] += 1
    print(f"{sum(counts.values())} cached values: {dict(counts)}")
    if isinstance(names, dict):
        named = sum(1 for value in names.values() if value)
        print(f"{len(names)} nameable channels, {named} of them named")


async def dump(mixer: AsyncQuMixer, args: argparse.Namespace) -> None:
    snapshot = mixer.snapshot()
    summarise(snapshot)
    if args.out:
        mixer.export_snapshot(args.out)
        print(f"wrote {args.out}")
    if args.full:
        print(json.dumps(snapshot, indent=2, sort_keys=True))


async def main() -> int:
    args = parse_args()
    async with AsyncQuMixer(args.host, port=args.port) as mixer:
        await mixer.wait_synced()
        print(f"connected to {mixer.model} firmware {mixer.firmware}")
        print(f"MIDI channel {mixer.midi_channel_display}, caps {mixer.caps}")
        while True:
            await dump(mixer, args)
            if args.once:
                break
            await asyncio.sleep(args.interval)

            # A later dump compares against the first, so a show operator can
            # see what actually moved rather than re-reading 800 numbers.
            for change in mixer.diff(mixer.snapshot()):
                print(change)
    return 0


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        raise SystemExit(asyncio.run(main()))
