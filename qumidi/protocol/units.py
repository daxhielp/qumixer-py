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
)
