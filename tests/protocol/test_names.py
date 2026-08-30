import pytest

from qumidi.errors import ParameterRangeError
from qumidi.protocol import constants as c
from qumidi.protocol.sysex import QuSysex, get_name, parse_name_reply, set_name

HEADER = bytes([0xF0, 0x00, 0x00, 0x1A, 0x50, 0x11, 0x01, 0x00])


def test_get_name_request() -> None:
    assert get_name(0, 0x25) == HEADER + bytes([0x00, 0x01, 0x25, 0xF7])


def test_set_name_request() -> None:
    assert set_name(0, 0x25, "Kick") == HEADER + bytes(
        [0x00, 0x03, 0x25, ord("K"), ord("i"), ord("c"), ord("k"), 0xF7]
    )


def test_set_name_rejects_an_over_long_name() -> None:
    # PRD 9.4: raise rather than let the console truncate, because a truncated
    # name round-trips differently than it was set and oscillates any
    # state-mirroring application.
    with pytest.raises(ParameterRangeError) as excinfo:
        set_name(0, 0x25, "A" * (c.MAX_NAME_LENGTH + 1))
    assert str(c.MAX_NAME_LENGTH) in str(excinfo.value)


@pytest.mark.parametrize("bad", ["Ki\nck", "Kick\x00", "Kìck", "Kick\x7f"])
def test_set_name_rejects_non_printable_and_non_ascii(bad) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ParameterRangeError):
        set_name(0, 0x25, bad)


def test_parse_name_reply() -> None:
    sx = QuSysex(midi_channel=0, command=0x02, payload=bytes([0x25]) + b"Kick")
    assert parse_name_reply(sx) == (0x25, "Kick")


def test_parse_name_reply_is_tolerant_of_bad_bytes(caplog) -> None:  # type: ignore[no-untyped-def]
    # PRD 9.4: on receive, replace bytes >= 0x80 or below 0x20 with ? and log.
    sx = QuSysex(midi_channel=0, command=0x02, payload=bytes([0x25, 0x4B, 0x01, 0x63]))
    ch, name = parse_name_reply(sx)
    assert (ch, name) == (0x25, "K?c")


def test_parse_name_reply_of_an_empty_name_returns_empty_string() -> None:
    sx = QuSysex(midi_channel=0, command=0x02, payload=bytes([0x25]))
    assert parse_name_reply(sx) == (0x25, "")


def test_parse_name_reply_rejects_an_empty_payload() -> None:
    with pytest.raises(ValueError):
        parse_name_reply(QuSysex(midi_channel=0, command=0x02, payload=b""))
