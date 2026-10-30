#!/usr/bin/env python3
"""Mirror one channel's fader onto another, live.

The point of this example is the thing you must get right in *every* follow
script: react to `Source.MIXER` only. A write you make yourself comes back as
`Source.LOCAL`, and following your own writes is how a follow script turns
into a feedback loop that ramps a fader into the ceiling.

Run it against the bundled fake console::

    python -m qumixer_py.testing.fake_qu --port 51325 &
    python examples/fader_follow.py --host 127.0.0.1 --once

`--once` watches for a couple of seconds and exits, which is what the test
suite does; without it, the script runs until you interrupt it.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib

from qumixer_py import AsyncQuMixer, ChangeEvent, Input, Source


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=51325)
    ap.add_argument("--leader", type=int, default=6, help="input channel to follow")
    ap.add_argument("--follower", type=int, default=7, help="input channel to drive")
    ap.add_argument("--once", action="store_true", help="watch briefly, then exit")
    ap.add_argument("--seconds", type=float, default=2.0, help="watch window for --once")
    return ap.parse_args()


async def main() -> int:
    args = parse_args()
    leader, follower = Input(args.leader), Input(args.follower)
    pending: asyncio.Queue[int] = asyncio.Queue()
    mirrored = 0

    def on_change(event: ChangeEvent) -> None:
        # Source.MIXER means a human moved something on the surface.
        # Source.LOCAL is our own echo; Source.SYNC is the state burst.
        # Following either of those is a feedback loop.
        if event.source is not Source.MIXER:
            return
        if event.param != "fader" or event.channel != leader:
            return
        if event.new.raw is not None:
            pending.put_nowait(event.new.raw)

    async with AsyncQuMixer(args.host, port=args.port) as mixer:
        await mixer.wait_synced()
        print(f"connected to {mixer.model} firmware {mixer.firmware}")
        mixer.on_change(on_change)

        start = mixer.get(leader, "fader")
        print(f"{leader.label} fader is currently {start.freshness.name} raw={start.raw}")
        print(f"following {leader.label} -> {follower.label}; move the leader's fader")

        deadline = asyncio.get_running_loop().time() + args.seconds
        while True:
            timeout = deadline - asyncio.get_running_loop().time() if args.once else None
            if args.once and timeout is not None and timeout <= 0:
                break
            try:
                raw = await asyncio.wait_for(pending.get(), timeout)
            except asyncio.TimeoutError:
                break
            await mixer.set(follower, "fader", raw=raw)
            mirrored += 1
            print(f"mirrored raw={raw:#04x} onto {follower.label}")

    print(f"mirrored {mirrored} move(s)")
    return 0


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        raise SystemExit(asyncio.run(main()))
