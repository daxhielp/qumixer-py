import asyncio

import pytest

from qumidi.errors import QuConnectionError
from qumidi.transport.base import ConnectionState
from qumidi.transport.tcp import TcpTransport


async def test_reconnects_after_the_server_drops_the_connection(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory()
    states: list[ConnectionState] = []
    t = TcpTransport(
        "127.0.0.1", port=fake.port, auto_reconnect=True, reconnect_backoff_min=0.05
    )
    t.on_state_change(states.append)
    await t.connect()
    try:
        await fake.drop_connection()
        await asyncio.sleep(0.5)
        assert ConnectionState.RECONNECTING in states
        assert t.is_connected
    finally:
        await t.close()


async def test_backoff_grows_and_is_jittered() -> None:
    t = TcpTransport("127.0.0.1", port=1, reconnect_backoff_min=1.0, reconnect_backoff_max=10.0)
    delays = [t._backoff_delay(n) for n in range(6)]
    assert 0.8 <= delays[0] <= 1.2
    assert 1.6 <= delays[1] <= 2.4
    assert delays[-1] <= 12.0  # capped, with jitter headroom
    # Jitter means two draws at the same attempt differ.
    assert len({round(t._backoff_delay(3), 6) for _ in range(20)}) > 1


async def test_max_attempts_gives_up_instead_of_spinning_forever() -> None:
    states: list[ConnectionState] = []
    t = TcpTransport(
        "127.0.0.1", port=1, connect_timeout=0.05,
        auto_reconnect=True, reconnect_backoff_min=0.01, max_reconnect_attempts=2,
    )
    t.on_state_change(states.append)
    with pytest.raises(QuConnectionError):
        await t.connect()
    await asyncio.sleep(0.3)
    assert states[-1] is ConnectionState.DISCONNECTED
    await t.close()


async def test_close_during_backoff_does_not_resurrect_the_connection(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # PRD 4.4: a user-initiated close must not race with a pending backoff
    # timer. PRD 16.16.
    fake = await fake_qu_factory()
    t = TcpTransport(
        "127.0.0.1", port=fake.port, auto_reconnect=True, reconnect_backoff_min=0.3
    )
    await t.connect()
    await fake.drop_connection()
    await asyncio.sleep(0.05)          # inside the backoff window
    await t.close()
    await asyncio.sleep(0.6)           # past when the retry would have fired
    assert not t.is_connected
    assert t.state is ConnectionState.DISCONNECTED


async def test_close_is_idempotent_and_leaves_no_tasks(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory()
    before = len(asyncio.all_tasks())
    t = TcpTransport("127.0.0.1", port=fake.port)
    await t.connect()
    await t.close()
    await t.close()
    await asyncio.sleep(0.05)
    assert len(asyncio.all_tasks()) <= before
