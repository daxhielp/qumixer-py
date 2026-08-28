"""Command line tools (PRD 17).

``probe`` is the first thing to run when something is wrong: connect, report
model, firmware and MIDI channel, disconnect.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from qumidi.client_async import AsyncQuMixer
from qumidi.errors import QuError
from qumidi.protocol import constants as c


async def cmd_probe(args: argparse.Namespace) -> int:
    async with AsyncQuMixer(args.host, port=args.port) as mixer:
        await mixer.wait_synced()
        print(f"host:          {args.host}:{args.port}")
        print(f"BoxID:         {mixer.box_id}")
        assert mixer.firmware is not None
        print(f"firmware:      {mixer.firmware[0]}.{mixer.firmware[1]}")
        print(f"MIDI channel:  {mixer.midi_channel_display} (wire {mixer.midi_channel})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="qumidi", description="Allen & Heath Qu control")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    ap.add_argument("--wire", action="store_true", help="hex-dump all MIDI traffic")
    sub = ap.add_subparsers(dest="command", required=True)

    probe = sub.add_parser("probe", help="connect, report model/firmware/channel, disconnect")
    probe.add_argument("--host", required=True)
    probe.add_argument("--port", type=int, default=c.QU_TCP_PORT)
    probe.set_defaults(func=cmd_probe)

    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if args.wire:
        logging.getLogger("qumidi.wire").setLevel(logging.DEBUG)
    try:
        result: int = asyncio.run(args.func(args))
        return result
    except QuError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
