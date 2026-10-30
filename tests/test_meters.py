import asyncio

import pytest

from qumixer_py.client_async import AsyncQuMixer
from qumixer_py.errors import NotSyncedError
from qumixer_py.protocol.meters import LAYOUTS, Meter, MeterSlot


def frame_for(model: str, slot: MeterSlot, raw_u16: int) -> bytes:
    layout = LAYOUTS[model]
    payload = bytearray(layout.payload_bytes)
    offset = layout.offset_of(slot) * 2
    payload[offset : offset + 2] = raw_u16.to_bytes(2, "big")
    return bytes(payload)


async def test_meters_are_off_by_default(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory()
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        assert mixer.meters.enable_count == 0
        assert fake.meters_enabled is False


async def test_enable_before_sync_raises(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # the model determines the frame layout.
    mixer = AsyncQuMixer("127.0.0.1", port=1)
    with pytest.raises(NotSyncedError):
        await mixer.meters.enable()


async def test_the_context_manager_enables_and_disables(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory()
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        async with mixer.meters.enabled():
            await asyncio.sleep(0.05)
            assert fake.meters_enabled is True
        await asyncio.sleep(0.05)
        assert fake.meters_enabled is False


async def test_the_context_manager_disables_on_an_exception_path(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory()
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        with pytest.raises(RuntimeError):
            async with mixer.meters.enabled():
                raise RuntimeError("boom")
        await asyncio.sleep(0.05)
        assert fake.meters_enabled is False


async def test_nested_enables_use_a_count_not_a_flag(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # two overlapping context managers must not disable metering
    # when the inner one exits.
    fake = await fake_qu_factory()
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        async with mixer.meters.enabled():
            async with mixer.meters.enabled():
                assert mixer.meters.enable_count == 2
            await asyncio.sleep(0.05)
            assert fake.meters_enabled is True
        await asyncio.sleep(0.05)
        assert fake.meters_enabled is False


async def test_a_frame_decodes_to_the_expected_db(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    slot = MeterSlot.input(6, Meter.POST_PREAMP)
    fake = await fake_qu_factory(
        model="qu32", meter_frames=[frame_for("qu32", slot, 0x7C80)], meter_rate=50.0
    )
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        async with mixer.meters.enabled():
            frame = await asyncio.wait_for(mixer.meters.stream().__anext__(), timeout=2.0)
            assert frame.db(slot) == pytest.approx(-3.5)


async def test_a_wrong_size_frame_disables_metering_and_delivers_nothing(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # a silently mis-indexed meter reads a plausible dB value from
    # the wrong channel, which is worse than no meter at all.
    fake = await fake_qu_factory(model="qu32", meter_frames=[bytes(100)], meter_rate=50.0)
    errors: list[str] = []
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        mixer.events.on_meter_layout_error(errors.append)
        await mixer.meters.enable()
        await asyncio.sleep(0.3)
        assert errors
        assert "100" in errors[0] and "1580" in errors[0]
        assert mixer.meters.enable_count == 0
        assert fake.meters_enabled is False


async def test_an_odd_payload_is_counted_and_dropped(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(model="qu32", meter_frames=[bytes(1579)], meter_rate=50.0)
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        await mixer.meters.enable()
        await asyncio.sleep(0.3)
        assert mixer.meters.malformed_frames >= 1


async def test_slow_consumers_drop_old_frames_rather_than_growing(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # meters are a sampled signal; a stale frame has no value.
    slot = MeterSlot.input(1, Meter.POST_PREAMP)
    frames = [frame_for("qu32", slot, 0x8000 + i) for i in range(50)]
    fake = await fake_qu_factory(model="qu32", meter_frames=frames, meter_rate=500.0)
    async with AsyncQuMixer("127.0.0.1", port=fake.port, meter_queue_size=4) as mixer:
        await mixer.wait_synced()
        await mixer.meters.enable()
        await asyncio.sleep(0.4)
        assert mixer.meters.dropped_frames > 0
        await mixer.meters.disable()


async def test_set_layout_lets_a_user_correct_an_untested_console(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # a user with a console we have never tested can fix their own
    # installation without waiting for a release.
    from qumixer_py.protocol.meters import QU24_ALTERNATE

    slot = MeterSlot.input(6, Meter.POST_PREAMP)
    payload = bytearray(QU24_ALTERNATE.payload_bytes)
    offset = QU24_ALTERNATE.offset_of(slot) * 2
    payload[offset : offset + 2] = (0x7C80).to_bytes(2, "big")
    fake = await fake_qu_factory(model="qu24", meter_frames=[bytes(payload)], meter_rate=50.0)
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        mixer.meters.set_layout(QU24_ALTERNATE)
        async with mixer.meters.enabled():
            frame = await asyncio.wait_for(mixer.meters.stream().__anext__(), timeout=2.0)
            assert frame.db(slot) == pytest.approx(-3.5)


async def test_metering_is_re_enabled_after_a_reconnect(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # if metering was enabled before the drop, re-enable after sync.
    slot = MeterSlot.input(1, Meter.POST_PREAMP)
    fake = await fake_qu_factory(
        model="qu32", meter_frames=[frame_for("qu32", slot, 0x8000)], meter_rate=50.0
    )
    async with AsyncQuMixer(
        "127.0.0.1", port=fake.port, reconnect_backoff_min=0.05
    ) as mixer:
        await mixer.wait_synced()
        await mixer.meters.enable()
        await asyncio.sleep(0.1)
        await fake.drop_connection()
        for _ in range(60):
            await asyncio.sleep(0.05)
            if fake.meters_enabled:
                break
        assert fake.meters_enabled is True
        await mixer.meters.disable()
