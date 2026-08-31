#!/usr/bin/env python3
"""Sweep a parameter and record what the console displays (PRD 8.4, 13.5).

The spec gives endpoints only for frequencies, widths and the gate/compressor
times, so those tapers ship as best-effort log interpolations marked
unverified (see ``qumidi.protocol.units.log_taper``). This tool replaces them
with measured ones: it sweeps raw 0x00-0x7F on real hardware, pauses at each
step for an operator to read the console's display, and emits a ``Taper(...)``
literal ready to paste into ``qumidi/protocol/units.py`` with
``verified=True``.

This is an interactive, hardware-only operator tool. It lives outside the
``qumidi`` package (no runtime dependency on it, no --strict mypy coverage)
because it is a one-off calibration instrument, not library code.

Usage:
    python tools/calibrate.py --host 192.168.1.60 --param peq.lf.freq \\
        --channel input:1 --step 8 --out tables/peq_lf_freq.py

Open the relevant screen on the console, then read each value as prompted.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from qumidi.client_async import AsyncQuMixer
from qumidi.errors import QuError
from qumidi.protocol.channels import parse_channel_ref


def format_taper_source(
    param: str,
    channel_label: str,
    unit: str,
    points: list[tuple[int, float]],
) -> str:
    """Render collected ``(raw, value)`` points as a pasteable ``Taper(...)``.

    Pure formatting, no I/O -- kept separate from ``sweep()`` so it can be
    unit-tested without hardware or interactive input. Mirrors
    ``Taper.__post_init__``'s own "at least two anchor points" contract, so a
    bad sweep is caught here rather than at paste time in ``units.py``.
    """
    if len(points) < 2:
        raise ValueError(f"need at least two measured points, got {len(points)}")
    body = ",\n        ".join(f"({raw:#04x}, {value!r})" for raw, value in points)
    name = param.upper().replace(".", "_") + "_TAPER"
    return (
        f"#: Measured on hardware, {param} on {channel_label}.\n"
        f"{name} = Taper(\n"
        f"    points=(\n        {body},\n    ),\n"
        f'    unit="{unit}",\n'
        f"    verified=True,\n)\n"
    )


async def sweep(args: argparse.Namespace) -> tuple[list[tuple[int, float]], str]:
    """Drive the hardware sweep; return the collected points and channel label.

    Deliberately does no formatting or file I/O itself -- those are pure/sync
    concerns handled by ``main()`` after this coroutine (and the connection it
    owns) has finished, which is also what keeps this the *only* place in the
    tool that talks to hardware or blocks on interactive input.
    """
    ch = parse_channel_ref(args.channel)
    points: list[tuple[int, float]] = []
    async with AsyncQuMixer(args.host, port=args.port) as mixer:
        await mixer.wait_synced()
        print(f"Open the screen showing {args.param} for {ch.label}, then press Enter.")
        input()
        for raw in range(0x00, 0x80, args.step):
            await mixer.set(ch, args.param, raw=raw)
            await asyncio.sleep(args.settle)
            while True:
                entry = input(f"raw {raw:#04x} -> value shown on the console (blank to skip): ")
                if not entry.strip():
                    break
                try:
                    points.append((raw, float(entry)))
                    break
                except ValueError:
                    print("  enter a number, or blank to skip")
        # Always finish at the top of the range.
        if 0x7F % args.step:
            await mixer.set(ch, args.param, raw=0x7F)
            await asyncio.sleep(args.settle)
            entry = input("raw 0x7f -> value shown on the console: ")
            if entry.strip():
                points.append((0x7F, float(entry)))

    return points, ch.label


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="calibrate", description=__doc__)
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, default=51325)
    ap.add_argument("--param", required=True, help="parameter name, e.g. peq.lf.freq")
    ap.add_argument("--channel", default="input:1", help="channel reference, e.g. input:1")
    ap.add_argument("--step", type=int, default=8, help="raw step size between prompts")
    ap.add_argument("--settle", type=float, default=0.3, help="seconds to wait before prompting")
    ap.add_argument("--unit", default="Hz", help='unit label for the emitted Taper, e.g. "ms"')
    ap.add_argument("--out", help="also write the Taper source to this file")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        points, channel_label = asyncio.run(sweep(args))
    except QuError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130

    if len(points) < 2:
        print(
            f"\nonly {len(points)} point(s) recorded; a Taper needs at least two -- "
            "not writing output.",
            file=sys.stderr,
        )
        return 1

    source = format_taper_source(args.param, channel_label, args.unit, points)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(source)
        print(f"\nwrote {args.out}")
    print("\n" + source)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
