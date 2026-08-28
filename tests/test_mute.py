import asyncio

import pytest

from qumidi.client_async import AsyncQuMixer
from qumidi.errors import NotSyncedError, UnsupportedOnModelError
from qumidi.protocol.channels import Dca, Input, MuteGroup


async def test_mute_on_emits_the_documented_note_pair(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory()
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        fake.received.clear()
        await mixer.mute(Input(6), True)
        await asyncio.sleep(0.05)
    assert bytes([0x90, 0x25, 0x7F, 0x90, 0x25, 0x00]) in bytes(fake.received)


async def test_mute_off_uses_velocity_3f(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory()
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        fake.received.clear()
        await mixer.mute(Input(6), False)
        await asyncio.sleep(0.05)
    assert bytes([0x90, 0x25, 0x3F, 0x90, 0x25, 0x00]) in bytes(fake.received)


async def test_mute_uses_the_consoles_midi_channel(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # PRD 9.3.2: everything subsequently sent must use channel N -- do not
    # assume 1 (or 0).
    fake = await fake_qu_factory(midi_channel=5)
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        fake.received.clear()
        await mixer.mute(Input(6), True)
        await asyncio.sleep(0.05)
    assert bytes([0x95, 0x25, 0x7F, 0x95, 0x25, 0x00]) in bytes(fake.received)


async def test_fake_qu_tracks_the_mute(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory()
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        await mixer.mute(Input(6), True)
        await asyncio.sleep(0.05)
        assert fake.mutes[0x25] is True
        await mixer.mute(Input(6), False)
        await asyncio.sleep(0.05)
        assert fake.mutes[0x25] is False


async def test_muting_a_dca_and_a_mute_group_is_the_same_write(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # PRD 7.2: a Note On to 0x50-0x53 fires the whole mute group and to
    # 0x10-0x13 mutes the DCA. This is the *only* way to fire a mute group.
    fake = await fake_qu_factory()
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        fake.received.clear()
        await mixer.mute(Dca(1), True)
        await mixer.mute(MuteGroup(2), True)
        await asyncio.sleep(0.05)
    raw = bytes(fake.received)
    assert bytes([0x90, 0x10, 0x7F]) in raw
    assert bytes([0x90, 0x51, 0x7F]) in raw


async def test_mute_on_a_mixer_that_was_never_connected_raises() -> None:
    # PRD 11.3: NotSyncedError for an operation needing channel N before sync.
    mixer = AsyncQuMixer("127.0.0.1", port=1)
    with pytest.raises(NotSyncedError):
        await mixer.mute(Input(6), True)


async def test_a_write_issued_during_connect_is_queued_not_refused(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # PRD 11.1: writes before sync are queued and flushed after the 11 reply
    # establishes N -- they cannot be sent earlier because the MIDI channel is
    # not yet known. A write issued this way is transmitted AFTER the state
    # burst and will therefore appear to "win" over the console's own state.
    fake = await fake_qu_factory(sync_delay=0.2)
    mixer = AsyncQuMixer("127.0.0.1", port=fake.port)
    connecting = asyncio.create_task(mixer.connect())
    await asyncio.sleep(0.05)          # the 11 reply has not arrived yet
    await mixer.mute(Input(6), True)   # queues, then flushes
    await connecting
    try:
        assert fake.mutes[0x25] is True
    finally:
        await mixer.close()


async def test_mute_on_a_channel_the_model_lacks_raises(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(model="qu16")
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        with pytest.raises(UnsupportedOnModelError):
            await mixer.mute(Input(17), True)


async def test_the_note_pair_is_one_queued_payload(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # Concurrent mutes must never interleave their two notes.
    fake = await fake_qu_factory()
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        fake.received.clear()
        await asyncio.gather(*(mixer.mute(Input(n), True) for n in range(1, 9)))
        await asyncio.sleep(0.1)
    raw = bytes(fake.received)
    for n in range(1, 9):
        note = 0x20 + n - 1
        assert bytes([0x90, note, 0x7F, 0x90, note, 0x00]) in raw
