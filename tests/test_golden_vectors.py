"""The highest-value test set (PRD 13.2).

These vectors are transcribed by hand from the spec, not generated from the
registry. They exist to catch a wrong VX filler byte or a transposed NRPN id,
which produce messages the mixer silently ignores -- the single hardest
failure mode to debug in the field.
"""

from __future__ import annotations

import pathlib

import pytest

from qumidi.midi.nrpn import encode_nrpn
from qumidi.protocol.channels import channel_from_raw
from qumidi.protocol.registry import REGISTRY

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "golden_params.yaml"


def load_vectors(path: pathlib.Path) -> list[dict[str, object]]:
    """Read the deliberately flat fixture format without a YAML dependency."""
    vectors: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    for line in path.read_text().splitlines():
        stripped = line.split("#", 1)[0].rstrip()
        if not stripped.strip():
            continue
        if stripped.startswith("- "):
            current = {}
            vectors.append(current)
            stripped = "  " + stripped[2:]
        assert current is not None, f"fixture line outside an entry: {line!r}"
        key, _, raw = stripped.strip().partition(":")
        value = raw.strip()
        if value.startswith('"') and value.endswith('"'):
            current[key] = value[1:-1]
        elif value == "null":
            current[key] = None
        else:
            # Bare scalars are almost always hex/int literals (channel, value,
            # vx, midi_channel) here, but `param: fader` is a bare string too
            # -- fall back to the literal text rather than assuming every
            # unquoted, non-null scalar parses as a number.
            try:
                current[key] = int(value, 0)
            except ValueError:
                current[key] = value
    return vectors


VECTORS = load_vectors(FIXTURE)


def vector_id(vec: dict[str, object]) -> str:
    return f"{vec['param']}-ch{vec['channel']:#04x}-v{vec['value']:#04x}-vx{vec['vx']}"  # type: ignore[str-format]


def spec_for(param: str, cls):  # type: ignore[no-untyped-def]
    """Resolve a vector's ParamSpec, tolerating console-global parameters.

    group_mode has no documented CH (PRD 7.2), so its vector carries a
    placeholder channel byte whose class the spec does not claim. Fall back to
    a class-free lookup for those rather than special-casing the fixture.
    """
    from qumidi.errors import UnknownParameterError

    try:
        return REGISTRY.lookup(param, cls)
    except UnknownParameterError:
        return REGISTRY.lookup(param)


@pytest.mark.parametrize("vec", VECTORS, ids=vector_id)
def test_golden_vector_encodes_to_the_expected_bytes(vec: dict[str, object]) -> None:
    ch = channel_from_raw(int(vec["channel"]))  # type: ignore[arg-type]
    spec = spec_for(str(vec["param"]), ch.cls)
    vx = spec.vx.validate(vec["vx"], spec.name)  # type: ignore[arg-type]
    got = encode_nrpn(
        midi_channel=int(vec.get("midi_channel", 0)),  # type: ignore[arg-type]
        ch=ch.value,
        param_id=spec.nrpn_id,
        value=int(vec["value"]),  # type: ignore[arg-type]
        vx=vx,
    )
    expected = bytes.fromhex(str(vec["expected"]).replace(" ", ""))
    assert got == expected, (
        f"{spec.name}: encoded {got.hex(' ')} but the spec says {expected.hex(' ')}"
    )


@pytest.mark.parametrize("vec", VECTORS, ids=vector_id)
def test_golden_vector_parses_back_to_the_same_logical_value(vec: dict[str, object]) -> None:
    # PRD 16.2: set via API -> observed bytes match the vector -> parsed back
    # to the same logical value.
    from qumidi.midi.nrpn import NrpnDecoder
    from qumidi.midi.parser import MidiStreamParser

    expected = bytes.fromhex(str(vec["expected"]).replace(" ", ""))
    decoder = NrpnDecoder()
    updates = []
    for msg in MidiStreamParser().feed(expected):
        updates.extend(decoder.feed(msg))
    updates.extend(decoder.flush())
    assert len(updates) == 1
    update = updates[0]
    ch = channel_from_raw(int(vec["channel"]))  # type: ignore[arg-type]
    spec = spec_for(str(vec["param"]), ch.cls)
    assert update.ch == ch.value
    assert update.param_id == spec.nrpn_id
    assert update.value == int(vec["value"])  # type: ignore[arg-type]
    assert REGISTRY.resolve(ch.cls, update.param_id, update.vx) is spec


def test_the_fixture_actually_loaded() -> None:
    assert len(VECTORS) >= 14
