"""Ordered, rate-limited outbound queue (PRD 4.5).

MIDI-over-TCP has no flow control back to the console's MIDI parser. Emitting
a 128-step fade in one event-loop tick makes the mixer drop what it cannot
parse -- silently, exactly like a wrong VX byte. Everything outbound except
the keepalive and the meter enable/disable passes through here, in issue order.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from qumidi.protocol import constants as c

log = logging.getLogger(__name__)

Sink = Callable[[bytes], Awaitable[None]]

_Item = tuple[bytes, "asyncio.Future[None]"]


class OutboundQueue:
    """A single ordered send queue with a byte-rate throttle."""

    def __init__(
        self,
        sink: Sink,
        *,
        max_send_rate: int | None = c.MAX_SEND_RATE,
        maxsize: int = c.OUTBOUND_QUEUE_MAXSIZE,
    ) -> None:
        self._sink = sink
        self.max_send_rate = max_send_rate
        self._queue: asyncio.Queue[_Item] = asyncio.Queue(maxsize=maxsize)
        self._worker: asyncio.Task[None] | None = None
        #: Serialises priority writes against the worker's own sink call, so a
        #: keepalive can never land inside another payload's bytes.
        self._sink_lock = asyncio.Lock()
        self._next_send_at = 0.0

    async def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._run(), name="qumidi-outbound")

    async def stop(self) -> None:
        if self._worker is not None:
            worker, self._worker = self._worker, None
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
        self.clear()

    @property
    def pending(self) -> int:
        """Payloads queued but not yet handed to the socket."""
        return self._queue.qsize()

    async def send(self, data: bytes) -> None:
        """Queue ``data`` and return once it has been written and drained."""
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        await self._queue.put((data, future))
        await future

    async def send_priority(self, data: bytes) -> None:
        """Write immediately, bypassing both the queue and the throttle.

        Reserved for the keepalive FE and meter enable/disable: liveness must
        never be starved by a bulk write (PRD 4.5).
        """
        async with self._sink_lock:
            await self._sink(data)

    def clear(self) -> None:
        """Drop the backlog, failing every waiter (PRD 4.4, on disconnect)."""
        dropped = 0
        while True:
            try:
                _, future = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if not future.done():
                future.cancel()
            dropped += 1
        if dropped:
            log.info("dropped %d queued outbound payloads", dropped)

    async def _run(self) -> None:
        while True:
            data, future = await self._queue.get()
            try:
                await self._pace(len(data))
                async with self._sink_lock:
                    await self._sink(data)
            except asyncio.CancelledError:
                if not future.done():
                    future.cancel()
                raise
            except Exception as exc:  # noqa: BLE001 - reported to the waiter, not raised
                if not future.done():
                    future.set_exception(exc)
                continue
            if not future.done():
                future.set_result(None)

    async def _pace(self, nbytes: int) -> None:
        if self.max_send_rate is None:
            return
        loop = asyncio.get_running_loop()
        now = loop.time()
        if self._next_send_at > now:
            await asyncio.sleep(self._next_send_at - now)
            now = loop.time()
        self._next_send_at = now + nbytes / self.max_send_rate
