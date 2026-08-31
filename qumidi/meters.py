"""Live metering (PRD 12). Tier: EXPERIMENTAL -- the API may change.

Meters are off by default and the context manager exists so a firehose cannot
be left running by accident, including on exception paths.

The layout tables are unverified, so the runtime frame-size check is the
authority. A silently mis-indexed meter reads a plausible dB value from the
wrong channel, which is worse than no meter at all -- a user will trust it.

Decoding happens off the read path: ``feed()`` (called synchronously from the
client's read loop) only ever does a non-blocking queue put. A dedicated pump
task -- started for the lifetime of "at least one enable() is outstanding" --
does the actual 7-to-8 unpacking, the frame-size validation and the delivery
to ``stream()``. This is also what makes the frame-size check fire even when
no caller is currently iterating ``stream()`` at all: the pump runs
regardless of whether anyone is consuming decoded frames.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Iterable
from typing import TYPE_CHECKING, TypeVar

from qumidi.errors import MeterLayoutError, NotSyncedError
from qumidi.protocol import constants as c
from qumidi.protocol.meters import LAYOUTS, MeterFrame, MeterLayout, MeterSlot
from qumidi.protocol.sysex import meters_enable, unpack7to8

if TYPE_CHECKING:  # pragma: no cover
    from qumidi.client_async import AsyncQuMixer

log = logging.getLogger(__name__)

_T = TypeVar("_T")


def _drain(queue: asyncio.Queue[_T]) -> None:
    """Discard everything currently queued, without blocking."""
    while True:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            return


class MeterController:
    """Enable, decode and deliver meter frames."""

    def __init__(self, mixer: AsyncQuMixer, queue_size: int = c.METER_QUEUE_SIZE) -> None:
        self._mixer = mixer
        self._enable_count = 0
        self._layout: MeterLayout | None = None
        self._layout_override: MeterLayout | None = None
        #: Raw (still 7-bit-packed) payloads, as handed in by feed(). Bounded
        #: so a pump that is somehow behind drops OLD bytes rather than the
        #: read loop ever blocking on a full queue.
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=queue_size)
        #: Decoded, validated frames awaiting a stream() consumer. Also
        #: bounded and drop-old -- this is the queue PRD 12.4's "slow
        #: consumers drop old frames" rule is really about.
        self._frames: asyncio.Queue[MeterFrame] = asyncio.Queue(maxsize=queue_size)
        self._pump_task: asyncio.Task[None] | None = None
        self._subscribed: tuple[MeterSlot, ...] | None = None
        self.dropped_frames = 0
        self.malformed_frames = 0

    # -- control ----------------------------------------------------------

    @property
    def enable_count(self) -> int:
        """Nesting depth, not a flag (PRD 12.1)."""
        return self._enable_count

    @property
    def layout(self) -> MeterLayout | None:
        return self._layout_override or self._layout

    def set_layout(self, layout: MeterLayout) -> None:
        """Override the per-model layout.

        These numbers are unverified, so a user with a console we have never
        tested can correct their own installation without waiting for a
        release (PRD 12.3). ``qumidi record`` (Slice 11) captures a raw frame
        for exactly this purpose.
        """
        self._layout_override = layout

    def subscribe(self, slots: Iterable[MeterSlot]) -> None:
        """Record which slots the caller actually cares about.

        Advisory only: ``MeterFrame`` already decodes lazily (PRD 12.3), so
        nothing here skips work today. It exists so a future revision can
        filter what crosses the process boundary (e.g. over a network
        transport) without changing any call site.
        """
        self._subscribed = tuple(slots)

    async def enable(self) -> None:
        if self._mixer.midi_channel is None or self._mixer.model is None:
            raise NotSyncedError(
                "meters cannot be enabled before sync: the model determines the frame "
                "layout. Await wait_synced() first."
            )
        self._enable_count += 1
        if self._enable_count > 1:
            return
        self._layout = LAYOUTS.get(self._mixer.model.name.lower())
        if self._layout is None and self._layout_override is None:
            log.warning("no meter layout for %s; frames will be rejected", self._mixer.model)
        # A pump task from a previous enable/disable cycle (in particular one
        # left running across a dropped connection -- see _reenable_meters())
        # must never be allowed to survive alongside a new one: two pump
        # tasks racing to .get() the same queue would split frames between
        # them at random. Stopping unconditionally, and clearing whatever
        # stale bytes/frames a previous session left behind, makes enable()
        # safe to call from a completely clean state or a half-torn-down one.
        await self._stop_pump()
        _drain(self._queue)
        _drain(self._frames)
        self._pump_task = asyncio.create_task(self._pump(), name="qumidi-meter-pump")
        # PRD 4.5: meter enable/disable bypasses the throttle.
        await self._mixer._outbound.send_priority(
            meters_enable(self._mixer.midi_channel, True)
        )

    async def disable(self) -> None:
        if self._enable_count == 0:
            return
        self._enable_count -= 1
        if self._enable_count > 0:
            return
        await self._stop_pump()
        if self._mixer.midi_channel is not None:
            await self._mixer._outbound.send_priority(
                meters_enable(self._mixer.midi_channel, False)
            )

    @contextlib.asynccontextmanager
    async def enabled(self) -> AsyncIterator[MeterController]:
        """Enable metering for the duration of the block, exception paths included."""
        await self.enable()
        try:
            yield self
        finally:
            await self.disable()

    async def _stop_pump(self) -> None:
        task, self._pump_task = self._pump_task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def shutdown(self) -> None:
        """Called from ``AsyncQuMixer.close()``: stop the pump unconditionally.

        Not a `disable()` -- it does not matter whether the enable count was
        1 or 10, or whether the wire-level "meters off" message can even
        still be sent; the connection is going away either way, and the pump
        task must not outlive it.
        """
        self._enable_count = 0
        await self._stop_pump()

    async def _force_disable(self, reason: str) -> None:
        self._enable_count = 0
        if self._mixer.midi_channel is not None:
            with contextlib.suppress(Exception):
                await self._mixer._outbound.send_priority(
                    meters_enable(self._mixer.midi_channel, False)
                )
        log.error("metering disabled: %s", reason)
        self._mixer.events.emit_meter_layout_error(reason)

    # -- ingest -----------------------------------------------------------

    def feed(self, encoded: bytes) -> None:
        """Called from the read loop. Decodes nothing; queues the payload.

        PRD 12.4: meter frames must not delay control traffic or the
        keepalive, so unpacking and decoding happen off the read path, in
        the pump task started by enable().
        """
        if self._enable_count == 0:
            # Metering was disabled (possibly by a layout error) between the
            # console sending this frame and it arriving here; there is no
            # pump task to drain it, so queuing it would just make the next
            # enable() see stale data. Drop it now instead.
            return
        try:
            self._queue.put_nowait(encoded)
        except asyncio.QueueFull:
            # PRD 12.4: drop OLD frames. A stale meter has no value.
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
                self.dropped_frames += 1
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(encoded)

    def _deliver(self, frame: MeterFrame) -> None:
        try:
            self._frames.put_nowait(frame)
        except asyncio.QueueFull:
            # PRD 12.4: a stream() consumer that fell behind loses the OLD
            # frame, not the new one.
            with contextlib.suppress(asyncio.QueueEmpty):
                self._frames.get_nowait()
                self.dropped_frames += 1
            with contextlib.suppress(asyncio.QueueFull):
                self._frames.put_nowait(frame)

    async def stream(self) -> AsyncIterator[MeterFrame]:
        """Yield validated frames. Never yields a frame of the wrong size."""
        while True:
            yield await self._frames.get()

    async def _pump(self) -> None:
        """Runs for as long as enable_count > 0. Decodes off the read path."""
        while True:
            encoded = await self._queue.get()
            try:
                frame = await self._decode(encoded)
            except Exception:
                # PRD: inbound data must never crash anything, including this
                # background task -- a bug in decoding one frame must not
                # silently end metering for good.
                log.exception("meter frame decode failed; dropping this frame")
                continue
            if frame is None:
                if self._enable_count == 0:
                    # _decode() called _force_disable(): stop pumping. Do not
                    # cancel ourselves -- we ARE the task disable()/
                    # _stop_pump() would otherwise try to cancel-and-await.
                    return
                continue
            self._deliver(frame)

    async def _decode(self, encoded: bytes) -> MeterFrame | None:
        payload = unpack7to8(encoded)
        layout = self.layout
        if layout is None:
            await self._force_disable(
                f"no meter layout is known for {self._mixer.model}; "
                f"observed {len(payload)} payload bytes"
            )
            return None
        if len(payload) % 2:
            # PRD 12.2: an odd length means truncation or a misunderstanding.
            self.malformed_frames += 1
            log.warning(
                "meter frame payload is %d bytes (odd); discarding rather than decoding "
                "a misaligned array",
                len(payload),
            )
            return None
        if len(payload) != layout.payload_bytes:
            await self._force_disable(
                f"meter frame is {len(payload)} payload bytes but the {layout.name} "
                f"layout computes {layout.payload_bytes}. The layout table is wrong for "
                f"this console; capture a frame with `qumidi record` and supply a "
                f"corrected layout via mixer.meters.set_layout()."
            )
            return None
        try:
            return MeterFrame(payload, layout)
        except MeterLayoutError as exc:
            await self._force_disable(str(exc))
            return None
