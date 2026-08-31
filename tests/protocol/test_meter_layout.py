import pytest

from qumidi.errors import MeterLayoutError
from qumidi.protocol.channels import Mix
from qumidi.protocol.meters import (
    LAYOUTS,
    QU24_ALTERNATE,
    Meter,
    MeterFrame,
    MeterSlot,
)


@pytest.mark.parametrize(
    "model,total",
    [("qu16", 590), ("qu24", 830), ("qu32", 790), ("qupac", 790), ("qusb", 790)],
)
def test_totals_match_the_prd_arithmetic(model, total) -> None:  # type: ignore[no-untyped-def]
    assert LAYOUTS[model].total_meters == total


def test_the_qu24_alternate_candidate_is_encoded_too() -> None:
    # PRD 14.12: a Qu-24 frame cannot plausibly be larger than a Qu-32 frame.
    # Encode both candidates; the runtime size check picks.
    assert QU24_ALTERNATE.total_meters == 750
    assert LAYOUTS["qu24"].total_meters > LAYOUTS["qu32"].total_meters  # the suspect one


@pytest.mark.parametrize(
    "model,payload,encoded",
    [("qu16", 1180, 1349), ("qu24", 1660, 1898), ("qu32", 1580, 1806)],
)
def test_payload_and_encoded_sizes(model, payload, encoded) -> None:  # type: ignore[no-untyped-def]
    layout = LAYOUTS[model]
    assert layout.payload_bytes == payload
    assert layout.encoded_bytes == encoded


def test_mono_input_meters_are_ten_wide() -> None:
    layout = LAYOUTS["qu32"]
    assert layout.offset_of(MeterSlot.input(1, Meter.POST_PREAMP)) == 0
    assert layout.offset_of(MeterSlot.input(1, Meter.DUCKER_GR)) == 9
    assert layout.offset_of(MeterSlot.input(2, Meter.POST_PREAMP)) == 10


def test_qu32_inputs_are_not_contiguous() -> None:
    # PRD 12.3: channels 25-32 appear after the stereo inputs and a 20-meter gap.
    layout = LAYOUTS["qu32"]
    assert layout.offset_of(MeterSlot.input(24, Meter.POST_PREAMP)) == 230
    # 24 mono (240) + 3 stereo (60) + 20 unused = 320
    assert layout.offset_of(MeterSlot.input(25, Meter.POST_PREAMP)) == 320


def test_the_fourth_stereo_mix_block_is_lr() -> None:
    # PRD 12.3: getting this wrong shifts every subsequent block by 20 meters.
    layout = LAYOUTS["qu32"]
    mix910 = layout.offset_of(MeterSlot.mix(Mix.MIX910, Meter.POST_FADER))
    lr = layout.offset_of(MeterSlot.mix(Mix.LR, Meter.POST_FADER))
    assert lr - mix910 == 20


def test_mix_slots_land_in_the_stereo_mix_block_not_the_group_block() -> None:
    # The channel-id table numbers MIX1..MIX4 (mono) before MIX56..LR
    # (stereo), but offset_of walks each block *kind* independently. A mix()
    # factory that naively reuses that combined numbering as the STEREO_MIX
    # index would overshoot the real "4 stereo mix" block (count 4) and land
    # in the following "4 stereo group" block instead -- same 20-meter
    # stride, so a bare before/after diff test cannot tell the difference.
    # Pin the absolute offset instead.
    layout = LAYOUTS["qu32"]
    # 24 mono input (240) + 3 stereo input (60) + 20 unused + 8 mono input
    # (80) + 4 mono mix (40) = 440: where the "4 stereo mix" block starts.
    assert layout.offset_of(MeterSlot.mix(Mix.MIX56, Meter.TB_SIGGEN)) == 440
    assert layout.offset_of(MeterSlot.mix(Mix.MIX78, Meter.TB_SIGGEN)) == 460
    assert layout.offset_of(MeterSlot.mix(Mix.MIX910, Meter.TB_SIGGEN)) == 480
    assert layout.offset_of(MeterSlot.mix(Mix.LR, Meter.TB_SIGGEN)) == 500


def test_stereo_mix_indexing_does_not_depend_on_mono_mix_count() -> None:
    # Qu-16 has only one STEREO_MIX block (the 4-wide mix block itself, no
    # separate group/matrix blocks). If mix()'s STEREO_MIX index were derived
    # from a combined mono+stereo numbering (5..8 instead of 1..4), every
    # stereo mix lookup on this layout would overrun the block and raise.
    layout = LAYOUTS["qu16"]
    # 16 mono input (160) + 80 unused + 3 stereo input (60) + 20 unused
    # + 4 mono mix (40) = 360: where the stereo mix block starts.
    assert layout.offset_of(MeterSlot.mix(Mix.MIX56, Meter.TB_SIGGEN)) == 360
    assert layout.offset_of(MeterSlot.mix(Mix.LR, Meter.TB_SIGGEN)) == 420


def test_rta_has_31_bands_per_side() -> None:
    layout = LAYOUTS["qu32"]
    left0 = layout.offset_of(MeterSlot.rta("L", 0))
    left30 = layout.offset_of(MeterSlot.rta("L", 30))
    right0 = layout.offset_of(MeterSlot.rta("R", 0))
    assert left30 - left0 == 30
    assert right0 - left0 == 31
    with pytest.raises(MeterLayoutError):
        layout.offset_of(MeterSlot.rta("L", 31))


def test_asking_for_a_meter_the_block_does_not_have_is_an_error() -> None:
    # PRD 12.1: asking for POST_PREAMP on a mix block is an error, not a
    # silent wrong index.
    layout = LAYOUTS["qu32"]
    with pytest.raises(MeterLayoutError):
        layout.offset_of(MeterSlot.mix(Mix.MIX1, Meter.POST_PREAMP))


def test_frames_decode_lazily_and_correctly() -> None:
    layout = LAYOUTS["qu32"]
    payload = bytearray(layout.payload_bytes)
    offset = layout.offset_of(MeterSlot.input(6, Meter.POST_PREAMP)) * 2
    payload[offset : offset + 2] = (0x7C80).to_bytes(2, "big")
    frame = MeterFrame(bytes(payload), layout)
    assert frame.db(MeterSlot.input(6, Meter.POST_PREAMP)) == pytest.approx(-3.5)


def test_a_frame_of_the_wrong_size_is_rejected_at_construction() -> None:
    with pytest.raises(MeterLayoutError):
        MeterFrame(b"\x00" * 10, LAYOUTS["qu32"])


def test_an_odd_payload_length_is_rejected() -> None:
    # PRD 12.2: an odd length means the frame was truncated or the packing is
    # misunderstood.
    layout = LAYOUTS["qu32"]
    with pytest.raises(MeterLayoutError):
        MeterFrame(b"\x00" * (layout.payload_bytes - 1), layout)
