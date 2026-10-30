#!/usr/bin/env python3
"""Recall a scene, wait for the console to settle, then apply an overlay.

A scene recall is the one operation that invalidates everything the client
believes. The protocol gives no "recall finished" signal at all, so the
library recalls, waits a settling delay, resyncs, re-sweeps names, and only
then emits `SyncComplete` (see protocol note 13 in docs/protocol-notes.md).

The mistake this example exists to prevent is writing your overlay immediately
after `recall_scene()` returns *without* waiting for that resync, and then
reading back values that belong to the scene you just left.

Run it against the bundled fake console::

    python -m qumixer_py.testing.fake_qu --port 51325 &
    python examples/scene_automation.py --host 127.0.0.1 --once
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib

from qumixer_py import AsyncQuMixer, Input, Mix


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=51325)
    ap.add_argument("--scene", type=int, default=1, help="scene number, as displayed")
    ap.add_argument("--once", action="store_true", help="run one cycle, then exit")
    ap.add_argument("--interval", type=float, default=30.0, help="seconds between cycles")
    return ap.parse_args()


async def apply_overlay(mixer: AsyncQuMixer) -> None:
    """The per-scene tweaks that live in your show file, not in the console."""
    ch = mixer.input(6)
    await ch.fader.set_db(-6.0)
    await ch.send(Mix.MIX3).set_level_db(-12.0)
    await ch.mute(False)
    print(f"overlay applied to {ch.id.label}")


async def run_cycle(mixer: AsyncQuMixer, scene: int) -> None:
    order: list[str] = []
    mixer.on_scene_recalled(lambda n: order.append(f"SceneRecalled({n})"))
    mixer.on_sync_complete(lambda: order.append("SyncComplete"))

    print(f"recalling scene {scene}")
    # recall_scene() does the whole dance: STALE cache, settle delay, resync,
    # name re-sweep. It does not return until the console is believed settled.
    await mixer.recall_scene(scene)
    print(f"event order: {' -> '.join(order)}")
    print(f"last_recalled_scene = {mixer.last_recalled_scene}")

    await apply_overlay(mixer)
    value = mixer.get(Input(6), "fader")
    print(f"Input 6 fader is now {value.freshness.name} raw={value.raw}")


async def main() -> int:
    args = parse_args()
    async with AsyncQuMixer(args.host, port=args.port) as mixer:
        await mixer.wait_synced()
        print(f"connected to {mixer.model} firmware {mixer.firmware}")
        while True:
            await run_cycle(mixer, args.scene)
            if args.once:
                break
            await asyncio.sleep(args.interval)
    return 0


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        raise SystemExit(asyncio.run(main()))
