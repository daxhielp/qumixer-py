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
    FX_TAP_LEFT,
    FX_TAP_LINK,
    FX_TAP_RIGHT,
    Boolean,
    Direction,
    DsnakeSocket,
    EnumDomain,
    Fixed,
    FxTap,
    GeqBand,
    Interpolated,
    ParamSpec,
    Raw7Bit,
    SendTarget,
    Tier,
)
from qumidi.protocol.units import (
    COMP_ATTACK_TAPER,
    COMP_RATIO_TAPER,
    COMP_RELEASE_TAPER,
    COMP_THRESHOLD_TAPER,
    DSNAKE_GAIN_TAPER,
    FADER_TAPER,
    FREQ_TAPER,
    GATE_ATTACK_TAPER,
    GATE_DEPTH_TAPER,
    GATE_HOLD_TAPER,
    GATE_RELEASE_TAPER,
    GATE_THRESHOLD_TAPER,
    GEQ_GAIN_TAPER,
    HPF_TAPER,
    INPUT_DELAY_TAPER,
    LOCAL_GAIN_TAPER,
    MAKEUP_TAPER,
    MIX_DELAY_TAPER,
    PEQ_GAIN_TAPER,
    TRIM_TAPER,
    WIDTH_TAPER,
)

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
#: PEQ/HPF live on inputs, FX returns and mixes -- not DCAs.
_EQ_CLASSES = _INPUTS | _FX_RETURNS | _MIXES
#: Gate and compressor live on inputs and mixes -- not DCAs (PRD 7.2).
_DYNAMICS_CLASSES = _INPUTS | _MIXES

#: PRD 7.2: 28 bands, index 0x00-0x1B. 0-based throughout, matching the wire
#: encoding. Band 14 is 800 Hz and band 15 is 1 kHz (PRD 11.1).
GEQ_BANDS: tuple[float, ...] = (
    31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630, 800,
    1000, 1250, 1600, 2000, 2500, 3150, 4000, 5000, 6300, 8000, 10000, 12500, 16000,
)


def geq_band_for_freq(hz: float) -> int:
    """Resolve a frequency to its 0-based GEQ band index.

    Most callers want this rather than the raw index -- see
    geq.set_band_db(freq=1000) in the object facade.
    """
    if not GEQ_BANDS[0] / 1.5 <= hz <= GEQ_BANDS[-1] * 1.5:
        raise ParameterRangeError(
            f"{hz} Hz is outside the GEQ range {GEQ_BANDS[0]}-{GEQ_BANDS[-1]} Hz"
        )
    return min(range(len(GEQ_BANDS)), key=lambda i: abs(GEQ_BANDS[i] - hz))


_PEQ_TYPE = EnumDomain({0x00: "bell", 0x06: "shelf"})
_COMP_TYPE = EnumDomain({
    0x00: "manual_rms", 0x01: "auto_slow_opto",
    0x02: "auto_punchbag", 0x03: "manual_peak",
})
_KNEE = EnumDomain({0x00: "hard", 0x01: "soft"})


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
    # --- Input source and preamp (PRD 7.2) ---
    # NOTE the VX filler alternates: 12, 57 and 5D use 0x00 while the rest of
    # this family uses 0x07. Transcribed literally, not inferred.
    ParamSpec(
        name="usb_source", nrpn_id=0x12, vx=Fixed(0x00), domain=Boolean(),
        applies_to=_INPUTS, notes="0x00 preamp, 0x01 USB.",
    ),
    ParamSpec(
        name="preamp_source", nrpn_id=0x57, vx=Fixed(0x00), domain=Boolean(),
        applies_to=_INPUTS, notes="0x00 local, 0x01 dSNAKE. ch.preamp dispatches on this.",
    ),
    ParamSpec(
        name="dsnake_patch", nrpn_id=0x5D, vx=Fixed(0x00),
        domain=Raw7Bit(lo=0x00, hi=0x27), applies_to=_INPUTS,
        direction=Direction.RX_ONLY,
        notes="PRD 7.2: RX only. The channel->socket map, and the only way to know "
        "which socket a channel's remote preamp lives on.",
    ),
    ParamSpec(
        name="local_gain", nrpn_id=0x19, vx=Fixed(0x07),
        domain=Interpolated(lo=0x00, hi=0x7F, taper=LOCAL_GAIN_TAPER),
        applies_to=frozenset({ChannelClass.INPUT}),
        notes="-5..+60 dB (PRD 8.2). Local preamp has NO pad parameter.",
    ),
    ParamSpec(
        name="local_48v", nrpn_id=0x69, vx=Fixed(0x07), domain=Boolean(),
        applies_to=frozenset({ChannelClass.INPUT}),
    ),
    ParamSpec(
        name="dsnake_gain", nrpn_id=0x58, vx=DsnakeSocket(),
        domain=Interpolated(lo=0x00, hi=0x7F, taper=DSNAKE_GAIN_TAPER),
        applies_to=frozenset({ChannelClass.INPUT}), tier=Tier.EXPERIMENTAL,
        notes="+5..+60 dB (PRD 8.3) -- a different curve from local gain. Socket in VX; "
        "whether CH addresses anything is an M6 question (PRD 7.2), so the cache keys "
        "on the socket under the synthetic DSNAKE channel.",
    ),
    ParamSpec(
        name="dsnake_pad", nrpn_id=0x59, vx=DsnakeSocket(), domain=Boolean(),
        applies_to=frozenset({ChannelClass.INPUT}), tier=Tier.EXPERIMENTAL,
        notes="0x00 out, 0x01 in. Only dSNAKE has a pad.",
    ),
    ParamSpec(
        name="dsnake_48v", nrpn_id=0x5A, vx=DsnakeSocket(), domain=Boolean(),
        applies_to=frozenset({ChannelClass.INPUT}), tier=Tier.EXPERIMENTAL,
    ),
    ParamSpec(
        name="digital_trim", nrpn_id=0x52, vx=Fixed(0x07),
        domain=Interpolated(lo=0x00, hi=0x7F, taper=TRIM_TAPER),
        applies_to=frozenset({ChannelClass.INPUT}),
        notes="-24..+24 dB, 0 dB = 0x40. Applies when the USB source is selected.",
    ),
    ParamSpec(
        name="stereo_trim", nrpn_id=0x54, vx=Fixed(0x07),
        domain=Interpolated(lo=0x00, hi=0x7F, taper=TRIM_TAPER),
        applies_to=frozenset({ChannelClass.STEREO_INPUT}),
    ),
    ParamSpec(
        name="polarity", nrpn_id=0x6A, vx=Fixed(0x07), domain=Boolean(),
        applies_to=_INPUTS, notes="0x00 normal, 0x01 reversed.",
    ),
    ParamSpec(
        name="insert", nrpn_id=0x6B, vx=Fixed(0x07), domain=Boolean(),
        applies_to=_INPUTS | _MIXES, notes="0x00 out, 0x01 in.",
    ),
    ParamSpec(
        name="group_mode", nrpn_id=0x5E, vx=Fixed(0x00),
        domain=Boolean(), applies_to=frozenset({ChannelClass.GLOBAL}),
        direction=Direction.RX_ONLY, tier=Tier.EXPERIMENTAL,
        notes="PRD 7.2: console-global with no documented CH. Stored under the synthetic "
        "GLOBAL channel and accepted from any CH until M6 records the real one. "
        "0x00 group mode, 0x01 mix mode.",
    ),

    # --- PEQ and HPF (PRD 7.2). IDs 08 and 0C are deliberately unassigned. ---
    ParamSpec(name="peq.lf.gain", nrpn_id=0x01, vx=Fixed(0x07),
              domain=Interpolated(lo=0x00, hi=0x7F, taper=PEQ_GAIN_TAPER),
              applies_to=_EQ_CLASSES),
    ParamSpec(name="peq.lf.freq", nrpn_id=0x02, vx=Fixed(0x07),
              domain=Interpolated(lo=0x00, hi=0x7F, taper=FREQ_TAPER),
              applies_to=_EQ_CLASSES, notes="20 Hz-20 kHz, curve unverified (PRD 8.4)."),
    ParamSpec(name="peq.lf.width", nrpn_id=0x03, vx=Fixed(0x07),
              domain=Interpolated(lo=0x00, hi=0x7F, taper=WIDTH_TAPER),
              applies_to=_EQ_CLASSES, notes="1.5 to 1/9 octave, curve unverified."),
    ParamSpec(name="peq.lf.type", nrpn_id=0x04, vx=Fixed(0x07), domain=_PEQ_TYPE,
              applies_to=_EQ_CLASSES),
    ParamSpec(name="peq.lm.gain", nrpn_id=0x05, vx=Fixed(0x07),
              domain=Interpolated(lo=0x00, hi=0x7F, taper=PEQ_GAIN_TAPER),
              applies_to=_EQ_CLASSES),
    ParamSpec(name="peq.lm.freq", nrpn_id=0x06, vx=Fixed(0x07),
              domain=Interpolated(lo=0x00, hi=0x7F, taper=FREQ_TAPER),
              applies_to=_EQ_CLASSES),
    ParamSpec(name="peq.lm.width", nrpn_id=0x07, vx=Fixed(0x07),
              domain=Interpolated(lo=0x00, hi=0x7F, taper=WIDTH_TAPER),
              applies_to=_EQ_CLASSES),
    # 0x08 unassigned: LM has no Type.
    ParamSpec(name="peq.hm.gain", nrpn_id=0x09, vx=Fixed(0x07),
              domain=Interpolated(lo=0x00, hi=0x7F, taper=PEQ_GAIN_TAPER),
              applies_to=_EQ_CLASSES),
    ParamSpec(name="peq.hm.freq", nrpn_id=0x0A, vx=Fixed(0x07),
              domain=Interpolated(lo=0x00, hi=0x7F, taper=FREQ_TAPER),
              applies_to=_EQ_CLASSES),
    ParamSpec(name="peq.hm.width", nrpn_id=0x0B, vx=Fixed(0x07),
              domain=Interpolated(lo=0x00, hi=0x7F, taper=WIDTH_TAPER),
              applies_to=_EQ_CLASSES),
    # 0x0C unassigned: HM has no Type.
    ParamSpec(name="peq.hf.gain", nrpn_id=0x0D, vx=Fixed(0x07),
              domain=Interpolated(lo=0x00, hi=0x7F, taper=PEQ_GAIN_TAPER),
              applies_to=_EQ_CLASSES),
    ParamSpec(name="peq.hf.freq", nrpn_id=0x0E, vx=Fixed(0x07),
              domain=Interpolated(lo=0x00, hi=0x7F, taper=FREQ_TAPER),
              applies_to=_EQ_CLASSES),
    ParamSpec(name="peq.hf.width", nrpn_id=0x0F, vx=Fixed(0x07),
              domain=Interpolated(lo=0x00, hi=0x7F, taper=WIDTH_TAPER),
              applies_to=_EQ_CLASSES),
    ParamSpec(name="peq.hf.type", nrpn_id=0x10, vx=Fixed(0x07), domain=_PEQ_TYPE,
              applies_to=_EQ_CLASSES),
    ParamSpec(name="peq_in", nrpn_id=0x11, vx=Fixed(0x00), domain=Boolean(),
              applies_to=_EQ_CLASSES, notes="VX is 0x00 here, not 0x07."),
    ParamSpec(name="hpf_freq", nrpn_id=0x13, vx=Fixed(0x07),
              domain=Interpolated(lo=0x00, hi=0x7F, taper=HPF_TAPER),
              applies_to=_INPUTS),
    ParamSpec(name="hpf_in", nrpn_id=0x14, vx=Fixed(0x00), domain=Boolean(),
              applies_to=_INPUTS, notes="VX is 0x00 here, not 0x07."),

    # --- GEQ (PRD 7.2). Mixes and LR only. ---
    ParamSpec(name="geq_gain", nrpn_id=0x70, vx=GeqBand(),
              domain=Interpolated(lo=0x00, hi=0x7F, taper=GEQ_GAIN_TAPER),
              applies_to=_MIXES,
              notes="28 bands, index 0x00-0x1B. Band 15 is 1 kHz (PRD 11.1)."),
    ParamSpec(name="geq_in", nrpn_id=0x71, vx=Fixed(0x00), domain=Boolean(),
              applies_to=_MIXES, notes="VX is 0x00 here, not 0x07."),

    # --- Gate (PRD 7.2). gate_in uses VX 0x00. ---
    ParamSpec(name="gate.attack", nrpn_id=0x41, vx=Fixed(0x07),
              domain=Interpolated(lo=0x00, hi=0x7F, taper=GATE_ATTACK_TAPER),
              applies_to=_INPUTS, notes="50 us - 300 ms, curve unverified (PRD 8.4)."),
    ParamSpec(name="gate.release", nrpn_id=0x42, vx=Fixed(0x07),
              domain=Interpolated(lo=0x00, hi=0x7F, taper=GATE_RELEASE_TAPER),
              applies_to=_INPUTS, notes="10 ms - 1 s, curve unverified."),
    ParamSpec(name="gate.hold", nrpn_id=0x43, vx=Fixed(0x07),
              domain=Interpolated(lo=0x00, hi=0x7F, taper=GATE_HOLD_TAPER),
              applies_to=_INPUTS, notes="10 ms - 5 s, curve unverified."),
    ParamSpec(name="gate.threshold", nrpn_id=0x44, vx=Fixed(0x07),
              domain=Interpolated(lo=0x00, hi=0x7F, taper=GATE_THRESHOLD_TAPER),
              applies_to=_INPUTS),
    ParamSpec(name="gate.depth", nrpn_id=0x45, vx=Fixed(0x07),
              domain=Interpolated(lo=0x00, hi=0x7F, taper=GATE_DEPTH_TAPER),
              applies_to=_INPUTS),
    ParamSpec(name="gate_in", nrpn_id=0x46, vx=Fixed(0x00), domain=Boolean(),
              applies_to=_INPUTS, notes="VX is 0x00 here, not 0x07."),

    # --- Compressor (PRD 7.2). comp_in uses VX 0x00. ---
    ParamSpec(name="comp.type", nrpn_id=0x61, vx=Fixed(0x07), domain=_COMP_TYPE,
              applies_to=_DYNAMICS_CLASSES),
    ParamSpec(name="comp.attack", nrpn_id=0x62, vx=Fixed(0x07),
              domain=Interpolated(lo=0x00, hi=0x7F, taper=COMP_ATTACK_TAPER),
              applies_to=_DYNAMICS_CLASSES, notes="300 us - 300 ms, curve unverified."),
    ParamSpec(name="comp.release", nrpn_id=0x63, vx=Fixed(0x07),
              domain=Interpolated(lo=0x00, hi=0x7F, taper=COMP_RELEASE_TAPER),
              applies_to=_DYNAMICS_CLASSES, notes="100 ms - 2 s, curve unverified."),
    ParamSpec(name="comp.knee", nrpn_id=0x64, vx=Fixed(0x07), domain=_KNEE,
              applies_to=_DYNAMICS_CLASSES),
    ParamSpec(name="comp.ratio", nrpn_id=0x65, vx=Fixed(0x07),
              domain=Interpolated(lo=0x00, hi=0x7F, taper=COMP_RATIO_TAPER),
              applies_to=_DYNAMICS_CLASSES, notes="1:1 to inf; 2.6:1 = 0x50."),
    ParamSpec(name="comp.threshold", nrpn_id=0x66, vx=Fixed(0x07),
              domain=Interpolated(lo=0x00, hi=0x7F, taper=COMP_THRESHOLD_TAPER),
              applies_to=_DYNAMICS_CLASSES),
    ParamSpec(name="comp.makeup", nrpn_id=0x67, vx=Fixed(0x07),
              domain=Interpolated(lo=0x00, hi=0x7F, taper=MAKEUP_TAPER),
              applies_to=_DYNAMICS_CLASSES),
    ParamSpec(name="comp_in", nrpn_id=0x68, vx=Fixed(0x00), domain=Boolean(),
              applies_to=_DYNAMICS_CLASSES, notes="VX is 0x00 here, not 0x07."),

    # --- Delay (PRD 7.2). Registered twice: the input range is half-scale, or
    # the ms helper is wrong by 2x on inputs. ---
    ParamSpec(name="delay_time", nrpn_id=0x6C, vx=Fixed(0x07),
              domain=Interpolated(lo=0x00, hi=0x40, taper=INPUT_DELAY_TAPER),
              applies_to=_INPUTS,
              notes="Inputs: 0-85 ms across 0x00-0x40. Writing 0x41-0x7F here is a "
              "ParameterRangeError; receiving it clamps to 0x40 (PRD 7.2)."),
    ParamSpec(name="delay_time", nrpn_id=0x6C, vx=Fixed(0x07),
              domain=Interpolated(lo=0x00, hi=0x7F, taper=MIX_DELAY_TAPER),
              applies_to=_MIXES,
              notes="Mix/group/matrix: 0-170 ms across 0x00-0x7F. Linear."),
    ParamSpec(name="delay_in", nrpn_id=0x6D, vx=Fixed(0x00), domain=Boolean(),
              applies_to=_INPUTS | _MIXES, notes="VX is 0x00 here, not 0x07."),

    # --- FX delay (PRD 7.2, 14.5). Experimental. ---
    ParamSpec(name="fx_delay_fine", nrpn_id=0x49, vx=FxTap(),
              domain=Raw7Bit(), applies_to=frozenset({ChannelClass.FX_SEND}),
              tier=Tier.EXPERIMENTAL,
              notes="LSB. For fine resolution send this BEFORE the coarse MSB (PRD 7.2)."),
    ParamSpec(name="fx_delay_coarse", nrpn_id=0x48, vx=FxTap(),
              domain=Raw7Bit(), applies_to=frozenset({ChannelClass.FX_SEND}),
              tier=Tier.EXPERIMENTAL,
              notes="MSB. Shares id 0x48 with fx_delay_link, discriminated only by VX "
              "(PRD 14.5): 0x05 left, 0x07 right."),
    ParamSpec(name="fx_delay_link", nrpn_id=0x48, vx=Fixed(0x06),
              domain=Boolean(off=0x00, on=0x7F),
              applies_to=frozenset({ChannelClass.FX_SEND}), tier=Tier.EXPERIMENTAL,
              notes="Shares id 0x48 with fx_delay_coarse (PRD 14.5). 0x00 unlinked, "
              "0x7F linked."),
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
        # PRD 7.2: Group Mode has no documented CH, so accept it from any class.
        for spec in self._specs:
            if ChannelClass.GLOBAL in spec.applies_to:
                for cls in ChannelClass:
                    self._by_class_id.setdefault((cls, spec.nrpn_id), spec)

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
            close = ", ".join(sorted(self._by_name_any)[:20])
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

# PRD 14.5: 0x48 is both FX-delay coarse time and FX-delay link, discriminated
# only by VX. Registry lookup for inbound decode must key on (id, vx) for this
# family, not id alone.
_COARSE = REGISTRY.lookup("fx_delay_coarse")
REGISTRY.register_exact(_COARSE, FX_TAP_LEFT)
REGISTRY.register_exact(_COARSE, FX_TAP_RIGHT)
REGISTRY.register_exact(REGISTRY.lookup("fx_delay_link"), FX_TAP_LINK)
