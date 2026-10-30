import pytest

from qumixer_py.errors import ParameterRangeError
from qumixer_py.protocol.channels import ChannelClass
from qumixer_py.protocol.params import Fixed, FxTap, Tier
from qumixer_py.protocol.registry import REGISTRY
from qumixer_py.protocol.units import INPUT_DELAY_TAPER, MIX_DELAY_TAPER

IDS = [
    ("gate.attack", 0x41, 0x07), ("gate.release", 0x42, 0x07),
    ("gate.hold", 0x43, 0x07), ("gate.threshold", 0x44, 0x07),
    ("gate.depth", 0x45, 0x07), ("gate_in", 0x46, 0x00),
    ("comp.type", 0x61, 0x07), ("comp.attack", 0x62, 0x07),
    ("comp.release", 0x63, 0x07), ("comp.knee", 0x64, 0x07),
    ("comp.ratio", 0x65, 0x07), ("comp.threshold", 0x66, 0x07),
    ("comp.makeup", 0x67, 0x07), ("comp_in", 0x68, 0x00),
    ("delay_in", 0x6D, 0x00),
]


@pytest.mark.parametrize("name,nrpn_id,filler", IDS)
def test_ids_and_fillers(name, nrpn_id, filler) -> None:  # type: ignore[no-untyped-def]
    spec = REGISTRY.lookup(name)
    assert spec.nrpn_id == nrpn_id
    assert isinstance(spec.vx, Fixed)
    assert spec.vx.value == filler


def test_comp_type_enumerates_the_four_documented_modes() -> None:
    domain = REGISTRY.lookup("comp.type").domain
    assert domain.as_dict == {  # type: ignore[attr-defined]
        0x00: "manual_rms",
        0x01: "auto_slow_opto",
        0x02: "auto_punchbag",
        0x03: "manual_peak",
    }


def test_delay_time_is_registered_twice_with_disjoint_classes() -> None:
    # this is the one place a single parameter needs two domains.
    on_input = REGISTRY.lookup("delay_time", ChannelClass.INPUT)
    on_mix = REGISTRY.lookup("delay_time", ChannelClass.MIX_MONO)
    assert on_input is not on_mix
    assert on_input.nrpn_id == on_mix.nrpn_id == 0x6C
    assert on_input.applies_to.isdisjoint(on_mix.applies_to)


def test_input_delay_is_capped_at_0x40() -> None:
    spec = REGISTRY.lookup("delay_time", ChannelClass.INPUT)
    assert (spec.domain.lo, spec.domain.hi) == (0x00, 0x40)  # type: ignore[attr-defined]
    assert spec.domain.taper is INPUT_DELAY_TAPER  # type: ignore[attr-defined]
    with pytest.raises(ParameterRangeError):
        spec.domain.validate_raw(0x41, "delay_time", "Input 6")


def test_mix_delay_uses_the_full_range() -> None:
    spec = REGISTRY.lookup("delay_time", ChannelClass.MIX_MONO)
    assert (spec.domain.lo, spec.domain.hi) == (0x00, 0x7F)  # type: ignore[attr-defined]
    assert spec.domain.taper is MIX_DELAY_TAPER  # type: ignore[attr-defined]
    assert spec.domain.validate_raw(0x7F, "delay_time", "Mix 1") == 0x7F


def test_receiving_an_over_range_input_delay_clamps_rather_than_raising(caplog) -> None:  # type: ignore[no-untyped-def]
    spec = REGISTRY.lookup("delay_time", ChannelClass.INPUT)
    assert spec.domain.clamp_inbound(0x60, "delay_time", "Input 6") == 0x40


def test_the_fx_delay_48_collision_resolves_on_vx() -> None:
    # 48 is both coarse time and link, discriminated only by VX.
    coarse = REGISTRY.resolve(ChannelClass.FX_SEND, 0x48, 0x05)
    link = REGISTRY.resolve(ChannelClass.FX_SEND, 0x48, 0x06)
    right = REGISTRY.resolve(ChannelClass.FX_SEND, 0x48, 0x07)
    assert coarse is not None and coarse.name == "fx_delay_coarse"
    assert link is not None and link.name == "fx_delay_link"
    assert right is not None and right.name == "fx_delay_coarse"


def test_fx_delay_parameters_are_experimental_and_use_the_tap_vx_mode() -> None:
    for name in ("fx_delay_fine", "fx_delay_coarse", "fx_delay_link"):
        spec = REGISTRY.lookup(name)
        assert spec.tier is Tier.EXPERIMENTAL
    assert isinstance(REGISTRY.lookup("fx_delay_fine").vx, FxTap)


def test_dynamics_do_not_apply_to_dcas() -> None:
    assert ChannelClass.DCA not in REGISTRY.lookup("gate.attack").applies_to
    assert ChannelClass.DCA not in REGISTRY.lookup("comp.ratio").applies_to
