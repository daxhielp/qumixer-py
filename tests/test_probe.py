import asyncio

import pytest

from qumidi.client_async import AsyncQuMixer
from qumidi.errors import QuConnectionError, SyncTimeoutError
from qumidi.transport.base import ConnectionState


async def test_probe_learns_box_id_firmware_and_channel(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(model="qu24", midi_channel=5, firmware=(1, 9))
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        assert mixer.box_id == 2
        assert mixer.firmware == (1, 9)
        assert mixer.midi_channel == 5
        # PRD 9.1: the public API and all log messages use the 1-based number
        # the console displays.
        assert mixer.midi_channel_display == 6
        assert mixer.state is ConnectionState.READY


async def test_no_reply_at_all_raises_connection_error_naming_the_limit(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # PRD 9.3.9: no 11 within reply_timeout means the host is not a Qu, or
    # another client holds the session.
    #
    # The connection handler must be a coroutine function that holds the
    # accepted connection open until we explicitly close it. A plain
    # (non-async) callback -- e.g. ``lambda r, w: None`` -- is called
    # synchronously and its return value (plain ``None``, not a coroutine) is
    # never scheduled as a task, so nothing ever closes the accepted
    # StreamWriter; it is later garbage-collected unclosed, which raises
    # ResourceWarning and, under this project's `filterwarnings = ["error"]`,
    # fails the test.
    accepted: list[asyncio.StreamWriter] = []

    async def _accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        accepted.append(writer)

    server = await asyncio.start_server(_accept, "127.0.0.1", 0)
    port = int(server.sockets[0].getsockname()[1])
    try:
        before = len(asyncio.all_tasks())
        mixer = AsyncQuMixer("127.0.0.1", port=port, reply_timeout=0.3)
        with pytest.raises(QuConnectionError) as excinfo:
            await mixer.connect()
        assert "one" in str(excinfo.value).lower()
        # connect() itself must clean up on failure -- __aenter__ raising
        # skips __aexit__ under `async with`, so nothing else would call
        # close() for a caller using that form. Check this *before* our own
        # explicit close() below, so a leak in connect() isn't masked by it.
        await asyncio.sleep(0.05)
        assert len(asyncio.all_tasks()) <= before
        await mixer.close()  # idempotent; mirrors a caller that always closes
    finally:
        for writer in accepted:
            writer.close()
        server.close()
        await server.wait_closed()


async def test_reply_but_no_end_marker_raises_sync_timeout(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # PRD 9.3.9: a 11 but no 14 is a *different* failure and must say so.
    fake = await fake_qu_factory(send_end_marker=False)
    before = len(asyncio.all_tasks())
    mixer = AsyncQuMixer("127.0.0.1", port=fake.port, sync_timeout=0.4)
    with pytest.raises(SyncTimeoutError):
        await mixer.connect()
    # Same leak check as above, for the other sync-failure path.
    await asyncio.sleep(0.05)
    assert len(asyncio.all_tasks()) <= before
    await mixer.close()  # idempotent; mirrors a caller that always closes


async def test_context_manager_closes_cleanly(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory()
    before = len(asyncio.all_tasks())
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
    await asyncio.sleep(0.05)
    assert len(asyncio.all_tasks()) <= before
