import asyncio

import pytest

import qumidi.client_async as client_async_module
from qumidi.client_async import AsyncQuMixer
from qumidi.errors import ValueUnknownError
from qumidi.events import Source
from qumidi.protocol.channels import ChannelClass, Input, Mix
from qumidi.protocol.params import Fixed, FlaggedIndex, ParamSpec
from qumidi.protocol.registry import REGISTRY, Registry
from qumidi.state import Freshness

SCENE = {
    (0x25, 0x17, 0x07): 0x62,   # Input 6 fader, 0 dB
    (0x25, 0x20, 0x02): 0x50,   # Input 6 send to Mix 3
    (0x67, 0x17, 0x07): 0x7F,   # LR fader
}


async def test_the_state_burst_populates_the_cache(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(scene=SCENE)
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        assert mixer.get(Input(6), "fader").raw == 0x62
        assert mixer.get(Input(6), "fader").freshness is Freshness.KNOWN
        assert mixer.get(Input(6), "send_level", 0x02).raw == 0x50
        assert mixer.get(Mix.LR, "fader").raw == 0x7F


async def test_the_last_parameter_of_the_burst_is_not_lost(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # PRD 9.3.7: flush the NRPN decoder's held update when 14 arrives, or the
    # cache is silently one value short after every sync.
    fake = await fake_qu_factory(scene=SCENE, drop_last_vx=True)
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        assert mixer.get(Mix.LR, "fader").raw == 0x7F


async def test_sync_emits_one_sync_complete_and_no_external_changes(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # PRD 9.3.4: do not emit external-change events for the burst; emit a
    # single SyncComplete at the end.
    fake = await fake_qu_factory(scene=SCENE)
    external: list[object] = []
    syncs: list[bool] = []
    mixer = AsyncQuMixer("127.0.0.1", port=fake.port)
    mixer.on_change(lambda e: external.append(e) if e.source is Source.MIXER else None)
    mixer.on_sync_complete(lambda: syncs.append(True))
    async with mixer:
        await mixer.wait_synced()
    assert external == []
    assert syncs == [True]


async def test_sync_complete_fires_after_the_name_sweep(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # PRD 9.4/9.3.8: SyncComplete means "everything -- values and names -- is
    # trustworthy again", so it must fire only once the name sweep has
    # already populated mixer.name(...), not at the wire-level end-of-sync
    # marker (which lands before the sweep even starts).
    fake = await fake_qu_factory(scene=SCENE, names={0x25: "Kick"})
    name_when_synced: list[str | None] = []
    mixer = AsyncQuMixer("127.0.0.1", port=fake.port)
    mixer.on_sync_complete(lambda: name_when_synced.append(mixer.name(Input(6))))
    async with mixer:
        await mixer.wait_synced()
    assert name_when_synced == ["Kick"]


async def test_sync_sourced_changes_are_still_visible_to_subscribers(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(scene=SCENE)
    sourced: list[Source] = []
    mixer = AsyncQuMixer("127.0.0.1", port=fake.port)
    mixer.on_change(lambda e: sourced.append(e.source))
    async with mixer:
        await mixer.wait_synced()
    assert sourced and set(sourced) == {Source.SYNC}


async def test_a_surface_move_after_sync_fires_an_external_change(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # PRD 16.3.
    fake = await fake_qu_factory(scene=SCENE)
    seen: list[object] = []
    mixer = AsyncQuMixer("127.0.0.1", port=fake.port)
    async with mixer:
        await mixer.wait_synced()
        mixer.on_change(lambda e: seen.append(e) if e.source is Source.MIXER else None)
        await fake.send_surface_move(0x25, 0x17, 0x40, 0x07)
        await asyncio.sleep(0.1)
    assert len(seen) == 1
    assert mixer.get(Input(6), "fader").raw == 0x40


async def test_an_unknown_parameter_id_does_not_break_the_read_loop(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # PRD 5.3: an unrecognised (CH, ID) must not raise. Undocumented
    # parameters exist.
    fake = await fake_qu_factory(scene=SCENE)
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        await fake.send_surface_move(0x25, 0x7E, 0x01, 0x07)
        await asyncio.sleep(0.05)
        await fake.send_surface_move(0x25, 0x17, 0x30, 0x07)
        await asyncio.sleep(0.05)
        assert mixer.get(Input(6), "fader").raw == 0x30


async def test_refresh_is_documented_as_a_full_resync(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # PRD 10.2: no targeted get exists in the protocol.
    fake = await fake_qu_factory(scene=SCENE)
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        before = fake.sync_count
        await mixer.refresh(Input(6), "fader")
        assert fake.sync_count == before + 1
    assert "resync" in (AsyncQuMixer.refresh.__doc__ or "").lower()


async def test_reading_an_unknown_value_returns_unknown_not_a_number(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(scene={})
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        assert mixer.get(Input(6), "fader").freshness is Freshness.UNKNOWN
        assert mixer.get(Input(6), "fader").raw is None


async def test_membership_of_a_partially_known_dca_set_raises(fake_qu_factory, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # NOTE: dca_assign is not yet a REGISTRY row -- task 3.4 deliberately
    # seeded the registry with the level/pan/routing family only and left
    # DCA/mute-group assign for Slice 5 (see task-3.4-brief.md). Rather than
    # add a production registry entry out of scope for this task (an
    # unverified NRPN transcription with no golden-vector coverage), this
    # test wires in a local, test-only FlaggedIndex spec so it still
    # exercises the real mixer.membership() -> REGISTRY.lookup ->
    # StateCache.membership() path end to end.
    dca_spec = ParamSpec(
        name="dca_assign",
        nrpn_id=0x40,
        vx=Fixed(0x07),
        domain=FlaggedIndex(on_flag=0x40, count=4),
        applies_to=frozenset({ChannelClass.INPUT}),
    )
    monkeypatch.setattr(client_async_module, "REGISTRY", Registry([*REGISTRY, dca_spec]))
    fake = await fake_qu_factory(scene={(0x25, 0x40, 0x07): 0x40})
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        with pytest.raises(ValueUnknownError):
            mixer.membership(Input(6), "dca_assign").require()


async def test_an_inbound_5507_normalises_onto_the_lr_assign_key(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # PRD 14.8: 55/07 is a syntactically valid second encoding for LR assign.
    # set() refuses to emit it, but the console may still speak it (which of
    # the two it uses is an M6 open question), so an inbound 55/07 must land on
    # the same cache key that an inbound 18/07 does -- not in a parallel
    # mix_assign slot that nothing reads.
    fake = await fake_qu_factory(scene={})
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        await fake.send_surface_move(0x25, 0x55, 0x01, 0x07)
        await asyncio.sleep(0.05)
        assert mixer.get(Input(6), "lr_assign").raw == 0x01
        assert mixer.get(Input(6), "mix_assign", 0x07).freshness is Freshness.UNKNOWN


async def test_an_inbound_mix_assign_to_a_real_mix_is_left_alone(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # The 14.8 normalisation must be surgical: only VX 07 is the LR alias.
    fake = await fake_qu_factory(scene={})
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        await fake.send_surface_move(0x25, 0x55, 0x01, 0x02)  # Mix 3
        await asyncio.sleep(0.05)
        assert mixer.get(Input(6), "mix_assign", 0x02).raw == 0x01
        assert mixer.get(Input(6), "lr_assign").freshness is Freshness.UNKNOWN
