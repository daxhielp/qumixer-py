import asyncio

import pytest

from qumidi.client_async import AsyncQuMixer
from qumidi.protocol.channels import Input

SCENE = {(0x25, 0x17, 0x07): 0x62, (0x26, 0x17, 0x07): 0x40}


@pytest.mark.parametrize("split", [1, 2, 3, 5, 7, 13])
async def test_survives_writes_split_at_every_awkward_boundary(fake_qu_factory, split) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(scene=SCENE, split_writes=split)
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        assert mixer.get(Input(6), "fader").raw == 0x62
        assert mixer.get(Input(7), "fader").raw == 0x40


async def test_survives_active_sensing_inside_a_sysex(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(scene=SCENE, fe_inside_sysex=True)
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        assert mixer.box_id == 3


async def test_survives_active_sensing_between_every_byte(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # PRD 13.1: running-status NRPN bursts with FE inserted between every pair
    # of bytes. This is the case that breaks a naive NRPN decoder.
    fake = await fake_qu_factory(scene=SCENE, running_status=True, fe_between_every_byte=True)
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        assert mixer.get(Input(6), "fader").raw == 0x62
        assert mixer.get(Input(7), "fader").raw == 0x40


async def test_survives_a_running_status_burst(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(scene=SCENE, running_status=True)
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        assert mixer.get(Input(6), "fader").raw == 0x62


async def test_survives_a_malformed_sysex(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(scene=SCENE, malformed_sysex=True)
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        assert mixer.get(Input(6), "fader").raw == 0x62


async def test_a_second_state_reply_is_ignored(fake_qu_factory, caplog) -> None:  # type: ignore[no-untyped-def]
    # PRD 9.3.10: log a warning naming both BoxIDs and bind to the first.
    fake = await fake_qu_factory(scene=SCENE, duplicate_state_reply=True)
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        assert mixer.box_id == 3
    assert any("second" in r.message.lower() for r in caplog.records)


async def test_a_name_with_high_bytes_is_sanitised(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(scene=SCENE, names={0x25: "Kick"}, name_bytes_high=True)
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        name = mixer.name(Input(6))
        assert name is not None
        assert all(0x20 <= ord(ch) <= 0x7E for ch in name)


async def test_a_drop_mid_sync_is_survivable(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(scene=SCENE, drop_after_sync=True,
                                 reconnect_after_drops=1)
    async with AsyncQuMixer(
        "127.0.0.1", port=fake.port, reconnect_backoff_min=0.05
    ) as mixer:
        for _ in range(80):
            await asyncio.sleep(0.05)
            if mixer.get(Input(6), "fader").raw == 0x62:
                break
        assert mixer.get(Input(6), "fader").raw == 0x62


async def test_partial_sync_ok_keeps_what_arrived(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # PRD 9.3.9: keep whatever arrived, marked KNOWN, and proceed to READY
    # with a warning.
    from qumidi.state import Freshness

    fake = await fake_qu_factory(scene=SCENE, send_end_marker=False)
    async with AsyncQuMixer(
        "127.0.0.1", port=fake.port, sync_timeout=0.4, partial_sync_ok=True
    ) as mixer:
        await mixer.wait_synced()
        assert mixer.get(Input(6), "fader").freshness is Freshness.KNOWN
