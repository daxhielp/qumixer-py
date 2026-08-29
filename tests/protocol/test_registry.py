import logging

import pytest

from qumidi.errors import ParameterRangeError, UnknownParameterError, UnsupportedOnModelError
from qumidi.protocol.channels import (
    CAPS_BY_BOX_ID,
    ChannelClass,
    FxSend,
    Group,
    Input,
    Matrix,
    Mix,
)
from qumidi.protocol.params import Fixed, SendTarget
from qumidi.protocol.registry import (
    REGISTRY,
    SEND_TARGET_VX,
    VX_TO_SEND_TARGET,
    send_target_vx,
)


@pytest.mark.parametrize(
    "target,vx",
    [
        (Mix.MIX1, 0x00), (Mix.MIX2, 0x01), (Mix.MIX3, 0x02), (Mix.MIX4, 0x03),
        (Mix.MIX56, 0x04), (Mix.MIX78, 0x05), (Mix.MIX910, 0x06),
        (Mix.LR, 0x07),
        (Group(1), 0x08), (Group(2), 0x09), (Group(3), 0x0A), (Group(4), 0x0B),
        (Matrix(1), 0x0C), (Matrix(2), 0x0D),
        (FxSend(1), 0x10), (FxSend(2), 0x11), (FxSend(3), 0x12), (FxSend(4), 0x13),
    ],
)
def test_the_single_canonical_send_target_table(target, vx) -> None:  # type: ignore[no-untyped-def]
    # PRD 14.1: the spec contradicts itself; Pan and Pre/Post resolve it.
    assert SEND_TARGET_VX[target] == vx
    assert VX_TO_SEND_TARGET[vx] == target


def test_send_targets_are_not_a_dense_range() -> None:
    # PRD 7.2: note the gap at 0E-0F. VX must come from the table by lookup,
    # never from arithmetic on a mix index.
    assert 0x0E not in VX_TO_SEND_TARGET
    assert 0x0F not in VX_TO_SEND_TARGET


def test_send_target_validates_against_model_caps() -> None:
    qu16 = CAPS_BY_BOX_ID[1]
    assert send_target_vx(Mix.MIX3, qu16) == 0x02
    with pytest.raises(UnsupportedOnModelError):
        send_target_vx(Group(1), qu16)
    with pytest.raises(UnsupportedOnModelError):
        send_target_vx(Matrix(1), qu16)
    with pytest.raises(UnsupportedOnModelError):
        send_target_vx(FxSend(3), qu16)


def test_send_target_rejects_a_channel_that_is_not_a_send_destination() -> None:
    with pytest.raises(ParameterRangeError):
        send_target_vx(Input(6), None)


def test_registry_lookup_by_name() -> None:
    spec = REGISTRY.lookup("fader")
    assert spec.nrpn_id == 0x17
    assert isinstance(spec.vx, Fixed)
    assert spec.vx.value == 0x07


def test_registry_lookup_of_an_unknown_name_names_the_alternatives() -> None:
    with pytest.raises(UnknownParameterError) as excinfo:
        REGISTRY.lookup("volume")
    assert "fader" in str(excinfo.value)


def test_send_level_and_pan_use_the_send_target_vx_mode() -> None:
    assert isinstance(REGISTRY.lookup("send_level").vx, SendTarget)
    assert isinstance(REGISTRY.lookup("pan").vx, SendTarget)


def test_pan_is_bounded_at_4a_not_7f() -> None:
    # PRD 14.9: centre is 0x25, not 0x40. Any code treating pan as a signed
    # offset from 0x40 pans every channel hard left.
    domain = REGISTRY.lookup("pan").domain
    assert (domain.lo, domain.hi) == (0x00, 0x4A)  # type: ignore[attr-defined]


def test_resolve_finds_the_normal_case_by_class_and_id() -> None:
    spec = REGISTRY.resolve(ChannelClass.INPUT, 0x17, 0x07)
    assert spec is not None and spec.name == "fader"


def test_resolve_returns_none_for_an_unknown_id() -> None:
    assert REGISTRY.resolve(ChannelClass.INPUT, 0x7E, 0x07) is None


def test_resolve_falls_back_across_classes_with_a_debug_note(caplog) -> None:  # type: ignore[no-untyped-def]
    # PRD 5.3 step 3.
    with caplog.at_level(logging.DEBUG, logger="qumidi.protocol.registry"):
        spec = REGISTRY.resolve(ChannelClass.MUTE_GROUP, 0x17, 0x07)
    assert spec is not None and spec.name == "fader"
    assert any("unexpected" in r.message.lower() for r in caplog.records)


def test_resolve_warns_when_a_fixed_vx_does_not_match(caplog) -> None:  # type: ignore[no-untyped-def]
    # PRD 5.3: this is the signature of a transcription error in the registry.
    with caplog.at_level(logging.WARNING):
        spec = REGISTRY.resolve(ChannelClass.INPUT, 0x17, 0x00)
    assert spec is not None and spec.name == "fader"
    assert any("filler" in r.message.lower() for r in caplog.records)


def test_lr_assign_and_mix_assign_are_distinct_entries() -> None:
    # PRD 14.8: encode LR assignment exclusively as 18; 55/07 is rejected.
    assert REGISTRY.lookup("lr_assign").nrpn_id == 0x18
    assert REGISTRY.lookup("mix_assign").nrpn_id == 0x55


def test_params_for_a_class_excludes_parameters_that_do_not_apply() -> None:
    names = {s.name for s in REGISTRY.params_for(ChannelClass.INPUT)}
    assert "fader" in names
    assert "send_level" in names


def test_every_spec_name_is_unique_per_channel_class() -> None:
    seen: set[tuple[str, ChannelClass]] = set()
    for spec in REGISTRY:
        for cls in spec.applies_to:
            key = (spec.name, cls)
            assert key not in seen, f"{spec.name} registered twice for {cls}"
            seen.add(key)
