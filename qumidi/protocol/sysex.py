"""Qu system-exclusive framing (PRD 9.1, 9.2).

All Qu sysex shares one eight-byte prefix, then a channel byte, then a command
byte and its payload. The channel byte is the wire channel 0-15, or 0x7F for
the all-call form used before the console's channel is known.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from qumidi.midi.messages import SysEx
from qumidi.protocol import constants as c

log = logging.getLogger(__name__)

_PREFIX_BODY = c.QU_SYSEX_PREFIX[1:]  # the prefix without its leading F0


@dataclass(frozen=True)
class QuSysex:
    """A decoded Qu sysex message.

    ``midi_channel`` is the wire channel 0-15, or ``None`` for the all-call form.
    """

    midi_channel: int | None
    command: int
    payload: bytes


def build_sysex(midi_channel: int | None, payload: bytes) -> bytes:
    """Frame ``payload`` (command byte first) for ``midi_channel``.

    ``midi_channel=None`` selects the all-call header (PRD 9.1).
    """
    if midi_channel is not None and not 0 <= midi_channel <= 15:
        raise ValueError(f"midi_channel must be the wire channel 0..15, got {midi_channel!r}")
    if any(b > 0x7F for b in payload):
        raise ValueError("all sysex payload bytes must be 7-bit (PRD 9.2)")
    channel_byte = c.ALL_CALL_CHANNEL_BYTE if midi_channel is None else midi_channel
    return c.QU_SYSEX_PREFIX + bytes([channel_byte]) + payload + bytes([c.SYSEX_END])


def parse_sysex(msg: SysEx) -> QuSysex | None:
    """Decode a Qu sysex, or return None if this is not one.

    Never raises: a foreign or malformed sysex is simply not ours.
    """
    data = msg.data
    if len(data) < len(_PREFIX_BODY) + 2:
        return None
    if data[: len(_PREFIX_BODY)] != _PREFIX_BODY:
        return None
    channel_byte = data[len(_PREFIX_BODY)]
    if channel_byte == c.ALL_CALL_CHANNEL_BYTE:
        midi_channel: int | None = None
    elif 0x00 <= channel_byte <= 0x0F:
        midi_channel = channel_byte
    else:
        # PRD 9.1: reject rather than deriving a nonsense channel.
        log.warning("Qu sysex with invalid channel byte %#04x; ignoring", channel_byte)
        return None
    body = data[len(_PREFIX_BODY) + 1 :]
    return QuSysex(midi_channel=midi_channel, command=body[0], payload=bytes(body[1:]))


def get_system_state(ipad_flag: int = c.DEFAULT_IPAD_FLAG) -> bytes:
    """Build the all-call system-state request (PRD 9.3)."""
    if ipad_flag not in (0, 1):
        raise ValueError(f"ipad_flag must be 0 or 1, got {ipad_flag!r}")
    return build_sysex(None, bytes([c.CMD_GET_SYSTEM_STATE, ipad_flag]))


def parse_system_state_reply(sx: QuSysex) -> tuple[int, tuple[int, int]]:
    """Return ``(box_id, (major, minor))`` from an ``11`` reply (PRD 9.3)."""
    if sx.command != c.CMD_SYSTEM_STATE_REPLY:
        raise ValueError(f"expected command 0x11, got {sx.command:#04x}")
    if len(sx.payload) < 3:
        raise ValueError(
            f"system state reply needs 3 payload bytes, got {len(sx.payload)}: "
            f"{sx.payload.hex(' ')}"
        )
    return sx.payload[0], (sx.payload[1], sx.payload[2])


def meters_enable(midi_channel: int, on: bool) -> bytes:
    """Build the ``12`` meter on/off command (PRD 9.2)."""
    return build_sysex(midi_channel, bytes([c.CMD_METERS, 0x01 if on else 0x00]))
