"""Coverage for build_parser()'s flag placement (PRD 17).

--wire/-v are shared between the top-level parser and every subparser so they
can be given either before or after the subcommand. argparse's subparsers
mechanism re-defaults flags in a fresh sub-namespace and then copies every one
of its attributes onto the outer namespace, which silently clobbers a
top-level flag unless the shared flags use default=argparse.SUPPRESS -- see
build_parser().
"""

from __future__ import annotations

from qumidi.cli import build_parser


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
