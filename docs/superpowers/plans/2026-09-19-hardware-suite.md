# Comprehensive Hardware Suite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a hardware conformance suite that exercises every user-facing capability of the library against the Qu-32 at 192.168.4.63.

**Architecture:** One session-scoped connection (`hw_session`) with one snapshot and one scene recall per run, a `restoring()` helper that writes each touched NRPN parameter back, and a shared denylist guard. Seven independent test modules sit on top. Each is written and dry-run offline against a local `FakeQu`, then run on the console one at a time (the console accepts a single connection).

**Tech Stack:** Python 3.14, pytest 9.1, pytest-asyncio 1.4 (`loop_scope="session"`), ruff, mypy --strict, `qumixer_py.testing.fake_qu.FakeQu` for dry runs.

**Spec:** `docs/superpowers/specs/2026-09-19-hardware-suite-design.md`

## Global Constraints

- Run Python through `/home/daxhielp/Projects/qumixer_py/.venv/bin/python`; the system Python has no pytest.
- Hardware runs use `pytest tests/hardware -m hardware --qu-host=192.168.4.63 --qu-spare-scene=100 --qu-confirm-scene-saved -s -v --no-cov -p no:logging`. The `tests/hardware` path is required or pytest does not register `--qu-host`.
- Denylist, never written: `shutdown`, `transport`, `local_48v`, `dsnake_48v`, `preamp_source`, `usb_source`, `insert`, and every `dsnake_*` param unless `--qu-ar-rack` is passed.
- Writers never connect to 192.168.4.63. Only Task 9 does, and only from the main session.
- Existing files `tests/hardware/test_conformance.py` and the function-scoped `hw` fixture are not modified.
- Every new file passes `ruff check` and `mypy --strict`.
- Failures found on the console are recorded and reported, not silently patched in library code.
- Do not commit unless the user asks.

---

### Task 1: Shared fixtures and safety guard

**Files:**
- Create: `tests/hardware/_safety.py`
- Modify: `tests/hardware/conftest.py` (add `--qu-port`, `--qu-ar-rack`, `hw_session`, `restoring`; leave `hw` untouched)
- Test: `tests/test_hardware_helpers.py` (runs in the normal offline suite against `FakeQu`)

**Interfaces:**
- Produces:
  - `DENYLIST: frozenset[str]`
  - `assert_writable(param: str, *, ar_rack: bool = False) -> None` raises `ParameterRangeError` for a denied param.
  - `restoring(mixer: AsyncQuMixer) -> AbstractAsyncContextManager[Restorer]`; `Restorer.skipped: list[tuple[str, str]]` records `(channel label, param)` pairs it could not restore.
  - `hw_session` (session scope) and `hw_module` (module scope) async fixtures, both with `loop_scope="session"`, yielding a connected, synced `AsyncQuMixer`. Both come from one shared async context manager `_session_mixer(request)`. A pytest process that never requests either fixture never opens a connection.
  - `async def settle(mixer: AsyncQuMixer, seconds: float | None = None) -> None` sleeps `mixer.echo_window + 0.2` by default.

- [ ] **Step 1: Write the failing tests** in `tests/test_hardware_helpers.py`

```python
from __future__ import annotations

import pytest

from qumixer_py.client_async import AsyncQuMixer
from qumixer_py.errors import ParameterRangeError
from qumixer_py.protocol.channels import Input, Mix
from qumixer_py.testing.fake_qu import FakeQu
from tests.hardware._safety import assert_writable
from tests.hardware.conftest import restoring


@pytest.mark.parametrize("param", ["local_48v", "dsnake_gain", "insert", "preamp_source"])
def test_denylisted_params_are_refused(param: str) -> None:
    with pytest.raises(ParameterRangeError):
        assert_writable(param)


def test_dsnake_is_allowed_with_an_ar_rack() -> None:
    assert_writable("dsnake_gain", ar_rack=True)


async def test_restoring_writes_back_a_fixed_vx_param(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory()
    mixer = AsyncQuMixer("127.0.0.1", fake.port, sync_names=False)
    await mixer.connect()
    await mixer.wait_synced()
    before = mixer.get(Input(1), "fader").raw
    async with restoring(mixer):
        await mixer.set(Input(1), "fader", raw=0x22)
        assert mixer.get(Input(1), "fader").raw == 0x22
    assert mixer.get(Input(1), "fader").raw == before
    await mixer.close()


async def test_restoring_writes_back_a_send_target_param(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory()
    mixer = AsyncQuMixer("127.0.0.1", fake.port, sync_names=False)
    await mixer.connect()
    await mixer.wait_synced()
    await mixer.set(Input(1), "send_level", raw=0x30, vx=Mix.MIX1)
    sub_before = mixer.get(Input(1), "send_level", 0x00).raw
    async with restoring(mixer):
        await mixer.set(Input(1), "send_level", raw=0x50, vx=Mix.MIX1)
    assert mixer.get(Input(1), "send_level", 0x00).raw == sub_before
    await mixer.close()


async def test_restoring_skips_and_reports_an_unknown_prior_value(fake_qu_factory) -> None:  # type: ignore[no-untyped-def]
    fake = await fake_qu_factory()
    mixer = AsyncQuMixer("127.0.0.1", fake.port, sync_names=False)
    await mixer.connect()
    await mixer.wait_synced()
    prior = mixer.get(Input(7), "hpf_freq").raw
    async with restoring(mixer) as restorer:
        await mixer.set(Input(7), "hpf_freq", raw=0x10)
    if prior is None:
        assert restorer.skipped == [("Input 7", "hpf_freq")]
    else:
        assert restorer.skipped == []
        assert mixer.get(Input(7), "hpf_freq").raw == prior
    await mixer.close()
```

(`Mix.MIX1`'s send-target sub-index is its `SEND_TARGET_VX` byte, not necessarily `0x00`; the implementer replaces `0x00` in the two `get` calls with `send_target_vx(Mix.MIX1, mixer.caps)` from `qumixer_py.protocol.channels`.)

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_hardware_helpers.py --no-cov -q`
Expected: FAIL with `ModuleNotFoundError: tests.hardware._safety`.

- [ ] **Step 3: Write `tests/hardware/_safety.py`**

```python
"""Write guard for the hardware suite (spec: docs/superpowers/specs/2026-09-19-hardware-suite-design.md)."""

from __future__ import annotations

from qumixer_py.errors import ParameterRangeError

DENYLIST: frozenset[str] = frozenset(
    {"shutdown", "transport", "local_48v", "preamp_source", "usb_source", "insert"}
)


def assert_writable(param: str, *, ar_rack: bool = False) -> None:
    if param in DENYLIST or (param.startswith("dsnake_") and not ar_rack):
        raise ParameterRangeError(f"{param} is on the hardware-suite denylist and is never written")
```

`dsnake_48v` is covered by the `dsnake_` prefix rule; `local_48v` is listed explicitly.

- [ ] **Step 4: Add `restoring` and `hw_session` to `tests/hardware/conftest.py`**

```python
import contextlib
import pytest_asyncio
from qumixer_py.events import Source
from qumixer_py.protocol.params import FlaggedIndex
from qumixer_py.protocol.registry import REGISTRY

# additions to pytest_addoption:
#   parser.addoption("--qu-port", type=int, default=None, help="TCP port (default: the Qu port)")
#   parser.addoption("--qu-ar-rack", action="store_true", default=False,
#                    help="an AR rack is patched in; allows dsnake_* writes")


class Restorer:
    def __init__(self) -> None:
        self.prior: dict[tuple[ChannelId, str, int | None], int] = {}
        self._seen: set[tuple[ChannelId, str, int | None]] = set()
        self.skipped: list[tuple[str, str]] = []


@contextlib.asynccontextmanager
async def restoring(mixer: AsyncQuMixer) -> AsyncIterator[Restorer]:
    restorer = Restorer()

    def on_change(event) -> None:  # type: ignore[no-untyped-def]
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
            vx = None if isinstance(spec.domain, FlaggedIndex) else sub
            await mixer.set(channel, param, raw=raw, vx=vx)
        await settle(mixer)


async def settle(mixer: AsyncQuMixer, seconds: float | None = None) -> None:
    await asyncio.sleep(seconds if seconds is not None else mixer.echo_window + 0.2)


@contextlib.asynccontextmanager
async def _session_mixer(request: pytest.FixtureRequest) -> AsyncIterator[AsyncQuMixer]:
    host = request.config.getoption("--qu-host")
    if not host:
        pytest.skip("no --qu-host given")
    port = request.config.getoption("--qu-port")
    spare = int(request.config.getoption("--qu-spare-scene"))
    mixer = AsyncQuMixer(str(host)) if port is None else AsyncQuMixer(str(host), int(port))
    await mixer.connect()
    await mixer.wait_synced()
    before = mixer.snapshot()
    snapshot_path = Path(f"qu-hardware-snapshot-session-{int(time.time())}.json")
    snapshot_path.write_text(json.dumps(before, indent=2))
    print(f"\n*** session snapshot saved to {snapshot_path.resolve()} ***")
    try:
        yield mixer
    finally:
        try:
            await mixer.recall_scene(spare)
            drift = mixer.diff(before)
            if drift:
                drift_path = snapshot_path.with_suffix(".drift.json")
                drift_path.write_text(json.dumps([
                    {"channel": d.channel.label, "param": d.param, "sub_index": d.sub_index,
                     "before": d.snapshot_raw, "after": d.current_raw}
                    for d in drift
                ], indent=2))
                print(f"\n*** {len(drift)} parameters differ after the final recall; see {drift_path.resolve()} ***")
        finally:
            await mixer.close()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def hw_session(request: pytest.FixtureRequest) -> AsyncIterator[AsyncQuMixer]:
    async with _session_mixer(request) as mixer:
        yield mixer


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def hw_module(request: pytest.FixtureRequest) -> AsyncIterator[AsyncQuMixer]:
    async with _session_mixer(request) as mixer:
        yield mixer
```

The skip-if-no-host check, port handling, snapshot write and final recall/drift block shown in the `hw_session` body above belong in `_session_mixer` (an `asynccontextmanager` that yields the mixer); `hw_session` and `hw_module` only delegate to it. The snapshot filename uses `session` for both. `restoring`'s `on_change` callback parameter is typed `ChangeEvent` (import from `qumixer_py.events`), and `ChannelId` comes from `qumixer_py.protocol.channels`.

- [ ] **Step 5: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_hardware_helpers.py --no-cov -q`
Expected: 8 passed.

- [ ] **Step 6: Run the full offline suite and linters**

Run: `.venv/bin/python -m pytest -m "not hardware" tests --cov-fail-under=90 -q; .venv/bin/ruff check qumixer-py tests; .venv/bin/mypy --strict qumixer-py`
Expected: all green except the known README "TODO" docs test (`test_the_document_exists_and_is_not_a_stub[README.md]`), which is unrelated.

---

### Task 2: Registry sweep (`tests/hardware/test_hw_registry_sweep.py`)

**Files:** Create `tests/hardware/test_hw_registry_sweep.py`.

**Interfaces:** Consumes `hw_session`, `restoring`, `settle`, `assert_writable`, `REGISTRY`, `ALL_CHANNELS`, `check_channel`, `CAPS_BY_BOX_ID`. Produces nothing later tasks use.

Module header (every hardware module uses this exact pattern):

```python
pytestmark = [pytest.mark.hardware, pytest.mark.asyncio(loop_scope="session")]
```

- [ ] **Step 1: Build the case list at import time.** One case per `(ChannelClass, param)` on the Qu-32: pick the lowest-numbered channel of that class that passes `check_channel(CAPS_BY_BOX_ID[3], ch)`. Drop denylisted params, `Direction.RX_ONLY` params (they get their own read-only test below), and `mix_assign` targeting LR.
- [ ] **Step 2: Write these tests**
  - `test_boolean_params_toggle_and_return`: for `Boolean` domains, write `on`, `settle`, read back; write `off`, `settle`, read back. `fx_delay_link` uses `0x00`/`0x7F`.
  - `test_interpolated_params_round_trip_raw_at_lo_mid_hi`: raw `lo`, `(lo+hi)//2`, `hi`.
  - `test_interpolated_params_round_trip_db`: write `db=` from `to_db(mid)`, read back raw and assert `abs(raw - mid) <= 1`. Mark as `xfail(strict=False)` with the reason "taper not verified" when `spec.domain.taper.verified is False`, so unverified tapers report rather than fail the run.
  - `test_enum_params_accept_every_member`: iterate `EnumDomain.mapping`.
  - `test_flagged_index_params_set_and_clear_membership`: `dca_assign`, `mute_group_assign` via `mixer.membership`.
  - `test_raw7bit_params_round_trip_at_bounds`: `pan` centre `0x25` and bounds, send targets from `SEND_TARGET_VX`.
  - `test_rx_only_params_refuse_writes`: `group_mode`, `dsnake_patch` raise `ReadOnlyParameterError`, and reading them does not raise.
  - `test_every_case_is_recorded`: print the number of cases run and a table of `(class, param, freshness)` so the run log shows echo behavior per param.
- [ ] **Step 3:** Every write sits inside `async with restoring(hw_session)`. Assert `restorer.skipped` is empty or print the skipped list (do not fail on it).
- [ ] **Step 4: Dry-run** (Task 9 step 1) and lint.

---

### Task 3: Object facade (`tests/hardware/test_hw_object_facade.py`)

**Files:** Create `tests/hardware/test_hw_object_facade.py`. Same header and fixtures as Task 2.

Tests (each inside `restoring`, on `hw_session.input(1)` unless stated):
- `test_peq_bands_set_gain_freq_width`: `peq.lf/lm/hm/hf` with `.raw` and `.db` setters; `is_approximate` is True for unverified tapers.
- `test_comp_and_gate_sections`: `comp` (threshold, ratio, attack, release, makeup, type, knee), `gate` (threshold, attack, hold, release, depth).
- `test_hpf_and_preamp_gain`: `hpf`, `preamp.set_gain_db`. Do not call `preamp.set_phantom` (denylist).
- `test_geq_bands_on_a_mix`: `hw_session.mix(Mix.MIX1).geq.set_band_db(band, db)` for bands 0, 13, 27.
- `test_sends_level_pre_and_assign`: `input(1).send(mix1).set_level_db/set_pre/set_assigned`.
- `test_dca_and_mute_group_membership`: `dca_assign`, `mute_group_assign`.
- `test_facade_on_every_channel_class`: read one parameter through each of `input`, `stereo_input`, `mix`, `lr`, `group`, `matrix`, `dca`, `mute_group`, `fx_send`, `fx_return` and assert each returns a `CachedValue` without raising.

---

### Task 4: Mute, names, ramps (`tests/hardware/test_hw_mute_names_ramps.py`)

**Files:** Create `tests/hardware/test_hw_mute_names_ramps.py`.

The library cannot read mute back (Note On/Off is never ingested). Mute tests therefore assert on the bytes that leave the wire, using the client's transport send hook or `qumixer-py record` in a subprocess, and rely on the session's final scene recall to restore mute state (mutes cannot be restored per test).

- `test_mute_sends_note_on_and_off`: for `Input(1)`, `Mix.MIX1`, `Dca(1)`, `MuteGroup(1)`, call `mute(ch, True)` then `mute(ch, False)`; capture the outgoing bytes and assert a Note On then a Note Off with the expected note number for that channel.
- `test_mute_group_master_fire`: `MuteGroupMaster.fire` matches `mute(MuteGroup(n), True)` on the wire.
- `test_name_round_trip_on_every_nameable_class`: `set_name` to a 4-character name and back on one channel per class in `nameable_channels(caps)`, restoring the original. The original comes from `mixer.name(ch)`, which is now NUL-stripped.
- `test_name_sweep_matches_individual_reads`: `sweep_names` result equals a fresh `name()` for 5 channels.
- `test_ramp_to_raw_reaches_the_target`: `input(1).fader.ramp_to_raw(0x50)` inside `restoring`, wait, assert raw. Then `cancel_ramp` mid-ramp leaves the value between start and target.
- `test_all_fade_out_reaches_the_floor`: `all_fade_out` on LR inside `restoring`; assert LR fader raw is at or below `0x08` afterwards. The PA is muted, per the operator.
- `test_ramp_respects_the_send_rate`: time a 40-step ramp and assert it takes at least `40 * 12 / MAX_SEND_RATE` seconds.

---

### Task 5: Scenes and snapshot (`tests/hardware/test_hw_scenes_snapshot.py`)

**Files:** Create `tests/hardware/test_hw_scenes_snapshot.py`.

- `test_snapshot_is_json_serialisable_and_has_values`
- `test_diff_is_empty_immediately_after_a_snapshot`
- `test_diff_names_a_changed_parameter`: change one fader inside `restoring`, assert `diff` lists `Input 1 fader`.
- `test_recall_scene_emits_the_event_and_resyncs`: recall the spare scene (slot 100, which the operator has stored), assert `on_scene_recalled` fired with 100, `last_recalled_scene == 100`, and `state` returns to synced.
- `test_refresh_and_resync_leave_the_cache_populated`: cache length after `refresh()` is within 5% of before.
- `test_export_snapshot_writes_a_file`: to `tmp_path`.
- `test_resync_cache_is_consistent_with_the_snapshot`: `diff(snapshot_before_resync)` is empty or lists only meters and transient values; print the differences.

---

### Task 6: Events and sync facade (`tests/hardware/test_hw_events_sync.py`)

**Files:** Create `tests/hardware/test_hw_events_sync.py`. Uses `hw_module`, never `hw_session`, so its connection is released when the module ends.

- `test_on_change_reports_local_then_mixer_source`: subscribe filtered to `Input(1)`/`fader`; write; expect a `Source.LOCAL` event, then a `Source.MIXER` event if the console echoes. Print the echo verdict.
- `test_on_write_unconfirmed_fires_when_no_echo`: only meaningful if the console does not echo; skip with the reason if it does.
- `test_connection_state_events_on_close_and_reconnect`: on `hw_module`, subscribe `on_connection_state`, call `close()` then `connect()` and `wait_synced()`, and assert the states seen are a disconnect followed by a return to synced.
- `test_sync_facade_parity`: capture `model`, `firmware`, `midi_channel` and one fader read from `hw_module`, `close()` it, open `QuMixer` (blocking) on the same host, assert the four values match, close it, then reconnect `hw_module` in a `finally`.
- `test_second_client_is_refused_while_connected`: the existing conformance test covers this; do not duplicate.

---

### Task 7: Meters and strips (`tests/hardware/test_hw_meters_strips.py`)

**Files:** Create `tests/hardware/test_hw_meters_strips.py`.

- `test_meters_enabled_yields_frames_at_a_steady_rate`: `async with meters.enabled()`, read 20 frames, assert monotone timestamps and that no `meter_layout_error` fired.
- `test_meter_values_decode_within_range`: every decoded meter is finite and within the taper's floor and ceiling.
- `test_silent_input_reads_near_the_floor`: mute an input that has no signal (choose the lowest-metered input over 20 frames and report which), assert its slot is within 6 dB of the floor. This closes `docs/hardware-verification.md` section 5.
- `test_meters_disable_stops_the_stream`.
- `test_strips_are_available_on_midi_channel_0`: `mixer.strips is not None`.
- `test_strip_fader_move_sends_on_channel_1`: `strips.set_fader(...)` on strip 1, restore with a second call. Assert the bytes on the wire use MIDI channel `N+1`.
- `test_strips_bank_and_press_do_not_raise`: `bank`, `press` on one strip.

---

### Task 8: CLI (`tests/hardware/test_hw_cli.py`)

**Files:** Create `tests/hardware/test_hw_cli.py`.

The CLI tests never request `hw_session` or `hw_module`; each runs `python -m qumixer_py ...` in a subprocess, so nothing else holds the console's one connection. The CLI writes cannot be seen by a mixer's `restoring`, so each writing test reads the original value with `dump-state --json` first and writes it back with the same `set` command in a `finally`. A module-scoped fixture `cli_scene_restore` runs `python -m qumixer_py recall --host H 100` at module teardown (using `--qu-spare-scene`).

- `test_probe_reports_model_and_firmware`: `python -m qumixer_py probe --host H` exits 0 and mentions `Qu-32` and `1.97`.
- `test_dump_state_json_is_valid`: `dump-state --json --out tmp` parses and has a non-empty `values`.
- `test_set_command_writes_and_reads_back`: `set fader input:1 --to <raw>` then `dump-state`, then restore.
- `test_set_name_command`: `set-name input:1 Cli` then restore the original name.
- `test_recall_command_recalls_the_spare_scene`
- `test_record_captures_a_sync_handshake`: `record --out tmp.bin --seconds 10`, assert the file is larger than 1 KB.
- `test_monitor_prints_a_line_after_a_change`: start `monitor --wire` in a subprocess, change a value from a second process, assert output. (Two processes cannot both connect, so this test expects the change to come from the console's own surface; make it a manual test that skips with the reason "needs a person at the console" unless `--qu-manual` is passed, and add that option in this task.)

---

### Task 9: Serial run against the console and findings report

**Files:** Create `docs/hardware-findings-2026-09-19.md` (results only, written by the main session).

- [ ] **Step 1: Dry-run each module against FakeQu.** Start a fake console on a fixed port from the scratchpad, then run:
  `.venv/bin/python -m pytest tests/hardware/<module>.py -m hardware --qu-host=127.0.0.1 --qu-port=<port> --qu-confirm-scene-saved -s --no-cov -p no:logging -q`
  Fix test bugs until the module runs clean or fails only where `FakeQu` cannot model the console (record each such case).
- [ ] **Step 2: Confirm with the operator** the PA is still muted and nothing else is connected.
- [ ] **Step 3: Run each module against the console, one at a time**, logging to the scratchpad and reading only the summary:
  `.venv/bin/python -m pytest tests/hardware/<module>.py -m hardware --qu-host=192.168.4.63 --qu-spare-scene=100 --qu-confirm-scene-saved -s -v --no-cov -p no:logging > $S/<module>.txt 2>&1; grep -E "passed|failed|\*\*\*|xfail" $S/<module>.txt | tail`
- [ ] **Step 4: Explain each failure** as a library bug, registry error, or test bug. Fix test bugs. Record library bugs and registry errors in the findings file without changing library code.
- [ ] **Step 5: Read the final drift report** and record it in the findings file in full.
- [ ] **Step 6: Report** pass/fail counts, new findings, the drift count, and what `docs/hardware-verification.md` section 7 items the results support.
