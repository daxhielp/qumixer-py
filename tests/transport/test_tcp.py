import asyncio

import pytest

from qumixer_py.errors import QuConnectionError
from qumixer_py.midi.messages import ControlChange, SystemRealtime
from qumixer_py.transport.base import ConnectionState
from qumixer_py.transport.tcp import TcpTransport


class Echo:
    """A minimal TCP server for transport-level tests."""

    def __init__(self) -> None:
        self.received = bytearray()
        self.writers: list[asyncio.StreamWriter] = []
        self.connections = 0
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return int(self._server.sockets[0].getsockname()[1])

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        self.writers.append(writer)
        try:
            while data := await reader.read(4096):
                self.received.extend(data)
        except (ConnectionResetError, asyncio.CancelledError):
            pass

    async def push(self, data: bytes) -> None:
        for w in self.writers:
            w.write(data)
            await w.drain()

    async def stop(self) -> None:
        for w in self.writers:
            w.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


@pytest.fixture
async def echo():  # type: ignore[no-untyped-def]
    server = Echo()
    port = await server.start()
    yield server, port
    await server.stop()


async def test_connect_reports_connected(echo) -> None:  # type: ignore[no-untyped-def]
    _server, port = echo
    states: list[ConnectionState] = []
    t = TcpTransport("127.0.0.1", port=port)
    t.on_state_change(states.append)
    await t.connect()
    try:
        assert t.is_connected
        assert states == [ConnectionState.CONNECTING, ConnectionState.CONNECTED]
    finally:
        await t.close()
    assert states[-1] == ConnectionState.DISCONNECTED


async def test_connect_to_a_closed_port_raises_qu_connection_error() -> None:
    t = TcpTransport("127.0.0.1", port=1, connect_timeout=0.5)
    with pytest.raises(QuConnectionError) as excinfo:
        await t.connect()
    # the message must name the single-connection limit -- this is
    # the number one user support question.
    assert "one" in str(excinfo.value).lower()


async def test_send_reaches_the_server(echo) -> None:  # type: ignore[no-untyped-def]
    server, port = echo
    t = TcpTransport("127.0.0.1", port=port)
    await t.connect()
    try:
        await t.send(bytes([0xB0, 0x63, 0x25]))
        await asyncio.sleep(0.05)
        assert bytes([0xB0, 0x63, 0x25]) in bytes(server.received)
    finally:
        await t.close()


async def test_inbound_bytes_arrive_as_parsed_messages(echo) -> None:  # type: ignore[no-untyped-def]
    server, port = echo
    t = TcpTransport("127.0.0.1", port=port)
    await t.connect()
    try:
        await server.push(bytes([0xB0, 0x63]))
        await server.push(bytes([0x25]))
        agen = t.messages()
        msg = await asyncio.wait_for(agen.__anext__(), timeout=1.0)
        assert msg == ControlChange(channel=0, controller=0x63, value=0x25)
    finally:
        await t.close()


async def test_active_sense_is_consumed_not_forwarded(echo) -> None:  # type: ignore[no-untyped-def]
    server, port = echo
    t = TcpTransport("127.0.0.1", port=port)
    await t.connect()
    try:
        await server.push(bytes([0xFE, 0xB0, 0x63, 0x25]))
        msg = await asyncio.wait_for(t.messages().__anext__(), timeout=1.0)
        assert msg == ControlChange(channel=0, controller=0x63, value=0x25)
    finally:
        await t.close()


async def test_active_sense_is_forwarded_when_requested(echo) -> None:  # type: ignore[no-untyped-def]
    server, port = echo
    t = TcpTransport("127.0.0.1", port=port, forward_active_sense=True)
    await t.connect()
    try:
        await server.push(bytes([0xFE]))
        msg = await asyncio.wait_for(t.messages().__anext__(), timeout=1.0)
        assert msg == SystemRealtime(status=0xFE)
    finally:
        await t.close()


async def test_keepalive_is_emitted_on_the_configured_interval(echo) -> None:  # type: ignore[no-untyped-def]
    server, port = echo
    t = TcpTransport("127.0.0.1", port=port, keepalive_interval=0.05)
    await t.connect()
    try:
        await asyncio.sleep(0.28)
        assert bytes(server.received).count(0xFE) >= 4
    finally:
        await t.close()


async def test_inbound_silence_declares_the_link_dead(echo) -> None:  # type: ignore[no-untyped-def]
    # do not wait for the socket to notice.
    _server, port = echo
    states: list[ConnectionState] = []
    t = TcpTransport("127.0.0.1", port=port, keepalive_interval=0.05, inbound_timeout=0.2)
    t.on_state_change(states.append)
    await t.connect()
    try:
        await asyncio.sleep(0.5)
        assert ConnectionState.DISCONNECTED in states
        assert not t.is_connected
    finally:
        await t.close()


async def test_close_is_idempotent_and_leaves_no_tasks(echo) -> None:  # type: ignore[no-untyped-def]
    _server, port = echo
    before = len(asyncio.all_tasks())
    t = TcpTransport("127.0.0.1", port=port)
    await t.connect()
    await t.close()
    await t.close()
    await asyncio.sleep(0.05)
    assert len(asyncio.all_tasks()) <= before
