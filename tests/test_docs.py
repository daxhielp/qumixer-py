"""The documentation deliverables exist and are honest (PRD 17)."""

from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

DOCS = pathlib.Path("docs")
EXAMPLES = pathlib.Path("examples")


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "docs/limitations.md",
        "docs/troubleshooting.md",
        "docs/tiers.md",
        "docs/protocol-notes.md",
    ],
)
def test_the_document_exists_and_is_not_a_stub(path) -> None:  # type: ignore[no-untyped-def]
    text = pathlib.Path(path).read_text()
    assert len(text) > 500, f"{path} looks like a stub"
    assert "TODO" not in text and "TBD" not in text


def test_the_readme_carries_both_warnings_prominently() -> None:
    # PRD 17 and 18: the single-TCP-connection caveat prominently placed, and
    # the security statement said plainly.
    text = pathlib.Path("README.md").read_text()
    head = text[:2000]
    assert "one tcp midi connection" in head.lower() or "one connection" in head.lower()
    assert "unauthenticated" in head.lower()
    assert "docs/limitations.md" in text


def test_the_library_offers_no_password_parameter() -> None:
    # PRD 18: do not add a "password" parameter -- there is nothing to
    # authenticate against.
    import inspect

    from qumidi.client_async import AsyncQuMixer

    params = inspect.signature(AsyncQuMixer.__init__).parameters
    assert not any("pass" in name or "auth" in name for name in params)


def test_limitations_covers_every_documented_gap() -> None:
    text = pathlib.Path("docs/limitations.md").read_text().lower()
    for topic in (
        "custom layer",
        "scene",
        "targeted",
        "acknowledge",
        "one tcp client",
        "usb",
        "fx",
        "unverified",
    ):
        assert topic in text, f"limitations.md does not mention {topic}"


@pytest.mark.parametrize(
    "script",
    ["fader_follow.py", "scene_automation.py", "meter_monitor.py", "state_dump.py"],
)
def test_every_example_runs_against_fake_qu(script, fake_qu_sync) -> None:  # type: ignore[no-untyped-def]
    result = subprocess.run(
        [
            sys.executable,
            str(EXAMPLES / script),
            "--host",
            "127.0.0.1",
            "--port",
            str(fake_qu_sync.port),
            "--once",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_the_tier_table_matches_the_registry() -> None:
    from qumidi.protocol.registry import REGISTRY

    text = pathlib.Path("docs/tiers.md").read_text()
    for spec in REGISTRY:
        assert f"`{spec.name}`" in text, f"{spec.name} is missing from docs/tiers.md"


def test_the_tier_table_is_not_hand_edited_out_of_sync_with_the_registry() -> None:
    # tools/gen_tiers.py is the single source of docs/tiers.md; regenerating
    # it must reproduce the checked-in file byte for byte, or someone hand
    # edited the doc (or the registry moved on without it).
    import importlib

    gen_tiers = importlib.import_module("tools.gen_tiers")
    generated = gen_tiers.emit()
    on_disk = pathlib.Path("docs/tiers.md").read_text()
    assert generated == on_disk, (
        "docs/tiers.md is out of sync with the registry; regenerate with "
        "`python tools/gen_tiers.py > docs/tiers.md`"
    )


def test_the_library_configures_no_logging_handlers() -> None:
    # PRD 18: the library configures no handlers and never calls basicConfig.
    for path in pathlib.Path("qumidi").rglob("*.py"):
        if path.name in ("cli.py", "fake_qu.py"):
            continue  # entry points may configure logging
        source = path.read_text()
        assert "basicConfig" not in source, path
        assert "addHandler" not in source, path
