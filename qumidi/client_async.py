"""The async client (PRD 9.3, 11.1).

Slice 1 scope: connect, run the state-sync handshake, learn BoxID, firmware
and the console's MIDI channel. Later slices add the cache, events, the
registry-driven set/get, names, scenes, reconnect and meters.
"""

from __future__ import annotations

import asyncio
import logging
from types import TracebackType

from qumidi.errors import (
    NotSyncedError,
    ParameterRangeError,
    QuConnectionError,
    ReadOnlyParameterError,
    SyncTimeoutError,
)
from qumidi.midi.messages import MidiMessage, NoteOn, SysEx
from qumidi.midi.nrpn import encode_nrpn
from qumidi.protocol import constants as c
from qumidi.protocol.channels import ChannelId, Model, ModelCaps, caps_for_box_id, check_channel
from qumidi.protocol.params import Direction, Interpolated, ParamSpec, SendTarget
from qumidi.protocol.registry import REGISTRY, send_target_vx
from qumidi.protocol.sysex import QuSysex, get_system_state, parse_sysex, parse_system_state_reply
from qumidi.transport.base import ConnectionState, Transport
from qumidi.transport.queue import OutboundQueue
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
        max_send_rate: int | None = c.MAX_SEND_RATE,
    ) -> None:
        self.host = host
        self.port = port
        self.ipad_flag = ipad_flag
        self.reply_timeout = reply_timeout
        self.sync_timeout = sync_timeout

        self._transport = transport if transport is not None else TcpTransport(host, port)
        self._outbound = OutboundQueue(self._transport.send, max_send_rate=max_send_rate)
        self._reader_task: asyncio.Task[None] | None = None
        self._reply_event = asyncio.Event()
        self._sync_event = asyncio.Event()

        self._box_id: int | None = None
        self._firmware: tuple[int, int] | None = None
        self._midi_channel: int | None = None
        self._caps: ModelCaps | None = None

    # -- properties -------------------------------------------------------

    @property
    def box_id(self) -> int | None:
        return self._box_id

    @property
    def firmware(self) -> tuple[int, int] | None:
        return self._firmware

    @property
    def raw_box_id(self) -> int | None:
        """The BoxID byte exactly as reported, even when unrecognised."""
        return self._box_id

    @property
    def model(self) -> Model | None:
        """The detected model, or None for an unrecognised BoxID."""
        return None if self._box_id is None else Model.from_box_id(self._box_id)

    @property
    def caps(self) -> ModelCaps | None:
        """Capability counts for the detected model; None before sync."""
        return self._caps

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
        await self._outbound.start()
        try:
            await self._sync()
        except BaseException:
            # PRD: the Qu permits exactly one TCP MIDI connection at a time.
            # A sync failure (no reply / no end marker) must not leak this
            # connection -- __aenter__ raising skips __aexit__ entirely under
            # `async with`, so connect() must clean up after itself rather
            # than relying on the caller to also call close().
            await self.close()
            raise

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
        await self._outbound.stop()
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

    # -- writes -------------------------------------------------------------

    @property
    def pending_writes(self) -> int:
        """Payloads queued for the console but not yet on the wire (PRD 4.5)."""
        return self._outbound.pending

    def _require_synced(self, what: str) -> int:
        """Return the console's MIDI channel, or explain why we cannot write."""
        if self._midi_channel is None:
            raise NotSyncedError(
                f"cannot {what} before sync: the console's MIDI channel is not yet "
                f"known. Await connect()/wait_synced() first."
            )
        return self._midi_channel

    async def _await_channel(self, what: str) -> int:
        """Wait for the ``11`` reply if a connect is in flight, then return N.

        PRD 11.1 says writes before sync are queued and flushed once the reply
        establishes N; PRD 11.3 says NotSyncedError is raised for a write
        needing N before sync. Those reconcile as: if a connect is running,
        wait for it (the write queues); if no connect was ever started, raise,
        because there is nothing to wait for.

        A write issued this way is transmitted *after* the state burst (the
        write only enters the outbound queue once N is known, and the state
        burst is already in flight or complete by then), so it will appear to
        "win" over the console's own state.
        """
        if self._midi_channel is not None:
            return self._midi_channel
        if self._reader_task is None:
            raise NotSyncedError(f"cannot {what}: not connected. Await connect() first.")
        try:
            await asyncio.wait_for(self._reply_event.wait(), timeout=self.reply_timeout)
        except asyncio.TimeoutError as exc:
            raise NotSyncedError(
                f"cannot {what}: no system-state reply from {self.host}:{self.port} within "
                f"{self.reply_timeout}s while a connect was in progress"
            ) from exc
        return self._require_synced(what)

    async def _send(self, data: bytes) -> None:
        """The single funnel for every control write (PRD 4.5)."""
        await self._outbound.send(data)

    async def mute(self, ch: ChannelId, muted: bool) -> None:
        """Mute or unmute a channel (PRD 7.2).

        Mute is Note On/Off, not NRPN. DCA masters (0x10-0x13) and mute-group
        masters (0x50-0x53) are muted exactly the same way -- firing a mute
        group has no dedicated message.
        """
        n = await self._await_channel(f"mute {ch.label}")
        check_channel(self._caps, ch)
        velocity = 0x7F if muted else 0x3F
        payload = (
            NoteOn(channel=n, note=ch.value, velocity=velocity).to_bytes()
            + NoteOn(channel=n, note=ch.value, velocity=0x00).to_bytes()
        )
        await self._send(payload)

    def encode(
        self,
        ch: ChannelId,
        param: str,
        raw: int,
        vx: ChannelId | int | None = None,
    ) -> bytes:
        """Build the wire bytes for one parameter write, without sending.

        Public so that the golden-vector tests and the CLI can exercise the
        encoder without a socket.
        """
        n = self._require_synced(f"encode {param} for {ch.label}")
        spec = REGISTRY.lookup(param, ch.cls)
        if spec.direction is Direction.RX_ONLY:
            raise ReadOnlyParameterError(
                f"{ch.label} {spec.name} is receive-only ({spec.notes or 'PRD 7.1'}); "
                f"writing it would emit bytes the mixer ignores"
            )
        check_channel(self._caps, ch)
        vx_byte = self._resolve_vx(spec, vx)
        value = spec.domain.validate_raw(raw, spec.name, ch.label)
        return encode_nrpn(n, ch=ch.value, param_id=spec.nrpn_id, value=value, vx=vx_byte)

    def _resolve_vx(self, spec: ParamSpec, vx: ChannelId | int | None) -> int:
        if isinstance(spec.vx, SendTarget):
            if isinstance(vx, ChannelId):
                target_vx = send_target_vx(vx, self._caps)
            elif isinstance(vx, int):
                target_vx = vx
            else:
                raise ParameterRangeError(
                    f"{spec.name} needs a send destination; pass vx=Mix.MIX3, "
                    f"vx=Group(1), vx=Mix.LR and so on"
                )
            if spec.name == "mix_assign" and target_vx == 0x07:
                # PRD 14.8: 55/07 is a syntactically valid second encoding for
                # LR assignment, but only one is what the console listens to.
                raise ParameterRangeError(
                    "mix_assign to LR is ambiguous with lr_assign (PRD 14.8); "
                    "use set(ch, 'lr_assign', ...) instead"
                )
            return spec.vx.validate(target_vx, spec.name)
        if isinstance(vx, ChannelId):
            raise ParameterRangeError(
                f"{spec.name} does not take a send destination; its VX is not a target"
            )
        return spec.vx.validate(vx, spec.name)

    async def set(
        self,
        ch: ChannelId,
        param: str,
        *,
        raw: int | None = None,
        db: float | None = None,
        vx: ChannelId | int | None = None,
    ) -> None:
        """Write one parameter. Resolves once the bytes have been drained."""
        if (raw is None) == (db is None):
            raise ValueError("pass exactly one of raw= or db=")
        spec = REGISTRY.lookup(param, ch.cls)
        if db is not None:
            if not isinstance(spec.domain, Interpolated):
                raise ParameterRangeError(
                    f"{ch.label} {spec.name} has no taper; pass raw= instead of db="
                )
            raw = spec.domain.to_raw(db)
        assert raw is not None
        # PRD 11.1: a write issued while a connect is in flight waits here for
        # the 11 reply rather than being refused.
        await self._await_channel(f"set {param} on {ch.label}")
        await self._send(self.encode(ch, param, raw, vx))

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
        self._caps = caps_for_box_id(box_id)
        if firmware < c.MIN_FIRMWARE:
            # PRD 6.2: do not raise, and do not silently pretend the registry applies.
            log.warning(
                "console reports firmware %d.%d, below the documented floor %d.%d; "
                "parameter coverage is unverified below that firmware",
                firmware[0], firmware[1], c.MIN_FIRMWARE[0], c.MIN_FIRMWARE[1],
            )
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
