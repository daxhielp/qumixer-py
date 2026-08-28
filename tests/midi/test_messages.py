import pytest

from qumidi.midi.messages import (
    ControlChange,
    NoteOff,
    NoteOn,
    ProgramChange,
    SysEx,
    SystemRealtime,
)


def test_note_on_encodes() -> None:
    assert NoteOn(channel=0, note=0x25, velocity=0x7F).to_bytes() == bytes([0x90, 0x25, 0x7F])


def test_note_on_uses_the_wire_channel() -> None:
    assert NoteOn(channel=5, note=0x25, velocity=0x7F).to_bytes() == bytes([0x95, 0x25, 0x7F])


def test_note_off_encodes() -> None:
    assert NoteOff(channel=0, note=0x25, velocity=0x00).to_bytes() == bytes([0x80, 0x25, 0x00])


def test_control_change_encodes() -> None:
    assert ControlChange(channel=3, controller=0x63, value=0x25).to_bytes() == bytes(
        [0xB3, 0x63, 0x25]
    )


def test_program_change_encodes_two_bytes() -> None:
    assert ProgramChange(channel=0, program=0x0B).to_bytes() == bytes([0xC0, 0x0B])


def test_sysex_data_excludes_the_framing_bytes() -> None:
    msg = SysEx(data=bytes([0x00, 0x00, 0x1A]))
    assert msg.to_bytes() == bytes([0xF0, 0x00, 0x00, 0x1A, 0xF7])


def test_system_realtime_encodes_one_byte() -> None:
    assert SystemRealtime(status=0xFE).to_bytes() == bytes([0xFE])


def test_messages_are_frozen_and_hashable() -> None:
    msg = ControlChange(channel=0, controller=6, value=0x62)
    assert hash(msg) == hash(ControlChange(channel=0, controller=6, value=0x62))
    with pytest.raises(AttributeError):
        msg.value = 1  # type: ignore[misc]


@pytest.mark.parametrize(
    "factory",
    [
        lambda: NoteOn(channel=16, note=0, velocity=0),
        lambda: NoteOn(channel=0, note=0x80, velocity=0),
        lambda: ControlChange(channel=0, controller=0, value=0x80),
        lambda: SysEx(data=bytes([0x80])),
    ],
)
def test_out_of_range_fields_are_rejected(factory) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        factory()
