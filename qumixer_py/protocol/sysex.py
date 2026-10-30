"""Qu system-exclusive framing.

All Qu sysex shares one eight-byte prefix, then a channel byte, then a command
byte and its payload. The channel byte is the wire channel 0-15, or 0x7F for
the all-call form used before the console's channel is known.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from qumixer_py.errors import ParameterRangeError
from qumixer_py.midi.messages import SysEx
from qumixer_py.protocol import constants as c

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

    ``midi_channel=None`` selects the all-call header.
    """
    if midi_channel is not None and not 0 <= midi_channel <= 15:
        raise ValueError(f"midi_channel must be the wire channel 0..15, got {midi_channel!r}")
    if any(b > 0x7F for b in payload):
        raise ValueError("all sysex payload bytes must be 7-bit")
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
        # reject rather than deriving a nonsense channel.
        log.warning("Qu sysex with invalid channel byte %#04x; ignoring", channel_byte)
        return None
    body = data[len(_PREFIX_BODY) + 1 :]
    return QuSysex(midi_channel=midi_channel, command=body[0], payload=bytes(body[1:]))


def get_system_state(ipad_flag: int = c.DEFAULT_IPAD_FLAG) -> bytes:
    """Build the all-call system-state request."""
    if ipad_flag not in (0, 1):
        raise ValueError(f"ipad_flag must be 0 or 1, got {ipad_flag!r}")
    return build_sysex(None, bytes([c.CMD_GET_SYSTEM_STATE, ipad_flag]))


def parse_system_state_reply(sx: QuSysex) -> tuple[int, tuple[int, int]]:
    """Return ``(box_id, (major, minor))`` from an ``11`` reply."""
    if sx.command != c.CMD_SYSTEM_STATE_REPLY:
        raise ValueError(f"expected command 0x11, got {sx.command:#04x}")
    if len(sx.payload) < 3:
        raise ValueError(
            f"system state reply needs 3 payload bytes, got {len(sx.payload)}: "
            f"{sx.payload.hex(' ')}"
        )
    return sx.payload[0], (sx.payload[1], sx.payload[2])


def meters_enable(midi_channel: int, on: bool) -> bytes:
    """Build the ``12`` meter on/off command."""
    return build_sysex(midi_channel, bytes([c.CMD_METERS, 0x01 if on else 0x00]))


def get_name(midi_channel: int, ch: int) -> bytes:
    """Build a ``01`` channel-name request."""
    return build_sysex(midi_channel, bytes([c.CMD_GET_NAME, ch]))


def set_name(midi_channel: int, ch: int, name: str) -> bytes:
    """Build a ``03`` channel-name write, validating length and characters.

    over-long names raise rather than being silently truncated by the
    console -- a truncated name round-trips differently than it was set and
    will oscillate any state-mirroring application. The length limit is
    provisional until measured on hardware.
    """
    if len(name) > c.MAX_NAME_LENGTH:
        raise ParameterRangeError(
            f"channel name {name!r} is {len(name)} characters; the console's strip "
            f"display holds {c.MAX_NAME_LENGTH}"
        )
    for index, char in enumerate(name):
        if not 0x20 <= ord(char) <= 0x7E:
            raise ParameterRangeError(
                f"channel name {name!r} contains a non-printable or non-ASCII character "
                f"{char!r} at position {index}; the protocol carries 7-bit ASCII only"
            )
    return build_sysex(midi_channel, bytes([c.CMD_SET_NAME, ch]) + name.encode("ascii"))


def parse_name_reply(sx: QuSysex) -> tuple[int, str]:
    """Decode a ``02`` name reply, tolerantly."""
    if not sx.payload:
        raise ValueError("name reply has no channel byte")
    ch = sx.payload[0]
    # The console right-pads names with NULs to a fixed 7 bytes (observed on a
    # Qu-32, fw 1.97). That is padding, not corruption: strip it before the
    # name sanitiser sees it.
    raw = sx.payload[1:].rstrip(b"\x00")
    chars = []
    replaced = False
    for byte in raw:
        if 0x20 <= byte <= 0x7E:
            chars.append(chr(byte))
        else:
            chars.append("?")
            replaced = True
    if replaced:
        log.warning(
            "channel %#04x name contained non-printable bytes (%s); replaced with '?'",
            ch, raw.hex(" "),
        )
    return ch, "".join(chars)


def unpack7to8(data: bytes) -> bytes:
    """Expand the Qu's 7-bit packing to 8-bit bytes.

    Every 7 payload bytes were sent as 8 MIDI bytes, with a leading byte
    carrying the high bits:

        0ABCDEFG 0AAAaaaa 0BBBbbbb ... 0GGGgggg

    A is bit 6 of the leading byte and belongs to the FIRST following byte;
    G is bit 0 and belongs to the seventh. A reversed convention produces
    valid-looking output and corrupts every meter above the first.
    """
    out = bytearray()
    for start in range(0, len(data), 8):
        group = data[start : start + 8]
        if not group:
            break
        high = group[0]
        for index, byte in enumerate(group[1:]):
            bit = (high >> (6 - index)) & 1
            out.append(byte | (bit << 7))
    return bytes(out)


def pack8to7(data: bytes) -> bytes:
    """Inverse of unpack7to8. The final group's high-bit byte is left-aligned."""
    out = bytearray()
    for start in range(0, len(data), 7):
        group = data[start : start + 7]
        high = 0
        for index, byte in enumerate(group):
            if byte & 0x80:
                high |= 1 << (6 - index)
        out.append(high)
        out.extend(byte & 0x7F for byte in group)
    return bytes(out)


def decode_meter_db(raw_u16: int) -> float:
    """Decode one 7Q8 meter value.

    Unbounded above by the encoding -- (0xFFFF - 0x8000)/256 is about +128 dB.
    Deliberately not clamped: an out-of-range reading is diagnostic
    information about a layout error, and hiding it is worse than showing it.
    """
    return (raw_u16 - 0x8000) / 256.0
