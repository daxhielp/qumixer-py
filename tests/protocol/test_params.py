import logging

import pytest

from qumidi.errors import ParameterRangeError
from qumidi.protocol.channels import ChannelClass
from qumidi.protocol.params import (
    Boolean,
    Direction,
    DsnakeSocket,
    EnumDomain,
    Fixed,
    FlaggedIndex,
    FxTap,
    GeqBand,
    Interpolated,
    ParamSpec,
    Raw7Bit,
    SendTarget,
    Tier,
)
from qumidi.protocol.units import FADER_TAPER


def test_fixed_vx_supplies_the_filler_byte() -> None:
    assert Fixed(0x07).validate(None, "fader") == 0x07
    assert Fixed(0x00).validate(None, "peq_in") == 0x00


def test_fixed_vx_rejects_a_caller_supplied_index() -> None:
    with pytest.raises(ParameterRangeError) as excinfo:
        Fixed(0x07).validate(0x02, "fader")
    assert "fader" in str(excinfo.value)


def test_fixed_vx_has_no_sub_index() -> None:
    # PRD 10.1a: a Fixed filler VX contributes no addressing index.
    assert Fixed(0x07).sub_index(0x07, 0x62) is None


def test_geq_band_bounds() -> None:
    assert GeqBand().validate(0x00, "geq_gain") == 0x00
    assert GeqBand().validate(0x1B, "geq_gain") == 0x1B
    with pytest.raises(ParameterRangeError):
        GeqBand().validate(0x1C, "geq_gain")
    with pytest.raises(ParameterRangeError):
        GeqBand().validate(None, "geq_gain")
    assert GeqBand().sub_index(0x0F, 0x40) == 0x0F


def test_dsnake_socket_bounds() -> None:
    assert DsnakeSocket().validate(0x27, "dsnake_gain") == 0x27
    with pytest.raises(ParameterRangeError):
        DsnakeSocket().validate(0x28, "dsnake_gain")


def test_send_target_requires_a_vx_and_checks_the_7bit_range() -> None:
    # PRD 7.2: SendTarget only enforces presence and 7-bit range here; the
    # real SEND_TARGET_VX lookup table belongs to the registry (a later task).
    assert SendTarget().validate(0x50, "send_level") == 0x50
    with pytest.raises(ParameterRangeError):
        SendTarget().validate(None, "send_level")
    with pytest.raises(ParameterRangeError):
        SendTarget().validate(0x80, "send_level")
    assert SendTarget().sub_index(0x50, 0x62) == 0x50


def test_fx_tap_accepts_only_the_three_documented_values() -> None:
    for vx in (0x05, 0x06, 0x07):
        assert FxTap().validate(vx, "fx_delay_coarse") == vx
    with pytest.raises(ParameterRangeError):
        FxTap().validate(0x04, "fx_delay_coarse")


def test_boolean_domain() -> None:
    d = Boolean()
    assert d.validate_raw(0x00, "mute", "Input 6") == 0x00
    assert d.validate_raw(0x01, "mute", "Input 6") == 0x01
    with pytest.raises(ParameterRangeError):
        d.validate_raw(0x02, "peq_in", "Input 6")


def test_raw7bit_bounds_are_explicit_and_enforced_on_write() -> None:
    # PRD 7.1: several parameters do not use the full 00-7F span.
    pan = Raw7Bit(lo=0x00, hi=0x4A)
    assert pan.validate_raw(0x4A, "pan", "Input 6") == 0x4A
    with pytest.raises(ParameterRangeError) as excinfo:
        pan.validate_raw(0x7F, "pan", "Input 6")
    assert "Input 6" in str(excinfo.value)
    assert "pan" in str(excinfo.value)
    assert "0x4a" in str(excinfo.value).lower()


def test_raw7bit_clamps_rather_than_rejecting_on_receive(caplog) -> None:  # type: ignore[no-untyped-def]
    # PRD 7.1: out-of-range reads from the console are clamped and logged,
    # never rejected.
    pan = Raw7Bit(lo=0x00, hi=0x4A)
    with caplog.at_level(logging.WARNING):
        assert pan.clamp_inbound(0x7F, "pan", "Input 6") == 0x4A
    assert any("clamp" in r.message.lower() for r in caplog.records)


def test_flagged_index_puts_the_index_in_the_value() -> None:
    # PRD 14.10 / 10.1a: this is the single easiest thing to get wrong.
    d = FlaggedIndex(on_flag=0x40, count=4)
    assert d.sub_index_from_value(0x00) == 0
    assert d.sub_index_from_value(0x03) == 3
    assert d.sub_index_from_value(0x40) == 0
    assert d.sub_index_from_value(0x42) == 2
    assert d.is_on(0x42) is True
    assert d.is_on(0x02) is False
    assert d.encode(index=2, on=True) == 0x42
    assert d.encode(index=2, on=False) == 0x02


def test_flagged_index_rejects_an_out_of_range_group() -> None:
    d = FlaggedIndex(on_flag=0x40, count=4)
    with pytest.raises(ParameterRangeError):
        d.encode(index=4, on=True)
    with pytest.raises(ParameterRangeError):
        d.validate_raw(0x44, "dca_assign", "Input 6")


def test_interpolated_domain_delegates_to_the_taper() -> None:
    # Always construct Interpolated by keyword: it inherits Raw7Bit's lo/hi,
    # so the first positional argument is `lo`, not the taper.
    d = Interpolated(lo=0x00, hi=0x7F, taper=FADER_TAPER)
    assert d.to_raw(0.0) == 0x62
    assert d.to_db(0x62) == pytest.approx(0.0, abs=0.5)


def test_enum_domain() -> None:
    d = EnumDomain({0x00: "bell", 0x06: "shelf"})
    assert d.validate_raw(0x06, "lf_type", "Input 6") == 0x06
    assert d.label(0x00) == "bell"
    assert d.from_label("shelf") == 0x06
    with pytest.raises(ParameterRangeError):
        d.validate_raw(0x01, "lf_type", "Input 6")


def test_param_spec_defaults() -> None:
    spec = ParamSpec(
        name="fader",
        nrpn_id=0x17,
        vx=Fixed(0x07),
        domain=Interpolated(lo=0x00, hi=0x7F, taper=FADER_TAPER),
        applies_to=frozenset({ChannelClass.INPUT}),
    )
    assert spec.direction is Direction.BIDIRECTIONAL
    assert spec.tier is Tier.SUPPORTED
    assert spec.notes == ""
