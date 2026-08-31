"""FakeQu -- an asyncio server implementing the Qu's state machine (PRD 13.1).

This is both the primary test fixture and a shipped dev tool, so that anyone
can build against qumidi without owning a console. Run it standalone with:

    python -m qumidi.testing.fake_qu --model qu32 --port 51325
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from collections.abc import Iterable
from itertools import cycle
from pathlib import Path

from qumidi.midi.messages import MidiMessage, NoteOn, ProgramChange, SysEx
from qumidi.midi.nrpn import NrpnDecoder, encode_nrpn
from qumidi.midi.parser import MidiStreamParser
from qumidi.protocol import constants as c
from qumidi.protocol.sysex import build_sysex, pack8to7, parse_sysex

log = logging.getLogger(__name__)

#: model name -> BoxID (PRD 6.2).
BOX_IDS = {"qu16": 1, "qu24": 2, "qu32": 3, "qupac": 4, "qusb": 5}


class FakeQu:
    """A configurable stand-in for a real Qu console."""

    def __init__(
        self,
        model: str = "qu32",
        *,
        midi_channel: int = 0,
        firmware: tuple[int, int] = c.MIN_FIRMWARE,
        box_id: int | None = None,
        echo: bool = True,
        send_end_marker: bool = True,
        sync_delay: float = 0.0,
        keepalive_interval: float = 0.3,
        scene: dict[tuple[int, int, int], int] | None = None,
        drop_last_vx: bool = False,
        names: dict[int, str] | None = None,
        name_silent: set[int] | None = None,
        ignore_name_sets: bool = False,
        scenes: dict[int, dict[tuple[int, int, int], int]] | None = None,
        scene_names: dict[int, dict[int, str]] | None = None,
        drop_after_sync: bool = False,
        reconnect_after_drops: int | None = None,
        meter_frames: Iterable[bytes] | None = None,
        meter_rate: float = 20.0,
        replay: str | os.PathLike[str] | None = None,
        split_writes: int | None = None,
        fe_inside_sysex: bool = False,
        fe_between_every_byte: bool = False,
        malformed_sysex: bool = False,
        duplicate_state_reply: bool = False,
        name_bytes_high: bool = False,
        running_status: bool = False,
    ) -> None:
        self.model = model
        self.midi_channel = midi_channel
        self.firmware = firmware
        self.box_id = BOX_IDS[model] if box_id is None else box_id
        self.echo = echo
        self.send_end_marker = send_end_marker
        self.sync_delay = sync_delay
        self.keepalive_interval = keepalive_interval
        self.drop_last_vx = drop_last_vx
        #: PRD 13.1: drop the connection immediately after the `14` end-of-
        #: sync marker, so the "drop mid-sync" fault is exercisable.
        self.drop_after_sync = drop_after_sync
        #: Cap on how many syncs drop_after_sync actually drops -- None means
        #: unbounded. Lets a test exercise "drop, then reconnect and settle"
        #: rather than looping forever, since every resync after a drop would
        #: otherwise drop again too.
        self.reconnect_after_drops = reconnect_after_drops
        self._drops_done = 0
        #: PRD 13.1 fault-injection flags -- see _write() for how the first
        #: three compose, and _do_sync()/_on_message() for the rest.
        self.split_writes = split_writes
        self.fe_inside_sysex = fe_inside_sysex
        self.fe_between_every_byte = fe_between_every_byte
        self.malformed_sysex = malformed_sysex
        self.duplicate_state_reply = duplicate_state_reply
        self.name_bytes_high = name_bytes_high
        self.running_status = running_status
        self.names: dict[int, str] = dict(names or {})
        self.name_requests: list[int] = []
        self.name_silent: set[int] = set(name_silent or ())
        self.ignore_name_sets = ignore_name_sets
        #: PRD 12: whether the client has last asked for meters on/off.
        self.meters_enabled = False
        self._meter_frames = list(meter_frames or [])
        self.meter_rate = meter_rate
        #: PRD 13.1/13.5: when set, `_handle` serves this raw capture verbatim
        #: instead of running the synthesised state machine below -- a real
        #: session recorded once and replayed forever as a regression fixture.
        self._replay: bytes | None = Path(replay).read_bytes() if replay is not None else None

        self.port = 0
        self.connections = 0
        self.sync_count = 0
        self.received = bytearray()
        self.mutes: dict[int, bool] = {}
        #: keyed (ch, param_id, vx); seeded from ``scene`` and updated by
        #: every inbound NRPN write.
        self.state: dict[tuple[int, int, int], int] = dict(scene or {})
        self._scene = dict(scene or {})
        #: scene number (1-based, as sent on the wire in a Program Change) ->
        #: that scene's parameter map, pushed as `.state`/`._scene` once
        #: recalled (PRD 7.2).
        self.scenes: dict[int, dict[tuple[int, int, int], int]] = dict(scenes or {})
        self.scene_names: dict[int, dict[int, str]] = dict(scene_names or {})
        #: The last scene recalled via Program Change, 1-based. None until
        #: the first recall -- a real Qu offers no way to read this either.
        self.current_scene: int | None = None
        self._decoder = NrpnDecoder()
        self._server: asyncio.AbstractServer | None = None
        self._client: asyncio.StreamWriter | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        self._server = await asyncio.start_server(self._handle, host, port)
        self.port = int(self._server.sockets[0].getsockname()[1])
        return self.port

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        if self._client is not None:
            self._client.close()
            self._client = None
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    # -- connection handling ---------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._client is not None:
            # PRD 4.2: exactly one TCP MIDI connection at a time.
            log.info("refusing a second connection")
            writer.close()
            return
        self._client = writer
        self.connections += 1
        keepalive = asyncio.create_task(self._keepalive(writer))
        self._tasks.add(keepalive)
        try:
            await self._write(writer, bytes([c.ACTIVE_SENSING]))
            if self._replay is not None:
                # Give the client a moment to open its socket and send its
                # own opening request before the capture starts arriving --
                # the capture already contains whatever reply that request
                # would have produced, so nothing needs to be parsed here.
                await asyncio.sleep(0.05)
                writer.write(self._replay)
                await writer.drain()
                while await reader.read(4096):
                    pass
            else:
                parser = MidiStreamParser()
                while data := await reader.read(4096):
                    self.received.extend(data)
                    for msg in parser.feed(data):
                        await self._on_message(msg, writer)
        except (ConnectionResetError, asyncio.CancelledError, OSError):
            pass
        finally:
            keepalive.cancel()
            self._tasks.discard(keepalive)
            if self._client is writer:
                self._client = None
            # PRD 12/4.2: a meter subscription is session-scoped, not
            # persisted console state -- a new TCP session starts with meters
            # off, exactly like the real console does. This must not be
            # guarded by "self._client is writer" above: drop_connection()
            # already nulls self._client itself (synchronously, without
            # waiting for this coroutine to unwind), so by the time this
            # finally block runs after a drop_connection()-initiated close,
            # that identity check would already be false and this reset
            # would never run -- leaving a test that polls meters_enabled
            # after a drop racing a stale True from before the drop.
            self.meters_enabled = False
            writer.close()

    async def _keepalive(self, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                await asyncio.sleep(self.keepalive_interval)
                await self._write(writer, bytes([c.ACTIVE_SENSING]))
        except (asyncio.CancelledError, OSError):
            pass

    async def _write(self, writer: asyncio.StreamWriter, data: bytes) -> None:
        """Every synthesised outbound byte passes through here (PRD 13.1).

        Applying the fault flags at one chokepoint, rather than at each call
        site, is what makes them compose -- e.g. fe_between_every_byte and
        split_writes both apply to the same already-mangled bytes below.
        send_raw()/send_surface_move() and replay mode bypass this
        deliberately: they hand bytes straight to the wire unparsed, or
        replay a real capture verbatim, so there is nothing here for the
        fault flags to apply to.
        """
        if self.fe_inside_sysex and data[:1] == bytes([c.SYSEX_START]):
            # PRD 13.1: active sensing landing mid-sysex. Rule 3 of the
            # client's own parser extracts a realtime byte from anywhere,
            # including here, without disturbing the sysex in progress --
            # this is exactly the case that a naive "assume no other bytes
            # interrupt a sysex" parser gets wrong.
            mid = len(data) // 2
            data = data[:mid] + bytes([c.ACTIVE_SENSING]) + data[mid:]
        if self.fe_between_every_byte:
            data = b"".join(bytes([byte, c.ACTIVE_SENSING]) for byte in data)
        if self.split_writes:
            for start in range(0, len(data), self.split_writes):
                writer.write(data[start : start + self.split_writes])
                await writer.drain()
                await asyncio.sleep(0)
            return
        writer.write(data)
        await writer.drain()

    async def _on_message(self, msg: MidiMessage, writer: asyncio.StreamWriter) -> None:
        for update in self._decoder.feed(msg):
            if update.vx is None:
                continue
            self.state[(update.ch, update.param_id, update.vx)] = update.value
            if self.echo:
                await self._write(
                    writer,
                    encode_nrpn(
                        self.midi_channel, update.ch, update.param_id, update.value, update.vx
                    ),
                )
        if isinstance(msg, NoteOn):
            # PRD 7.2 receive rule: velocity 00 and Note Off are ignored;
            # 01-3F unmutes, 40-7F mutes.
            if msg.velocity == 0x00:
                return
            self.mutes[msg.note] = msg.velocity >= 0x40
            return
        if isinstance(msg, ProgramChange):
            # PRD 7.2: the bank select immediately before is plain CC on
            # controllers 0/32, not NRPN, so it never reaches here -- only
            # the Program Change itself models "which scene is now live".
            self.current_scene = msg.program + 1
            new_state = self.scenes.get(self.current_scene)
            if new_state is not None:
                self._scene = dict(new_state)
                self.state = dict(new_state)
            new_names = self.scene_names.get(self.current_scene)
            if new_names is not None:
                self.names = dict(new_names)
            return
        if not isinstance(msg, SysEx):
            return
        parsed = parse_sysex(msg)
        if parsed is None:
            return
        if parsed.command == c.CMD_GET_SYSTEM_STATE:
            await self._do_sync(writer)
        elif parsed.command == c.CMD_METERS:
            self.meters_enabled = bool(parsed.payload and parsed.payload[0])
            if self.meters_enabled and self._meter_frames:
                task = asyncio.create_task(self._stream_meters(writer))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        elif parsed.command == c.CMD_GET_NAME:
            ch = parsed.payload[0]
            self.name_requests.append(ch)
            if ch in self.name_silent:
                return
            name = self.names.get(ch, "")
            name_bytes = bytearray(name.encode("ascii"))
            if self.name_bytes_high and name_bytes:
                # PRD 9.4 describes this fault as a payload byte >= 0x80, but
                # every Qu sysex payload byte is wire-constrained to 7 bits
                # (PRD 9.2/build_sysex) -- a genuine >=0x80 byte is a MIDI
                # status byte and would abort the sysex outright (see Rule 4
                # in MidiStreamParser), not survive as corrupted payload for
                # the receiver to sanitise. 0x7F -- the top of the legal
                # 7-bit range, and already the "non-printable" byte used in
                # tests/protocol/test_names.py -- reaches the exact same
                # receive-side sanitiser (parse_name_reply) without
                # violating the wire format.
                name_bytes[0] = 0x7F
            await self._write(
                writer,
                build_sysex(self.midi_channel, bytes([c.CMD_NAME_REPLY, ch]) + bytes(name_bytes)),
            )
        elif parsed.command == c.CMD_SET_NAME and not self.ignore_name_sets:
            self.names[parsed.payload[0]] = parsed.payload[1:].decode("ascii", "replace")

    async def drop_connection(self) -> None:
        """Cut the client's socket, as pulling the cable would."""
        if self._client is not None:
            self._client.close()
            self._client = None

    async def send_raw(self, data: bytes) -> None:
        """Write raw bytes straight to the client, unparsed.

        For traffic this fake's own message model has no representation for
        yet (e.g. MIDI-strip channel N+1 CCs, PRD 6.3/14.7) -- everything
        else should prefer send_surface_move() or the sysex helpers.
        """
        if self._client is None:
            raise RuntimeError("no client connected")
        self._client.write(data)
        await self._client.drain()

    async def send_surface_move(self, ch: int, param_id: int, value: int, vx: int) -> None:
        """Emit an unsolicited NRPN, as a physical control move would."""
        if self._client is None:
            raise RuntimeError("no client connected")
        self.state[(ch, param_id, vx)] = value
        self._client.write(encode_nrpn(self.midi_channel, ch, param_id, value, vx))
        await self._client.drain()

    async def _do_sync(self, writer: asyncio.StreamWriter) -> None:
        self.sync_count += 1
        if self.malformed_sysex:
            # PRD 13.1: a sysex with no F7 terminator, sent immediately
            # before the real reply. The client's parser must recover by
            # treating the next F0 (the real reply, below) as aborting this
            # truncated one (Rule 4 of MidiStreamParser), not by getting
            # stuck waiting for a terminator that never comes.
            await self._write(
                writer,
                c.QU_SYSEX_PREFIX
                + bytes([self.midi_channel, c.CMD_SYSTEM_STATE_REPLY, self.box_id]),
            )
        await self._write(
            writer,
            build_sysex(
                self.midi_channel,
                bytes([c.CMD_SYSTEM_STATE_REPLY, self.box_id, *self.firmware]),
            ),
        )
        if self.duplicate_state_reply:
            # PRD 9.3.10: a second `11` reply, with a different BoxID, that
            # the client must log a warning about and ignore, staying bound
            # to the first.
            await self._write(
                writer,
                build_sysex(
                    self.midi_channel,
                    bytes([c.CMD_SYSTEM_STATE_REPLY, self.box_id + 1, *self.firmware]),
                ),
            )
        if self.sync_delay:
            await asyncio.sleep(self.sync_delay)
        await self._push_state(writer)
        if self.send_end_marker:
            await self._write(writer, build_sysex(self.midi_channel, bytes([c.CMD_END_OF_SYNC])))
        if self.drop_after_sync and (
            self.reconnect_after_drops is None or self._drops_done < self.reconnect_after_drops
        ):
            self._drops_done += 1
            await self.drop_connection()

    async def _stream_meters(self, writer: asyncio.StreamWriter) -> None:
        """PRD 12: cycle the configured frames at meter_rate Hz while enabled."""
        try:
            for payload in cycle(self._meter_frames):
                if not self.meters_enabled:
                    return
                await self._write(
                    writer,
                    build_sysex(self.midi_channel, bytes([c.CMD_METER_DATA]) + pack8to7(payload)),
                )
                await asyncio.sleep(1.0 / self.meter_rate)
        except (asyncio.CancelledError, OSError):
            pass

    async def _push_state(self, writer: asyncio.StreamWriter) -> None:
        """Emit the canned scene as an NRPN burst (PRD 9.3)."""
        items = list(self._scene.items())
        chunks: list[bytes] = []
        for index, ((ch, param_id, vx), value) in enumerate(items):
            data = encode_nrpn(self.midi_channel, ch, param_id, value, vx)
            if self.drop_last_vx and index == len(items) - 1:
                # PRD 9.3.7: a burst ending without its Data LSB. The client
                # must flush the held update when 14 arrives.
                data = data[:9]
            chunks.append(data)
        burst = b"".join(chunks)
        if self.running_status and burst:
            # PRD 13.1: every message in this burst shares one status byte
            # (Bn on self.midi_channel) throughout, so running status holds
            # across the whole burst, not just within one NRPN quad -- strip
            # the repeated status byte from every CC triplet after the
            # first.
            out = bytearray(burst[:3])
            for start in range(3, len(burst), 3):
                out.extend(burst[start + 1 : start + 3])
            burst = bytes(out)
        await self._write(writer, burst)


def main() -> None:  # pragma: no cover - manual dev tool
    ap = argparse.ArgumentParser(description="Fake Allen & Heath Qu mixer")
    ap.add_argument("--model", default="qu32", choices=sorted(BOX_IDS))
    ap.add_argument("--port", type=int, default=c.QU_TCP_PORT)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--midi-channel", type=int, default=0, help="wire channel 0-15")
    ap.add_argument("--no-echo", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO)

    async def run() -> None:
        fake = FakeQu(args.model, midi_channel=args.midi_channel, echo=not args.no_echo)
        await fake.start(args.host, args.port)
        print(f"FakeQu {args.model} listening on {args.host}:{fake.port}")
        await asyncio.Event().wait()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":  # pragma: no cover
    main()
