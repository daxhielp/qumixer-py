"""Coverage for build_parser()'s flag placement (PRD 17).

--wire/-v are shared between the top-level parser and every subparser so they
can be given either before or after the subcommand. argparse's subparsers
mechanism re-defaults flags in a fresh sub-namespace and then copies every one
of its attributes onto the outer namespace, which silently clobbers a
top-level flag unless the shared flags use default=argparse.SUPPRESS -- see
build_parser().
"""

from __future__ import annotations

import asyncio

import pytest

from qumidi.cli import build_parser, main


def test_wire_before_the_subcommand_is_not_clobbered() -> None:
    args = build_parser().parse_args(["--wire", "probe", "--host", "1.2.3.4"])
    assert args.wire is True


def test_wire_after_the_subcommand_still_works() -> None:
    args = build_parser().parse_args(["probe", "--host", "1.2.3.4", "--wire"])
    assert args.wire is True


def test_wire_after_set_and_its_positionals_works() -> None:
    # PRD 7.2 hand-test ordering: `qumidi set --host H mute input:6 on --wire`.
    args = build_parser().parse_args(
        ["set", "--host", "1.2.3.4", "mute", "input:6", "on", "--wire"]
    )
    assert args.wire is True


def test_wire_defaults_to_false_when_never_given() -> None:
    args = build_parser().parse_args(["probe", "--host", "1.2.3.4"])
    assert getattr(args, "wire", False) is False


def test_verbose_before_and_after_the_subcommand_both_work() -> None:
    before = build_parser().parse_args(["-v", "probe", "--host", "1.2.3.4"])
    after = build_parser().parse_args(["probe", "--host", "1.2.3.4", "-v"])
    assert before.verbose is True
    assert after.verbose is True


def test_to_band_parses_as_a_plain_integer() -> None:
    # geq_gain's VX mode is GeqBand, a raw band index -- not a ChannelId like
    # --to's SendTarget resolution, so --to-band must stay type=int.
    args = build_parser().parse_args(
        ["set", "--host", "1.2.3.4", "geq_gain", "lr", "-6db", "--to-band", "15"]
    )
    assert args.to_band == 15
    assert isinstance(args.to_band, int)
    assert args.to is None


def test_to_and_to_band_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["set", "--host", "1.2.3.4", "geq_gain", "lr", "-6db",
             "--to", "mix:3", "--to-band", "15"]
        )


async def test_set_to_band_reaches_the_console_as_a_raw_vx_integer(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # geq_gain lr -6db --to-band 15 (PRD 7.2, 11.1: band 15 is 1 kHz) must
    # encode with VX = 0x0F on channel 0x67 (LR), id 0x70 -- exactly the CLI
    # hand-test in the task 5.5 brief, run here against FakeQu instead of a
    # real console.
    fake = await fake_qu_factory()
    rc = await asyncio.get_running_loop().run_in_executor(
        None, main,
        ["set", "--host", "127.0.0.1", "--port", str(fake.port),
         "geq_gain", "lr", "-6db", "--to-band", "15"],
    )
    assert rc == 0
    assert (0x67, 0x70, 15) in fake.state
