"""FakeQu -- an asyncio server implementing the Qu's state machine (PRD 13.1).

This is both the primary test fixture and a shipped dev tool, so that anyone
can build against qumidi without owning a console. Run it standalone with:

    python -m qumidi.testing.fake_qu --model qu32 --port 51325
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from qumidi.midi.messages import MidiMessage, NoteOn, SysEx
from qumidi.midi.nrpn import NrpnDecoder, encode_nrpn
from qumidi.midi.parser import MidiStreamParser
from qumidi.protocol import constants as c
from qumidi.protocol.sysex import build_sysex, parse_sysex

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
    ) -> None:
        self.model = model
        self.midi_channel = midi_channel
        self.firmware = firmware
        self.box_id = BOX_IDS[model] if box_id is None else box_id
        self.echo = echo
        self.send_end_marker = send_end_marker
        self.sync_delay = sync_delay
        self.keepalive_interval = keepalive_interval

        self.port = 0
        self.connections = 0
        self.received = bytearray()
        self.mutes: dict[int, bool] = {}
        #: keyed (ch, param_id, vx); seeded from ``scene`` and updated by
        #: every inbound NRPN write.
        self.state: dict[tuple[int, int, int], int] = dict(scene or {})
        self._scene = dict(scene or {})
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
            writer.write(bytes([c.ACTIVE_SENSING]))
            await writer.drain()
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
            writer.close()

    async def _keepalive(self, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                await asyncio.sleep(self.keepalive_interval)
                writer.write(bytes([c.ACTIVE_SENSING]))
                await writer.drain()
        except (asyncio.CancelledError, OSError):
            pass

    async def _on_message(self, msg: MidiMessage, writer: asyncio.StreamWriter) -> None:
        for update in self._decoder.feed(msg):
            if update.vx is None:
                continue
            self.state[(update.ch, update.param_id, update.vx)] = update.value
            if self.echo:
                writer.write(
                    encode_nrpn(
                        self.midi_channel, update.ch, update.param_id, update.value, update.vx
                    )
                )
                await writer.drain()
        if isinstance(msg, NoteOn):
            # PRD 7.2 receive rule: velocity 00 and Note Off are ignored;
            # 01-3F unmutes, 40-7F mutes.
            if msg.velocity == 0x00:
                return
            self.mutes[msg.note] = msg.velocity >= 0x40
            return
        if not isinstance(msg, SysEx):
            return
        parsed = parse_sysex(msg)
        if parsed is None:
            return
        if parsed.command == c.CMD_GET_SYSTEM_STATE:
            await self._do_sync(writer)

    async def _do_sync(self, writer: asyncio.StreamWriter) -> None:
        writer.write(
            build_sysex(
                self.midi_channel,
                bytes([c.CMD_SYSTEM_STATE_REPLY, self.box_id, *self.firmware]),
            )
        )
        await writer.drain()
        if self.sync_delay:
            await asyncio.sleep(self.sync_delay)
        await self._push_state(writer)
        if self.send_end_marker:
            writer.write(build_sysex(self.midi_channel, bytes([c.CMD_END_OF_SYNC])))
            await writer.drain()

    async def _push_state(self, writer: asyncio.StreamWriter) -> None:
        """Emit the canned scene as an NRPN burst (PRD 9.3)."""
        for (ch, param_id, vx), value in self._scene.items():
            writer.write(encode_nrpn(self.midi_channel, ch, param_id, value, vx))
        await writer.drain()


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
