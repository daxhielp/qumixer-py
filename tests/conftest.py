from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

import pytest

from qumidi.client_async import AsyncQuMixer
from qumidi.testing.fake_qu import FakeQu

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
