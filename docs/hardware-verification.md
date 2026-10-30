# Hardware verification TODO

Everything the automated suite can check, it already checks against `FakeQu`:
918 tests, 96% coverage on `protocol/`, `midi/` and `state.py`, `mypy --strict`
and `ruff` clean. What none of that can do is prove any of it against a real
console. **No parameter is at `Tier.VERIFIED`** (see [tiers.md](tiers.md)) and
this file is the checklist for the one thing that changes that: a hardware
verification run that closes every open question listed in
[tiers.md](tiers.md).

Needs: a Qu-24 and a Qu-32 on the network, and for the
dSNAKE item, an AR audio rack patched to at least one input. The suite is not
destructive by intent — it requires saving the current show to a spare
scene first, and `tests/hardware/conftest.py`'s `hw` fixture restores it in a
`finally` on every test including a failure — but every test in between that
restore runs live writes against whatever the console is actually patched
into. Read this section before running anything against a console you don't
control end to end.

## 0. Safety measures

The protocol gives the harness less to work with than a normal "restore a
backup" story implies: no scene-store message, no scene-list or scene-content
query, and no read-back that would let code confirm the console has an
intact copy of the previous show anywhere before it starts overwriting
things (`docs/limitations.md`). Everything below is either what the harness
now enforces in code, or what has to be a process/hardware precaution
instead because the protocol leaves nothing for code to check.

**Enforced by the harness (`tests/hardware/conftest.py`):**

- **`--qu-confirm-scene-saved` is required alongside `--qu-host`.** The old
  behaviour only printed a reminder to store the current show to the spare
  scene; that print is fixture-time stdout, and pytest captures stdout by
  default and only ever shows it for a *failed* test. A clean run could
  silently skip the one reminder standing between the operator and losing
  the current show. The flag turns a missed reminder into a hard,
  before-anything-connects `pytest.UsageError` instead.
- **Always invoke the suite with `-s`** (`--capture=no`). This is necessary
  even with the flag above: the pre-test snapshot path, the spare-scene
  reminder, and the post-run drift report are all prints, and none of them
  reach the terminal on a passing run without `-s`.
- **A full state snapshot is written to disk before any test runs**
  (`qu-hardware-snapshot-<timestamp>.json`, via `mixer.snapshot()`), not just
  held in the fixture's memory. If the process is killed hard enough that the
  `finally` block restoring the spare scene never executes, this file is the
  only remaining record of what the console looked like beforehand — restore
  it by hand, parameter by parameter, using `qumixer-py set`. `snapshot()`/
  `diff()` deliberately do not support a blind automated restore (see the
  module docstring in `qumixer_py/snapshot.py`) — replaying a snapshot as raw
  writes with no pacing is exactly the "single most dangerous operation this
  library could offer" that.5 explicitly ruled out.
- **The post-run diff is written to disk in full**
  (`qu-hardware-snapshot-<timestamp>.drift.json`), not just the first 20
  entries printed to the terminal. A nonzero drift count means the spare-scene
  recall did not fully reproduce the pre-test state — the scene mechanism is
  not documented to capture literally everything the protocol can set — and
  the full file is what you diff by hand against a fresh `qumixer-py dump-state`.

**Cannot be enforced by the harness — these are process and hardware
precautions:**

- **Confirm the spare scene slot is actually free before the run**, and that
  what you store into it *is* the current show. `--qu-confirm-scene-saved` is
  an attestation, not a check — the protocol has no scene-list query, so
  nothing in this library can verify either fact. Track which slot is "the
  test slot" outside the console (venue paperwork, a piece of tape on the
  surface) so it doesn't collide with a real saved show.
- **Never point this suite at a console mixing a live show**, and not only
  because of the single-TCP-connection limit refusing a second client. Once
  connected, the fixture and several tests write real parameter values —
  faders move, a scene recalls, names change — on whatever is patched into
  that console's outputs, audibly and immediately, regardless of what gets
  restored thirty seconds later.
- **Mute or disconnect the PA before running anything that sweeps a live
  level**, in particular `tools/calibrate.py` (section 4 below) and
  `test_a_fader_write_round_trips` /
  `test_whether_the_console_echoes_writes` in the conformance suite.
  `calibrate.py` walks a parameter across its full raw range with a real
  input or output live on it; nothing in the library mutes a channel for you
  before doing that, because the library has no way to know what's actually
  connected to it.
- **Treat the restored state as unverified until you've looked at the drift
  file**, not just at whether the suite exited 0. A recall with zero drift
  reported is reassuring; the fixture cannot promise the console's scene
  mechanism captures every parameter this library can write, only that it
  will tell you, by name, everything it can still see disagreeing afterward.

## 1. Run the conformance suite

```bash
python -m pytest -m hardware --qu-host=192.168.1.60 --qu-spare-scene=100 \
    --qu-confirm-scene-saved -s -v
```

Store the console's current state to scene 100 (or whatever `--qu-spare-scene`
names) from the console's own UI *before* running this — there is no
scene-store message (see [limitations.md](limitations.md)), so nothing here
can do it for you or verify you did it. `--qu-confirm-scene-saved` only
attests that you have; omit it (or skip the store) and the run fails outright
before connecting, per section 0 above. `-s` is required too, or the snapshot path,
the reminder, and the post-run drift report are all silently captured by
pytest and never reach the terminal.

Every test in `tests/hardware/test_conformance.py` already exists; running
them is the task. What each one settles:

| Test | Closes | Interim behaviour it may overturn |
|---|---|---|
| `test_model_firmware_and_channel_are_detected` | Model/firmware detection | — |
| `test_the_state_push_is_substantial` | How complete the state push actually is (undocumented) | — |
| `test_a_fader_write_round_trips` | Basic write path | — |
| `test_whether_the_console_echoes_writes` | Whether the console echoes commanded writes | Assumed yes; downgrades to `UNCONFIRMED` after `echo_window` if not |
| `test_name_get_set_round_trip` | Name write path | — |
| `test_measure_the_name_length_limit` | Channel-name length limit | `MAX_NAME_LENGTH = 6` (provisional) |
| `test_the_meter_frame_size_matches_the_computed_layout` | Meter frame size | Runtime check is already authoritative over the table; this just confirms a real frame passes it |
| `test_the_console_refuses_a_second_connection` | Single-connection behaviour | — |
| `test_measure_the_scene_recall_settling_time` | Scene-recall settle time | `SCENE_SETTLE_DELAY = 0.5s` (protocol note 13) |
| `test_group_mode_reports_its_channel` | What `CH` Group Mode (`5E`) reports on | Accepted from any `CH`, cached under synthetic `GLOBAL` |
| `test_dsnake_addressing` | dSNAKE `CH`/`VX` addressing | Cached under synthetic `DSNAKE` channel, keyed on socket (only meaningful with an AR unit patched in — otherwise this reads `UNKNOWN` and proves nothing) |
| `test_behaviour_at_midi_channel_16` | Behaviour at MIDI channel 16 | Auto-skips unless the console is *already* on channel 16 — see section 2 |

Record the console model, firmware, and every printed measurement (state
push count, echo verdict, measured name limit, settle time) — they're the raw
data the "after" section below acts on.

## 2. Enable the manual-only tests

Three tests are gated behind conditions the fixture can't arrange for you and
need a deliberate extra step:

- **`test_which_qu24_meter_layout_is_correct`** and
  **`test_whether_a_qu24_has_group_5_6`** — both carry
  `@pytest.mark.skipif(True, ...)`. Remove the decorator (or comment it out)
  for the run, and only run these against a Qu-24. They resolve:
  - Whether a Qu-24 has groups 5-6 and 7-8 (`CAPS_BY_BOX_ID[2].groups`,
    currently `2` with `groups_provisional=True`).
  - Which Qu-24 meter layout is correct — `QU24_AS_WRITTEN` (currently
    authoritative) vs `QU24_ALTERNATE` (protocol note 12: the "180 unused" vs
    "100" discrepancy in `qumixer_py/protocol/meters.py`).
- **`test_behaviour_at_midi_channel_16`** — skips unless
  `hw.midi_channel == 15`. Set the Qu's MIDI channel to 16 on its own UI, then
  re-run just this test against it. Confirms the strip sub-API disables
  itself (`mixer.strips is None`, `strips_available is False`) rather than
  wrapping to channel 0.

## 3. Manual observation with no test at all

**LR-assign encoding (`55/07` vs `18`).** The spec permits `55` with `VX=07`
as a second encoding for LR assignment, indistinguishable on paper from the
canonical `18` (protocol note 8). There is no automated test for this — the
`tiers.md` open-questions table names the check as "observe a live LR-assign
toggle under `qumixer-py monitor`":

```bash
qumixer-py monitor --host 192.168.1.60 --wire
```

Toggle a channel's LR assign from the console's own surface and read which
byte sequence (`55 07 ...` or `18 ...`) actually comes across the wire.

## 4. Calibrate the twelve unverified tapers

`docs/tiers.md`'s taper table marks these `verified: no` — every one is a
best-effort log/linear interpolation between two spec-given endpoints, with
nothing measured in between:

| Taper | Param(s) | Range | Suggested channel | Suggested `--unit` |
|---|---|---|---|---|
| `FREQ_TAPER` | `peq.hf/hm/lf/lm.freq` | 20–20000 Hz | `input:1` | `Hz` |
| `HPF_TAPER` | `hpf_freq` | 20–20000 Hz | `input:1` | `Hz` |
| `WIDTH_TAPER` | `peq.hf/hm/lf/lm.width` | 1.5–0.111 oct | `input:1` | `oct` |
| `GATE_ATTACK_TAPER` | `gate.attack` | 0.05–300 ms | `input:1` | `ms` |
| `GATE_RELEASE_TAPER` | `gate.release` | 10–1000 ms | `input:1` | `ms` |
| `GATE_HOLD_TAPER` | `gate.hold` | 10–5000 ms | `input:1` | `ms` |
| `GATE_DEPTH_TAPER` | `gate.depth` | 0–60 dB | `input:1` | `dB` |
| `COMP_ATTACK_TAPER` | `comp.attack` | 0.3–300 ms | `input:1` | `ms` |
| `COMP_RELEASE_TAPER` | `comp.release` | 100–2000 ms | `input:1` | `ms` |
| `COMP_RATIO_TAPER` | `comp.ratio` | 1–1000:1 | `input:1` | `:1` |
| `COMP_THRESHOLD_TAPER` | `comp.threshold` | -46–18 dB | `input:1` | `dB` |
| `MAKEUP_TAPER` | `comp.makeup` | 0–18 dB | `input:1` | `dB` |

For each row:

```bash
python tools/calibrate.py --host 192.168.1.60 --param <param> \
    --channel <channel> --step 8 --unit "<unit>" \
    --out /tmp/<taper_name>.py
```

Open the matching screen on the console when prompted, read off the
displayed value at each step (blank to skip a point you can't read cleanly),
and the tool emits a pasteable `Taper(..., verified=True)` literal. Paste
each one over its current best-effort definition in
`qumixer_py/protocol/units.py`. `comp.*` and `gate.*` also apply to `GROUP`,
`LR`, `MATRIX`, `MIX_MONO`, `MIX_STEREO` and `STEREO_INPUT` — a single input
sweep is sufficient to calibrate the taper (it's a property of the DSP, not
the channel class), so there is no need to repeat this per channel type.

## 5. A hardware assertion the suite is currently missing

The metering requirements say that "known-silent channels read
as very low dB," but
`test_the_meter_frame_size_matches_the_computed_layout` only checks frame
size and layout errors — it never inspects a value. While hardware is
available, add an assertion (mute an input, confirm its decoded meter slot
reads near the floor of the taper) so this requirement actually has a test
behind it, not just a frame-size check standing in for it.

## 6. Record and commit replay fixtures

"Record every hardware session with the `qumixer-py record` CLI and
commit the captures as replay fixtures — they become the regression corpus
and let CI test against real traffic shapes forever after." No `.bin`
captures exist in the tree yet (only `tests/fixtures/golden_params.yaml`,
which is hand-transcribed, not recorded). While hardware is connected:

```bash
qumixer-py record --host 192.168.1.60 --out tests/fixtures/hardware/sync.bin --seconds 30
```

Capture at least one full sync handshake and, if convenient, one with
metering enabled and one spanning a scene recall. `FakeQu` already accepts
`--replay <file>` (`qumixer_py/testing/fake_qu.py`); once fixtures are committed,
add a test that serves each one back through `FakeQu` and exercises the
client against it, so real traffic shapes are covered by every future CI run
without needing hardware again.

## 7. After the run: fold the findings back into the code

None of the above is done until the measurements change something. For each
open question resolved:

- **Promote or demote tiers.** A parameter whose behaviour was confirmed
  moves from `SUPPORTED`/`EXPERIMENTAL` to `VERIFIED` — set on the
  `ParamSpec`, not by hand-editing `docs/tiers.md`.
- **Update the interim values named in the table in section 1** if hardware
  disagreed with them: `CAPS_BY_BOX_ID[2].groups` and `groups_provisional`,
  `LAYOUTS["qu24"]` vs `QU24_ALTERNATE`, `MAX_NAME_LENGTH`,
  `SCENE_SETTLE_DELAY`, the dSNAKE cache key shape, the `CH` `group_mode` is
  accepted under.
- **Paste the twelve calibrated tapers** into `qumixer_py/protocol/units.py` with
  `verified=True`.
- **Regenerate `docs/tiers.md`**: `python tools/gen_tiers.py > docs/tiers.md`
  — never hand-edit it; `tests/test_docs.py::test_the_tier_table_matches_the_registry`
  enforces this.
- **Trim resolved items** from the "Open questions" table in `docs/tiers.md`
  (it is generated -- edit `tools/gen_tiers.py`).
- **Update `docs/protocol-notes.md`** if a deviation (notes 8, 11, 12,
  13) turns out to need a different resolution than the one currently
  documented.
- Commit the new replay fixtures from section 6 and wire them into the non-hardware
  suite.

## Definition of done

Every row in the table below is checked, every open question is
closed, `docs/tiers.md` shows at least one `VERIFIED` row, and the "Status"
section of the README no longer says no parameter has been checked against
hardware.

- [ ] section 1 conformance suite run against a Qu-24
- [ ] section 1 conformance suite run against a Qu-32
- [ ] section 2 Qu-24 group-count test enabled and run
- [ ] section 2 Qu-24 meter-layout test enabled and run
- [ ] section 2 MIDI-channel-16 test run with the console actually on channel 16
- [ ] section 3 LR-assign wire encoding observed
- [ ] section 4 all twelve tapers calibrated and pasted in with `verified=True`
- [ ] section 5 silent-channel meter assertion added
- [ ] section 6 at least one hardware session recorded and committed as a replay fixture
- [ ] section 7 tiers promoted/demoted, interim constants updated, docs regenerated, open-questions lists trimmed
