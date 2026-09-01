"""One entry per PRD 16 acceptance criterion, naming the test that covers it."""

CRITERIA = {
    1: "tests/hardware/test_conformance.py::test_model_firmware_and_channel_are_detected",
    2: "tests/test_golden_vectors.py::test_golden_vector_parses_back_to_the_same_logical_value",
    3: "tests/test_echo.py::test_a_write_never_fires_external_change",
    4: "tests/test_lifecycle.py::test_reconnect_re_syncs_and_returns_to_ready",
    5: "tests/test_golden_vectors.py::test_the_fixture_covers_every_registry_entry",
    6: "the whole suite runs against FakeQu; see tests/conftest.py",
    7: "the mypy --strict and ruff gates in every task",
    8: "tests/hardware/test_conformance.py::test_the_meter_frame_size_matches_the_computed_layout",
    9: "tests/test_shutdown.py::test_shutdown_without_the_constructor_flag_raises",
    10: "tests/test_state_cache.py::test_two_dca_memberships_are_independently_known",
    11: "tests/test_echo.py::test_an_unmatched_expectation_downgrades_to_unconfirmed",
    12: "tests/test_scene_recall.py::test_scene_recalled_precedes_sync_complete",
    13: "tests/test_meters.py::test_a_wrong_size_frame_disables_metering_and_delivers_nothing",
    14: "tests/model/test_ramp.py::test_a_128_step_ramp_stays_within_the_send_rate",
    15: (
        "tests/conftest.py's fake_qu_factory/fake_qu_sync default midi_channel to "
        "QUMIDI_TEST_MIDI_CHANNEL; `QUMIDI_TEST_MIDI_CHANNEL=5 python -m pytest "
        '-m "not hardware"` runs the whole suite against a non-zero N'
    ),
    16: "tests/transport/test_reconnect.py::test_close_during_backoff_does_not_resurrect_the_connection",
}


def test_every_criterion_names_a_real_test() -> None:
    import pathlib

    for number, ref in CRITERIA.items():
        if "::" not in ref:
            continue
        path, _, name = ref.partition("::")
        assert pathlib.Path(path).exists(), f"criterion {number}: {path} does not exist"
        source = pathlib.Path(path).read_text()
        assert f"def {name}" in source, f"criterion {number}: {name} not found in {path}"


def test_every_prd_16_criterion_is_present() -> None:
    # 16 numbered acceptance criteria in PRD 16, none skipped.
    assert set(CRITERIA) == set(range(1, 17))
