import pytest

from qumidi.errors import ParameterRangeError, UnknownParameterError
from qumidi.protocol.channels import ChannelClass
from qumidi.protocol.params import Fixed, GeqBand
from qumidi.protocol.registry import GEQ_BANDS, REGISTRY, geq_band_for_freq

PEQ_IDS = [
    ("peq.lf.gain", 0x01, 0x07), ("peq.lf.freq", 0x02, 0x07),
    ("peq.lf.width", 0x03, 0x07), ("peq.lf.type", 0x04, 0x07),
    ("peq.lm.gain", 0x05, 0x07), ("peq.lm.freq", 0x06, 0x07),
    ("peq.lm.width", 0x07, 0x07),
    ("peq.hm.gain", 0x09, 0x07), ("peq.hm.freq", 0x0A, 0x07),
    ("peq.hm.width", 0x0B, 0x07),
    ("peq.hf.gain", 0x0D, 0x07), ("peq.hf.freq", 0x0E, 0x07),
    ("peq.hf.width", 0x0F, 0x07), ("peq.hf.type", 0x10, 0x07),
    ("peq_in", 0x11, 0x00),
    ("hpf_freq", 0x13, 0x07), ("hpf_in", 0x14, 0x00),
    ("geq_in", 0x71, 0x00),
]


@pytest.mark.parametrize("name,nrpn_id,filler", PEQ_IDS)
def test_eq_ids_and_fillers(name, nrpn_id, filler) -> None:  # type: ignore[no-untyped-def]
    spec = REGISTRY.lookup(name)
    assert spec.nrpn_id == nrpn_id
    assert isinstance(spec.vx, Fixed)
    assert spec.vx.value == filler


def test_ids_08_and_0c_are_unassigned() -> None:
    # PRD 7.2: the band blocks are 4 wide with the fourth slot used only by
    # LF/HF for Type. Do not fill the gaps.
    used = {spec.nrpn_id for spec in REGISTRY}
    assert 0x08 not in used
    assert 0x0C not in used


def test_only_lf_and_hf_have_a_type() -> None:
    REGISTRY.lookup("peq.lf.type")
    REGISTRY.lookup("peq.hf.type")
    for missing in ("peq.lm.type", "peq.hm.type"):
        with pytest.raises(UnknownParameterError):
            REGISTRY.lookup(missing)


def test_peq_type_is_an_enum_of_bell_and_shelf() -> None:
    domain = REGISTRY.lookup("peq.lf.type").domain
    assert domain.label(0x00) == "bell"  # type: ignore[attr-defined]
    assert domain.label(0x06) == "shelf"  # type: ignore[attr-defined]
    with pytest.raises(ParameterRangeError):
        domain.validate_raw(0x01, "peq.lf.type", "Input 6")


def test_geq_gain_is_band_addressed() -> None:
    spec = REGISTRY.lookup("geq_gain")
    assert spec.nrpn_id == 0x70
    assert isinstance(spec.vx, GeqBand)


def test_geq_applies_only_to_mixes_and_lr() -> None:
    # PRD 5.3: GEQ 70 exists only on mixes/LR -- this is exactly why keying on
    # the parameter id alone is insufficient.
    applies = REGISTRY.lookup("geq_gain").applies_to
    assert ChannelClass.LR in applies
    assert ChannelClass.MIX_MONO in applies
    assert ChannelClass.INPUT not in applies


def test_there_are_28_geq_bands_starting_at_31_5_hz() -> None:
    assert len(GEQ_BANDS) == 28
    assert GEQ_BANDS[0] == 31.5
    assert GEQ_BANDS[-1] == 16000.0


def test_one_kilohertz_is_band_fifteen_not_fourteen() -> None:
    # PRD 11.1: the v1.0 example was wrong. Band 14 is 800 Hz.
    assert GEQ_BANDS[14] == 800.0
    assert GEQ_BANDS[15] == 1000.0
    assert geq_band_for_freq(1000) == 15


def test_geq_band_lookup_snaps_to_the_nearest_band() -> None:
    assert geq_band_for_freq(31.5) == 0
    assert geq_band_for_freq(16000) == 27
    assert geq_band_for_freq(1050) == 15
    with pytest.raises(ParameterRangeError):
        geq_band_for_freq(50000)
