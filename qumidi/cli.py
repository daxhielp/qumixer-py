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
    from qumidi.protocol.channels import ChannelId, parse_channel_ref

    async with AsyncQuMixer(args.host, port=args.port) as mixer:
        await mixer.wait_synced()
        ch = parse_channel_ref(args.channel)
        if args.param == "mute":
            muted = args.value.lower() in ("on", "1", "true", "yes", "mute")
            await mixer.mute(ch, muted)
            print(f"{ch.label}: mute {'on' if muted else 'off'}")
            return 0

        # --to (a channel reference, e.g. mix:3) resolves through SendTarget;
        # --to-band (a plain integer, e.g. 15) is geq_gain's GeqBand VX mode --
        # a raw band index, never a ChannelId. A parameter takes one VX mode
        # or the other, never both, so the two flags are mutually exclusive.
        if args.to:
            vx: ChannelId | int | None = parse_channel_ref(args.to)
        elif args.to_band is not None:
            vx = args.to_band
        else:
            vx = None
        value = args.value.lower()
        if value.endswith("db"):
            await mixer.set(ch, args.param, db=float(value[:-2]), vx=vx)
            print(f"{ch.label} {args.param}: {value[:-2]} dB")
        else:
            raw = int(value.removeprefix("raw:"), 0)
            await mixer.set(ch, args.param, raw=raw, vx=vx)
            print(f"{ch.label} {args.param}: raw {raw:#04x}")
        return 0


async def cmd_set_name(args: argparse.Namespace) -> int:
    from qumidi.protocol.channels import parse_channel_ref

    async with AsyncQuMixer(args.host, port=args.port) as mixer:
        await mixer.wait_synced()
        ch = parse_channel_ref(args.channel)
        await mixer.set_name(ch, args.name)
        print(f"{ch.label}: {args.name!r}")
    return 0


async def cmd_monitor(args: argparse.Namespace) -> int:
    from qumidi.events import ChangeEvent, Source
    from qumidi.protocol.channels import parse_channel_ref

    channel = parse_channel_ref(args.channel) if args.channel else None

    def show(event: ChangeEvent) -> None:
        if event.source is Source.SYNC and not args.include_sync:
            return
        tag = {Source.LOCAL: "local", Source.MIXER: "SURFACE", Source.SYNC: "sync"}[event.source]
        sub = "" if event.sub_index is None else f"[{event.sub_index}]"
        old = "?" if event.old.raw is None else f"{event.old.raw:#04x}"
        print(
            f"{tag:8} {event.channel.label:<14} {event.param}{sub:<6} "
            f"{old} -> {event.new.raw:#04x}"
        )

    # The subscription must exist *before* connect() runs: the entire sync
    # burst (every Source.SYNC event --include-sync exists to show) happens
    # inside connect(), and on_change() only ever sees events emitted after
    # it is called. Constructing the mixer and subscribing first, then using
    # `async with mixer:` instead of `async with AsyncQuMixer(...) as mixer:`,
    # is what makes --include-sync see anything at all.
    mixer = AsyncQuMixer(args.host, port=args.port)
    mixer.on_change(show, channel=channel, param=args.param)
    async with mixer:
        await mixer.wait_synced()
        print(f"monitoring {args.host}; move something on the console. Ctrl-C to stop.")
        await asyncio.Event().wait()
    return 0


async def cmd_dump_state(args: argparse.Namespace) -> int:
    import json

    from qumidi.protocol.channels import channel_from_raw

    async with AsyncQuMixer(args.host, port=args.port) as mixer:
        await mixer.wait_synced()
        rows = sorted(
            mixer.cache.items(), key=lambda item: (item[0][0], item[0][2], item[0][3] or -1)
        )
        entries: list[dict[str, object]] = [
            {
                "channel_raw": raw_ch,
                "channel_class": cls.name,
                "param": param,
                "sub_index": sub,
                "raw": value.raw,
                "freshness": value.freshness.name,
            }
            for (raw_ch, cls, param, sub), value in rows
        ]
        # PRD 9.4: None stays None (never a sentinel string) all the way out
        # to the JSON, where it renders as null rather than "" or "UNKNOWN".
        names = {
            ch.label: name for ch, name in sorted(mixer.names.items(), key=lambda kv: kv[0].value)
        }
        if args.json:
            print(json.dumps(
                {
                    "box_id": mixer.box_id,
                    "firmware": mixer.firmware,
                    "names": names,
                    "values": entries,
                },
                indent=2,
            ))
        else:
            print(f"BoxID {mixer.box_id}, firmware {mixer.firmware}, {len(entries)} values")
            for (raw_ch, _cls, param, sub_index), value in rows:
                ch = channel_from_raw(raw_ch)
                name = mixer.names.get(ch)
                label_display = f"{ch.label} ({name})" if name else ch.label
                sub_display = "" if sub_index is None else f"[{sub_index}]"
                print(
                    f"  {label_display:<14} {param}{sub_display:<6} "
                    f"{value.raw:#04x}  {value.freshness.name}"
                )
    return 0


async def cmd_recall(args: argparse.Namespace) -> int:
    async with AsyncQuMixer(args.host, port=args.port) as mixer:
        await mixer.wait_synced()
        await mixer.recall_scene(args.scene, resync=not args.no_resync)
        print(f"recalled scene {args.scene}")
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
    dest_group = setp.add_mutually_exclusive_group()
    dest_group.add_argument("--to", help="send destination for send parameters, e.g. mix:3, lr")
    dest_group.add_argument(
        "--to-band", type=int, help="GEQ band index for geq_gain, e.g. 15"
    )
    setp.set_defaults(func=cmd_set)

    namep = sub.add_parser("set-name", parents=[common], help="rename a channel")
    namep.add_argument("--host", required=True)
    namep.add_argument("--port", type=int, default=c.QU_TCP_PORT)
    namep.add_argument("channel", help="channel reference, e.g. input:6, mix:3, lr")
    namep.add_argument("name", help="new name, ASCII printable characters only")
    namep.set_defaults(func=cmd_set_name)

    mon = sub.add_parser(
        "monitor", parents=[common], help="print state changes as they arrive"
    )
    mon.add_argument("--host", required=True)
    mon.add_argument("--port", type=int, default=c.QU_TCP_PORT)
    mon.add_argument("--channel", help="filter, e.g. input:6")
    mon.add_argument("--param", help="filter, e.g. fader")
    mon.add_argument("--include-sync", action="store_true", help="also show the sync burst")
    mon.set_defaults(func=cmd_monitor)

    dump = sub.add_parser(
        "dump-state", parents=[common], help="sync, print the whole cache, exit"
    )
    dump.add_argument("--host", required=True)
    dump.add_argument("--port", type=int, default=c.QU_TCP_PORT)
    dump.add_argument("--json", action="store_true")
    dump.set_defaults(func=cmd_dump_state)

    rec = sub.add_parser(
        "recall", parents=[common], help="recall a scene by its displayed number"
    )
    rec.add_argument("--host", required=True)
    rec.add_argument("--port", type=int, default=c.QU_TCP_PORT)
    rec.add_argument("scene", type=int, help="1-100, as shown on the console")
    rec.add_argument("--no-resync", action="store_true")
    rec.set_defaults(func=cmd_recall)

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
