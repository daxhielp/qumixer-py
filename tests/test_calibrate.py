"""Tests for tools/calibrate.py's pure formatting logic.

The tool itself is an interactive hardware operator instrument -- ``sweep()``
drives a real console and blocks on ``input()`` -- so it cannot be exercised
without physical hardware and a human reading a display. What *can* and must
be tested without either is ``format_taper_source``: the pure string
formatting that turns collected ``(raw, value)`` points into a ``Taper(...)``
literal. These tests do not exercise the interactive sweep, and no hardware
measurement data is fabricated anywhere here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from calibrate import build_parser, format_taper_source


def test_format_taper_source_basic_shape() -> None:
    source = format_taper_source(
        "peq.lf.freq", "In 1", "Hz", [(0x00, 20.0), (0x40, 1000.0), (0x7F, 20000.0)]
    )
    assert "PEQ_LF_FREQ_TAPER = Taper(" in source
    assert "peq.lf.freq on In 1" in source
    assert '"Hz"' in source
    assert "verified=True" in source
    assert "(0x00, 20.0)" in source
    assert "(0x40, 1000.0)" in source
    assert "(0x7f, 20000.0)" in source


def test_format_taper_source_name_replaces_dots() -> None:
    source = format_taper_source("gate.attack", "In 3", "ms", [(0x00, 0.05), (0x7F, 300.0)])
    assert source.startswith("#: Measured on hardware, gate.attack on In 3.\n")
    assert "GATE_ATTACK_TAPER = Taper(" in source


def test_format_taper_source_is_valid_python_defining_a_taper() -> None:
    """The emitted text is exactly what gets pasted into units.py -- it must
    actually parse and construct a real, valid Taper (ascending raws, >= 2
    points), not merely look plausible as a string.
    """
    from qumixer_py.protocol.units import Taper

    source = format_taper_source(
        "comp.release", "In 5", "ms", [(0x00, 100.0), (0x40, 900.0), (0x7F, 2000.0)]
    )
    namespace: dict[str, object] = {"Taper": Taper}
    exec(compile(source, "<calibrate-output>", "exec"), namespace)  # noqa: S102 -- trusted, self-generated source under test, not external input
    taper = namespace["COMP_RELEASE_TAPER"]
    assert isinstance(taper, Taper)
    assert taper.verified is True
    assert taper.unit == "ms"
    assert taper.to_db(0x00) == pytest.approx(100.0)
    assert taper.to_db(0x7F) == pytest.approx(2000.0)


def test_format_taper_source_rejects_fewer_than_two_points() -> None:
    with pytest.raises(ValueError, match="at least two"):
        format_taper_source("hpf_freq", "In 1", "Hz", [(0x40, 100.0)])


def test_format_taper_source_rejects_no_points() -> None:
    with pytest.raises(ValueError, match="at least two"):
        format_taper_source("hpf_freq", "In 1", "Hz", [])


def test_build_parser_requires_host_and_param() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_build_parser_defaults() -> None:
    args = build_parser().parse_args(["--host", "10.0.0.5", "--param", "peq.lf.freq"])
    assert args.host == "10.0.0.5"
    assert args.port == 51325
    assert args.channel == "input:1"
    assert args.step == 8
    assert args.settle == 0.3
    assert args.unit == "Hz"
    assert args.out is None


def test_build_parser_parses_all_options() -> None:
    args = build_parser().parse_args(
        [
            "--host", "192.168.1.60",
            "--port", "12345",
            "--param", "gate.attack",
            "--channel", "input:3",
            "--step", "4",
            "--settle", "0.5",
            "--unit", "ms",
            "--out", "/tmp/out.py",
        ]
    )
    assert args.port == 12345
    assert args.param == "gate.attack"
    assert args.channel == "input:3"
    assert args.step == 4
    assert args.settle == 0.5
    assert args.unit == "ms"
    assert args.out == "/tmp/out.py"
