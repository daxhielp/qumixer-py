import pytest

from qumixer_py.errors import UnsupportedOnModelError
from qumixer_py.protocol.channels import (
    CAPS_BY_BOX_ID,
    PERMISSIVE_CAPS,
    Dca,
    FxSend,
    Group,
    Input,
    Matrix,
    Mix,
    Model,
    caps_for_box_id,
    check_channel,
)


@pytest.mark.parametrize(
    "box_id,name,mono_inputs,groups,matrices,fx",
    [
        (1, "Qu-16", 16, 0, 0, 2),
        (2, "Qu-24", 24, 2, 2, 4),
        (3, "Qu-32", 32, 4, 2, 4),
        (4, "Qu-Pac", 32, 4, 2, 4),
        (5, "Qu-SB", 32, 4, 2, 4),
    ],
)
def test_capability_table(box_id, name, mono_inputs, groups, matrices, fx) -> None:  # type: ignore[no-untyped-def]
    caps = CAPS_BY_BOX_ID[box_id]
    assert caps.name == name
    assert caps.mono_inputs == mono_inputs
    assert caps.groups == groups
    assert caps.matrices == matrices
    assert caps.fx_units == fx
    # mixes are *stereo pair* counts, matching the channel-id table.
    assert (caps.mono_mixes, caps.stereo_mixes) == (4, 3)
    assert caps.stereo_inputs == 3


def test_qu24_groups_are_marked_provisional() -> None:
    # unverified and contradictory; an hardware-verification item.
    assert CAPS_BY_BOX_ID[2].groups_provisional is True
    assert CAPS_BY_BOX_ID[3].groups_provisional is False


def test_input_17_is_unsupported_on_qu16() -> None:
    with pytest.raises(UnsupportedOnModelError) as excinfo:
        check_channel(CAPS_BY_BOX_ID[1], Input(17))
    assert "Qu-16" in str(excinfo.value)
    assert "Input 17" in str(excinfo.value)


def test_groups_and_matrix_are_unsupported_on_qu16() -> None:
    with pytest.raises(UnsupportedOnModelError):
        check_channel(CAPS_BY_BOX_ID[1], Group(1))
    with pytest.raises(UnsupportedOnModelError):
        check_channel(CAPS_BY_BOX_ID[1], Matrix(1))


def test_fx_3_and_4_are_unsupported_on_qu16() -> None:
    check_channel(CAPS_BY_BOX_ID[1], FxSend(2))
    with pytest.raises(UnsupportedOnModelError):
        check_channel(CAPS_BY_BOX_ID[1], FxSend(3))


def test_qu24_group_beyond_the_provisional_count_warns_but_does_not_raise(caplog) -> None:  # type: ignore[no-untyped-def]
    # until verified, a wrong guess here must not break working code.
    check_channel(CAPS_BY_BOX_ID[2], Group(3))
    assert any("provisional" in r.message.lower() for r in caplog.records)


def test_universal_channels_pass_on_every_model() -> None:
    for caps in CAPS_BY_BOX_ID.values():
        for ch in (Input(1), Dca(1), Mix.MIX1, Mix.MIX56, Mix.LR):
            check_channel(caps, ch)


def test_unknown_caps_are_permissive_with_a_warning(caplog) -> None:  # type: ignore[no-untyped-def]
    # before sync, capability checks are permissive with a warning.
    check_channel(None, Group(4))
    assert any("before sync" in r.message.lower() for r in caplog.records)


def test_unknown_box_id_falls_back_to_the_permissive_set(caplog) -> None:  # type: ignore[no-untyped-def]
    # refusing to connect to hardware that would otherwise work is
    # the worse failure.
    caps = caps_for_box_id(99)
    assert caps is PERMISSIVE_CAPS
    assert any("99" in r.message for r in caplog.records)


def test_model_enum_maps_to_box_ids() -> None:
    assert Model.QU32.box_id == 3
    assert Model.from_box_id(2) is Model.QU24
    assert Model.from_box_id(99) is None
