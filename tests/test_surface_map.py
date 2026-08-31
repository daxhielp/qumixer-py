import json

import pytest

from qumidi.layout import SurfaceMap
from qumidi.protocol.channels import Dca, Input, Mix


def test_lookup_in_both_directions() -> None:
    custom = SurfaceMap.from_dict({1: Input(6), 2: Input(7), 3: Dca(1), 4: Mix.MIX3})
    assert custom.channel_at(3) == Dca(1)
    assert custom.position_of(Input(7)) == 2
    assert custom.channel_at(9) is None
    assert custom.position_of(Input(30)) is None


def test_load_from_json(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "layer.json"
    path.write_text(json.dumps({"1": "input:6", "2": "dca:1", "3": "lr"}))
    custom = SurfaceMap.from_json(path)
    assert custom.channel_at(2) == Dca(1)
    assert custom.channel_at(3) == Mix.LR


def test_a_map_starts_valid_and_can_be_invalidated() -> None:
    custom = SurfaceMap.from_dict({1: Input(6)})
    assert custom.valid is True
    custom.invalidate()
    assert custom.valid is False
    with pytest.raises(RuntimeError) as excinfo:  # type: ignore
        custom.channel_at(1)
    assert "scene" in str(excinfo.value).lower()


def test_the_docstring_states_the_limitation() -> None:
    # PRD 6.3: the docstring must state that the map is user-supplied, that
    # the library cannot validate it, and that a scene recall can silently
    # invalidate it.
    doc = (SurfaceMap.__doc__ or "").lower()
    assert "user-supplied" in doc or "user supplied" in doc
    assert "cannot validate" in doc
    assert "scene" in doc


def test_the_module_contains_no_protocol_involvement() -> None:
    import pathlib
    import re

    source = pathlib.Path("qumidi/layout.py").read_text()
    assert not re.search(r"0x[0-9A-Fa-f]{2}\b", source)
    assert "sysex" not in source.lower()
    assert "nrpn" not in source.lower()
