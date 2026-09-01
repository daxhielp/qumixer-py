import inspect
import threading
import time
from typing import Any

import pytest

from qumidi.client_sync import QuMixer
from qumidi.errors import NotSyncedError, ParameterRangeError
from qumidi.protocol.channels import Input, Mix
from qumidi.protocol.meters import LAYOUTS, Meter, MeterSlot
from qumidi.protocol.strips import Strip, StripKey

SCENE = {(0x25, 0x17, 0x07): 0x62}

#: The one slot the metering tests below read back, and the frame FakeQu is
#: told to stream. 0x7C80 decodes to -3.5 dB (see tests/test_meters.py, which
#: asserts the same value through the async API).
METER_SLOT = MeterSlot.input(6, Meter.POST_PREAMP)


def meter_frame(model: str, slot: MeterSlot, raw_u16: int) -> bytes:
    layout = LAYOUTS[model]
    payload = bytearray(layout.payload_bytes)
    offset = layout.offset_of(slot) * 2
    payload[offset : offset + 2] = raw_u16.to_bytes(2, "big")
    return bytes(payload)


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


def test_the_two_apis_expose_the_same_method_names(fake_qu_sync) -> None:  # type: ignore[no-untyped-def]
    # PRD 11.2: the sync facade is the async one minus await, generated from
    # the same registry, never a hand-maintained parallel surface.
    #
    # Class-level dir() alone is not enough, and that gap is exactly what let
    # the unwrapped meters/strips sub-APIs ship: dir() on a *class* only sees
    # entries in its __dict__ (and its bases'), never an attribute the async
    # mixer sets in __init__. So this checks both levels -- classes, and two
    # live, fully-synced instances.
    from qumidi.client_async import AsyncQuMixer

    async_names = {n for n in dir(AsyncQuMixer) if not n.startswith("_")}
    sync_names = {n for n in dir(QuMixer) if not n.startswith("_")}
    missing = async_names - sync_names
    assert not missing, f"the sync facade class is missing: {sorted(missing)}"

    with QuMixer("127.0.0.1", port=fake_qu_sync.port) as mixer:
        mixer.wait_synced()
        # strips is None until sync learns N -- assert it is populated so the
        # instance-level diff below is exercising the real sub-API.
        assert mixer.strips is not None
        async_instance = {n for n in dir(mixer._async) if not n.startswith("_")}
        sync_instance = {n for n in dir(mixer) if not n.startswith("_")}
        missing = async_instance - sync_instance
        assert not missing, f"the sync facade instance is missing: {sorted(missing)}"
        assert {"meters", "strips"} <= sync_instance


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


def test_a_loop_thread_callback_calling_an_explicit_method_raises_not_hangs(  # type: ignore[no-untyped-def]
    fake_qu_sync,
) -> None:
    # Regression: with thread_safe=False (the default), on_change callbacks
    # run inline on the mixer's own loop thread (PRD 11.2). A callback that
    # then calls an *explicit* proxied method -- e.g. the brief's own
    # recommended `fader.set_db(...)` form, not just the `.db =` sugar --
    # used to hang forever: QuMixer._run would submit a coroutine to the
    # loop and block waiting for it, but the loop thread doing the waiting
    # is the only thread that could ever run it. This must raise instead,
    # the same way the property-setter sugar already did.
    #
    # The callback itself catches whatever it gets and records it -- it runs
    # on the loop thread inside EventBus._call, which already catches and
    # logs callback exceptions, so nothing here can propagate out and hang
    # the test; the assertions below are a bounded wait plus a plain check,
    # never an indefinite one.
    with QuMixer("127.0.0.1", port=fake_qu_sync.port) as mixer:
        mixer.wait_synced()
        captured: list[BaseException | None] = []

        def cb(event: object) -> None:
            try:
                mixer.input(6).fader.set_db(-6.0)
            except BaseException as exc:  # noqa: BLE001
                captured.append(exc)
            else:
                captured.append(None)

        mixer.on_change(cb)
        fake_qu_sync.send_surface_move_sync(0x25, 0x17, 0x40, 0x07)
        time.sleep(0.3)
        assert len(captured) == 1
        assert isinstance(captured[0], RuntimeError)


def test_concurrent_close_calls_are_safe(fake_qu_sync) -> None:  # type: ignore[no-untyped-def]
    # PRD 11.2: close() must be safe to call from multiple threads at once,
    # not just safe to call twice in sequence -- a naive
    # "if self._closed: return; ...; self._closed = True" is a TOCTOU that
    # lets two concurrent callers both pass the guard before either sets it.
    mixer = QuMixer("127.0.0.1", port=fake_qu_sync.port)
    mixer.connect()
    mixer.wait_synced()

    errors: list[BaseException] = []
    barrier = threading.Barrier(5)

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            mixer.close()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not errors
    assert not any(t.is_alive() for t in threads)


# -- the structural drift guard (final review, finding 1) ----------------------
#
# The dir()-based guard above compares *names*. It cannot, by construction,
# catch a name that IS present but hands back something a synchronous caller
# can only misuse -- which is precisely how mixer.meters (Slice 10) and
# mixer.strips (Slice 11) shipped unwrapped: both are instance attributes, so
# they arrived through QuMixer.__getattr__ and, with their modules missing
# from _FACADE_MODULES, were returned raw. mixer.meters.enable() then handed
# back a live coroutine that nothing ever awaited: a silent no-op.
#
# So this guard is structural instead. It walks a real, connected QuMixer and
# asserts that nothing reachable from it is an unwrapped coroutine function,
# async generator function, or async-only context manager. Nothing is ever
# *called* -- shutdown(), close() and recall_scene() are all on the walk --
# only inspected, which is enough: an unwrapped sub-API's methods are
# coroutine functions and are caught on sight.

#: How deep to walk. mixer -> meters -> (its own attributes) is depth 2;
#: mixer -> lr -> fader -> (ParamProxy's attributes) is depth 3. Anything
#: deeper is the same handful of proxy types over again.
_MAX_DEPTH = 3


def _unwrap(value: Any) -> Any:
    """The real object behind a _FacadeProxy, or the value itself."""
    return getattr(value, "_target", value)


def _offence(value: Any, path: str) -> str | None:
    if inspect.iscoroutine(value):
        return f"{path} is a live coroutine object -- it will never be awaited"
    if inspect.isasyncgen(value):
        return f"{path} is a live async generator -- a sync caller cannot iterate it"
    if inspect.iscoroutinefunction(value):
        return f"{path} is an unwrapped coroutine function -- calling it drops the coroutine"
    if inspect.isasyncgenfunction(value):
        return f"{path} is an unwrapped async generator function"
    cls = type(_unwrap(value))
    if hasattr(cls, "__aenter__") and not hasattr(cls, "__enter__"):
        return f"{path} is an async-only context manager -- a sync caller cannot enter it"
    return None


def _walk(obj: Any, path: str, seen: set[type], offences: list[str], depth: int = 0) -> None:
    if depth > _MAX_DEPTH:
        return
    for name in dir(obj):
        if name.startswith("_"):
            continue
        try:
            value = getattr(obj, name)
        except Exception:  # noqa: BLE001, S112
            # A property that needs live state this walk has not set up --
            # e.g. ch.preamp raises until preamp_source is known. What it
            # would have returned is not this guard's concern; what matters
            # is that nothing it *does* return is an unawaited coroutine.
            continue
        if value is None:
            continue
        found = _offence(value, f"{path}.{name}")
        if found is not None:
            offences.append(found)
            continue
        target = _unwrap(value)
        cls = type(target)
        if not cls.__module__.startswith("qumidi.") or cls in seen:
            continue
        seen.add(cls)
        _walk(value, f"{path}.{name}", seen, offences, depth + 1)


def test_nothing_reachable_hands_back_an_unawaited_coroutine(fake_qu_sync) -> None:  # type: ignore[no-untyped-def]
    with QuMixer("127.0.0.1", port=fake_qu_sync.port) as mixer:
        mixer.wait_synced()
        # Both instance sub-APIs must actually be live for this walk to mean
        # anything -- strips stays None until sync learns the console's N.
        assert mixer.strips is not None
        assert mixer.meters is not None
        offences: list[str] = []
        _walk(mixer, "mixer", {QuMixer}, offences)
        assert not offences, "unwrapped async surface reachable from QuMixer:\n" + "\n".join(
            sorted(offences)
        )


def test_the_structural_guard_actually_detects_an_unwrapped_sub_api(fake_qu_sync) -> None:  # type: ignore[no-untyped-def]
    # A guard that cannot fail is not a guard. Re-run the same walk with
    # qumidi.meters removed from _FACADE_MODULES -- i.e. the exact state the
    # code was in before this fix -- and assert it reports the sub-API.
    from qumidi import client_sync

    original = client_sync._FACADE_MODULES
    client_sync._FACADE_MODULES = original - {"qumidi.meters"}
    try:
        with QuMixer("127.0.0.1", port=fake_qu_sync.port) as mixer:
            mixer.wait_synced()
            offences: list[str] = []
            _walk(mixer, "mixer", {QuMixer}, offences)
    finally:
        client_sync._FACADE_MODULES = original
    assert any("mixer.meters.enable" in o for o in offences), offences


# -- metering from the sync facade (PRD 12) ------------------------------------


def test_meters_enable_and_disable_block_and_reach_the_console(fake_qu_sync) -> None:  # type: ignore[no-untyped-def]
    with QuMixer("127.0.0.1", port=fake_qu_sync.port) as mixer:
        mixer.wait_synced()
        assert mixer.meters.enable_count == 0
        mixer.meters.enable()
        assert mixer.meters.enable_count == 1
        # Same pattern as every other write test here: the call blocks until
        # the bytes leave this client's socket, not until FakeQu -- on its own
        # loop thread -- has read them.
        time.sleep(0.1)
        assert fake_qu_sync.meters_enabled is True
        mixer.meters.disable()
        assert mixer.meters.enable_count == 0
        time.sleep(0.1)
        assert fake_qu_sync.meters_enabled is False


def test_meters_enable_raises_the_real_exception_not_a_dropped_coroutine() -> None:
    # enable() before sync raises NotSyncedError in the async API; the sync
    # facade must surface the same exception on the caller's own thread, and
    # certainly must not hand back a coroutine that swallows it.
    mixer = QuMixer("127.0.0.1", port=1, auto_reconnect=False)
    try:
        with pytest.raises(NotSyncedError):
            mixer.meters.enable()
    finally:
        mixer.close()


def test_meters_enabled_is_a_real_synchronous_context_manager(fake_qu_sync) -> None:  # type: ignore[no-untyped-def]
    with QuMixer("127.0.0.1", port=fake_qu_sync.port) as mixer:
        mixer.wait_synced()
        with mixer.meters.enabled() as meters:
            assert meters.enable_count == 1
            time.sleep(0.1)
            assert fake_qu_sync.meters_enabled is True
        assert mixer.meters.enable_count == 0
        time.sleep(0.1)
        assert fake_qu_sync.meters_enabled is False


def test_meters_enabled_disables_on_an_exception_path(fake_qu_sync) -> None:  # type: ignore[no-untyped-def]
    # PRD 12: the whole point of the context manager is that a firehose
    # cannot be left running by accident. The synchronous equivalent has to
    # keep that guarantee, and must let the exception through unchanged.
    with QuMixer("127.0.0.1", port=fake_qu_sync.port) as mixer:
        mixer.wait_synced()
        with pytest.raises(RuntimeError, match="boom"), mixer.meters.enabled():
            raise RuntimeError("boom")
        assert mixer.meters.enable_count == 0
        time.sleep(0.1)
        assert fake_qu_sync.meters_enabled is False


def test_nested_meters_enabled_blocks_use_a_count_not_a_flag(fake_qu_sync) -> None:  # type: ignore[no-untyped-def]
    with QuMixer("127.0.0.1", port=fake_qu_sync.port) as mixer:
        mixer.wait_synced()
        with mixer.meters.enabled():
            with mixer.meters.enabled():
                assert mixer.meters.enable_count == 2
            time.sleep(0.1)
            assert fake_qu_sync.meters_enabled is True
        time.sleep(0.1)
        assert fake_qu_sync.meters_enabled is False


def test_meters_stream_is_a_real_blocking_iterator(fake_qu_sync_factory) -> None:  # type: ignore[no-untyped-def]
    # A genuine round-trip: FakeQu streams a real 7-bit-packed meter frame,
    # the mixer's own pump task decodes it on the loop thread, and the sync
    # caller pulls it out with a plain for-loop.
    fake = fake_qu_sync_factory(
        model="qu32",
        meter_frames=[meter_frame("qu32", METER_SLOT, 0x7C80)],
        meter_rate=50.0,
    )
    with QuMixer("127.0.0.1", port=fake.port) as mixer:
        mixer.wait_synced()
        seen = []
        with mixer.meters.enabled(), mixer.meters.stream() as frames:
            for frame in frames:
                seen.append(frame.db(METER_SLOT))
                if len(seen) == 3:
                    break
    assert len(seen) == 3
    assert all(db == pytest.approx(-3.5) for db in seen)


def test_meters_stream_closes_cleanly_after_the_mixer_does(fake_qu_sync_factory) -> None:  # type: ignore[no-untyped-def]
    # close() on the iterator has to survive the mixer having gone away
    # underneath it (its loop is stopped and the meter pump is already torn
    # down) rather than raising out of a cleanup path.
    fake = fake_qu_sync_factory(
        model="qu32",
        meter_frames=[meter_frame("qu32", METER_SLOT, 0x7C80)],
        meter_rate=50.0,
    )
    mixer = QuMixer("127.0.0.1", port=fake.port)
    mixer.connect()
    mixer.wait_synced()
    mixer.meters.enable()
    frames = mixer.meters.stream()
    assert next(iter(frames)).db(METER_SLOT) == pytest.approx(-3.5)
    mixer.close()
    frames.close()
    frames.close()  # idempotent


def test_on_meter_layout_error_honours_thread_safe(fake_qu_sync_factory) -> None:  # type: ignore[no-untyped-def]
    # PRD 11.2: every mixer-level on_* registration must marshal onto the
    # caller's thread under thread_safe=True. on_meter_layout_error was the
    # one event with no mixer-level wrapper, so it always fired inline on the
    # loop thread. A 100-byte frame cannot match any layout, so the client
    # force-disables metering and emits the event (PRD 12.2).
    fake = fake_qu_sync_factory(model="qu32", meter_frames=[bytes(100)], meter_rate=50.0)
    with QuMixer("127.0.0.1", port=fake.port, thread_safe=True) as mixer:
        mixer.wait_synced()
        mixer.on_meter_layout_error(lambda message: None)
        mixer.meters.enable()
        deadline = time.monotonic() + 5.0
        drained: list[object] = []
        while time.monotonic() < deadline and not drained:
            drained = list(mixer.drain_callbacks())
            time.sleep(0.05)
        assert drained, "the layout error was never queued for the caller's thread"
        assert mixer.meters.enable_count == 0


# -- MIDI strips from the sync facade (PRD 7.2, 14.7) --------------------------


def test_strip_writes_block_and_reach_the_console(fake_qu_sync) -> None:  # type: ignore[no-untyped-def]
    n = fake_qu_sync.midi_channel
    with QuMixer("127.0.0.1", port=fake_qu_sync.port) as mixer:
        mixer.wait_synced()
        assert mixer.strips_available is True
        mixer.strips.set_fader(Strip(3), 0x40)
        mixer.strips.press(Strip(3), StripKey.MUTE)
        mixer.strips.bank(True)
        time.sleep(0.1)
        seen = bytes(fake_qu_sync.received)
    # Strip traffic lives on channel N+1, a namespace entirely separate from
    # the console's own channel N (PRD 6.3, 14.7).
    assert bytes([0xB0 | (n + 1), 0x02, 0x40]) in seen
    assert bytes([0x90 | (n + 1), 0x02, 0x7F]) in seen
    assert bytes([0x80 | (n + 1), 0x02, 0x00]) in seen
    assert bytes([0x90 | (n + 1), 0x7E, 0x7F]) in seen


def test_strip_writes_raise_the_real_exception(fake_qu_sync) -> None:  # type: ignore[no-untyped-def]
    # Raised inside the coroutine, on the mixer's own loop thread, and it has
    # to come back out on the caller's thread -- the whole point of blocking
    # rather than handing back a coroutine nobody awaits.
    with QuMixer("127.0.0.1", port=fake_qu_sync.port) as mixer:
        mixer.wait_synced()
        with pytest.raises(ValueError, match="value must be 0..127"):
            mixer.strips.set_fader(Strip(3), 0x80)


def test_on_strip_event_delivers_inbound_strip_traffic(fake_qu_sync) -> None:  # type: ignore[no-untyped-def]
    n = fake_qu_sync.midi_channel
    with QuMixer("127.0.0.1", port=fake_qu_sync.port) as mixer:
        mixer.wait_synced()
        seen: list[object] = []
        mixer.strips.on_strip_event(seen.append)
        fake_qu_sync.send_raw_sync(bytes([0xB0 | (n + 1), 0x04, 0x55]))
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not seen:
            time.sleep(0.05)
        assert len(seen) == 1
