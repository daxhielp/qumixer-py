"""Incremental MIDI stream parser.

Byte-at-a-time, pure, and it never raises. Malformed input yields fewer
messages plus a log line. Every numbered rule below is a real behaviour of Qu
traffic, not a hypothetical.
"""

from __future__ import annotations

import logging

from qumixer_py.midi.messages import (
    ControlChange,
    MidiMessage,
    NoteOff,
    NoteOn,
    ProgramChange,
    SysEx,
    SystemRealtime,
)
from qumixer_py.protocol import constants as c

log = logging.getLogger(__name__)

#: Number of data bytes each channel-voice status expects.
_DATA_LENGTH = {0x80: 2, 0x90: 2, 0xA0: 2, 0xB0: 2, 0xC0: 1, 0xD0: 1, 0xE0: 2}


class MidiStreamParser:
    """Feed bytes in, get whole messages out. Holds partial state between calls."""

    def __init__(self, max_sysex_bytes: int = c.MAX_SYSEX_BYTES) -> None:
        self._max_sysex_bytes = max_sysex_bytes
        self._running_status: int | None = None
        self._data: list[int] = []
        self._expected: int = 0
        self._in_sysex = False
        self._sysex: bytearray = bytearray()

    def feed(self, data: bytes) -> list[MidiMessage]:
        out: list[MidiMessage] = []
        for byte in data:
            self._feed_byte(byte, out)
        return out

    # -- internals --------------------------------------------------------

    def _feed_byte(self, byte: int, out: list[MidiMessage]) -> None:
        # Rule 3/6: system realtime is extracted from anywhere, including
        # inside a sysex payload and between the data bytes of a message, and
        # it does not clear running status or disturb anything in progress.
        if byte >= 0xF8:
            out.append(SystemRealtime(status=byte))
            return

        if byte >= 0x80:
            self._handle_status(byte, out)
            return

        self._handle_data(byte, out)

    def _handle_status(self, byte: int, out: list[MidiMessage]) -> None:
        if self._in_sysex:
            # Rule 4: any status byte before F7 aborts the sysex.
            if byte == c.SYSEX_END:
                out.append(SysEx(data=bytes(self._sysex)))
                self._end_sysex()
                return
            log.debug("truncated sysex abandoned after %d bytes", len(self._sysex))
            self._end_sysex()
            # fall through and treat this byte as the new status

        if byte == c.SYSEX_START:
            self._in_sysex = True
            self._sysex = bytearray()
            self._data.clear()
            # A sysex is a system common message: it clears running status.
            self._running_status = None
            return

        if byte == c.SYSEX_END:
            # Rule 8: a lone F7 with no open sysex is ignored, not an error.
            return

        if 0xF1 <= byte <= 0xF6:
            # Rule 6: system common clears running status. We do not surface
            # these -- the Qu never sends them and they carry nothing we need.
            self._running_status = None
            self._data.clear()
            return

        status = byte & 0xF0
        if status not in _DATA_LENGTH:
            # Rule 5: unknown status byte, skip gracefully.
            log.debug("skipping unknown status byte %#04x", byte)
            self._running_status = None
            self._data.clear()
            return

        self._running_status = byte
        self._expected = _DATA_LENGTH[status]
        self._data.clear()

    def _handle_data(self, byte: int, out: list[MidiMessage]) -> None:
        if self._in_sysex:
            if len(self._sysex) >= self._max_sysex_bytes:
                # Rule 7: a lost F7 must not grow the buffer without limit.
                log.warning(
                    "sysex exceeded %d bytes without F7; discarding and resynchronising",
                    self._max_sysex_bytes,
                )
                self._end_sysex()
                return
            self._sysex.append(byte)
            return

        if self._running_status is None:
            log.debug("orphan data byte %#04x with no running status", byte)
            return

        self._data.append(byte)
        if len(self._data) < self._expected:
            return

        message = self._build(self._running_status, self._data)
        self._data.clear()  # Rule 1: running status persists; only data resets.
        if message is not None:
            out.append(message)

    def _build(self, status: int, data: list[int]) -> MidiMessage | None:
        channel = status & 0x0F
        kind = status & 0xF0
        if kind == c.NOTE_ON:
            return NoteOn(channel=channel, note=data[0], velocity=data[1])
        if kind == c.NOTE_OFF:
            return NoteOff(channel=channel, note=data[0], velocity=data[1])
        if kind == c.CONTROL_CHANGE:
            return ControlChange(channel=channel, controller=data[0], value=data[1])
        if kind == c.PROGRAM_CHANGE:
            return ProgramChange(channel=channel, program=data[0])
        # Polyphonic aftertouch (A0), channel pressure (D0) and pitch bend (E0)
        # are consumed for framing but not surfaced; the Qu does not use them.
        return None

    def _end_sysex(self) -> None:
        self._in_sysex = False
        self._sysex = bytearray()
