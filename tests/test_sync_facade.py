import threading
import time

import pytest

from qumidi.client_sync import QuMixer
from qumidi.errors import ParameterRangeError
from qumidi.protocol.channels import Input, Mix

SCENE = {(0x25, 0x17, 0x07): 0x62}


def test_context_manager_and_basic_read(fake_qu_sync) -> None:  # type: ignore[no-untyped-def]
    with QuMixer("127.0.0.1", port=fake_qu_sync.port) as mixer:
        mixer.wait_synced()
        assert mixer.get(Input(6), "fader").raw == 0x62
        assert mixer.input(6).fader.raw == 0x62


def test_property_setter_blocks_until_flushed(fake_qu_sync) -> None:  # type: ignore[no-untyped-def]
    # PRD 11.2: the setter blocks until the write is flushed, the same
    # guarantee as await in the async API. It does not return early.
    with QuMixer("127.0.0.1", port=fake_qu_sync.port) as mixer:
        mixer.wait_synced()
        mixer.input(6).fader.db = -10.0
        # Matches tests/test_set_param.py's own pattern: the setter blocks
        # only until the write is drained from *this* client's socket, not
        # until FakeQu -- on its own separate loop thread here, not the same
        # loop as the async suite -- has read and applied it.
        time.sleep(0.05)
        assert fake_qu_sync.state[(0x25, 0x17, 0x07)] == 0x3F


def test_property_setter_raises_the_same_exceptions(fake_qu_sync) -> None:  # type: ignore[no-untyped-def]
    with QuMixer("127.0.0.1", port=fake_qu_sync.port) as mixer:
        mixer.wait_synced()
        with pytest.raises(ParameterRangeError):
            mixer.input(6).send(Mix.LR).pan.raw = 0x7F


def test_explicit_set_methods_exist_alongside_the_properties(fake_qu_sync) -> None:  # type: ignore[no-untyped-def]
    with QuMixer("127.0.0.1", port=fake_qu_sync.port) as mixer:
        mixer.wait_synced()
        mixer.input(6).fader.set_db(-6.0)
        mixer.input(6).mute(True)
        # Matches tests/test_mute.py's own pattern: mute()/set_db() block only
        # until the write is drained from *this* client's socket, not until
        # FakeQu -- here running on its own separate loop thread, not the
        # same loop as the async suite -- has read and applied it.
        time.sleep(0.05)
        assert fake_qu_sync.mutes[0x25] is True


def test_the_two_apis_expose_the_same_method_names() -> None:
    # PRD 11.2: the sync facade is the async one minus await, generated from
    # the same registry, never a hand-maintained parallel surface.
    from qumidi.client_async import AsyncQuMixer

    async_names = {n for n in dir(AsyncQuMixer) if not n.startswith("_")}
    sync_names = {n for n in dir(QuMixer) if not n.startswith("_")}
    missing = async_names - sync_names
    assert not missing, f"the sync facade is missing: {sorted(missing)}"


def test_usable_from_multiple_threads(fake_qu_sync) -> None:  # type: ignore[no-untyped-def]
    with QuMixer("127.0.0.1", port=fake_qu_sync.port) as mixer:
        mixer.wait_synced()
        errors: list[BaseException] = []

        def worker(n: int) -> None:
            try:
                for _ in range(5):
                    mixer.input(n).fader.set_raw(0x40)
                    mixer.get(Input(n), "fader")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(1, 5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert not errors


def test_close_joins_the_loop_thread(fake_qu_sync) -> None:  # type: ignore[no-untyped-def]
    before = threading.active_count()
    mixer = QuMixer("127.0.0.1", port=fake_qu_sync.port)
    mixer.connect()
    mixer.wait_synced()
    mixer.close()
    time.sleep(0.2)
    assert threading.active_count() <= before


def test_close_is_idempotent(fake_qu_sync) -> None:  # type: ignore[no-untyped-def]
    mixer = QuMixer("127.0.0.1", port=fake_qu_sync.port)
    mixer.connect()
    mixer.close()
    mixer.close()


def test_thread_safe_callbacks_are_queued_for_the_caller(fake_qu_sync) -> None:  # type: ignore[no-untyped-def]
    with QuMixer("127.0.0.1", port=fake_qu_sync.port, thread_safe=True) as mixer:
        mixer.wait_synced()
        mixer.on_change(lambda e: None)
        fake_qu_sync.send_surface_move_sync(0x25, 0x17, 0x40, 0x07)
        time.sleep(0.2)
        drained = mixer.drain_callbacks()
        assert len(drained) >= 1
