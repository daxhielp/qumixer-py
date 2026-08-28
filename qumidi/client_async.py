"""The async client (PRD 9.3, 11.1).

Slice 1 scope: connect, run the state-sync handshake, learn BoxID, firmware
and the console's MIDI channel. Later slices add the cache, events, the
registry-driven set/get, names, scenes, reconnect and meters.
"""

from __future__ import annotations

import asyncio
import logging
from types import TracebackType

from qumidi.errors import QuConnectionError, SyncTimeoutError
from qumidi.midi.messages import MidiMessage, SysEx
from qumidi.protocol import constants as c
from qumidi.protocol.sysex import QuSysex, get_system_state, parse_sysex, parse_system_state_reply
from qumidi.transport.base import ConnectionState, Transport
from qumidi.transport.tcp import TcpTransport

log = logging.getLogger(__name__)

_NOT_A_QU_HINT = (
    "no system-state reply from {host}:{port} within {timeout}s. Either the "
    "host is not a Qu mixer, or another client holds the session -- the Qu "
    "permits exactly one TCP MIDI connection at a time."
)


class AsyncQuMixer:
    """Async-first control of one Qu console."""

    def __init__(
        self,
        host: str,
        port: int = c.QU_TCP_PORT,
        *,
        ipad_flag: int = c.DEFAULT_IPAD_FLAG,
        reply_timeout: float = c.REPLY_TIMEOUT,
        sync_timeout: float = c.SYNC_TIMEOUT,
        transport: Transport | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.ipad_flag = ipad_flag
        self.reply_timeout = reply_timeout
        self.sync_timeout = sync_timeout

        self._transport = transport if transport is not None else TcpTransport(host, port)
        self._reader_task: asyncio.Task[None] | None = None
        self._reply_event = asyncio.Event()
        self._sync_event = asyncio.Event()

        self._box_id: int | None = None
        self._firmware: tuple[int, int] | None = None
        self._midi_channel: int | None = None

    # -- properties -------------------------------------------------------

    @property
    def box_id(self) -> int | None:
        return self._box_id

    @property
    def firmware(self) -> tuple[int, int] | None:
        return self._firmware

    @property
    def midi_channel(self) -> int | None:
        """The console's MIDI channel as a wire value, 0-15."""
        return self._midi_channel

    @property
    def midi_channel_display(self) -> int | None:
        """The console's MIDI channel as the console displays it, 1-16."""
        return None if self._midi_channel is None else self._midi_channel + 1

    @property
    def state(self) -> ConnectionState:
        return self._transport.state

    # -- lifecycle --------------------------------------------------------

    async def connect(self) -> None:
        await self._transport.connect()
        self._reader_task = asyncio.create_task(self._read_loop(), name="qumidi-client-read")
        await self._sync()

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass  # expected: we just cancelled it ourselves
            except Exception:
                log.exception("client read loop raised while closing")
            self._reader_task = None
        await self._transport.close()

    async def __aenter__(self) -> AsyncQuMixer:  # noqa: PYI034 -- typing.Self needs py311+
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def wait_synced(self, timeout: float | None = None) -> None:
        """Block until the state push has completed (PRD 11.1)."""
        await asyncio.wait_for(self._sync_event.wait(), timeout=timeout)

    # -- sync -------------------------------------------------------------

    async def _sync(self) -> None:
        self._reply_event.clear()
        self._sync_event.clear()
        self._transport.set_state(ConnectionState.SYNCING)
        # PRD 9.1: the all-call form is required here, because the mixer's
        # MIDI channel is unknown until it replies.
        await self._transport.send(get_system_state(self.ipad_flag))
        try:
            await asyncio.wait_for(self._reply_event.wait(), timeout=self.reply_timeout)
        except asyncio.TimeoutError as exc:
            raise QuConnectionError(
                _NOT_A_QU_HINT.format(host=self.host, port=self.port, timeout=self.reply_timeout)
            ) from exc
        try:
            await asyncio.wait_for(self._sync_event.wait(), timeout=self.sync_timeout)
        except asyncio.TimeoutError as exc:
            raise SyncTimeoutError(
                f"{self.host}:{self.port} replied to the state request but sent no end-of-sync "
                f"marker within {self.sync_timeout}s; the state push failed"
            ) from exc
        self._transport.set_state(ConnectionState.READY)

    # -- inbound ----------------------------------------------------------

    async def _read_loop(self) -> None:
        async for msg in self._transport.messages():
            try:
                self._dispatch(msg)
            except Exception:
                # PRD: the read loop must never die on one bad message.
                log.exception("error handling inbound message %r", msg)

    def _dispatch(self, msg: MidiMessage) -> None:
        if isinstance(msg, SysEx):
            self._on_sysex(msg)

    def _on_sysex(self, msg: SysEx) -> None:
        parsed = parse_sysex(msg)
        if parsed is None:
            return
        if parsed.command == c.CMD_SYSTEM_STATE_REPLY:
            self._on_state_reply(parsed)
        elif parsed.command == c.CMD_END_OF_SYNC:
            self._sync_event.set()

    def _on_state_reply(self, parsed: QuSysex) -> None:
        if self._reply_event.is_set():
            # PRD 9.3.10: ignore a second 11 reply; bind to the first.
            log.warning(
                "second system-state reply (BoxID %s) after binding to BoxID %s; ignoring",
                parsed.payload[0] if parsed.payload else "?",
                self._box_id,
            )
            return
        try:
            box_id, firmware = parse_system_state_reply(parsed)
        except ValueError as exc:
            log.warning("malformed system-state reply: %s", exc)
            return
        if parsed.midi_channel is None:
            log.warning("system-state reply carried the all-call channel byte; ignoring")
            return
        self._box_id = box_id
        self._firmware = firmware
        self._midi_channel = parsed.midi_channel
        # PRD 9.1: the 0-based -> 1-based conversion happens in exactly one
        # place (midi_channel_display); log messages reuse it rather than
        # recomputing +1 themselves.
        log.info(
            "connected to BoxID %d firmware %d.%d on MIDI channel %d",
            box_id,
            firmware[0],
            firmware[1],
            self.midi_channel_display,
        )
        self._reply_event.set()
