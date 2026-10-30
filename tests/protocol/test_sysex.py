import pytest

from qumixer_py.midi.messages import SysEx
from qumixer_py.protocol.sysex import (
    QuSysex,
    build_sysex,
    get_system_state,
    meters_enable,
    parse_sysex,
    parse_system_state_reply,
)

HEADER = bytes([0xF0, 0x00, 0x00, 0x1A, 0x50, 0x11, 0x01, 0x00])


def test_build_uses_the_documented_header_and_channel_byte() -> None:
    assert build_sysex(0, bytes([0x10, 0x01])) == HEADER + bytes([0x00, 0x10, 0x01, 0xF7])


def test_build_encodes_a_non_zero_midi_channel() -> None:
    assert build_sysex(5, bytes([0x14])) == HEADER + bytes([0x05, 0x14, 0xF7])


def test_build_with_none_uses_the_all_call_byte() -> None:
    # the all-call form is required for the initial state request,
    # because the mixer's MIDI channel is unknown until it replies.
    assert build_sysex(None, bytes([0x10, 0x01])) == HEADER + bytes([0x7F, 0x10, 0x01, 0xF7])


def test_get_system_state_is_all_call_with_the_ipad_flag() -> None:
    assert get_system_state(1) == HEADER + bytes([0x7F, 0x10, 0x01, 0xF7])
    assert get_system_state(0) == HEADER + bytes([0x7F, 0x10, 0x00, 0xF7])


def test_parse_round_trips_a_built_message() -> None:
    raw = build_sysex(5, bytes([0x11, 0x03, 0x01, 0x09]))
    parsed = parse_sysex(SysEx(data=raw[1:-1]))
    assert parsed == QuSysex(midi_channel=5, command=0x11, payload=bytes([0x03, 0x01, 0x09]))


def test_parse_returns_none_for_a_foreign_sysex() -> None:
    # A universal MMC message must not be mistaken for a Qu message.
    assert parse_sysex(SysEx(data=bytes([0x7F, 0x7F, 0x06, 0x01]))) is None


def test_parse_returns_none_for_a_truncated_qu_sysex() -> None:
    assert parse_sysex(SysEx(data=bytes([0x00, 0x00, 0x1A]))) is None


def test_parse_accepts_the_all_call_channel_byte() -> None:
    parsed = parse_sysex(SysEx(data=bytes([0x00, 0x00, 0x1A, 0x50, 0x11, 0x01, 0x00, 0x7F, 0x14])))
    assert parsed is not None
    assert parsed.midi_channel is None
    assert parsed.command == 0x14


def test_parse_rejects_a_nonsense_channel_byte() -> None:
    # reject a header byte outside 00-0F (or the all-call 7F) rather
    # than deriving a nonsense channel from it.
    assert parse_sysex(SysEx(data=bytes([0x00, 0x00, 0x1A, 0x50, 0x11, 0x01, 0x00, 0x40, 0x14]))) is None


def test_parse_system_state_reply() -> None:
    sx = QuSysex(midi_channel=0, command=0x11, payload=bytes([0x03, 0x01, 0x09]))
    assert parse_system_state_reply(sx) == (3, (1, 9))


def test_parse_system_state_reply_rejects_a_short_payload() -> None:
    with pytest.raises(ValueError):
        parse_system_state_reply(QuSysex(midi_channel=0, command=0x11, payload=bytes([0x03])))


def test_meters_enable_and_disable() -> None:
    assert meters_enable(0, True) == HEADER + bytes([0x00, 0x12, 0x01, 0xF7])
    assert meters_enable(0, False) == HEADER + bytes([0x00, 0x12, 0x00, 0xF7])


def test_build_rejects_non_seven_bit_payload_bytes() -> None:
    with pytest.raises(ValueError):
        build_sysex(0, bytes([0x03, 0x80]))
