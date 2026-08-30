"""TCP MIDI transport for Qu mixers (PRD 4.2, 4.3)."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import AsyncIterator, Awaitable, Callable

from qumidi.errors import QuConnectionError
from qumidi.midi.messages import MidiMessage, SystemRealtime
from qumidi.midi.parser import MidiStreamParser
from qumidi.protocol import constants as c
from qumidi.transport.base import ConnectionState, Transport

log = logging.getLogger(__name__)
wire = logging.getLogger("qumidi.wire")

_SINGLE_CONNECTION_HINT = (
    "The Qu permits exactly one TCP MIDI connection at a time. If Qu-Pad, "
    "Qu-Control or another client holds the session, close it and retry."
)


class TcpTransport(Transport):
    """One TCP session to a Qu, with keepalive and inbound-liveness accounting."""

    def __init__(
        self,
        host: str,
        port: int = c.QU_TCP_PORT,
        *,
        connect_timeout: float = c.CONNECT_TIMEOUT,
        keepalive_interval: float = c.KEEPALIVE_INTERVAL,
        inbound_timeout: float = c.INBOUND_TIMEOUT,
        forward_active_sense: bool = False,
        auto_reconnect: bool = True,
        max_reconnect_attempts: int | None = None,
        reconnect_backoff_min: float = c.RECONNECT_BACKOFF_MIN,
        reconnect_backoff_max: float = c.RECONNECT_BACKOFF_MAX,
    ) -> None:
        super().__init__()
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self.keepalive_interval = keepalive_interval
        self.inbound_timeout = inbound_timeout
        self.forward_active_sense = forward_active_sense
        self.auto_reconnect = auto_reconnect
        self.max_reconnect_attempts = max_reconnect_attempts
        self.reconnect_backoff_min = reconnect_backoff_min
        self.reconnect_backoff_max = reconnect_backoff_max

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._parser = MidiStreamParser()
        self._inbox: asyncio.Queue[MidiMessage] = asyncio.Queue(maxsize=4096)
        self._tasks: list[asyncio.Task[None]] = []
        self._last_inbound = 0.0
        #: PRD 4.4/16.16: set before anything is cancelled, so a reconnect
        #: attempt in flight (or about to be scheduled) can never resurrect a
        #: connection the caller explicitly asked to close. Read only via
        #: _should_stop_reconnecting() below -- see that method for why.
        self._closing = False
        self._reconnect_task: asyncio.Task[None] | None = None
        self._on_reconnected: Callable[[], Awaitable[None]] | None = None

    # -- lifecycle --------------------------------------------------------

    def set_reconnect_hook(self, hook: Callable[[], Awaitable[None]]) -> None:
        """Called after each successful reconnect, before READY."""
        self._on_reconnected = hook

    async def connect(self) -> None:
        self._closing = False
        await self._open()

    async def _open(self) -> None:
        """The actual socket setup, shared by the first connect and every
        reconnect attempt.
        """
        self.set_state(ConnectionState.CONNECTING)
        if self._tasks:
            # A reconnect reuses this same instance. The previous
            # connection's loop tasks self-terminate by polling
            # is_connected, which only becomes false once self._writer is
            # replaced or closed -- but that polling is on a timer
            # (keepalive_interval / inbound_timeout/4), so a task from the
            # OLD connection can still be sleeping, unaware the link is
            # down, at the moment a NEW connection succeeds. Left alone, it
            # would then observe the NEW self._writer as healthy and keep
            # running alongside the new loop's own task -- two keepalive
            # loops on one socket, and a leaked task close() never learns
            # about because self._tasks is about to be overwritten below.
            # Cancelling and awaiting them here, before opening the new
            # socket, closes that window regardless of timing.
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks = []
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=self.connect_timeout
            )
        except (OSError, asyncio.TimeoutError) as exc:
            self.set_state(ConnectionState.DISCONNECTED)
            raise QuConnectionError(
                f"could not connect to {self.host}:{self.port} -- {exc}. {_SINGLE_CONNECTION_HINT}"
            ) from exc

        self._last_inbound = asyncio.get_running_loop().time()
        self._parser = MidiStreamParser()
        self._tasks = [
            asyncio.create_task(self._read_loop(), name="qumidi-read"),
            asyncio.create_task(self._keepalive_loop(), name="qumidi-keepalive"),
            asyncio.create_task(self._liveness_loop(), name="qumidi-liveness"),
        ]
        self.set_state(ConnectionState.CONNECTED)

    async def close(self) -> None:
        """Idempotent. Disables reconnect, cancels every task, closes the socket.

        PRD 4.4/16.16: ``_closing`` is set *before* anything is cancelled or
        awaited, so a concurrently-running reconnect attempt sees it and
        bails out (in ``_reconnect_loop``'s own loop condition, and again
        just after its backoff sleep) rather than resurrecting the
        connection this call is meant to end for good.
        """
        self._closing = True
        tasks = list(self._tasks)
        if self._reconnect_task is not None:
            tasks.append(self._reconnect_task)
        for task in tasks:
            task.cancel()
        if tasks:
            # return_exceptions=True: a cancelled or otherwise-failed loop task
            # must never propagate out of close().
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks = []
        self._reconnect_task = None
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except OSError:
                pass
        self._reader = None
        self._writer = None
        self.set_state(ConnectionState.DISCONNECTED)

    @property
    def is_connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    # -- io ---------------------------------------------------------------

    async def send(self, data: bytes) -> None:
        if self._writer is None or self._writer.is_closing():
            raise QuConnectionError(f"not connected to {self.host}:{self.port}")
        if wire.isEnabledFor(logging.DEBUG):
            wire.debug("TX %s", data.hex(" "))
        self._writer.write(data)
        await self._writer.drain()

    async def messages(self) -> AsyncIterator[MidiMessage]:
        while True:
            yield await self._inbox.get()

    # -- loops ------------------------------------------------------------

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                data = await self._reader.read(4096)
                if not data:
                    break
                self._last_inbound = asyncio.get_running_loop().time()
                if wire.isEnabledFor(logging.DEBUG):
                    wire.debug("RX %s", data.hex(" "))
                for msg in self._parser.feed(data):
                    if isinstance(msg, SystemRealtime) and not self.forward_active_sense:
                        # PRD 4.3: FE is consumed for liveness accounting and
                        # not forwarded to upper layers by default.
                        continue
                    try:
                        self._inbox.put_nowait(msg)
                    except asyncio.QueueFull:
                        log.warning("inbound queue full; dropping %r", msg)
        except asyncio.CancelledError:
            # close() cancels this task itself and reports DISCONNECTED; do
            # not also report it here.
            raise
        except (OSError, asyncio.IncompleteReadError) as exc:
            log.info("read loop ended: %s", exc)
        except Exception:
            # Any other failure mode still means the link is dead and the
            # caller must be told -- never let an unanticipated exception
            # type skip the state notification.
            log.exception("read loop ended unexpectedly")
        self._on_link_down()

    async def _keepalive_loop(self) -> None:
        # PRD 4.3: send FE unconditionally. Safe under both keepalive regimes
        # and satisfies the 5s iPadFlag=1 deadline.
        while True:
            await asyncio.sleep(self.keepalive_interval)
            if not self.is_connected:
                return
            try:
                assert self._writer is not None
                self._writer.write(bytes([c.ACTIVE_SENSING]))
                await self._writer.drain()
            except OSError as exc:
                log.info("keepalive write failed: %s", exc)
                return

    async def _liveness_loop(self) -> None:
        # PRD 4.3: declare the link dead ourselves rather than waiting for the
        # socket to notice.
        while True:
            await asyncio.sleep(self.inbound_timeout / 4)
            if not self.is_connected:
                return
            loop = asyncio.get_running_loop()
            if loop.time() - self._last_inbound > self.inbound_timeout:
                log.warning(
                    "no inbound bytes for %.1fs; declaring the link dead", self.inbound_timeout
                )
                self._on_link_down()
                return

    def _on_link_down(self) -> None:
        if self._writer is not None:
            self._writer.close()
        self.set_state(ConnectionState.DISCONNECTED)
        self._schedule_reconnect()

    # -- reconnect (PRD 4.4) ------------------------------------------------

    def _backoff_delay(self, attempt: int) -> float:
        """PRD 4.4: 0.5s -> 30s cap, x2 per attempt, +-20% jitter."""
        base = min(
            self.reconnect_backoff_min * (c.RECONNECT_BACKOFF_FACTOR**attempt),
            self.reconnect_backoff_max,
        )
        jitter = base * c.RECONNECT_JITTER
        return base + random.uniform(-jitter, jitter)

    def _schedule_reconnect(self) -> None:
        if self._closing or not self.auto_reconnect:
            return
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return
        self.set_state(ConnectionState.RECONNECTING)
        self._reconnect_task = asyncio.create_task(
            self._reconnect_loop(), name="qumidi-reconnect"
        )

    def _should_stop_reconnecting(self) -> bool:
        """A plain ``self._closing`` read, behind a call.

        close() flips ``_closing`` from a *different* task while this loop is
        asleep in its backoff ``await asyncio.sleep(delay)``. mypy narrows a
        directly-read ``self._closing`` to its earlier-observed value across
        that await (it has no model of concurrent mutation) and reports the
        post-sleep re-check as unreachable dead code -- a false positive that
        would hide the one check PRD 4.4/16.16 actually depends on. Routing
        the read through a call defeats that narrowing.
        """
        return self._closing

    async def _reconnect_loop(self) -> None:
        attempt = 0
        while not self._should_stop_reconnecting():
            if (
                self.max_reconnect_attempts is not None
                and attempt >= self.max_reconnect_attempts
            ):
                log.error(
                    "giving up after %d reconnect attempts to %s:%d",
                    attempt,
                    self.host,
                    self.port,
                )
                self.set_state(ConnectionState.DISCONNECTED)
                return
            delay = self._backoff_delay(attempt)
            log.info(
                "reconnecting to %s:%d in %.1fs (attempt %d)",
                self.host,
                self.port,
                delay,
                attempt + 1,
            )
            await asyncio.sleep(delay)
            if self._should_stop_reconnecting():
                return
            try:
                await self._open()
            except QuConnectionError as exc:
                log.debug("reconnect attempt %d failed: %s", attempt + 1, exc)
                attempt += 1
                continue
            if self._on_reconnected is not None:
                try:
                    await self._on_reconnected()
                except Exception:
                    log.exception("post-reconnect sync failed; will retry")
                    self._on_link_down()
                    attempt += 1
                    continue
            return
