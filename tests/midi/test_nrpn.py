import pytest

from qumixer_py.midi.messages import ControlChange, NoteOn, SysEx
from qumixer_py.midi.nrpn import NrpnDecoder, RawParamUpdate, encode_nrpn
from qumixer_py.midi.parser import MidiStreamParser


def test_encode_emits_the_four_ccs_in_the_documented_order() -> None:
    assert encode_nrpn(0, ch=0x25, param_id=0x17, value=0x62, vx=0x07) == bytes(
        [0xB0, 0x63, 0x25, 0xB0, 0x62, 0x17, 0xB0, 0x06, 0x62, 0xB0, 0x26, 0x07]
    )


def test_encode_uses_the_consoles_midi_channel() -> None:
    out = encode_nrpn(5, ch=0x25, param_id=0x17, value=0x62, vx=0x07)
    assert out[0] == 0xB5 and out[3] == 0xB5 and out[6] == 0xB5 and out[9] == 0xB5


@pytest.mark.parametrize(
    "kwargs",
    [
        {"midi_channel": 16, "ch": 0, "param_id": 0, "value": 0, "vx": 0},
        {"midi_channel": 0, "ch": 0x80, "param_id": 0, "value": 0, "vx": 0},
        {"midi_channel": 0, "ch": 0, "param_id": 0, "value": 0x80, "vx": 0},
        {"midi_channel": 0, "ch": 0, "param_id": 0, "value": 0, "vx": 0x80},
    ],
)
def test_encode_range_checks_every_field(kwargs) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        encode_nrpn(**kwargs)


def feed_bytes(decoder: NrpnDecoder, data: bytes) -> list[RawParamUpdate]:
    out: list[RawParamUpdate] = []
    for msg in MidiStreamParser().feed(data):
        out.extend(decoder.feed(msg))
    return out


def test_decode_a_complete_quad() -> None:
    d = NrpnDecoder()
    out = feed_bytes(d, encode_nrpn(0, 0x25, 0x17, 0x62, 0x07))
    out.extend(d.flush())
    assert out == [
        RawParamUpdate(midi_channel=0, ch=0x25, param_id=0x17, value=0x62, vx=0x07)
    ]


def test_decode_under_running_status() -> None:
    # the (CH, ID) pair persists across messages under running status.
    d = NrpnDecoder()
    data = bytes(
        [0xB0, 0x63, 0x25, 0x62, 0x17, 0x06, 0x62, 0x26, 0x07, 0x62, 0x18, 0x06, 0x01, 0x26, 0x07]
    )
    out = feed_bytes(d, data)
    out.extend(d.flush())
    assert out == [
        RawParamUpdate(0, 0x25, 0x17, 0x62, 0x07),
        RawParamUpdate(0, 0x25, 0x18, 0x01, 0x07),
    ]


def test_update_with_no_data_lsb_emits_with_vx_none() -> None:
    # some senders omit the LSB. Emit on 06, revise on a following 26.
    d = NrpnDecoder()
    data = bytes([0xB0, 0x63, 0x25, 0xB0, 0x62, 0x17, 0xB0, 0x06, 0x62, 0xB0, 0x62, 0x18])
    out = feed_bytes(d, data)
    out.extend(d.flush())
    assert out[0] == RawParamUpdate(0, 0x25, 0x17, 0x62, None)


def test_stream_ending_after_data_msb_still_yields_one_update() -> None:
    # test explicitly that a stream ending in "... B0 06 62" with
    # nothing after it still yields one ParamUpdate.
    d = NrpnDecoder()
    out = feed_bytes(d, bytes([0xB0, 0x63, 0x25, 0xB0, 0x62, 0x17, 0xB0, 0x06, 0x62]))
    assert out == []
    assert d.flush() == [RawParamUpdate(0, 0x25, 0x17, 0x62, None)]


def test_active_sensing_does_not_flush_the_held_update() -> None:
    # FE arrives every 300ms and would break every NRPN pair if it
    # were treated as "a subsequent message".
    from qumixer_py.midi.messages import SystemRealtime

    d = NrpnDecoder()
    out = feed_bytes(d, bytes([0xB0, 0x63, 0x25, 0xB0, 0x62, 0x17, 0xB0, 0x06, 0x62]))
    out.extend(d.feed(SystemRealtime(status=0xFE)))
    assert out == []
    out.extend(d.feed(ControlChange(channel=0, controller=0x26, value=0x07)))
    assert out == [RawParamUpdate(0, 0x25, 0x17, 0x62, 0x07)]


def test_a_non_cc_message_flushes_the_held_update() -> None:
    d = NrpnDecoder()
    out = feed_bytes(d, bytes([0xB0, 0x63, 0x25, 0xB0, 0x62, 0x17, 0xB0, 0x06, 0x62]))
    out.extend(d.feed(NoteOn(channel=0, note=0x25, velocity=0x7F)))
    assert out == [RawParamUpdate(0, 0x25, 0x17, 0x62, None)]


def test_a_sysex_flushes_the_held_update() -> None:
    # flush when 14 arrives, or the last parameter in the burst is
    # stranded and the cache is silently one value short after every sync.
    d = NrpnDecoder()
    out = feed_bytes(d, bytes([0xB0, 0x63, 0x25, 0xB0, 0x62, 0x17, 0xB0, 0x06, 0x62]))
    out.extend(d.feed(SysEx(data=bytes([0x00, 0x14]))))
    assert out == [RawParamUpdate(0, 0x25, 0x17, 0x62, None)]


def test_tick_flushes_after_the_silence_timeout() -> None:
    d = NrpnDecoder(pending_flush_timeout=0.05, now=lambda: 100.0)
    d.feed(ControlChange(channel=0, controller=0x63, value=0x25))
    d.feed(ControlChange(channel=0, controller=0x62, value=0x17))
    assert d.feed(ControlChange(channel=0, controller=0x06, value=0x62)) == []
    assert d.tick(now=100.02) == []
    assert d.tick(now=100.10) == [RawParamUpdate(0, 0x25, 0x17, 0x62, None)]


def test_decoder_is_per_midi_channel() -> None:
    d = NrpnDecoder()
    d.feed(ControlChange(channel=0, controller=0x63, value=0x25))
    d.feed(ControlChange(channel=0, controller=0x62, value=0x17))
    d.feed(ControlChange(channel=1, controller=0x63, value=0x26))
    d.feed(ControlChange(channel=1, controller=0x62, value=0x18))
    out = d.feed(ControlChange(channel=0, controller=0x06, value=0x62))
    out.extend(d.feed(ControlChange(channel=1, controller=0x06, value=0x63)))
    out.extend(d.flush())
    assert RawParamUpdate(0, 0x25, 0x17, 0x62, None) in out
    assert RawParamUpdate(1, 0x26, 0x18, 0x63, None) in out


def test_data_msb_with_no_preceding_address_is_ignored() -> None:
    d = NrpnDecoder()
    assert d.feed(ControlChange(channel=0, controller=0x06, value=0x62)) == []


def test_bank_select_ccs_do_not_disturb_the_accumulator() -> None:
    # Scene recall sends CC 0 and CC 0x20 immediately before a program change.
    d = NrpnDecoder()
    d.feed(ControlChange(channel=0, controller=0x63, value=0x25))
    d.feed(ControlChange(channel=0, controller=0x62, value=0x17))
    d.feed(ControlChange(channel=0, controller=0x00, value=0x00))
    d.feed(ControlChange(channel=0, controller=0x20, value=0x00))
    out = d.feed(ControlChange(channel=0, controller=0x06, value=0x62))
    out.extend(d.flush())
    assert out == [RawParamUpdate(0, 0x25, 0x17, 0x62, None)]


def test_round_trip_over_the_parser_for_a_long_burst() -> None:
    d = NrpnDecoder()
    expected = []
    stream = b""
    for i in range(40):
        stream += encode_nrpn(0, ch=0x20 + (i % 16), param_id=0x17, value=i, vx=0x07)
        expected.append(RawParamUpdate(0, 0x20 + (i % 16), 0x17, i, 0x07))
    out = feed_bytes(d, stream)
    out.extend(d.flush())
    assert out == expected
