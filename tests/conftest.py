from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import Any

import pytest

from qumidi.client_async import AsyncQuMixer
from qumidi.testing.fake_qu import FakeQu

#: PRD 16 acceptance criterion 15: the whole suite must also pass with the
#: console on a non-zero MIDI channel, not only the default 0. Both FakeQu
#: fixtures below default to this rather than to FakeQu's own default of 0,
#: so
#:
#:     QUMIDI_TEST_MIDI_CHANNEL=5 python -m pytest -m "not hardware"
#:
#: exercises a non-zero N everywhere a test does not itself pin midi_channel
#: (a test that passes midi_channel= explicitly -- e.g. to assert channel-16
#: overflow behaviour -- still wins, since an explicit keyword always beats a
#: fixture-supplied default).
_DEFAULT_TEST_MIDI_CHANNEL = int(os.environ.get("QUMIDI_TEST_MIDI_CHANNEL", "0"))

# Test-only speed-up: AsyncQuMixer's default name_sweep_interval (20ms,
# matching real hardware pacing) makes every test that connects a mixer pay a
# multi-second name sweep, since the vast majority of tests never care about
# names and never pass sync_names=False or their own name_sweep_interval.
# Lower the *default* here, once, rather than retrofitting every existing
# AsyncQuMixer(...) call site across the suite -- a test that explicitly
# passes name_sweep_interval=... (e.g. to assert the pacing itself) still
# wins, since an explicit keyword argument always overrides a function's
# default. Production behaviour (the real default read by library callers,
# e.g. via `python -m qumidi.cli`) is untouched -- this only patches the
# bound default on the class actually imported by the test process.
assert AsyncQuMixer.__init__.__kwdefaults__ is not None
AsyncQuMixer.__init__.__kwdefaults__["name_sweep_interval"] = 0.0


@pytest.fixture
async def fake_qu_factory() -> AsyncIterator[Callable[..., Awaitable[FakeQu]]]:
    started: list[FakeQu] = []

    async def make(**kwargs: object) -> FakeQu:
        kwargs.setdefault("midi_channel", _DEFAULT_TEST_MIDI_CHANNEL)
        fake = FakeQu(**kwargs)  # type: ignore[arg-type]
        await fake.start()
        started.append(fake)
        return fake

    yield make
    for fake in started:
        await fake.stop()


@pytest.fixture
async def fake_qu(fake_qu_factory) -> FakeQu:  # type: ignore[no-untyped-def]
    return await fake_qu_factory()


#: Preloaded on the fake console's Input 6 fader for tests/test_sync_facade.py
#: (its own module-level SCENE constant) -- kept as a plain literal here
#: rather than importing from the test module, since conftest.py must not
#: depend on the test files that use it.
_SYNC_FADER_SCENE = {(0x25, 0x17, 0x07): 0x62}


class _SyncFakeQu:
    """A FakeQu running on its own loop thread, for the sync-facade tests.

    tests/test_sync_facade.py is deliberately not async (QuMixer itself is a
    blocking API), so it cannot use the fake_qu_factory/fake_qu fixtures
    above, which rely on pytest-asyncio driving FakeQu's coroutines on the
    test's own event loop. This wraps a FakeQu that instead runs on a private
    background loop, and adds send_surface_move_sync -- everything else
    (``.port``, ``.state``, ``.mutes``, ...) is forwarded straight through.
    """

    def __init__(self, fake: FakeQu, loop: asyncio.AbstractEventLoop) -> None:
        self._fake = fake
        self._loop = loop

    def __getattr__(self, name: str) -> Any:
        return getattr(self._fake, name)

    def send_surface_move_sync(self, ch: int, param_id: int, value: int, vx: int) -> None:
        """Emit an unsolicited NRPN, blocking until it's on the wire.

        Schedules FakeQu.send_surface_move onto this fixture's own loop
        thread and waits for it, the same way the sync facade itself
        marshals writes onto its loop (PRD 11.2) -- send_surface_move is a
        coroutine and this fixture's FakeQu lives on a thread with no
        running loop of the caller's own to await it from.
        """
        asyncio.run_coroutine_threadsafe(
            self._fake.send_surface_move(ch, param_id, value, vx), self._loop
        ).result()

    def send_raw_sync(self, data: bytes) -> None:
        """Emit arbitrary bytes, blocking until they're on the wire.

        The send_surface_move_sync of raw traffic -- used for the MIDI-strip
        layer (channel N+1), which is a separate namespace with no NRPN form.
        """
        asyncio.run_coroutine_threadsafe(self._fake.send_raw(data), self._loop).result()


@pytest.fixture
def fake_qu_sync_factory() -> Iterator[Callable[..., _SyncFakeQu]]:
    """The threaded FakeQu of ``fake_qu_sync``, but configurable.

    Same private loop thread and the same teardown; the only difference is
    that the caller supplies FakeQu's keyword arguments (meter_frames,
    midi_channel, ...). ``fake_qu_sync`` below is the default configuration
    most of tests/test_sync_facade.py wants.
    """
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, name="fake-qu-sync-loop", daemon=True)
    thread.start()
    started: list[FakeQu] = []

    def make(**kwargs: object) -> _SyncFakeQu:
        kwargs.setdefault("scene", dict(_SYNC_FADER_SCENE))
        kwargs.setdefault("midi_channel", _DEFAULT_TEST_MIDI_CHANNEL)

        async def start() -> FakeQu:
            fake = FakeQu(**kwargs)  # type: ignore[arg-type]
            await fake.start()
            return fake

        fake = asyncio.run_coroutine_threadsafe(start(), loop).result()
        started.append(fake)
        return _SyncFakeQu(fake, loop)

    try:
        yield make
    finally:
        for fake in started:
            asyncio.run_coroutine_threadsafe(fake.stop(), loop).result()
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5.0)
        loop.close()


@pytest.fixture
def fake_qu_sync(fake_qu_sync_factory) -> _SyncFakeQu:  # type: ignore[no-untyped-def]
    return fake_qu_sync_factory()
