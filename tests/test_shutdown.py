import asyncio

import pytest

from qumixer_py.client_async import AsyncQuMixer, MmcCommand
from qumixer_py.errors import ShutdownNotPermittedError


async def test_shutdown_without_the_constructor_flag_raises(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory()
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        fake.received.clear()
        with pytest.raises(ShutdownNotPermittedError):
            await mixer.shutdown(confirm=True)
        assert bytes([0x5F]) not in bytes(fake.received)


async def test_shutdown_without_confirm_raises(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory()
    async with AsyncQuMixer("127.0.0.1", port=fake.port, allow_shutdown=True) as mixer:
        await mixer.wait_synced()
        fake.received.clear()
        with pytest.raises(ShutdownNotPermittedError):
            await mixer.shutdown()
        assert bytes([0x5F]) not in bytes(fake.received)


async def test_shutdown_with_both_guards_sends_the_literal_sequence(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # hardcoded to MIDI channel 0 and CH = 00, NOT the console's
    # configured channel N. Implement it literally as specified.
    fake = await fake_qu_factory(midi_channel=5)
    async with AsyncQuMixer("127.0.0.1", port=fake.port, allow_shutdown=True) as mixer:
        await mixer.wait_synced()
        fake.received.clear()
        await mixer.shutdown(confirm=True)
        await asyncio.sleep(0.05)
    assert bytes([0xB0, 0x63, 0x00, 0xB0, 0x62, 0x5F, 0xB0, 0x06, 0x00, 0xB0, 0x26, 0x00]) in bytes(
        fake.received
    )


def test_shutdown_is_not_in_the_object_facade() -> None:
    # do not expose it in the object facade.
    from qumixer_py.model.channel import BaseStrip, InputChannel

    for cls in (BaseStrip, InputChannel):
        assert not hasattr(cls, "shutdown")


async def test_mmc_transport_commands(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory()
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        fake.received.clear()
        await mixer.transport(MmcCommand.PLAY)
        await asyncio.sleep(0.05)
    assert bytes([0xF0, 0x7F, 0x7F, 0x06, 0x02, 0xF7]) in bytes(fake.received)
