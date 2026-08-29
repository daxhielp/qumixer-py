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


async def cmd_set(args: argparse.Namespace) -> int:
    from qumidi.protocol.channels import parse_channel_ref

    async with AsyncQuMixer(args.host, port=args.port) as mixer:
        await mixer.wait_synced()
        ch = parse_channel_ref(args.channel)
        if args.param == "mute":
            muted = args.value.lower() in ("on", "1", "true", "yes", "mute")
            await mixer.mute(ch, muted)
            print(f"{ch.label}: mute {'on' if muted else 'off'}")
            return 0

        vx = parse_channel_ref(args.to) if args.to else None
        value = args.value.lower()
        if value.endswith("db"):
            await mixer.set(ch, args.param, db=float(value[:-2]), vx=vx)
            print(f"{ch.label} {args.param}: {value[:-2]} dB")
        else:
            raw = int(value.removeprefix("raw:"), 0)
            await mixer.set(ch, args.param, raw=raw, vx=vx)
            print(f"{ch.label} {args.param}: raw {raw:#04x}")
        return 0


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
    # -v/--wire are accepted both before and after the subcommand (e.g. both
    # `qumidi --wire set ...` and `qumidi set ... --wire` work): argparse
    # hands every token after the subcommand name to that subparser alone, so
    # each subparser needs its own copy of these flags via `parents=`.
    #
    # default=SUPPRESS on both is required, not cosmetic: _SubParsersAction
    # parses the subcommand's tokens into a *fresh* sub-namespace and then
    # copies every one of its attributes onto the outer namespace. Without
    # SUPPRESS, an unset flag on the subparser still defaults to False there
    # and clobbers a True the top-level parse just set for a pre-subcommand
    # `--wire`. With SUPPRESS, the subparser's namespace only carries the
    # attribute when the flag actually appeared among the subcommand's own
    # tokens, so a value set at either level survives -- and main() must
    # therefore read both via getattr(), since the attribute can end up
    # missing from the final namespace entirely when neither level sets it.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help="debug logging"
    )
    common.add_argument(
        "--wire", action="store_true", default=argparse.SUPPRESS, help="hex-dump all MIDI traffic"
    )

    ap = argparse.ArgumentParser(
        prog="qumidi", description="Allen & Heath Qu control", parents=[common]
    )
    sub = ap.add_subparsers(dest="command", required=True)

    probe = sub.add_parser(
        "probe", parents=[common], help="connect, report model/firmware/channel, disconnect"
    )
    probe.add_argument("--host", required=True)
    probe.add_argument("--port", type=int, default=c.QU_TCP_PORT)
    probe.set_defaults(func=cmd_probe)

    setp = sub.add_parser("set", parents=[common], help="write one parameter and exit")
    setp.add_argument("--host", required=True)
    setp.add_argument("--port", type=int, default=c.QU_TCP_PORT)
    setp.add_argument("param", help="parameter name, e.g. mute")
    setp.add_argument("channel", help="channel reference, e.g. input:6, mix:3, lr")
    setp.add_argument("value", help="value, e.g. on/off, -6db, raw:0x62")
    setp.add_argument("--to", help="send destination for send parameters, e.g. mix:3, lr")
    setp.set_defaults(func=cmd_set)

    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # `wire`/`verbose` may be entirely absent from the namespace (SUPPRESS
    # default -- see build_parser()) when neither the top-level nor the
    # subcommand tokens set them.
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if getattr(args, "wire", False):
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
