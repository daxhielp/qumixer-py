"""Hardware conformance fixtures. Opt-in, excluded from CI.

Safety model, see docs/hardware-verification.md for the full rationale:

- The protocol has no scene-store or scene-list message (docs/limitations.md),
  so this fixture cannot verify that a spare scene was actually saved, or that
  the slot named by --qu-spare-scene was actually free. That verification is
  a human, out-of-band step, and --qu-confirm-scene-saved exists only to make
  skipping it a deliberate choice rather than an oversight.
- recall_scene() at teardown restores what the console's scene mechanism
  captures, not necessarily bit-for-bit everything the protocol can set (the
  spec does not document scene content exhaustively). The pre-test snapshot
  written to disk below is the fallback of last resort if a recall does not
  fully restore the room: diff it against a fresh `qumixer-py dump-state` by hand.
- Always run this suite with `-s` (or `--capture=no`). pytest captures stdout
  by default and only replays it for a *failed* test, so every print below --
  the scene-save reminder, the snapshot path, the post-run drift report -- is
  silently swallowed on a normal passing run without it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from qumixer_py.client_async import AsyncQuMixer
from qumixer_py.events import ChangeEvent, Source
from qumixer_py.protocol.channels import ChannelId
from qumixer_py.protocol.params import FlaggedIndex
from qumixer_py.protocol.registry import REGISTRY


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--qu-host", default=None, help="IP of a real Qu mixer")
    parser.addoption(
        "--qu-spare-scene", type=int, default=100,
        help="a scene slot the suite may overwrite",
    )
    parser.addoption(
        "--qu-port", type=int, default=None,
        help="TCP port (default: the Qu port)",
    )
    parser.addoption(
        "--qu-ar-rack", action="store_true", default=False,
        help="an AR rack is patched in; allows dsnake_* writes",
    )
    parser.addoption(
        "--qu-confirm-scene-saved",
        action="store_true",
        default=False,
        help=(
            "required alongside --qu-host: confirms you have already stored the "
            "console's current show to --qu-spare-scene from the console's own UI "
            "(Scenes -> Store), and that the slot was actually free. The suite "
            "cannot check either of these itself -- there is no scene-store or "
            "scene-list message in the protocol -- so it refuses to run without "
            "this flag rather than silently trusting that someone remembered."
        ),
    )


def pytest_configure(config: pytest.Config) -> None:
    # Fail collection outright, before anything touches the mixer, rather than
    # relying on a fixture-time print that pytest's default output capture
    # would likely hide from the very person who needs to see it.
    if config.getoption("--qu-host") and not config.getoption("--qu-confirm-scene-saved"):
        raise pytest.UsageError(
            "hardware tests require --qu-confirm-scene-saved once you have stored the "
            "console's current show to --qu-spare-scene (default 100) from the "
            "console's own UI, and confirmed that slot was actually free -- the "
            "protocol has no scene-store or scene-list message, so the suite has no "
            "way to check either fact for you."
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

    the suite must save the current scene to a spare slot before
    running and restore it after, INCLUDING ON FAILURE. Without this, a
    conformance run leaves someone's console in a state they did not ask for.
    """
    spare = int(request.config.getoption("--qu-spare-scene"))
    mixer = AsyncQuMixer(qu_host)
    await mixer.connect()
    await mixer.wait_synced()
    before = mixer.snapshot()

    # Written to disk, not just held in memory: if this process is killed
    # hard enough that the `finally` below never runs, this file is the only
    # record of what the console looked like before the suite touched it.
    snapshot_path = Path(f"qu-hardware-snapshot-{int(time.time())}.json")
    snapshot_path.write_text(json.dumps(before, indent=2))
    print(f"\n*** pre-test snapshot saved to {snapshot_path.resolve()} ***")
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
                drift_path = snapshot_path.with_suffix(".drift.json")
                drift_path.write_text(json.dumps([
                    {
                        "channel": d.channel.label, "param": d.param,
                        "sub_index": d.sub_index,
                        "before": d.snapshot_raw, "after": d.current_raw,
                    }
                    for d in drift
                ], indent=2))
                print(
                    f"\n*** {len(drift)} parameters differ from the pre-test snapshot after "
                    f"the restore recall -- scene {spare} did not fully reproduce the "
                    f"pre-test state. Full list written to {drift_path.resolve()}. ***"
                )
                for d in drift[:20]:
                    print(f"    {d.channel.label} {d.param}: "
                          f"{d.snapshot_raw} -> {d.current_raw}")
        finally:
            await mixer.close()


# -- shared fixtures for the per-area hardware modules ------------------------
#
# Everything below is additive: the function-scoped ``hw`` fixture above is
# untouched and keeps serving test_conformance.py. A pytest process that never
# requests ``hw_session`` / ``hw_module`` never opens a connection, because the
# connection is made inside the fixture body, not at import or collection.
#
# The console accepts one control connection at a time, so ``hw`` and the
# session fixtures must not be requested in the same run. Tests that use
# ``hw_session`` / ``hw_module`` must run on the session event loop, e.g.
# ``pytestmark = pytest.mark.asyncio(loop_scope="session")``.


class Restorer:
    """What ``restoring()`` captured: prior values to write back, and what it could not."""

    def __init__(self) -> None:
        self.prior: dict[tuple[ChannelId, str, int | None], int] = {}
        self._seen: set[tuple[ChannelId, str, int | None]] = set()
        #: (channel label, param) pairs whose prior value was unknown, so they
        #: could not be written back.
        self.skipped: list[tuple[str, str]] = []


async def settle(mixer: AsyncQuMixer, seconds: float | None = None) -> None:
    """Wait long enough for the console's echoes to land (echo window plus margin)."""
    await asyncio.sleep(seconds if seconds is not None else mixer.echo_window + 0.2)


@contextlib.asynccontextmanager
async def restoring(mixer: AsyncQuMixer) -> AsyncIterator[Restorer]:
    """Record the first prior value of everything this client writes, and put it back on exit.

    Runs the restore on the failure path too. A parameter whose prior value was
    unknown (never reported by the console) cannot be restored; it is listed in
    ``Restorer.skipped`` instead of being guessed at.
    """
    restorer = Restorer()

    def on_change(event: ChangeEvent) -> None:
        if event.source is not Source.LOCAL:
            return
        key = (event.channel, event.param, event.sub_index)
        if key in restorer._seen:
            return
        restorer._seen.add(key)
        if event.old.raw is None:
            restorer.skipped.append((event.channel.label, event.param))
            return
        restorer.prior[key] = event.old.raw

    unsubscribe = mixer.events.on_change(on_change)
    try:
        yield restorer
    finally:
        unsubscribe()
        for (channel, param, sub), raw in restorer.prior.items():
            spec = REGISTRY.lookup(param, channel.cls)
            # A FlaggedIndex sub-index lives in the value, not in VX; every
            # other cached sub-index is the VX byte itself (None for Fixed VX).
            vx = None if isinstance(spec.domain, FlaggedIndex) else sub
            await mixer.set(channel, param, raw=raw, vx=vx)
        await settle(mixer)


@contextlib.asynccontextmanager
async def _session_mixer(request: pytest.FixtureRequest) -> AsyncIterator[AsyncQuMixer]:
    """One connected, synced mixer with a disk snapshot and a final scene recall + drift check."""
    host = request.config.getoption("--qu-host")
    if not host:
        pytest.skip("no --qu-host given")
    port = request.config.getoption("--qu-port")
    spare = int(request.config.getoption("--qu-spare-scene"))
    mixer = AsyncQuMixer(str(host)) if port is None else AsyncQuMixer(str(host), int(port))
    await mixer.connect()
    await mixer.wait_synced()
    before = mixer.snapshot()

    # Written to disk so a hard kill still leaves a record of the pre-test state.
    snapshot_path = Path(f"qu-hardware-snapshot-session-{int(time.time())}.json")
    snapshot_path.write_text(json.dumps(before, indent=2))
    print(f"\n*** session snapshot saved to {snapshot_path.resolve()} ***")
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
                drift_path = snapshot_path.with_suffix(".drift.json")
                drift_path.write_text(json.dumps([
                    {
                        "channel": d.channel.label, "param": d.param,
                        "sub_index": d.sub_index,
                        "before": d.snapshot_raw, "after": d.current_raw,
                    }
                    for d in drift
                ], indent=2))
                print(
                    f"\n*** {len(drift)} parameters differ from the session snapshot after "
                    f"the final recall of scene {spare}. Full list written to "
                    f"{drift_path.resolve()}. ***"
                )
                for d in drift[:20]:
                    print(f"    {d.channel.label} {d.param}: "
                          f"{d.snapshot_raw} -> {d.current_raw}")
        finally:
            await mixer.close()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def hw_session(request: pytest.FixtureRequest) -> AsyncIterator[AsyncQuMixer]:
    """One mixer for the whole pytest session."""
    async with _session_mixer(request) as mixer:
        yield mixer


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def hw_module(request: pytest.FixtureRequest) -> AsyncIterator[AsyncQuMixer]:
    """One mixer per test module, with its own snapshot and final recall."""
    async with _session_mixer(request) as mixer:
        yield mixer
