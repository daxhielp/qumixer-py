"""The async client (PRD 9.3, 11.1).

Slice 1 scope: connect, run the state-sync handshake, learn BoxID, firmware
and the console's MIDI channel. Later slices add the cache, events, the
registry-driven set/get, names, scenes, reconnect and meters.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from collections.abc import Callable
from types import TracebackType
from typing import Any

from qumidi.errors import (
    NotSyncedError,
    ParameterRangeError,
    QuConnectionError,
    ReadOnlyParameterError,
    SyncTimeoutError,
    UnknownParameterError,
)
from qumidi.events import ChangeEvent, EventBus, Source
from qumidi.midi.messages import MidiMessage, NoteOn, SysEx
from qumidi.midi.nrpn import NrpnDecoder, RawParamUpdate, encode_nrpn
from qumidi.protocol import constants as c
from qumidi.protocol.channels import (
    GLOBAL,
    NAMEABLE_CLASSES,
    PERMISSIVE_CAPS,
    ChannelClass,
    ChannelId,
    Model,
    ModelCaps,
    caps_for_box_id,
    channel_from_raw,
    check_channel,
    nameable_channels,
)
from qumidi.protocol.params import Direction, FlaggedIndex, Interpolated, ParamSpec, SendTarget
from qumidi.protocol.registry import REGISTRY, send_target_vx
from qumidi.protocol.sysex import (
    QuSysex,
    get_name,
    get_system_state,
    parse_name_reply,
    parse_sysex,
    parse_system_state_reply,
    set_name,
)
from qumidi.state import CachedValue, CacheKey, Freshness, MembershipSet, StateCache, sub_index_for
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
        echo_window: float = c.ECHO_WINDOW,
        sync_names: bool = True,
        verify_name_writes: bool = True,
        name_sweep_interval: float = c.NAME_SWEEP_INTERVAL,
        name_request_timeout: float = c.NAME_REQUEST_TIMEOUT,
        name_request_retries: int = c.NAME_REQUEST_RETRIES,
        scene_settle_delay: float = c.SCENE_SETTLE_DELAY,
    ) -> None:
        self.host = host
        self.port = port
        self.ipad_flag = ipad_flag
        self.reply_timeout = reply_timeout
        self.sync_timeout = sync_timeout
        self.echo_window = echo_window
        self.sync_names = sync_names
        self.verify_name_writes = verify_name_writes
        self.name_sweep_interval = name_sweep_interval
        self.name_request_timeout = name_request_timeout
        self.name_request_retries = name_request_retries
        self.scene_settle_delay = scene_settle_delay
        self.names: dict[ChannelId, str | None] = {}
        self._name_waiters: dict[int, asyncio.Future[str]] = {}

        self._transport = transport if transport is not None else TcpTransport(host, port)
        self._outbound = OutboundQueue(self._transport.send, max_send_rate=max_send_rate)
        self._reader_task: asyncio.Task[None] | None = None
        self._tick_task: asyncio.Task[None] | None = None
        self._reply_event = asyncio.Event()
        self._sync_event = asyncio.Event()
        #: Set once the sweep (when enabled) has finished, so wait_synced()
        #: only returns once mixer.name(...) is populated (PRD 9.4).
        self._ready_event = asyncio.Event()

        self._box_id: int | None = None
        self._firmware: tuple[int, int] | None = None
        self._midi_channel: int | None = None
        self._caps: ModelCaps | None = None

        # -- Slice 4: cache, events, ingest ---------------------------------
        self.cache = StateCache()
        self.events = EventBus()
        self._decoder = NrpnDecoder()
        #: Pending optimistic writes awaiting a confirming echo, FIFO per key
        #: (PRD 10.3). Only ``_expect_echo``/``_consume_expectation`` mutate
        #: entries in; reads use ``.get()`` so a lookup never auto-vivifies an
        #: empty deque for a key that has never had a write.
        self._expectations: dict[CacheKey, deque[tuple[int, float]]] = defaultdict(deque)
        self._expectation_tasks: set[asyncio.Task[None]] = set()
        #: True for the entire state-burst handshake, including the
        #: flush-on-``14`` in ``_on_sysex`` (PRD 9.3.8).
        self._syncing = False
        self._transport.on_state_change(self.events.emit_connection_state)

        # -- Slice 7: scene recall ------------------------------------------
        #: 1-based, matching the console's own display. See the
        #: ``last_recalled_scene`` property for why this is the only scene
        #: state this client can ever know.
        self._last_recalled_scene: int | None = None
        #: Placeholder for a future custom-surface-layer feature (PRD 6.3):
        #: no ``SurfaceMap`` type exists yet, so this stays unset and is only
        #: ever consulted -- never assigned -- by ``recall_scene()``.
        self._surface_map: Any = None

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

    @property
    def last_recalled_scene(self) -> int | None:
        """The scene this client last recalled, 1-based.

        There is no way to read the current scene number from the console, so
        this reflects only what this client recalled. It is None after connect
        and after any recall made from the console's own surface (PRD 7.2).
        """
        return self._last_recalled_scene

    # -- reads and subscriptions -------------------------------------------

    def get(self, ch: ChannelId, param: str, sub: int | None = None) -> CachedValue:
        """Read the cache. Synchronous and cheap in both APIs (PRD 10.2)."""
        return self.cache.get(ch, param, sub)

    def membership(self, ch: ChannelId, param: str) -> MembershipSet:
        """Read a set-valued parameter (DCA / mute-group assign, PRD 10.1a)."""
        spec = REGISTRY.lookup(param, ch.cls)
        if not isinstance(spec.domain, FlaggedIndex):
            raise UnknownParameterError(f"{param} is not a set-valued parameter")
        return self.cache.membership(ch, param, spec.domain.count, spec.domain.on_flag)

    def on_change(
        self,
        cb: Callable[[ChangeEvent], None],
        channel: ChannelId | None = None,
        param: str | None = None,
    ) -> Callable[[], None]:
        return self.events.on_change(cb, channel=channel, param=param)

    def on_connection_state(self, cb: Callable[[ConnectionState], None]) -> None:
        self.events.on_connection_state(cb)

    def on_sync_complete(self, cb: Callable[[], None]) -> None:
        self.events.on_sync_complete(cb)

    def on_write_unconfirmed(self, cb: Callable[[ChannelId, str, int | None], None]) -> None:
        self.events.on_write_unconfirmed(cb)

    def on_scene_recalled(self, cb: Callable[[int], None]) -> None:
        self.events.on_scene_recalled(cb)

    async def refresh(self, ch: ChannelId | None = None, param: str | None = None) -> None:
        """Re-read state from the console.

        The protocol offers no targeted get, so this is implemented as a full
        resync regardless of the arguments (PRD 10.2). The arguments exist
        only to document intent at the call site.
        """
        await self.resync()

    async def resync(self) -> None:
        """Re-run the full state handshake against a live connection."""
        await self._sync()

    # -- lifecycle --------------------------------------------------------

    async def connect(self) -> None:
        await self._transport.connect()
        self._reader_task = asyncio.create_task(self._read_loop(), name="qumidi-client-read")
        self._tick_task = asyncio.create_task(self._tick_loop(), name="qumidi-client-tick")
        await self._outbound.start()
        try:
            await self._sync()
        except BaseException:
            # PRD: the Qu permits exactly one TCP MIDI connection at a time.
            # A sync failure (no reply / no end marker) must not leak this
            # connection -- __aenter__ raising skips __aexit__ entirely under
            # `async with`, so connect() must clean up after itself rather
            # than relying on the caller to also call close(). The tick task
            # is created before this try block (alongside the reader task) so
            # that this same close() call also tears it down -- neither task
            # may outlive a failed connect().
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
        if self._tick_task is not None:
            self._tick_task.cancel()
            try:
                await self._tick_task
            except asyncio.CancelledError:
                pass  # expected: we just cancelled it ourselves
            except Exception:
                log.exception("client tick loop raised while closing")
            self._tick_task = None
        # Reader and tick are both fully stopped by this point, so nothing
        # else can be concurrently touching expectations or the decoder.
        await self._clear_expectations()
        self._decoder.reset()
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
        """Block until the state push -- and the name sweep, if enabled --
        has completed (PRD 11.1, 9.4). ``mixer.name(...)`` is populated by
        the time this returns.
        """
        await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)

    # -- sync -------------------------------------------------------------

    async def _sync(self) -> None:
        self._reply_event.clear()
        self._sync_event.clear()
        self._ready_event.clear()
        self._transport.set_state(ConnectionState.SYNCING)
        # PRD 9.3.8: every update ingested while this is True is tagged
        # Source.SYNC and fires no external_change. Must stay True for the
        # *entire* burst, including the flush-on-``14`` in _on_sysex -- that
        # flush runs synchronously inside the read loop's handling of the
        # same end-of-sync message that wakes the wait_for below, so it is
        # guaranteed to complete before this method's continuation (and its
        # `finally`) ever runs.
        self._syncing = True
        try:
            # PRD 9.1: the all-call form is required here, because the
            # mixer's MIDI channel is unknown until it replies.
            await self._transport.send(get_system_state(self.ipad_flag))
            try:
                await asyncio.wait_for(self._reply_event.wait(), timeout=self.reply_timeout)
            except asyncio.TimeoutError as exc:
                raise QuConnectionError(
                    _NOT_A_QU_HINT.format(
                        host=self.host, port=self.port, timeout=self.reply_timeout
                    )
                ) from exc
            try:
                await asyncio.wait_for(self._sync_event.wait(), timeout=self.sync_timeout)
            except asyncio.TimeoutError as exc:
                raise SyncTimeoutError(
                    f"{self.host}:{self.port} replied to the state request but sent no "
                    f"end-of-sync marker within {self.sync_timeout}s; the state push failed"
                ) from exc
        finally:
            self._syncing = False
        self._transport.set_state(ConnectionState.READY)
        if self.sync_names:
            # PRD 9.3.8: keep the sync window short -- do not interleave the
            # name sweep with the burst. The sweep runs after _syncing has
            # already gone False, so an unrelated Source.MIXER event (e.g. a
            # surface move) arriving mid-sweep is reported normally, not
            # suppressed as sync traffic.
            await self.sweep_names()
        self._ready_event.set()
        # Slice 7: SyncComplete is the "everything -- values *and* names --
        # is trustworthy again" signal, so it must fire after the sweep
        # above, not at the wire-level end-of-sync marker. It used to be
        # emitted from _on_sysex's CMD_END_OF_SYNC handler, synchronously at
        # the ``14`` marker; that ran *before* this method's continuation
        # (this line) could ever resume -- asyncio.Event.set() only
        # schedules waiters via call_soon, it does not resume them inline --
        # so a `qumidi monitor` watching for SyncComplete would see it fire
        # while names were still mid-sweep, before a resync/recall_scene's
        # own re-sweep had actually finished. This is the one place that
        # fires it now, for both the initial connect and every future
        # resync()/recall_scene(), so it always follows the sweep.
        self.events.emit_sync_complete()

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

    # -- names (PRD 9.4) ---------------------------------------------------

    def name(self, ch: ChannelId) -> str | None:
        """The console's name for a channel, or None if it has none.

        None, never a sentinel string -- a channel can legitimately be *named*
        "UNKNOWN" and the two must stay distinguishable (PRD 9.4).
        """
        return self.names.get(ch)

    async def sweep_names(self) -> None:
        """Request every nameable channel's name, paced (PRD 9.4).

        Not part of the state push, so this runs after sync. A missing name
        yields None and never aborts the sweep.
        """
        n = self._require_synced("sweep names")
        caps = self._caps or PERMISSIVE_CAPS
        for ch in nameable_channels(caps):
            self.names[ch] = await self._request_name(n, ch)
            await asyncio.sleep(self.name_sweep_interval)
        log.info(
            "name sweep complete: %d named channels",
            sum(1 for v in self.names.values() if v),
        )

    async def _request_name(self, n: int, ch: ChannelId) -> str | None:
        for attempt in range(self.name_request_retries + 1):
            future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
            self._name_waiters[ch.value] = future
            try:
                await self._send(get_name(n, ch.value))
                return await asyncio.wait_for(future, timeout=self.name_request_timeout)
            except asyncio.TimeoutError:
                log.debug("no name reply for %s (attempt %d)", ch.label, attempt + 1)
            finally:
                self._name_waiters.pop(ch.value, None)
        return None

    async def set_name(self, ch: ChannelId, name: str) -> None:
        """Write a channel name, and confirm it (PRD 9.4).

        The console does not acknowledge a name write, so with
        verify_name_writes=True this issues a read-back and raises if the
        console did not take the name.
        """
        n = self._require_synced(f"set the name of {ch.label}")
        check_channel(self._caps, ch)
        if ch.cls not in NAMEABLE_CLASSES:
            raise ParameterRangeError(f"{ch.label} does not have a name")
        await self._send(set_name(n, ch.value, name))
        if not self.verify_name_writes:
            self.names[ch] = name
            return
        observed = await self._request_name(n, ch)
        self.names[ch] = observed
        if observed != name:
            raise ParameterRangeError(
                f"{ch.label}: set the name to {name!r} but the console reports "
                f"{observed!r}. Name writes are unacknowledged, so this read-back is "
                f"the only confirmation available (PRD 9.4)."
            )

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
        # Optimistic local write (PRD 10.3): reflect it immediately as
        # Source.LOCAL, then track it as a pending echo expectation. This
        # happens unconditionally, even if a resync() is concurrently in
        # flight -- a resync's snapshot may be older than this write and
        # overwrite it with a stale Source.SYNC value once ingested, and an
        # echo of this write arriving during that same window is ingested as
        # Source.SYNC too (not consumed as an echo), so the expectation can
        # expire and report UNCONFIRMED even though the console accepted the
        # write. Narrow and inherent to a snapshot protocol racing a live
        # write; see task-4.3-report.md.
        vx_byte = self._resolve_vx(spec, vx)
        sub = sub_index_for(spec, vx_byte, raw)
        old, new = self.cache.set(ch, spec.name, sub, raw, Freshness.KNOWN)
        self.events.emit_change(ChangeEvent(ch, spec.name, sub, old, new, Source.LOCAL))
        self._expect_echo(ch, spec.name, sub, raw)

    # -- scenes (PRD 7.2) ---------------------------------------------------

    async def recall_scene(self, scene: int, *, resync: bool = True) -> None:
        """Recall a scene, 1-based to match the console's display (PRD 7.2).

        A scene changes hundreds of parameters at once and the console does not
        promise to echo them, so this is a state-invalidating operation rather
        than a fire-and-forget write: the cache goes STALE, SceneRecalled is
        emitted, and after a settling delay the whole state is rebuilt.
        """
        n = self._require_synced(f"recall scene {scene}")
        if not 1 <= scene <= 100:
            raise ParameterRangeError(f"scene must be 1..100 (as displayed), got {scene}")
        status = c.CONTROL_CHANGE | n
        payload = bytes(
            [
                status, c.CC_BANK_SELECT_MSB, 0x00,
                status, c.CC_BANK_SELECT_LSB, 0x00,
                c.PROGRAM_CHANGE | n, scene - 1,
            ]
        )
        # PRD 7.2: the Qu ignores program changes unless Bank 1 is currently
        # selected, so the bank select must be sent immediately before every
        # single recall -- never once up front and reused.
        await self._send(payload)
        self._last_recalled_scene = scene

        # Everything we believe about the console is now suspect.
        self.cache.mark_all_stale()
        await self._clear_expectations()
        for ch in list(self.names):
            self.names[ch] = None
        if self._surface_map is not None:
            # PRD 6.3: the custom layer is scene data and may have changed
            # without notification.
            self._surface_map.invalidate()
        self.events.emit_scene_recalled(scene)

        if not resync:
            log.warning(
                "scene %d recalled with resync=False; the cache is STALE and will not "
                "be rebuilt until you call resync()", scene,
            )
            return

        # PRD 14.13: 500 ms is a heuristic, not a protocol guarantee. Program
        # Change is unacknowledged and the console gives no "recall finished"
        # message; a large scene may need longer.
        await asyncio.sleep(self.scene_settle_delay)
        # resync() -> _sync() already sweeps names internally whenever
        # sync_names is on -- that call is unconditional on *every* _sync()
        # run, not just the first connect (see _sync()'s own body, just
        # before it sets _ready_event) -- so a second, explicit sweep_names()
        # call here would silently re-run the entire paced sweep a second
        # time on every recall. Rely on the one resync() already does.
        await self.resync()

    # -- inbound ----------------------------------------------------------

    async def _read_loop(self) -> None:
        async for msg in self._transport.messages():
            try:
                self._dispatch(msg)
            except Exception:
                # PRD: the read loop must never die on one bad message.
                log.exception("error handling inbound message %r", msg)

    def _dispatch(self, msg: MidiMessage) -> None:
        # Every message is fed to the NRPN decoder, sysex included: a non-CC
        # message is itself flush trigger (b) in NrpnDecoder.feed(), which is
        # how the end-of-sync marker below flushes a held update *before*
        # _on_sysex ever runs (PRD 9.3.7).
        for update in self._decoder.feed(msg):
            self._ingest(update)
        if isinstance(msg, SysEx):
            self._on_sysex(msg)

    def _on_sysex(self, msg: SysEx) -> None:
        parsed = parse_sysex(msg)
        if parsed is None:
            return
        if parsed.command == c.CMD_SYSTEM_STATE_REPLY:
            self._on_state_reply(parsed)
        elif parsed.command == c.CMD_NAME_REPLY:
            try:
                raw_ch, name = parse_name_reply(parsed)
            except ValueError as exc:
                log.warning("malformed name reply: %s", exc)
                return
            waiter = self._name_waiters.get(raw_ch)
            if waiter is not None and not waiter.done():
                waiter.set_result(name)
        elif parsed.command == c.CMD_END_OF_SYNC:
            # PRD 9.3.7: flush the held update, or the last parameter in the
            # burst is stranded and the cache is silently one value short.
            # In practice _dispatch's decoder.feed(msg) above has already
            # flushed it (this sysex is itself a non-CC flush trigger) --
            # this call is a deliberate no-op safety net, kept in case
            # _dispatch's routing ever changes to skip the decoder for sysex.
            for update in self._decoder.flush():
                self._ingest(update)
            # Only the burst-wait wakeup lives here. SyncComplete itself is
            # emitted later, from _sync()'s own continuation after the name
            # sweep -- see the comment there for why.
            self._sync_event.set()

    def _ingest(self, update: RawParamUpdate) -> None:
        if self._midi_channel is not None and update.midi_channel != self._midi_channel:
            # MIDI strip traffic lives on N+1 and is a different namespace
            # entirely (PRD 6.3, 14.7). Slice 11 surfaces it.
            return
        try:
            ch = channel_from_raw(update.ch)
        except ParameterRangeError:
            log.debug("update for unassigned CH %#04x; ignoring", update.ch)
            return
        spec = REGISTRY.resolve(ch.cls, update.param_id, update.vx)
        if spec is None:
            # PRD 5.3: an unrecognised (CH, ID) must not raise. Undocumented
            # parameters exist.
            log.debug(
                "unrecognised parameter: CH %#04x ID %#04x VX %s",
                update.ch, update.param_id, update.vx,
            )
            return
        if ChannelClass.GLOBAL in spec.applies_to:
            ch = GLOBAL
        raw = spec.domain.clamp_inbound(update.value, spec.name, ch.label)
        sub = sub_index_for(spec, update.vx, raw)

        if self._syncing:
            old, new = self.cache.set(ch, spec.name, sub, raw, Freshness.KNOWN)
            self.events.emit_change(ChangeEvent(ch, spec.name, sub, old, new, Source.SYNC))
            return

        if self._consume_expectation(ch, spec.name, sub, raw):
            # An echo of our own write: update the cache, emit no external
            # change, so a fader-follow script cannot oscillate (PRD 10.3).
            old, new = self.cache.set(ch, spec.name, sub, raw, Freshness.KNOWN)
            self.events.emit_change(ChangeEvent(ch, spec.name, sub, old, new, Source.LOCAL))
            return

        old, new = self.cache.set(ch, spec.name, sub, raw, Freshness.KNOWN)
        self.events.emit_change(ChangeEvent(ch, spec.name, sub, old, new, Source.MIXER))

    def _consume_expectation(
        self, ch: ChannelId, param: str, sub: int | None, raw: int
    ) -> bool:
        """FIFO, one echo per expectation (PRD 10.3).

        Only the value at the *front* of the queue is compared -- a value
        arriving that matches some other, non-front expectation in the queue
        is external, not an echo, per the PRD 10.3 rule.
        """
        key = self.cache.key(ch, param, sub)
        queue = self._expectations.get(key)
        if not queue:
            return False
        if queue[0][0] != raw:
            return False
        queue.popleft()
        return True

    def _expect_echo(self, ch: ChannelId, param: str, sub: int | None, raw: int) -> None:
        key = self.cache.key(ch, param, sub)
        deadline = time.monotonic() + self.echo_window
        entry = (raw, deadline)
        self._expectations[key].append(entry)
        task = asyncio.create_task(self._expire_expectation(ch, param, sub, entry))
        self._expectation_tasks.add(task)
        task.add_done_callback(self._expectation_tasks.discard)

    async def _expire_expectation(
        self, ch: ChannelId, param: str, sub: int | None, entry: tuple[int, float]
    ) -> None:
        await asyncio.sleep(self.echo_window)
        key = self.cache.key(ch, param, sub)
        queue = self._expectations.get(key)
        if not queue:
            return
        try:
            # Remove exactly the (raw, deadline) tuple this task was
            # scheduled for -- not just any entry with a matching raw value.
            # Two expectations for the same value but different deadlines
            # (e.g. two quick writes of the same raw) would otherwise let one
            # expiry task delete the *other* expectation's still-live entry,
            # downgrading it before its own deadline and leaving its own
            # expiry task to silently no-op against an already-empty slot.
            queue.remove(entry)
        except ValueError:
            return  # already consumed by a genuine echo/surface move
        # PRD 10.3: the mixer may have ignored the write -- wrong VX, invalid
        # on this channel class, console in Group mode. Do not leave the
        # value looking KNOWN.
        self.cache.downgrade(ch, param, sub, Freshness.UNCONFIRMED)
        log.info(
            "write to %s %s was never confirmed by the console; the message may have "
            "been ignored (check VX, channel class, and Group mode -- PRD 14.6)",
            ch.label, param,
        )
        self.events.emit_write_unconfirmed(ch, param, sub)

    async def _clear_expectations(self) -> None:
        """PRD 4.4: drop pending write expectations on disconnect, or the
        first post-reconnect surface move is misclassified as an echo.

        Cancels every pending expiry task and waits for the cancellation to
        actually land before returning, matching how ``close()`` tears down
        ``_reader_task``/``_tick_task`` -- an unawaited cancel() can leave a
        task pending when the loop shuts down, which is what
        ``filterwarnings = ["error"]`` turns into a hard failure.
        """
        tasks = list(self._expectation_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._expectation_tasks.clear()
        self._expectations.clear()

    async def _tick_loop(self) -> None:
        """Flush trigger (c): PENDING_FLUSH_TIMEOUT of inbound silence.

        Without this, a held update with no following CC (or sysex) simply
        never surfaces -- NrpnDecoder.tick() is a cheap no-op whenever
        nothing is currently held, so this costs nothing between writes.
        """
        while True:
            await asyncio.sleep(c.PENDING_FLUSH_TIMEOUT)
            for update in self._decoder.tick():
                self._ingest(update)

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
