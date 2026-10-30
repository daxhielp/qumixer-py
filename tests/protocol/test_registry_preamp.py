import pytest

from qumixer_py.protocol.channels import ChannelClass
from qumixer_py.protocol.params import Direction, DsnakeSocket, Fixed, Tier
from qumixer_py.protocol.registry import REGISTRY
from qumixer_py.protocol.units import DSNAKE_GAIN_TAPER, LOCAL_GAIN_TAPER, TRIM_TAPER


@pytest.mark.parametrize(
    "name,nrpn_id,vx_filler",
    [
        ("usb_source", 0x12, 0x00),
        ("preamp_source", 0x57, 0x00),
        ("dsnake_patch", 0x5D, 0x00),
        ("local_gain", 0x19, 0x07),
        ("local_48v", 0x69, 0x07),
        ("digital_trim", 0x52, 0x07),
        ("stereo_trim", 0x54, 0x07),
        ("polarity", 0x6A, 0x07),
        ("insert", 0x6B, 0x07),
    ],
)
def test_ids_and_fillers(name, nrpn_id, vx_filler) -> None:  # type: ignore[no-untyped-def]
    spec = REGISTRY.lookup(name)
    assert spec.nrpn_id == nrpn_id
    assert isinstance(spec.vx, Fixed)
    assert spec.vx.value == vx_filler


@pytest.mark.parametrize("name,nrpn_id", [("dsnake_gain", 0x58), ("dsnake_pad", 0x59),
                                          ("dsnake_48v", 0x5A)])
def test_the_dsnake_family_is_socket_addressed_and_experimental(name, nrpn_id) -> None:  # type: ignore[no-untyped-def]
    # CH is ambiguous until hardware verification resolves it.
    spec = REGISTRY.lookup(name)
    assert spec.nrpn_id == nrpn_id
    assert isinstance(spec.vx, DsnakeSocket)
    assert spec.tier is Tier.EXPERIMENTAL


def test_dsnake_patch_is_rx_only() -> None:
    # it is the map from channel to socket, and the only way to know
    # which socket a channel's remote preamp lives on.
    assert REGISTRY.lookup("dsnake_patch").direction is Direction.RX_ONLY


def test_group_mode_is_rx_only_and_global() -> None:
    spec = REGISTRY.lookup("group_mode")
    assert spec.nrpn_id == 0x5E
    assert spec.direction is Direction.RX_ONLY
    assert ChannelClass.GLOBAL in spec.applies_to


def test_there_is_no_local_pad_parameter() -> None:
    # local preamp has no pad parameter; only dSNAKE does. Do not
    # invent one.
    from qumixer_py.errors import UnknownParameterError

    with pytest.raises(UnknownParameterError):
        REGISTRY.lookup("local_pad")


def test_the_two_gain_parameters_use_different_tapers() -> None:
    assert REGISTRY.lookup("local_gain").domain.taper is LOCAL_GAIN_TAPER  # type: ignore[attr-defined]
    assert REGISTRY.lookup("dsnake_gain").domain.taper is DSNAKE_GAIN_TAPER  # type: ignore[attr-defined]


def test_trims_share_the_trim_taper_and_apply_to_the_right_classes() -> None:
    assert REGISTRY.lookup("digital_trim").domain.taper is TRIM_TAPER  # type: ignore[attr-defined]
    stereo = REGISTRY.lookup("stereo_trim")
    assert stereo.applies_to == frozenset({ChannelClass.STEREO_INPUT})


def test_preamp_parameters_do_not_apply_to_mixes() -> None:
    assert ChannelClass.MIX_MONO not in REGISTRY.lookup("local_gain").applies_to
    assert ChannelClass.LR not in REGISTRY.lookup("local_48v").applies_to
