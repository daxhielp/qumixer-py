import pytest

from qumixer_py.errors import ParameterRangeError
from qumixer_py.protocol.channels import (
    GLOBAL,
    ChannelClass,
    Dca,
    FxReturn,
    FxSend,
    Group,
    Input,
    Matrix,
    Mix,
    MuteGroup,
    StereoInput,
    channel_from_raw,
    parse_channel_ref,
)


@pytest.mark.parametrize(
    "channel,raw,cls",
    [
        (FxSend(1), 0x00, ChannelClass.FX_SEND),
        (FxSend(4), 0x03, ChannelClass.FX_SEND),
        (FxReturn(1), 0x08, ChannelClass.FX_RETURN),
        (FxReturn(4), 0x0B, ChannelClass.FX_RETURN),
        (Dca(1), 0x10, ChannelClass.DCA),
        (Dca(4), 0x13, ChannelClass.DCA),
        (Input(1), 0x20, ChannelClass.INPUT),
        (Input(6), 0x25, ChannelClass.INPUT),
        (Input(32), 0x3F, ChannelClass.INPUT),
        (StereoInput(1), 0x40, ChannelClass.STEREO_INPUT),
        (StereoInput(3), 0x42, ChannelClass.STEREO_INPUT),
        (MuteGroup(1), 0x50, ChannelClass.MUTE_GROUP),
        (MuteGroup(4), 0x53, ChannelClass.MUTE_GROUP),
        (Mix.MIX1, 0x60, ChannelClass.MIX_MONO),
        (Mix.MIX4, 0x63, ChannelClass.MIX_MONO),
        (Mix.MIX56, 0x64, ChannelClass.MIX_STEREO),
        (Mix.MIX910, 0x66, ChannelClass.MIX_STEREO),
        (Mix.LR, 0x67, ChannelClass.LR),
        (Group(1), 0x68, ChannelClass.GROUP),
        (Group(4), 0x6B, ChannelClass.GROUP),
        (Matrix(1), 0x6C, ChannelClass.MATRIX),
        (Matrix(2), 0x6D, ChannelClass.MATRIX),
    ],
)
def test_channel_ids_match_the_spec_table(channel, raw, cls) -> None:  # type: ignore[no-untyped-def]
    assert channel.value == raw
    assert channel.cls is cls


def test_mix_is_seven_ids_not_ten() -> None:
    # "Mix 1 to 10" occupies only seven ids because 5-6/7-8/9-10 are stereo.
    ids = {Mix.MIX1, Mix.MIX2, Mix.MIX3, Mix.MIX4, Mix.MIX56, Mix.MIX78, Mix.MIX910}
    assert len(ids) == 7
    assert {m.value for m in ids} == set(range(0x60, 0x67))


def test_lr_is_its_own_class_not_a_stereo_mix() -> None:
    # pan and delay behave differently on LR, and the meter layout
    # treats it as a separate block.
    assert Mix.LR.cls is ChannelClass.LR
    assert Mix.MIX56.cls is ChannelClass.MIX_STEREO


def test_factories_are_one_based_and_range_checked() -> None:
    assert Input(1).value == 0x20
    with pytest.raises(ParameterRangeError):
        Input(0)
    with pytest.raises(ParameterRangeError):
        Input(33)
    with pytest.raises(ParameterRangeError):
        Dca(5)
    with pytest.raises(ParameterRangeError):
        Matrix(3)


def test_labels_use_the_numbers_the_console_displays() -> None:
    assert Input(6).label == "Input 6"
    assert Dca(2).label == "DCA 2"
    assert Mix.MIX56.label == "Mix 5-6"
    assert Mix.LR.label == "LR"
    assert Group(3).label == "Group 5-6"
    assert Matrix(2).label == "Matrix 3-4"


def test_channel_from_raw_round_trips() -> None:
    for raw in list(range(0x04)) + list(range(0x20, 0x40)) + list(range(0x60, 0x6E)):
        assert channel_from_raw(raw).value == raw


def test_channel_from_raw_rejects_an_unassigned_byte() -> None:
    with pytest.raises(ParameterRangeError):
        channel_from_raw(0x7E)


def test_global_is_synthetic_and_has_no_wire_value() -> None:
    # Group Mode has no documented CH; store it under a synthetic id.
    assert GLOBAL.cls is ChannelClass.GLOBAL
    assert GLOBAL.value == -1


def test_parse_channel_ref() -> None:
    assert parse_channel_ref("input:6") == Input(6)
    assert parse_channel_ref("mix:3") == Mix.MIX3
    assert parse_channel_ref("mix:5-6") == Mix.MIX56
    assert parse_channel_ref("lr") == Mix.LR
    assert parse_channel_ref("dca:2") == Dca(2)
    assert parse_channel_ref("st:1") == StereoInput(1)
    with pytest.raises(ParameterRangeError):
        parse_channel_ref("banana:1")


def test_channel_ids_are_hashable_and_usable_as_dict_keys() -> None:
    assert {Input(6): "kick"}[Input(6)] == "kick"
