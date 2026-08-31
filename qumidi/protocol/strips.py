"""MIDI fader strips, the DAW layer (PRD 7.2, 6.3, 14.7, 14.11).

Tier: EXPERIMENTAL, exposed as a raw sub-API only. No HUI/Mackie translation.

Strips are a SEPARATE NAMESPACE from channels. They are numbered by their own
identity (MIDI strip 5 sends 0x04 regardless of where it is placed on the
custom layer), they live on MIDI channel N+1, and moving one emits no channel
NRPN at all. StripId is deliberately not assignable to a ChannelId, because
strip key numbers (0x00-0x5F) DO overlap input channel ids (0x20-0x3F).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from qumidi.errors import ParameterRangeError
from qumidi.midi.messages import ControlChange, MidiMessage, NoteOff, NoteOn

if TYPE_CHECKING:
    from collections.abc import Callable

    from qumidi.client_async import AsyncQuMixer

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class StripId:
    """A MIDI strip identity. Distinct from ChannelId, and not interchangeable."""

    value: int
    index: int
    label: str


def Strip(n: int) -> StripId:
    """MIDI strip 1-32, 1-based to match the console's own numbering."""
    if not 1 <= n <= 32:
        raise ParameterRangeError(f"MIDI strip must be 1..32, got {n}")
    return StripId(value=n - 1, index=n, label=f"MIDI strip {n}")


class StripKey(Enum):
    """Strip key blocks (PRD 7.2). Note numbers within each block are 0x00-0x1F."""

    MUTE = 0x00
    SEL = 0x20
    PAFL = 0x40

    @property
    def base(self) -> int:
        return int(self.value)

    def note(self, strip: StripId) -> int:
        return self.base + strip.value


BANK_UP_NOTE = 0x7E
BANK_DOWN_NOTE = 0x7F


class StripApi:
    """Raw access to the DAW-layer MIDI strips. Experimental."""

    def __init__(self, mixer: AsyncQuMixer, midi_channel: int) -> None:
        self._mixer = mixer
        # PRD 7.2: every derived channel is computed once at sync time and
        # validated, not recomputed at each call site.
        self._channel = midi_channel
        self._callbacks: list[Callable[[object], None]] = []

    async def set_fader(self, strip: StripId, raw: int) -> None:
        await self._mixer._send(
            ControlChange(channel=self._channel, controller=strip.value, value=raw).to_bytes()
        )

    async def press(self, strip: StripId, key: StripKey) -> None:
        note = key.note(strip)
        await self._mixer._send(
            NoteOn(channel=self._channel, note=note, velocity=0x7F).to_bytes()
            + NoteOff(channel=self._channel, note=note, velocity=0x00).to_bytes()
        )

    async def bank(self, up: bool) -> None:
        note = BANK_UP_NOTE if up else BANK_DOWN_NOTE
        await self._mixer._send(
            NoteOn(channel=self._channel, note=note, velocity=0x7F).to_bytes()
        )

    def on_strip_event(self, cb: Callable[[object], None]) -> None:
        """Subscribe to raw inbound strip messages on channel N+1."""
        self._callbacks.append(cb)

    def _dispatch(self, msg: MidiMessage) -> None:
        for cb in list(self._callbacks):
            try:
                cb(msg)
            except Exception:
                log.exception("strip callback raised; continuing")
