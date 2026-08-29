"""THE parameter registry (PRD 7.2).

This is the core asset. Everything else is generic machinery over it. A
parameter is described here exactly once, and both the flat command API and
the object facade are generated from these rows.

The VX filler value is inconsistent across parameters and is transcribed
literally from the spec, never inferred. Getting one wrong produces messages
the mixer silently ignores, which is nearly impossible to debug in the field
-- tests/fixtures/golden_params.yaml exists primarily to guard this table.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence

from qumidi.errors import ParameterRangeError, UnknownParameterError
from qumidi.protocol.channels import (
    ChannelClass,
    ChannelId,
    FxSend,
    Group,
    Matrix,
    Mix,
    ModelCaps,
    check_channel,
)
from qumidi.protocol.params import (
    Boolean,
    Fixed,
    Interpolated,
    ParamSpec,
    Raw7Bit,
    SendTarget,
)
from qumidi.protocol.units import FADER_TAPER

log = logging.getLogger(__name__)

# --- Send targets (PRD 7.2, resolving the contradiction in PRD 14.1) ------

#: The single canonical send-target table. Used by all four send-family
#: parameters: Pan (16), Mix Pre/Post (50), Mix Assign (55), Send Level (20).
SEND_TARGET_VX: dict[ChannelId, int] = {
    Mix.MIX1: 0x00,
    Mix.MIX2: 0x01,
    Mix.MIX3: 0x02,
    Mix.MIX4: 0x03,
    Mix.MIX56: 0x04,
    Mix.MIX78: 0x05,
    Mix.MIX910: 0x06,
    Mix.LR: 0x07,
    Group(1): 0x08,
    Group(2): 0x09,
    Group(3): 0x0A,
    Group(4): 0x0B,
    Matrix(1): 0x0C,
    Matrix(2): 0x0D,
    # Note the gap at 0x0E-0x0F: send targets are NOT a dense range.
    FxSend(1): 0x10,
    FxSend(2): 0x11,
    FxSend(3): 0x12,
    FxSend(4): 0x13,
}

VX_TO_SEND_TARGET: dict[int, ChannelId] = {vx: ch for ch, vx in SEND_TARGET_VX.items()}


def send_target_vx(target: ChannelId, caps: ModelCaps | None) -> int:
    """Look up a send destination's VX byte, validated against the model.

    Never compute this from arithmetic on a mix index -- the table has a gap
    at 0x0E-0x0F (PRD 7.2).
    """
    try:
        vx = SEND_TARGET_VX[target]
    except KeyError:
        raise ParameterRangeError(
            f"{target.label} is not a send destination; valid destinations are "
            f"{', '.join(t.label for t in SEND_TARGET_VX)}"
        ) from None
    check_channel(caps, target)
    return vx


# --- The registry ---------------------------------------------------------

_INPUTS = frozenset({ChannelClass.INPUT, ChannelClass.STEREO_INPUT})
_FX_RETURNS = frozenset({ChannelClass.FX_RETURN})
_MIXES = frozenset(
    {ChannelClass.MIX_MONO, ChannelClass.MIX_STEREO, ChannelClass.LR,
     ChannelClass.GROUP, ChannelClass.MATRIX}
)
_DCA = frozenset({ChannelClass.DCA})
#: Everything with a fader: inputs, stereo inputs, FX returns, DCAs, mixes.
_FADER_CLASSES = _INPUTS | _FX_RETURNS | _DCA | _MIXES
#: Everything that can be a *source* of a send.
_SEND_SOURCES = _INPUTS | _FX_RETURNS | frozenset(
    {ChannelClass.MIX_MONO, ChannelClass.MIX_STEREO, ChannelClass.GROUP}
)


SPECS: list[ParamSpec] = [
    ParamSpec(
        name="fader",
        nrpn_id=0x17,
        vx=Fixed(0x07),
        domain=Interpolated(lo=0x00, hi=0x7F, taper=FADER_TAPER),
        applies_to=_FADER_CLASSES,
    ),
    ParamSpec(
        name="send_level",
        nrpn_id=0x20,
        vx=SendTarget(),
        domain=Interpolated(lo=0x00, hi=0x7F, taper=FADER_TAPER),
        applies_to=_SEND_SOURCES,
        notes="Same taper as the fader, including the undefined 0x01-0x0B band (PRD 8.1).",
    ),
    ParamSpec(
        name="pan",
        nrpn_id=0x16,
        vx=SendTarget(),
        # PRD 14.9: centre is 0x25 and the range stops at 0x4A. NOT symmetric
        # about 0x40 -- treating it that way pans every channel hard left.
        domain=Raw7Bit(lo=0x00, hi=0x4A),
        applies_to=_SEND_SOURCES,
        notes="Centre 0x25, full left 0x00, full right 0x4A. There is no separate "
        "channel-pan parameter: a channel's LR pan is pan with VX=0x07 (PRD 7.2).",
    ),
    ParamSpec(
        name="lr_assign",
        nrpn_id=0x18,
        vx=Fixed(0x07),
        domain=Boolean(),
        applies_to=_SEND_SOURCES,
        notes="PRD 14.8: the canonical encoding for LR assignment. 55/07 is rejected "
        "on write and normalised to this key on read.",
    ),
    ParamSpec(
        name="mix_assign",
        nrpn_id=0x55,
        vx=SendTarget(),
        domain=Boolean(),
        applies_to=_SEND_SOURCES,
        notes="PRD 14.8: valid only for VX in 00-06, 08-0D, 10-13. Encoding 55/07 is a "
        "ParameterRangeError -- use lr_assign (18) for LR.",
    ),
    ParamSpec(
        name="mix_pre",
        nrpn_id=0x50,
        vx=SendTarget(),
        domain=Boolean(off=0x00, on=0x01),
        applies_to=_SEND_SOURCES,
        notes="0x00 post, 0x01 pre.",
    ),
    ParamSpec(
        name="pafl",
        nrpn_id=0x51,
        vx=Fixed(0x07),
        domain=Boolean(),
        applies_to=_FADER_CLASSES,
    ),
]


class Registry:
    """Name and wire lookups over a ParamSpec table."""

    def __init__(self, specs: Sequence[ParamSpec]) -> None:
        self._specs = list(specs)
        self._by_name: dict[tuple[str, ChannelClass], ParamSpec] = {}
        self._by_name_any: dict[str, list[ParamSpec]] = {}
        self._by_class_id: dict[tuple[ChannelClass, int], ParamSpec] = {}
        self._by_class_id_vx: dict[tuple[ChannelClass, int, int], ParamSpec] = {}
        self._by_id: dict[int, list[ParamSpec]] = {}
        for spec in self._specs:
            self._by_name_any.setdefault(spec.name, []).append(spec)
            self._by_id.setdefault(spec.nrpn_id, []).append(spec)
            for cls in spec.applies_to:
                self._by_name[(spec.name, cls)] = spec
                self._by_class_id[(cls, spec.nrpn_id)] = spec
                if isinstance(spec.vx, Fixed):
                    self._by_class_id_vx[(cls, spec.nrpn_id, spec.vx.value)] = spec

    def __iter__(self) -> Iterator[ParamSpec]:
        return iter(self._specs)

    def __len__(self) -> int:
        return len(self._specs)

    def register_exact(self, spec: ParamSpec, vx: int) -> None:
        """Index a spec under an exact (class, id, vx) key.

        Used for the FX-delay 48 collision, where coarse-time and link share
        an id and are discriminated only by VX (PRD 14.5).
        """
        for cls in spec.applies_to:
            self._by_class_id_vx[(cls, spec.nrpn_id, vx)] = spec

    def lookup(self, name: str, cls: ChannelClass | None = None) -> ParamSpec:
        """Find a spec by name, disambiguating by channel class where needed.

        delay_time is registered twice with disjoint applies_to sets because
        its range is half-scale on inputs (PRD 7.2), so ``cls`` matters there.
        """
        if cls is not None:
            spec = self._by_name.get((name, cls))
            if spec is not None:
                return spec
        candidates = self._by_name_any.get(name)
        if not candidates:
            close = ", ".join(sorted(self._by_name_any)[:12])
            raise UnknownParameterError(
                f"no parameter named {name!r}; known parameters include: {close}"
            )
        if cls is not None:
            raise UnknownParameterError(
                f"parameter {name!r} does not apply to {cls.name}; it applies to "
                f"{', '.join(sorted(c.name for s in candidates for c in s.applies_to))}"
            )
        return candidates[0]

    def resolve(self, cls: ChannelClass, param_id: int, vx: int | None) -> ParamSpec | None:
        """Resolve an inbound raw update (PRD 5.3).

        Three steps, stopping at the first hit. Keying on id alone is
        insufficient: the same id means different things on different channel
        classes, and a future firmware could reuse one.
        """
        # 1. Exact (class, id, vx) -- needed for the FX-delay 48 collision.
        if vx is not None:
            spec = self._by_class_id_vx.get((cls, param_id, vx))
            if spec is not None:
                return spec

        # 2. (class, id) -- the normal case.
        spec = self._by_class_id.get((cls, param_id))
        if spec is not None:
            if isinstance(spec.vx, Fixed) and vx is not None and vx != spec.vx.value:
                # PRD 5.3: decode it anyway, but this is the signature of a
                # transcription error in the registry.
                log.warning(
                    "%s on %s: expected the fixed filler VX %#04x but observed %#04x. "
                    "The registry entry may be mis-transcribed.",
                    spec.name, cls.name, spec.vx.value, vx,
                )
            return spec

        # 3. (id,) across all classes.
        candidates = self._by_id.get(param_id)
        if candidates:
            log.debug(
                "parameter id %#04x resolved to %r on an unexpected channel class %s",
                param_id, candidates[0].name, cls.name,
            )
            return candidates[0]
        return None

    def params_for(self, cls: ChannelClass) -> list[ParamSpec]:
        return [spec for spec in self._specs if cls in spec.applies_to]


REGISTRY = Registry(SPECS)
