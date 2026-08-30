"""Unit conversion (PRD 8).

Contract: the raw 0-127 value is always the ground truth. ``.raw`` is exact
and lossless; ``.db`` is a convenience, documented as approximate (~+-0.5 dB,
limited by 128 steps across the range). Round-tripping db -> raw -> db is not
guaranteed exact; tests assert tolerance, not equality.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Taper:
    """Monotonic piecewise-linear interpolation over raw MIDI values.

    ``points`` are ``(raw, value)`` pairs, ascending by raw. The spec gives
    only sparse anchor points; everything between them is interpolated.
    """

    points: tuple[tuple[int, float], ...]
    #: Raw 0x00 means -inf rather than a point on the curve (fader, send level).
    neg_inf_at_zero: bool = False
    #: Values below the lowest anchor extrapolate its first segment, floored here.
    extrapolate_floor: float | None = None
    unit: str = "dB"
    #: True when this curve is transcribed from measured/spec anchor points.
    #: False for best-effort interpolations (see ``log_taper``) awaiting
    #: hardware calibration -- see tools/calibrate.py (Slice 12).
    verified: bool = False

    def __post_init__(self) -> None:
        raws = [raw for raw, _ in self.points]
        if raws != sorted(raws) or len(set(raws)) != len(raws):
            raise ValueError(f"taper points must be strictly ascending by raw, got {raws}")
        if len(self.points) < 2:
            raise ValueError("a taper needs at least two anchor points")

    # -- raw -> value -----------------------------------------------------

    def to_db(self, raw: int) -> float:
        """Convert a raw value to engineering units. Never raises."""
        if self.neg_inf_at_zero and raw == 0:
            return -math.inf

        lowest_raw, lowest_val = self.points[0]
        if raw < lowest_raw:
            return self._extrapolate_low(raw)
        highest_raw, highest_val = self.points[-1]
        if raw >= highest_raw:
            return highest_val

        for (r0, v0), (r1, v1) in zip(self.points, self.points[1:]):
            if r0 <= raw <= r1:
                if r1 == r0:
                    return v0
                return v0 + (v1 - v0) * (raw - r0) / (r1 - r0)
        return lowest_val  # pragma: no cover - unreachable given the guards

    def _extrapolate_low(self, raw: int) -> float:
        """PRD 8.1: raw 01-0B are transmitted by the console but undefined.

        Extrapolate the first segment rather than returning -inf, which would
        make raw 0x0B and raw 0x00 indistinguishable, and never raise.
        """
        (r0, v0), (r1, v1) = self.points[0], self.points[1]
        slope = (v1 - v0) / (r1 - r0)
        value = v0 + slope * (raw - r0)
        if self.extrapolate_floor is not None:
            value = max(value, self.extrapolate_floor)
        return value

    def is_approximate(self, raw: int) -> bool:
        """True where ``to_db`` extrapolated beyond the documented anchors."""
        if self.neg_inf_at_zero and raw == 0:
            return False
        return raw < self.points[0][0]

    # -- value -> raw -----------------------------------------------------

    def to_raw(self, value: float) -> int:
        """Convert engineering units to the nearest raw value.

        PRD 8.1: this direction is deliberately not surjective onto an
        extrapolated band. Anything strictly below the lowest anchor returns
        the bottom raw value (0x00 when ``neg_inf_at_zero``), so
        raw -> db -> raw is *not* an identity there. The lowest anchor's own
        value must still map back onto the lowest anchor's own raw -- it is a
        point on the curve, not part of the excluded band below it -- so this
        clamp is strict (``<``, not ``<=``) and equality falls through to the
        interpolation loop below.
        """
        lowest_raw, lowest_val = self.points[0]
        highest_raw, highest_val = self.points[-1]

        if value < lowest_val:
            return 0 if self.neg_inf_at_zero else lowest_raw
        if value >= highest_val:
            return highest_raw

        for (r0, v0), (r1, v1) in zip(self.points, self.points[1:]):
            if v0 <= value <= v1:
                if v1 == v0:
                    return r0
                return round(r0 + (r1 - r0) * (value - v0) / (v1 - v0))
        return highest_raw  # pragma: no cover - unreachable given the guards


def linear_taper(
    lo: float,
    hi: float,
    *,
    centre_raw: int | None = None,
    centre: float = 0.0,
    lo_raw: int = 0x00,
    hi_raw: int = 0x7F,
    unit: str = "dB",
) -> Taper:
    """Build a linear taper, optionally anchored on an asymmetric centre.

    PRD 8.4: 00-7F is 128 steps around a centre of 0x40, so the span is
    asymmetric -- 64 steps below zero and 63 above. Anchoring on the centre
    keeps 0 dB exactly on 0x40 rather than assuming symmetry lands +24 dB on
    0x7F.
    """
    if centre_raw is None:
        return Taper(points=((lo_raw, lo), (hi_raw, hi)), unit=unit)
    return Taper(points=((lo_raw, lo), (centre_raw, centre), (hi_raw, hi)), unit=unit)


#: PRD 8.1. Shared by Fader (17) and Send Level (20) -- the same taper, and
#: the same treatment of the undefined 01-0B band.
FADER_TAPER = Taper(
    points=(
        (0x0C, -45.0),
        (0x10, -40.0),
        (0x17, -35.0),
        (0x1F, -30.0),
        (0x27, -25.0),
        (0x2F, -20.0),
        (0x36, -15.0),
        (0x3F, -10.0),
        (0x4F, -5.0),
        (0x62, 0.0),
        (0x72, 5.0),
        (0x7F, 10.0),
    ),
    neg_inf_at_zero=True,
    extrapolate_floor=-60.0,
    verified=True,
)


def log_taper(lo: float, hi: float, *, lo_raw: int = 0x00, hi_raw: int = 0x7F,
              unit: str = "Hz", points: int = 17) -> Taper:
    """A logarithmic curve between two endpoints. UNVERIFIED.

    PRD 8.4: the spec gives endpoints only for frequencies, widths and the
    gate/compressor times. This is a best-effort interpolation, not a measured
    curve. Expose ``.raw`` as primary and treat these values as indicative
    until tools/calibrate.py has swept the parameter on real hardware.
    """
    span = hi_raw - lo_raw
    anchors: list[tuple[int, float]] = []
    for i in range(points):
        raw = lo_raw + round(span * i / (points - 1))
        frac = i / (points - 1)
        anchors.append((raw, lo * (hi / lo) ** frac))
    deduped = tuple(sorted({raw: value for raw, value in anchors}.items()))
    return Taper(points=deduped, unit=unit, verified=False)


#: PRD 8.2. Local preamp gain, ID 19. Measured anchors from the spec.
LOCAL_GAIN_TAPER = Taper(
    points=((0x00, -5.0), (0x0A, 0.0), (0x13, 5.0), (0x1D, 10.0), (0x30, 20.0),
            (0x44, 30.0), (0x57, 40.0), (0x6B, 50.0), (0x7F, 60.0)),
    verified=True,
)

#: PRD 8.3. dSNAKE preamp gain, ID 58. A DIFFERENT curve -- do not share the
#: table with the local preamp above.
DSNAKE_GAIN_TAPER = Taper(
    points=((0x00, 5.0), (0x0B, 10.0), (0x22, 20.0), (0x2E, 25.0), (0x39, 30.0),
            (0x45, 35.0), (0x50, 40.0), (0x67, 50.0), (0x7F, 60.0)),
    verified=True,
)

#: PRD 8.4. Digital/stereo trim (52, 54): linear, -24..+24 dB, 0 dB = 0x40.
TRIM_TAPER = linear_taper(-24.0, 24.0, centre_raw=0x40)

#: PRD 8.4. PEQ and GEQ gain: linear, -12..+12 dB, 0 dB = 0x40.
PEQ_GAIN_TAPER = linear_taper(-12.0, 12.0, centre_raw=0x40)
GEQ_GAIN_TAPER = linear_taper(-12.0, 12.0, centre_raw=0x40)

#: PRD 7.2. Input channels: 0-85 ms across 0x00-0x40. Half-scale.
INPUT_DELAY_TAPER = linear_taper(0.0, 85.0, hi_raw=0x40, unit="ms")
#: PRD 7.2. Mix/group/matrix: 0-170 ms across 0x00-0x7F.
MIX_DELAY_TAPER = linear_taper(0.0, 170.0, unit="ms")

# --- Unverified curves (PRD 8.4) -----------------------------------------
# The spec gives endpoints only. These are best-effort and are the main thing
# hardware calibration buys -- see tools/calibrate.py.

FREQ_TAPER = log_taper(20.0, 20000.0, unit="Hz")
HPF_TAPER = log_taper(20.0, 20000.0, unit="Hz")
#: 1.5 octaves down to 1/9 octave. Descending in value, so the anchors invert.
WIDTH_TAPER = Taper(
    points=tuple(
        (raw, 1.5 * ((1 / 9) / 1.5) ** (raw / 127.0)) for raw in range(0, 128, 8)
    ) + ((127, 1 / 9),),
    unit="oct",
    verified=False,
)

GATE_ATTACK_TAPER = log_taper(0.05, 300.0, unit="ms")      # 50 us .. 300 ms
GATE_RELEASE_TAPER = log_taper(10.0, 1000.0, unit="ms")    # 10 ms .. 1 s
GATE_HOLD_TAPER = log_taper(10.0, 5000.0, unit="ms")       # 10 ms .. 5 s
GATE_THRESHOLD_TAPER = linear_taper(-72.0, 18.0)           # -72 .. +18 dB
GATE_DEPTH_TAPER = linear_taper(0.0, 60.0)                 # 0 .. 60 dB

COMP_ATTACK_TAPER = log_taper(0.3, 300.0, unit="ms")       # 300 us .. 300 ms
COMP_RELEASE_TAPER = log_taper(100.0, 2000.0, unit="ms")   # 100 ms .. 2 s
COMP_THRESHOLD_TAPER = linear_taper(-46.0, 18.0)           # -46 .. +18 dB
MAKEUP_TAPER = linear_taper(0.0, 18.0)                     # 0 .. +18 dB

#: PRD 7.2 gives 1:1 at the bottom, infinity at the top, and one interior
#: anchor: 2.6:1 = 0x50. Anchored on that point and extended log-wise.
COMP_RATIO_TAPER = Taper(
    points=((0x00, 1.0), (0x50, 2.6), (0x7E, 20.0), (0x7F, 1000.0)),
    unit=":1",
    verified=False,
)
