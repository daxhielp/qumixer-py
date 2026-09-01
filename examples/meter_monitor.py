#!/usr/bin/env python3
"""Subscribe to a handful of meter slots and print live dB.

Metering is the experimental corner of this library, for two honest reasons:
the frame layout is model-specific and the Qu-24's is still unresolved
(erratum 14.12), and meter frames arrive fast enough that decoding all of them
would dominate the client's cost. So frames are decoded lazily, only the slots
you ask for are read, and a frame whose length does not match the computed
layout disables metering rather than being delivered mis-indexed.

Run it against the bundled fake console::

    python -m qumidi.testing.fake_qu --port 51325 &
    python examples/meter_monitor.py --host 127.0.0.1 --once

The bundled fake console emits no meter frames unless it was constructed with
some, so `--once` against it prints "0 frames" and exits cleanly. That is the
correct outcome, not a failure: point it at real hardware to see numbers.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib

from qumidi import AsyncQuMixer, Meter, MeterSlot, Mix


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=51325)
    ap.add_argument("--input", type=int, default=6, help="input channel to meter")
    ap.add_argument("--once", action="store_true", help="watch briefly, then exit")
    ap.add_argument("--seconds", type=float, default=2.0, help="watch window for --once")
    return ap.parse_args()


async def main() -> int:
    args = parse_args()
    # A mono input block and a mix block expose *different* meter taps; asking
    # a block for a tap it does not have raises rather than quietly returning
    # a neighbouring meter's number.
    slots = {
        f"Input {args.input} post-delay": MeterSlot.input(args.input, Meter.POST_DELAY),
        "LR post-fader L": MeterSlot.mix(Mix.LR, Meter.POST_FADER, "L"),
        "LR post-fader R": MeterSlot.mix(Mix.LR, Meter.POST_FADER, "R"),
    }

    frames = 0
    async with AsyncQuMixer(args.host, port=args.port) as mixer:
        await mixer.wait_synced()
        print(f"connected to {mixer.model} firmware {mixer.firmware}")

        # Metering is disabled again on exit even if the body raises, and
        # enable/disable nest, so two independent consumers cannot switch
        # each other off.
        mixer.meters.subscribe(slots.values())
        mixer.on_change(lambda e: None)  # keep the change bus alive, unused here

        problems: list[str] = []
        mixer.events.on_meter_layout_error(problems.append)

        async with mixer.meters.enabled() as meters:
            # The layout is resolved from the detected model on enable(), so
            # it is only knowable from here.
            layout = meters.layout
            print(
                f"layout {layout.name if layout else 'UNKNOWN'}: "
                f"{layout.total_meters if layout else '?'} meters per frame"
            )
            deadline = asyncio.get_running_loop().time() + args.seconds
            stream = meters.stream()
            while True:
                timeout = deadline - asyncio.get_running_loop().time() if args.once else None
                if args.once and timeout is not None and timeout <= 0:
                    break
                try:
                    frame = await asyncio.wait_for(stream.__anext__(), timeout)
                except asyncio.TimeoutError:
                    break
                except StopAsyncIteration:
                    break
                frames += 1
                line = "  ".join(
                    f"{label} {frame.db(slot):6.1f} dB" for label, slot in slots.items()
                )
                print(line)

        if problems:
            print("metering disabled:", problems[-1])
        print(f"{frames} frame(s); {mixer.meters.dropped_frames} dropped")
    return 0


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        raise SystemExit(asyncio.run(main()))
