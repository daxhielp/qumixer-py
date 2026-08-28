from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

import pytest

from qumidi.testing.fake_qu import FakeQu


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
