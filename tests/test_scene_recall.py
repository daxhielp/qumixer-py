import pytest

from qumidi.client_async import AsyncQuMixer
from qumidi.errors import ParameterRangeError
from qumidi.protocol.channels import Input
from qumidi.state import Freshness

SCENE1 = {(0x25, 0x17, 0x07): 0x62}
SCENE12 = {(0x25, 0x17, 0x07): 0x30}


async def test_recall_sends_bank_select_immediately_before_program_change(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # PRD 7.2: the Qu ignores program changes if Bank 1 is not currently
    # selected, so always send the bank select immediately before.
    fake = await fake_qu_factory(scene=SCENE1)
    async with AsyncQuMixer("127.0.0.1", port=fake.port, scene_settle_delay=0.01) as mixer:
        await mixer.wait_synced()
        fake.received.clear()
        await mixer.recall_scene(12)
    assert bytes([0xB0, 0x00, 0x00, 0xB0, 0x20, 0x00, 0xC0, 0x0B]) in bytes(fake.received)


async def test_the_api_is_one_based(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # PRD 7.2: recall_scene(1) sends SS = 00, matching the console's display.
    fake = await fake_qu_factory(scene=SCENE1)
    async with AsyncQuMixer("127.0.0.1", port=fake.port, scene_settle_delay=0.01) as mixer:
        await mixer.wait_synced()
        fake.received.clear()
        await mixer.recall_scene(1)
    assert bytes([0xC0, 0x00]) in bytes(fake.received)


@pytest.mark.parametrize("bad", [0, -1, 101, 1000])
async def test_out_of_range_scenes_are_rejected(fake_qu_factory, bad) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(scene=SCENE1)
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        with pytest.raises(ParameterRangeError) as excinfo:
            await mixer.recall_scene(bad)
        assert str(bad) in str(excinfo.value)


async def test_recall_uses_the_consoles_midi_channel(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(scene=SCENE1, midi_channel=5)
    async with AsyncQuMixer("127.0.0.1", port=fake.port, scene_settle_delay=0.01) as mixer:
        await mixer.wait_synced()
        fake.received.clear()
        await mixer.recall_scene(12)
    assert bytes([0xB5, 0x00, 0x00, 0xB5, 0x20, 0x00, 0xC5, 0x0B]) in bytes(fake.received)


async def test_recall_marks_the_cache_stale_then_rebuilds_it(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # PRD 16.12.
    fake = await fake_qu_factory(scene=SCENE1, scenes={12: SCENE12})
    freshness_during: list[Freshness] = []
    async with AsyncQuMixer("127.0.0.1", port=fake.port, scene_settle_delay=0.05) as mixer:
        await mixer.wait_synced()
        assert mixer.get(Input(6), "fader").raw == 0x62
        mixer.on_scene_recalled(
            lambda n: freshness_during.append(mixer.get(Input(6), "fader").freshness)
        )
        await mixer.recall_scene(12)
        assert mixer.get(Input(6), "fader").raw == 0x30
        assert mixer.get(Input(6), "fader").freshness is Freshness.KNOWN
    assert freshness_during == [Freshness.STALE]


async def test_scene_recalled_precedes_sync_complete(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # PRD 16.12: in that order.
    fake = await fake_qu_factory(scene=SCENE1, scenes={12: SCENE12})
    order: list[str] = []
    async with AsyncQuMixer("127.0.0.1", port=fake.port, scene_settle_delay=0.02) as mixer:
        await mixer.wait_synced()
        mixer.on_scene_recalled(lambda n: order.append(f"recalled:{n}"))
        mixer.on_sync_complete(lambda: order.append("synced"))
        await mixer.recall_scene(12)
    assert order[:2] == ["recalled:12", "synced"]


async def test_names_are_re_swept_after_a_recall(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # PRD 7.2: channel names are scene data and will have changed.
    fake = await fake_qu_factory(scene=SCENE1, names={0x25: "Kick"},
                                 scene_names={12: {0x25: "Snare"}})
    async with AsyncQuMixer("127.0.0.1", port=fake.port, scene_settle_delay=0.02) as mixer:
        await mixer.wait_synced()
        assert mixer.name(Input(6)) == "Kick"
        await mixer.recall_scene(12)
        assert mixer.name(Input(6)) == "Snare"


async def test_sync_complete_fires_after_the_post_recall_name_sweep(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # Slice 7 acceptance (PRD 16.12): stale, SceneRecalled, resync, name
    # re-sweep, and finally SyncComplete -- in that order. The re-sweep must
    # already be done by the time SyncComplete fires, not just the burst.
    fake = await fake_qu_factory(scene=SCENE1, names={0x25: "Kick"},
                                 scene_names={12: {0x25: "Snare"}})
    name_when_synced: list[str | None] = []
    async with AsyncQuMixer("127.0.0.1", port=fake.port, scene_settle_delay=0.02) as mixer:
        await mixer.wait_synced()
        mixer.on_sync_complete(lambda: name_when_synced.append(mixer.name(Input(6))))
        await mixer.recall_scene(12)
    assert name_when_synced == ["Snare"]


async def test_resync_false_skips_the_rebuild(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(scene=SCENE1, scenes={12: SCENE12})
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        before = fake.sync_count
        await mixer.recall_scene(12, resync=False)
        assert fake.sync_count == before
        # The cache is still invalidated -- we know it is wrong even if we
        # have chosen not to rebuild it.
        assert mixer.get(Input(6), "fader").freshness is Freshness.STALE


async def test_last_recalled_scene_reflects_only_this_clients_recalls(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # PRD 7.2: there is no way to read the current scene number.
    fake = await fake_qu_factory(scene=SCENE1)
    async with AsyncQuMixer("127.0.0.1", port=fake.port, scene_settle_delay=0.01) as mixer:
        await mixer.wait_synced()
        assert mixer.last_recalled_scene is None
        await mixer.recall_scene(12)
        assert mixer.last_recalled_scene == 12
    assert "this client" in (type(mixer).last_recalled_scene.__doc__ or "")
