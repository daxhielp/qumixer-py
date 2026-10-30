import asyncio

import pytest

from qumixer_py.client_async import AsyncQuMixer
from qumixer_py.errors import QuConnectionLostError
from qumixer_py.protocol.channels import Input
from qumixer_py.state import Freshness
from qumixer_py.transport.base import ConnectionState

SCENE = {(0x25, 0x17, 0x07): 0x62}


async def test_a_drop_marks_the_whole_cache_stale(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # never serve pre-disconnect values as current.
    fake = await fake_qu_factory(scene=SCENE)
    seen_stale = asyncio.Event()
    async with AsyncQuMixer(
        "127.0.0.1", port=fake.port, reconnect_backoff_min=0.05
    ) as mixer:
        await mixer.wait_synced()
        def on_state(state: ConnectionState) -> None:
            if state is ConnectionState.RECONNECTING:
                assert mixer.get(Input(6), "fader").freshness is Freshness.STALE
                seen_stale.set()
        mixer.on_connection_state(on_state)
        await fake.drop_connection()
        await asyncio.wait_for(seen_stale.wait(), timeout=2.0)


async def test_reconnect_re_syncs_and_returns_to_ready(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(scene=SCENE)
    async with AsyncQuMixer(
        "127.0.0.1", port=fake.port, reconnect_backoff_min=0.05
    ) as mixer:
        await mixer.wait_synced()
        before = fake.sync_count
        await fake.drop_connection()
        for _ in range(60):
            await asyncio.sleep(0.05)
            if mixer.state is ConnectionState.READY and fake.sync_count > before:
                break
        assert mixer.state is ConnectionState.READY
        assert mixer.get(Input(6), "fader").freshness is Freshness.KNOWN


async def test_pending_expectations_are_dropped_on_disconnect(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # leaving expectations live causes the first post-reconnect
    # surface move to be misclassified as an echo and swallowed.
    from qumixer_py.events import Source

    fake = await fake_qu_factory(scene=SCENE, echo=False)
    external: list[object] = []
    async with AsyncQuMixer(
        "127.0.0.1", port=fake.port, echo_window=5.0, reconnect_backoff_min=0.05
    ) as mixer:
        await mixer.wait_synced()
        await mixer.set(Input(6), "fader", raw=0x40)
        await fake.drop_connection()
        for _ in range(60):
            await asyncio.sleep(0.05)
            if mixer.state is ConnectionState.READY:
                break
        mixer.on_change(lambda e: external.append(e) if e.source is Source.MIXER else None)
        await fake.send_surface_move(0x25, 0x17, 0x40, 0x07)
        await asyncio.sleep(0.15)
    assert len(external) == 1


async def test_box_id_and_channel_are_relearned_not_cached(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # the operator may have changed the console's MIDI channel, or
    # the host may now be a different console at the same IP.
    fake = await fake_qu_factory(scene=SCENE, midi_channel=0)
    async with AsyncQuMixer(
        "127.0.0.1", port=fake.port, reconnect_backoff_min=0.05
    ) as mixer:
        await mixer.wait_synced()
        assert mixer.midi_channel == 0
        fake.midi_channel = 5
        await fake.drop_connection()
        for _ in range(60):
            await asyncio.sleep(0.05)
            if mixer.midi_channel == 5:
                break
        assert mixer.midi_channel == 5


async def test_a_different_box_id_rebuilds_the_cache_from_empty(fake_qu_factory, caplog) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(scene=SCENE, model="qu32")
    async with AsyncQuMixer(
        "127.0.0.1", port=fake.port, reconnect_backoff_min=0.05
    ) as mixer:
        await mixer.wait_synced()
        fake.box_id = 1  # a Qu-16 has appeared at this address
        fake._scene = {}
        before = fake.sync_count
        await fake.drop_connection()
        # A changed BoxID makes _after_reconnect() run _sync() *twice*: once
        # to learn the new identity, then again (after clearing the cache)
        # to rebuild it. Polling on mixer.box_id == 1 alone is not enough --
        # that flips true right after the *first* sync's `11` reply, well
        # before the cache-rebuilding second sync has even started, and was
        # observed to make this assertion flaky against the stale
        # pre-disconnect value. Waiting for two full sync cycles (matching
        # fake.sync_count, which increments once per state-request) and a
        # final READY together only becomes true once the rebuild is done.
        for _ in range(60):
            await asyncio.sleep(0.05)
            if mixer.state is ConnectionState.READY and fake.sync_count >= before + 2:
                break
        assert mixer.state is ConnectionState.READY
        assert fake.sync_count >= before + 2
        assert mixer.get(Input(6), "fader").raw is None
    assert any("boxid" in r.message.lower() for r in caplog.records)


async def test_a_write_after_an_unrecoverable_drop_raises(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory(scene=SCENE)
    mixer = AsyncQuMixer(
        "127.0.0.1", port=fake.port, auto_reconnect=False
    )
    await mixer.connect()
    await mixer.wait_synced()
    await fake.stop()
    await asyncio.sleep(0.2)
    with pytest.raises(QuConnectionLostError):
        await mixer.set(Input(6), "fader", raw=0x40)
    await mixer.close()


async def test_the_client_leaks_nothing_on_close(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # assert with asyncio.all_tasks() at teardown.
    fake = await fake_qu_factory(scene=SCENE)
    before = {t for t in asyncio.all_tasks()}
    async with AsyncQuMixer("127.0.0.1", port=fake.port) as mixer:
        await mixer.wait_synced()
        await mixer.set(Input(6), "fader", raw=0x40)
    await asyncio.sleep(0.1)
    leaked = {t for t in asyncio.all_tasks()} - before
    assert not [t for t in leaked if not t.done()], leaked


async def test_set_during_post_reconnect_sync_does_not_race(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    # _await_channel unblocks a set() as soon as
    # the `11` reply sets _midi_channel, which happens early in _sync()'s
    # burst -- well before _syncing goes back to False at the `14` marker.
    # _after_reconnect() calls _sync() automatically and unprompted, so any
    # set() that happens to land in that window is at risk of either being
    # silently overwritten by a stale Source.SYNC snapshot value, or having
    # its own echo mis-tagged as sync traffic and never consumed -- leaving
    # the expectation to expire as a spurious UNCONFIRMED. sync_delay widens
    # the window between the `11` reply and the burst so the race is
    # reliably reachable rather than depending on incidental scheduling.
    fake = await fake_qu_factory(scene=dict(SCENE), sync_delay=0.3)
    async with AsyncQuMixer(
        "127.0.0.1", port=fake.port, reconnect_backoff_min=0.05
    ) as mixer:
        await mixer.wait_synced()
        unconfirmed: list[object] = []
        mixer.on_write_unconfirmed(lambda *a: unconfirmed.append(a))
        await fake.drop_connection()
        # Wait until the reconnect's automatic resync has relearned the MIDI
        # channel (the `11` reply) but is still mid-burst (sync_delay holds
        # it there for 0.3s).
        for _ in range(100):
            await asyncio.sleep(0.02)
            if mixer.midi_channel is not None and mixer._syncing:
                break
        assert mixer.midi_channel is not None and mixer._syncing, (
            "did not observe the mid-burst window; the race this test covers "
            "cannot be exercised"
        )
        await mixer.set(Input(6), "fader", raw=0x40)
        for _ in range(100):
            await asyncio.sleep(0.02)
            if mixer.state is ConnectionState.READY:
                break
        assert mixer.state is ConnectionState.READY
        # The write must win over the stale scene value (0x62) the burst
        # re-asserts for this same key.
        assert mixer.get(Input(6), "fader").raw == 0x40
        assert mixer.get(Input(6), "fader").freshness is Freshness.KNOWN
        # Poll for the expectation actually clearing (by genuine echo
        # consumption, or -- if the race were still open -- by expiring
        # unconfirmed) rather than a blind echo_window-shaped sleep, so this
        # is not itself timing-flaky under a loaded machine: it reacts to
        # whichever happens first instead of assuming either takes some
        # specific duration.
        key = mixer.cache.key(Input(6), "fader", None)
        for _ in range(200):
            await asyncio.sleep(0.05)
            if not mixer._expectations.get(key) or unconfirmed:
                break
    assert unconfirmed == []
