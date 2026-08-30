import json

import pytest

from qumidi.client_async import AsyncQuMixer
from qumidi.protocol.channels import Input
from qumidi.snapshot import load_snapshot

SCENE = {(0x25, 0x17, 0x07): 0x62, (0x25, 0x20, 0x02): 0x50}


async def test_snapshot_is_json_serialisable_and_carries_identity(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(scene=SCENE, names={0x25: "Kick"})
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        snap = mixer.snapshot()
    text = json.dumps(snap)
    assert json.loads(text) == snap
    assert snap["box_id"] == 3
    assert snap["model"] == "QU32"
    assert snap["firmware"] == [1, 9]
    assert snap["names"]["37"] == "Kick"     # 0x25


async def test_snapshot_reports_a_currently_unnamed_channel_as_null_not_omitted(  # type: ignore[no-untyped-def]
    fake_qu_factory,
) -> None:
    # PRD 9.4/10.5: recall_scene() resets every previously-swept name back to
    # None until the post-recall sweep repopulates it. A snapshot taken in
    # that window must still report the channel, with a null name, rather
    # than silently drop the key -- otherwise "not yet re-swept" and
    # "channel absent from this snapshot" become indistinguishable.
    fake = await fake_qu_factory(scene=SCENE, names={0x25: "Kick"})
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        assert mixer.names[Input(6)] == "Kick"  # 0x25, sanity check before the recall
        await mixer.recall_scene(1, resync=False)
        snap = mixer.snapshot()
    assert "37" in snap["names"]  # 0x25
    assert snap["names"]["37"] is None


async def test_snapshot_includes_unconfirmed_and_known_entries_with_their_freshness(  # type: ignore[no-untyped-def]
    fake_qu_factory,
) -> None:
    # PRD 10.5: an UNCONFIRMED write must not be silently dropped or reported
    # as if it were KNOWN -- a snapshot that hid it would be indistinguishable
    # from one where the console actually confirmed the write. Note this is
    # not a test of UNKNOWN coverage: StateCache never materializes a row for
    # a parameter that was simply never observed (see build_snapshot()'s own
    # docstring), so a truly-unknown parameter is absent from snap["values"]
    # rather than present with freshness "UNKNOWN" -- there is nothing to
    # assert about that case here.
    fake = await fake_qu_factory(scene=SCENE, echo=False)
    async with AsyncQuMixer("127.0.0.1", port=fake.port, echo_window=0.05) as mixer:
        await mixer.wait_synced()
        await mixer.set(Input(6), "pafl", raw=0x01)
        import asyncio
        await asyncio.sleep(0.2)
        snap = mixer.snapshot()
    freshnesses = {entry["freshness"] for entry in snap["values"]}
    assert "UNCONFIRMED" in freshnesses
    assert "KNOWN" in freshnesses


async def test_export_and_load_round_trip(fake_qu_factory, tmp_path) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(scene=SCENE)
    path = tmp_path / "snap.json"
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        mixer.export_snapshot(path)
        loaded = load_snapshot(path)
        live = mixer.snapshot()
        # "age" is derived from time.monotonic() at the instant of each call,
        # so it legitimately advances between export_snapshot() and this
        # second, separate snapshot() call -- compare it with a tolerance
        # rather than expecting bit-for-bit equality with a moving clock.
        assert [v["age"] for v in loaded["values"]] == pytest.approx(
            [v["age"] for v in live["values"]], abs=0.05
        )

        def without_age(snap):  # type: ignore[no-untyped-def]
            return {
                **{k: v for k, v in snap.items() if k != "values"},
                "values": [
                    {k: v for k, v in entry.items() if k != "age"} for entry in snap["values"]
                ],
            }

        assert without_age(loaded) == without_age(live)


async def test_diff_reports_changed_and_missing_values(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(scene=SCENE)
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        snap = mixer.snapshot()
        await mixer.set(Input(6), "fader", raw=0x30)
        diffs = mixer.diff(snap)
    assert len(diffs) == 1
    d = diffs[0]
    assert d.channel == Input(6)
    assert d.param == "fader"
    assert (d.snapshot_raw, d.current_raw) == (0x62, 0x30)


async def test_diff_of_an_identical_snapshot_is_empty(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(scene=SCENE)
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        assert mixer.diff(mixer.snapshot()) == []


async def test_there_is_no_blind_restore(fake_qu_factory) -> None:
    # PRD 10.5: deliberately excluded. If this ever appears it must be paced,
    # resumable and dry-runnable -- see the section before adding it.
    assert not hasattr(AsyncQuMixer, "restore")
    assert not hasattr(AsyncQuMixer, "apply_snapshot")
