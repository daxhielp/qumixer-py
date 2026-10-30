from qumixer_py.midi.messages import (
    ControlChange,
    NoteOn,
    ProgramChange,
    SysEx,
    SystemRealtime,
)
from qumixer_py.midi.parser import MidiStreamParser


def parse_all(data: bytes) -> list[object]:
    return MidiStreamParser().feed(data)


def test_simple_control_change() -> None:
    assert parse_all(bytes([0xB0, 0x63, 0x25])) == [
        ControlChange(channel=0, controller=0x63, value=0x25)
    ]


def test_running_status_repeats_the_status_byte() -> None:
    # Rule 1: the Qu sends long NRPN bursts under running status.
    data = bytes([0xB0, 0x63, 0x25, 0x62, 0x17, 0x06, 0x62, 0x26, 0x07])
    assert parse_all(data) == [
        ControlChange(channel=0, controller=0x63, value=0x25),
        ControlChange(channel=0, controller=0x62, value=0x17),
        ControlChange(channel=0, controller=0x06, value=0x62),
        ControlChange(channel=0, controller=0x26, value=0x07),
    ]


def test_sysex_split_across_feeds() -> None:
    # Rule 2: a meter frame will not arrive in one TCP read.
    parser = MidiStreamParser()
    assert parser.feed(bytes([0xF0, 0x00, 0x00])) == []
    assert parser.feed(bytes([0x1A, 0x50])) == []
    assert parser.feed(bytes([0xF7])) == [SysEx(data=bytes([0x00, 0x00, 0x1A, 0x50]))]


def test_realtime_inside_sysex_is_extracted_without_disturbing_it() -> None:
    # Rule 3: FE arrives every 300ms, including mid-sysex.
    data = bytes([0xF0, 0x00, 0xFE, 0x01, 0xF7])
    assert parse_all(data) == [SystemRealtime(status=0xFE), SysEx(data=bytes([0x00, 0x01]))]


def test_realtime_between_running_status_data_does_not_clear_running_status() -> None:
    # Rule 3 + rule 6: F8-FF must not clear running status.
    data = bytes([0xB0, 0x63, 0x25, 0xFE, 0x62, 0x17])
    assert parse_all(data) == [
        ControlChange(channel=0, controller=0x63, value=0x25),
        SystemRealtime(status=0xFE),
        ControlChange(channel=0, controller=0x62, value=0x17),
    ]


def test_realtime_between_the_two_data_bytes_of_one_message() -> None:
    data = bytes([0xB0, 0x63, 0xFE, 0x25])
    assert parse_all(data) == [
        SystemRealtime(status=0xFE),
        ControlChange(channel=0, controller=0x63, value=0x25),
    ]


def test_truncated_sysex_is_discarded_not_raised() -> None:
    # Rule 4: a new status byte before F7 aborts the sysex.
    data = bytes([0xF0, 0x00, 0x01, 0xB0, 0x63, 0x25])
    assert parse_all(data) == [ControlChange(channel=0, controller=0x63, value=0x25)]


def test_unknown_status_byte_is_skipped() -> None:
    # Rule 5. 0xF4 is undefined; the CC after it must still parse.
    data = bytes([0xF4, 0xB0, 0x63, 0x25])
    assert parse_all(data) == [ControlChange(channel=0, controller=0x63, value=0x25)]


def test_system_common_clears_running_status() -> None:
    # Rule 6: F1-F6 clear running status; the trailing 62 17 is then orphaned.
    data = bytes([0xB0, 0x63, 0x25, 0xF6, 0x62, 0x17])
    assert parse_all(data) == [ControlChange(channel=0, controller=0x63, value=0x25)]


def test_oversized_sysex_is_discarded_and_the_stream_resynchronises() -> None:
    # Rule 7: a lost F7 must not grow the buffer without limit.
    parser = MidiStreamParser(max_sysex_bytes=8)
    out = parser.feed(bytes([0xF0]) + bytes(32) + bytes([0xB0, 0x63, 0x25]))
    assert out == [ControlChange(channel=0, controller=0x63, value=0x25)]


def test_lone_sysex_end_is_ignored() -> None:
    # Rule 8.
    assert parse_all(bytes([0xF7, 0xB0, 0x63, 0x25])) == [
        ControlChange(channel=0, controller=0x63, value=0x25)
    ]


def test_high_bit_byte_where_a_data_byte_is_expected_restarts_parsing() -> None:
    # Rule 9: never let 0x80+ reach a value field.
    data = bytes([0xB0, 0x63, 0x90, 0x25, 0x7F])
    assert parse_all(data) == [NoteOn(channel=0, note=0x25, velocity=0x7F)]


def test_program_change_is_two_bytes_and_supports_running_status() -> None:
    data = bytes([0xC0, 0x0B, 0x0C])
    assert parse_all(data) == [
        ProgramChange(channel=0, program=0x0B),
        ProgramChange(channel=0, program=0x0C),
    ]


def test_parser_never_raises_on_arbitrary_bytes() -> None:
    parser = MidiStreamParser()
    for value in range(256):
        parser.feed(bytes([value]) * 3)


REPRESENTATIVE = bytes(
    [0xB0, 0x63, 0x25, 0x62, 0x17, 0x06, 0x62, 0x26, 0x07]
    + [0xF0, 0x00, 0x00, 0x1A, 0x50, 0x11, 0x01, 0x00, 0x00, 0x11, 0x03, 0x01, 0x09, 0xF7]
    + [0x90, 0x25, 0x7F, 0x90, 0x25, 0x00]
    + [0xC0, 0x0B]
)


def test_boundary_fuzz_every_single_split_point() -> None:
    # split at every possible position; the message list is identical.
    expected = MidiStreamParser().feed(REPRESENTATIVE)
    for cut in range(len(REPRESENTATIVE) + 1):
        parser = MidiStreamParser()
        got = parser.feed(REPRESENTATIVE[:cut]) + parser.feed(REPRESENTATIVE[cut:])
        assert got == expected, f"split at {cut}"


def test_boundary_fuzz_byte_at_a_time() -> None:
    expected = MidiStreamParser().feed(REPRESENTATIVE)
    parser = MidiStreamParser()
    got: list[object] = []
    for byte in REPRESENTATIVE:
        got.extend(parser.feed(bytes([byte])))
    assert got == expected


def test_realtime_injection_at_every_index_is_transparent() -> None:
    # insert FE at every index; non-realtime messages parse identically.
    expected = [m for m in MidiStreamParser().feed(REPRESENTATIVE)]
    for index in range(len(REPRESENTATIVE) + 1):
        injected = REPRESENTATIVE[:index] + bytes([0xFE]) + REPRESENTATIVE[index:]
        got = [
            m for m in MidiStreamParser().feed(injected) if not isinstance(m, SystemRealtime)
        ]
        assert got == expected, f"FE injected at {index}"


from itertools import pairwise

from hypothesis import given, settings
from hypothesis import strategies as st

_messages = st.one_of(
    st.builds(
        ControlChange,
        channel=st.integers(0, 15),
        controller=st.integers(0, 0x7F),
        value=st.integers(0, 0x7F),
    ),
    st.builds(
        NoteOn,
        channel=st.integers(0, 15),
        note=st.integers(0, 0x7F),
        velocity=st.integers(0, 0x7F),
    ),
    st.builds(ProgramChange, channel=st.integers(0, 15), program=st.integers(0, 0x7F)),
    st.builds(SysEx, data=st.binary(min_size=0, max_size=32).map(bytes)
              .filter(lambda b: all(x < 0x80 for x in b))),
)


@settings(max_examples=200)
@given(msgs=st.lists(_messages, max_size=20), cuts=st.lists(st.integers(0, 500), max_size=10))
def test_encode_chunk_parse_is_the_identity(msgs: list[object], cuts: list[int]) -> None:
    # arbitrary well-formed messages -> encode -> chunk -> parse -> identity.
    stream = b"".join(m.to_bytes() for m in msgs)  # type: ignore[attr-defined]
    points = sorted({0, len(stream)} | {min(x, len(stream)) for x in cuts})
    parser = MidiStreamParser()
    got: list[object] = []
    for start, end in pairwise(points):
        got.extend(parser.feed(stream[start:end]))
    assert got == msgs
