"""MIDI message types (PRD 5.2).

This module knows nothing about mixers. It is generic MIDI.
"""

from __future__ import annotations

from dataclasses import dataclass

from qumidi.protocol import constants as c


def _check(name: str, value: int, hi: int) -> None:
    if not 0 <= value <= hi:
        raise ValueError(f"{name} must be 0..{hi}, got {value!r}")


@dataclass(frozen=True)
class NoteOn:
    channel: int
    note: int
    velocity: int

    def __post_init__(self) -> None:
        _check("channel", self.channel, 15)
        _check("note", self.note, 0x7F)
        _check("velocity", self.velocity, 0x7F)

    def to_bytes(self) -> bytes:
        return bytes([c.NOTE_ON | self.channel, self.note, self.velocity])


@dataclass(frozen=True)
class NoteOff:
    channel: int
    note: int
    velocity: int

    def __post_init__(self) -> None:
        _check("channel", self.channel, 15)
        _check("note", self.note, 0x7F)
        _check("velocity", self.velocity, 0x7F)

    def to_bytes(self) -> bytes:
        return bytes([c.NOTE_OFF | self.channel, self.note, self.velocity])


@dataclass(frozen=True)
class ControlChange:
    channel: int
    controller: int
    value: int

    def __post_init__(self) -> None:
        _check("channel", self.channel, 15)
        _check("controller", self.controller, 0x7F)
        _check("value", self.value, 0x7F)

    def to_bytes(self) -> bytes:
        return bytes([c.CONTROL_CHANGE | self.channel, self.controller, self.value])


@dataclass(frozen=True)
class ProgramChange:
    channel: int
    program: int

    def __post_init__(self) -> None:
        _check("channel", self.channel, 15)
        _check("program", self.program, 0x7F)

    def to_bytes(self) -> bytes:
        return bytes([c.PROGRAM_CHANGE | self.channel, self.program])


@dataclass(frozen=True)
class SysEx:
    """A system-exclusive message. ``data`` excludes the F0/F7 framing bytes."""

    data: bytes

    def __post_init__(self) -> None:
        if any(b > 0x7F for b in self.data):
            raise ValueError("sysex payload bytes must all be 7-bit")

    def to_bytes(self) -> bytes:
        return bytes([c.SYSEX_START]) + self.data + bytes([c.SYSEX_END])


@dataclass(frozen=True)
class SystemRealtime:
    """A single-byte F8-FF message."""

    status: int

    def __post_init__(self) -> None:
        if not 0xF8 <= self.status <= 0xFF:
            raise ValueError(f"system realtime status must be 0xF8..0xFF, got {self.status:#04x}")

    def to_bytes(self) -> bytes:
        return bytes([self.status])


MidiMessage = NoteOn | NoteOff | ControlChange | ProgramChange | SysEx | SystemRealtime
