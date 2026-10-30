import asyncio

import pytest

from qumixer_py.midi.messages import SysEx
from qumixer_py.midi.parser import MidiStreamParser
from qumixer_py.protocol.sysex import get_system_state, parse_sysex


async def read_qu_sysex(reader: asyncio.StreamReader, command: int, timeout: float = 2.0):
    """Read until a Qu sysex with the given command byte arrives.

    A single ``reader.read()`` call can return several whole Qu sysex messages
    at once (the server writes them back-to-back with no real event-loop
    yield in between, so the OS is free to coalesce them). A parser and any
    already-parsed-but-not-yet-consumed messages are cached on the reader
    itself so that a second call for a different command, on the same
    reader, sees a message that arrived bundled with the first call's rather
    than discarding it and blocking forever waiting for it to arrive again.
    """
    parser = getattr(reader, "_qu_test_parser", None)
    if parser is None:
        parser = MidiStreamParser()
        reader._qu_test_parser = parser  # type: ignore[attr-defined]
    pending: list = getattr(reader, "_qu_test_pending", None)  # type: ignore[type-arg]
    if pending is None:
        pending = []
        reader._qu_test_pending = pending  # type: ignore[attr-defined]

    async def _pump():
        while True:
            for i, parsed in enumerate(pending):
                if parsed.command == command:
                    return pending.pop(i)
            data = await reader.read(4096)
            if not data:
                raise AssertionError("connection closed while waiting for command")
            for msg in parser.feed(data):
                if isinstance(msg, SysEx):
                    parsed = parse_sysex(msg)
                    if parsed is not None:
                        pending.append(parsed)
    return await asyncio.wait_for(_pump(), timeout=timeout)


async def test_answers_the_all_call_state_request(fake_qu) -> None:  # type: ignore[no-untyped-def]
    reader, writer = await asyncio.open_connection("127.0.0.1", fake_qu.port)
    try:
        writer.write(get_system_state(1))
        await writer.drain()
        reply = await read_qu_sysex(reader, 0x11)
        assert reply.midi_channel == fake_qu.midi_channel
        assert reply.payload == bytes([3, 1, 9])  # qu32, firmware 1.9
    finally:
        writer.close()


async def test_sends_the_end_of_sync_marker(fake_qu) -> None:  # type: ignore[no-untyped-def]
    reader, writer = await asyncio.open_connection("127.0.0.1", fake_qu.port)
    try:
        writer.write(get_system_state(1))
        await writer.drain()
        await read_qu_sysex(reader, 0x11)
        end = await read_qu_sysex(reader, 0x14)
        assert end.payload == b""
    finally:
        writer.close()


async def test_emits_active_sensing_on_connect(fake_qu) -> None:  # type: ignore[no-untyped-def]
    reader, writer = await asyncio.open_connection("127.0.0.1", fake_qu.port)
    try:
        data = await asyncio.wait_for(reader.read(16), timeout=1.0)
        assert 0xFE in data
    finally:
        writer.close()


async def test_refuses_a_second_connection(fake_qu) -> None:  # type: ignore[no-untyped-def]
    # mirroring real behaviour, which tests must cover.
    _r1, w1 = await asyncio.open_connection("127.0.0.1", fake_qu.port)
    try:
        r2, w2 = await asyncio.open_connection("127.0.0.1", fake_qu.port)
        assert await asyncio.wait_for(r2.read(1), timeout=1.0) == b""
        w2.close()
    finally:
        w1.close()


@pytest.mark.parametrize("model,box_id", [("qu16", 1), ("qu24", 2), ("qu32", 3), ("qupac", 4), ("qusb", 5)])
async def test_reports_the_configured_model(fake_qu_factory, model, box_id) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(model=model)
    reader, writer = await asyncio.open_connection("127.0.0.1", fake.port)
    try:
        writer.write(get_system_state(1))
        await writer.drain()
        reply = await read_qu_sysex(reader, 0x11)
        assert reply.payload[0] == box_id
    finally:
        writer.close()


async def test_reports_a_non_zero_midi_channel(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # the suite must pass with a non-zero N, not only the default.
    fake = await fake_qu_factory(midi_channel=5)
    reader, writer = await asyncio.open_connection("127.0.0.1", fake.port)
    try:
        writer.write(get_system_state(1))
        await writer.drain()
        reply = await read_qu_sysex(reader, 0x11)
        assert reply.midi_channel == 5
    finally:
        writer.close()
