import asyncio

import pytest

from qumixer_py.client_async import AsyncQuMixer
from qumixer_py.errors import (
    NotSyncedError,
    ParameterRangeError,
    UnknownParameterError,
    UnsupportedOnModelError,
)
from qumixer_py.protocol.channels import Group, Input, Mix


async def test_set_fader_by_raw_emits_the_golden_bytes(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory()
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        fake.received.clear()
        await mixer.set(Input(6), "fader", raw=0x62)
        await asyncio.sleep(0.05)
    cc = 0xB0 | fake.midi_channel
    assert bytes([cc, 0x63, 0x25, cc, 0x62, 0x17, cc, 0x06, 0x62, cc, 0x26, 0x07]) in bytes(
        fake.received
    )


async def test_set_fader_by_db(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory()
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        fake.received.clear()
        await mixer.set(Input(6), "fader", db=-10.0)
        await asyncio.sleep(0.05)
        assert fake.state[(0x25, 0x17, 0x07)] == 0x3F


async def test_set_send_level_takes_a_channel_id_as_vx(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory()
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        await mixer.set(Input(6), "send_level", raw=0x50, vx=Mix.MIX3)
        await asyncio.sleep(0.05)
        assert fake.state[(0x25, 0x20, 0x02)] == 0x50


async def test_set_rejects_a_value_outside_the_domain(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory()
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        with pytest.raises(ParameterRangeError) as excinfo:
            await mixer.set(Input(6), "pan", raw=0x7F, vx=Mix.LR)
        assert "Input 6" in str(excinfo.value)
        assert "pan" in str(excinfo.value)


async def test_mix_assign_to_lr_is_rejected(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # encode LR assignment exclusively as 18; 55/07 is rejected.
    fake = await fake_qu_factory()
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        with pytest.raises(ParameterRangeError) as excinfo:
            await mixer.set(Input(6), "mix_assign", raw=0x01, vx=Mix.LR)
        assert "lr_assign" in str(excinfo.value)


async def test_send_to_a_group_is_rejected_on_qu16(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(model="qu16")
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        with pytest.raises(UnsupportedOnModelError):
            await mixer.set(Input(6), "send_level", raw=0x50, vx=Group(1))


async def test_set_an_unknown_parameter_raises(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory()
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        with pytest.raises(UnknownParameterError):
            await mixer.set(Input(6), "volume", raw=0x62)


async def test_set_requires_exactly_one_of_raw_and_db(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory()
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        with pytest.raises(ValueError):
            await mixer.set(Input(6), "fader")
        with pytest.raises(ValueError):
            await mixer.set(Input(6), "fader", raw=0x62, db=0.0)


async def test_db_on_a_parameter_with_no_taper_raises(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory()
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        with pytest.raises(ParameterRangeError):
            await mixer.set(Input(6), "lr_assign", db=0.0)


async def test_set_before_sync_raises_not_synced() -> None:
    mixer = AsyncQuMixer("127.0.0.1", port=1)
    with pytest.raises(NotSyncedError):
        await mixer.set(Input(6), "fader", raw=0x62)


async def test_the_state_burst_arrives_before_the_end_marker(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(scene={(0x25, 0x17, 0x07): 0x4F})
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        # A later test asserts on the cache; for now, just prove the
        # burst is emitted and parsed without breaking the sync handshake.
        assert mixer.state.name == "READY"
