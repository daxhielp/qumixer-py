# Comprehensive hardware suite: design

Target: Qu-32, firmware 1.97, MIDI channel 0 (192.168.4.63). Scope chosen by the
operator: broad sweep. The PA is muted or disconnected and no other client is
connected. Every parameter class may be written on any channel except the
denylist below. Values are restored afterwards, with one scene recall at the end.

## Goals

- Exercise every user-facing capability of `AsyncQuMixer`, the sync facade and the
  CLI against the real console, so each open question in `docs/tiers.md` has a
  test that answers it.
- Stay safe on a console holding a live show: bounded writes, per-parameter
  restore, a snapshot on disk before anything runs, one scene recall at the end.
- Keep the existing 14 conformance tests untouched.

## Non-goals

- Qu-24 tests (this console is a Qu-32).
- Anything on the denylist. `shutdown` and MMC `transport` are never sent. No
  dSNAKE test runs unless an AR rack is patched in.
- Taper calibration (`tools/calibrate.py` needs a person reading the display).
- Automated restore from a snapshot;.5 rules that out.

## Fixtures (tests/hardware/conftest.py, additive)

- `hw_session` (session scope): one connect and `wait_synced`, one snapshot file
  for the whole run, one `recall_scene(spare)` at teardown, one drift report.
- `restoring(mixer)`: an async context manager. It records the cached raw value
  of every (channel, param) written inside the block and writes them back in a
  `finally`. It skips parameters whose cache entry is UNKNOWN and lists them
  in the run output instead of guessing.
- `settle(mixer)`: waits out the echo window so a test never races the
  optimistic-write cache.
- The existing function-scoped `hw` fixture stays for the existing tests.

## Denylist (enforced in one shared helper, `tests/hardware/_safety.py`)

Never written: `shutdown`, `transport`, `local_48v`, `dsnake_48v`,
`preamp_source`, `usb_source`, `insert`, and every `dsnake_*` param unless
`--qu-ar-rack` is passed. The helper raises before any bytes are sent.

## Modules (one writer subagent each, in parallel, no hardware access)

| File | Covers |
|---|---|
| `test_hw_registry_sweep.py` | Data-driven over `REGISTRY`: one representative channel per (class, param) with raw and dB round-trips; RX-only params read-only |
| `test_hw_object_facade.py` | `peq`, `comp`, `gate`, `hpf`, `geq`, `preamp` gain, sends, assigns, DCA and mute-group membership |
| `test_hw_mute_names_ramps.py` | `mute` on inputs, mixes, DCA, mute groups (checked by raw capture, since the library does not read Note On/Off); names; `ramp_to_*`; `all_fade_out` |
| `test_hw_scenes_snapshot.py` | `recall_scene`, `snapshot`, `diff`, `refresh`, `resync` |
| `test_hw_events_sync.py` | `on_change` sources, `on_write_unconfirmed`, connection-state events, sync-facade parity |
| `test_hw_meters_strips.py` | Meters (including the silent-channel assertion from `docs/hardware-verification.md` section 5) and strips |
| `test_hw_cli.py` | `probe`, `set`, `set-name`, `dump-state`, `recall`, `record`, `monitor` via subprocess |

Each writer verifies its module offline: it must collect, pass ruff and
`mypy --strict`, and pass against `FakeQu` where a FakeQu path exists.
Writers must not connect to 192.168.4.63.

## Execution

The console accepts one connection, so modules run one at a time from the main
session. Each run logs to a scratchpad file with `-p no:logging`; the main
session reads only the pass/fail summary, printed measurements, and drift
report. New findings (registry mismatches, decode bugs) are recorded and
reported, not silently fixed.

## Success criteria

- Every module collects and lints clean.
- A full serial run exits with each failure explained as a library bug, a
  registry error or a test bug, none left unexplained.
- Drift after the final scene recall is reported in full.
- Results feed `docs/hardware-verification.md` section 7 (tier promotions, interim
  constants, open questions); the code changes themselves are a follow-up.
