"""NRPN assembly and disassembly.

The Qu's NRPN usage is idiosyncratic:

    BN 63 CH    NRPN MSB   = channel id
    BN 62 ID    NRPN LSB   = parameter id
    BN 06 VA    Data MSB   = parameter value
    BN 26 VX    Data LSB   = index / discriminator  <- NOT a fine value

VX is not a value refinement. It is either a filler constant, or a genuine
index (send destination, GEQ band, dSNAKE socket, FX tap). Which it is, is
registry data -- this module stays generic and just carries the byte through.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from qumixer_py.midi.messages import ControlChange, MidiMessage, SystemRealtime
from qumixer_py.protocol import constants as c

log = logging.getLogger(__name__)


def encode_nrpn(midi_channel: int, ch: int, param_id: int, value: int, vx: int) -> bytes:
    """Build the four CCs, always in the documented order, on channel N."""
    for name, val, hi in (
        ("midi_channel", midi_channel, 15),
        ("ch", ch, 0x7F),
        ("param_id", param_id, 0x7F),
        ("value", value, 0x7F),
        ("vx", vx, 0x7F),
    ):
        if not 0 <= val <= hi:
            raise ValueError(f"{name} must be 0..{hi}, got {val!r}")
    status = c.CONTROL_CHANGE | midi_channel
    return bytes(
        [
            status, c.CC_NRPN_MSB, ch,
            status, c.CC_NRPN_LSB, param_id,
            status, c.CC_DATA_MSB, value,
            status, c.CC_DATA_LSB, vx,
        ]
    )


@dataclass(frozen=True)
class RawParamUpdate:
    """One decoded NRPN parameter write, before registry resolution."""

    midi_channel: int
    ch: int
    param_id: int
    value: int
    vx: int | None


@dataclass
class _Accumulator:
    ch: int | None = None
    param_id: int | None = None


class NrpnDecoder:
    """Reassembles NRPN quads from a message stream, per MIDI channel."""

    def __init__(
        self,
        pending_flush_timeout: float = c.PENDING_FLUSH_TIMEOUT,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.pending_flush_timeout = pending_flush_timeout
        self._now = now
        self._acc: dict[int, _Accumulator] = {}
        self._held: RawParamUpdate | None = None
        self._held_at = 0.0

    def feed(self, msg: MidiMessage) -> list[RawParamUpdate]:
        # realtime bytes must not flush or disturb the held update --
        # they arrive constantly and would break every NRPN pair.
        if isinstance(msg, SystemRealtime):
            return []
        if not isinstance(msg, ControlChange):
            # Flush trigger (b): any non-CC message. A sysex 14 end-of-sync
            # marker reaches us here.
            return self._release()

        acc = self._acc.setdefault(msg.channel, _Accumulator())
        if msg.controller == c.CC_NRPN_MSB:
            out = self._release()
            acc.ch = msg.value
            acc.param_id = None
            return out
        if msg.controller == c.CC_NRPN_LSB:
            out = self._release()
            acc.param_id = msg.value
            return out
        if msg.controller == c.CC_DATA_MSB:
            out = self._release()
            if acc.ch is None or acc.param_id is None:
                log.debug("Data MSB with no NRPN address on channel %d; ignoring", msg.channel)
                return out
            # emit on 06, not on 26, and hold for at most one
            # subsequent CC in case a 26 follows.
            self._held = RawParamUpdate(msg.channel, acc.ch, acc.param_id, msg.value, None)
            self._held_at = self._now()
            return out
        if msg.controller == c.CC_DATA_LSB:
            if self._held is not None and self._held.midi_channel == msg.channel:
                update = RawParamUpdate(
                    self._held.midi_channel,
                    self._held.ch,
                    self._held.param_id,
                    self._held.value,
                    msg.value,
                )
                self._held = None
                return [update]
            return []
        # Any other CC (bank select during a scene recall, for instance) is
        # not part of the NRPN sequence and must not disturb the accumulator,
        # but it does end the hold window.
        return self._release()

    def tick(self, now: float | None = None) -> list[RawParamUpdate]:
        """Flush trigger (c): pending_flush_timeout of inbound silence."""
        if self._held is None:
            return []
        current = self._now() if now is None else now
        if current - self._held_at < self.pending_flush_timeout:
            return []
        return self._release()

    def flush(self) -> list[RawParamUpdate]:
        """Force-release the held update. Called on end-of-sync and shutdown."""
        return self._release()

    def reset(self) -> None:
        """Forget all state. Called on disconnect."""
        self._acc.clear()
        self._held = None

    def _release(self) -> list[RawParamUpdate]:
        if self._held is None:
            return []
        held, self._held = self._held, None
        return [held]
