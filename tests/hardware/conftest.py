"""Hardware conformance fixtures (PRD 13.5). Opt-in, excluded from CI."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from qumidi.client_async import AsyncQuMixer


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--qu-host", default=None, help="IP of a real Qu mixer")
    parser.addoption(
        "--qu-spare-scene", type=int, default=100,
        help="a scene slot the suite may overwrite",
    )


@pytest.fixture(scope="session")
def qu_host(request: pytest.FixtureRequest) -> str:
    host = request.config.getoption("--qu-host")
    if not host:
        pytest.skip("no --qu-host given")
    return str(host)


@pytest.fixture
async def hw(qu_host: str, request: pytest.FixtureRequest) -> AsyncIterator[AsyncQuMixer]:
    """A connected mixer whose state is snapshotted and restored.

    PRD 13.5: the suite must save the current scene to a spare slot before
    running and restore it after, INCLUDING ON FAILURE. Without this, a
    conformance run leaves someone's console in a state they did not ask for.
    """
    spare = int(request.config.getoption("--qu-spare-scene"))
    mixer = AsyncQuMixer(qu_host)
    await mixer.connect()
    await mixer.wait_synced()
    before = mixer.snapshot()
    print(
        f"\n*** Before running: store the console's current state to scene {spare} from "
        f"the console's own UI (Scenes -> Store). The suite will recall it afterwards. ***"
    )
    try:
        yield mixer
    finally:
        try:
            await mixer.recall_scene(spare)
            drift = mixer.diff(before)
            if drift:
                print(f"\n*** {len(drift)} parameters differ from the pre-test snapshot ***")
                for d in drift[:20]:
                    print(f"    {d.channel.label} {d.param}: "
                          f"{d.snapshot_raw} -> {d.current_raw}")
        finally:
            await mixer.close()
