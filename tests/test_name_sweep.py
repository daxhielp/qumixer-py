import asyncio

import pytest

from qumixer_py.client_async import AsyncQuMixer
from qumixer_py.errors import ParameterRangeError
from qumixer_py.protocol.channels import Dca, Input, Mix


async def test_names_arrive_after_sync(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(names={0x25: "Kick", 0x67: "MainLR"})
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        assert mixer.name(Input(6)) == "Kick"
        assert mixer.name(Mix.LR) == "MainLR"


async def test_a_channel_with_no_name_is_none_not_a_sentinel_string(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # a channel can legitimately be *named* "UNKNOWN".
    fake = await fake_qu_factory(names={0x25: "Kick"}, name_silent={0x26})
    async with AsyncQuMixer(
        "127.0.0.1", port=fake.port, name_request_timeout=0.05, name_request_retries=1
    ) as mixer:
        await mixer.wait_synced()
        assert mixer.name(Input(7)) is None


async def test_the_sweep_is_paced(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # do not fire 100 sysex requests at once.
    fake = await fake_qu_factory(model="qu16", names={})
    started = asyncio.get_running_loop().time()
    async with AsyncQuMixer(
        "127.0.0.1", port=fake.port, name_sweep_interval=0.01
    ) as mixer:
        await mixer.wait_synced()
    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed >= 0.1


async def test_the_sweep_covers_only_channels_the_model_has(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(model="qu16", names={})
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        requested = fake.name_requests
    assert 0x2F in requested          # Input 16
    assert 0x30 not in requested      # Input 17 does not exist on a Qu-16
    assert 0x68 not in requested      # no groups on a Qu-16


async def test_the_sweep_skips_classes_that_have_no_names(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # driven by an explicit NAMEABLE_CLASSES set.
    fake = await fake_qu_factory(names={})
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
    assert 0x50 not in fake.name_requests   # mute group masters


async def test_sync_names_false_skips_the_sweep_entirely(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(names={0x25: "Kick"})
    async with AsyncQuMixer("127.0.0.1", port=fake.port, sync_names=False) as mixer:
        await mixer.wait_synced()
        assert mixer.name(Input(6)) is None
        assert fake.name_requests == []


async def test_set_name_round_trips_and_is_verified(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # a 03 name-set is not acknowledged; confirm with a 01 get.
    fake = await fake_qu_factory(names={0x25: "Kick"})
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        fake.name_requests.clear()
        await mixer.set_name(Input(6), "Snare")
        assert fake.names[0x25] == "Snare"
        assert mixer.name(Input(6)) == "Snare"
        assert 0x25 in fake.name_requests  # the verifying get


async def test_set_name_reports_a_console_that_did_not_take_it(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(names={0x25: "Kick"}, ignore_name_sets=True)
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        with pytest.raises(ParameterRangeError) as excinfo:
            await mixer.set_name(Input(6), "Snare")
        assert "Snare" in str(excinfo.value)


async def test_set_name_on_a_dca_works(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(names={})
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        await mixer.set_name(Dca(1), "Drums")
        assert fake.names[0x10] == "Drums"


async def test_one_slow_name_does_not_abort_the_sweep(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(names={0x25: "Kick", 0x27: "Tom"}, name_silent={0x26})
    async with AsyncQuMixer(
        "127.0.0.1", port=fake.port, name_request_timeout=0.05, name_request_retries=1
    ) as mixer:
        await mixer.wait_synced()
        assert mixer.name(Input(6)) == "Kick"
        assert mixer.name(Input(7)) is None
        assert mixer.name(Input(8)) == "Tom"
