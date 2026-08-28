import math

import pytest

from qumidi.protocol.units import FADER_TAPER, Taper, linear_taper

# PRD 8.1, transcribed from the spec table.
FADER_ANCHORS = [
    (10.0, 0x7F), (5.0, 0x72), (0.0, 0x62), (-5.0, 0x4F), (-10.0, 0x3F),
    (-15.0, 0x36), (-20.0, 0x2F), (-25.0, 0x27), (-30.0, 0x1F), (-35.0, 0x17),
    (-40.0, 0x10), (-45.0, 0x0C),
]


@pytest.mark.parametrize("db,raw", FADER_ANCHORS)
def test_every_anchor_point_is_exact_in_both_directions(db, raw) -> None:  # type: ignore[no-untyped-def]
    assert FADER_TAPER.to_db(raw) == pytest.approx(db, abs=0.01)
    assert FADER_TAPER.to_raw(db) == raw


def test_raw_zero_is_a_discrete_minus_infinity() -> None:
    # PRD 8.1: 00 is a discrete -inf, not a point on the curve. Do not
    # interpolate between 00 and 0C.
    assert FADER_TAPER.to_db(0x00) == -math.inf


def test_anything_below_minus_45_db_maps_to_raw_zero() -> None:
    assert FADER_TAPER.to_raw(-46.0) == 0x00
    assert FADER_TAPER.to_raw(-100.0) == 0x00
    assert FADER_TAPER.to_raw(-math.inf) == 0x00


@pytest.mark.parametrize("raw", list(range(0x01, 0x0C)))
def test_the_undefined_band_extrapolates_rather_than_returning_minus_inf(raw) -> None:  # type: ignore[no-untyped-def]
    # PRD 8.1: the console sends these -- the physical fader passes through
    # them. Do not return -inf (raw 0B and raw 00 must stay distinguishable).
    db = FADER_TAPER.to_db(raw)
    assert math.isfinite(db)
    assert -60.0 <= db < -45.0
    assert FADER_TAPER.is_approximate(raw) is True


def test_the_extrapolation_follows_the_0c_to_10_slope() -> None:
    # 0x0C = -45 dB, 0x10 = -40 dB -> 1.25 dB per raw step.
    assert FADER_TAPER.to_db(0x0B) == pytest.approx(-46.25, abs=0.01)
    assert FADER_TAPER.to_db(0x01) == pytest.approx(-58.75, abs=0.01)


def test_the_extrapolation_is_floored_at_minus_60() -> None:
    steep = Taper(points=((0x0C, -45.0), (0x10, -20.0)), neg_inf_at_zero=True,
                  extrapolate_floor=-60.0)
    assert steep.to_db(0x01) == -60.0


@pytest.mark.parametrize("raw", list(range(0x01, 0x0C)))
def test_raw_to_db_to_raw_is_deliberately_not_an_identity_in_the_band(raw) -> None:  # type: ignore[no-untyped-def]
    # PRD 8.1: the inverse is not surjective onto this band, and this
    # asymmetry must be asserted rather than a round trip that cannot hold.
    assert FADER_TAPER.to_raw(FADER_TAPER.to_db(raw)) == 0x00


@pytest.mark.parametrize("raw", list(range(0x0C, 0x80)))
def test_raw_to_db_to_raw_round_trips_above_the_undefined_band(raw) -> None:  # type: ignore[no-untyped-def]
    assert FADER_TAPER.to_raw(FADER_TAPER.to_db(raw)) == pytest.approx(raw, abs=1)


def test_the_taper_is_monotonic_across_the_whole_range() -> None:
    values = [FADER_TAPER.to_db(raw) for raw in range(0x01, 0x80)]
    assert values == sorted(values)


def test_db_round_trips_within_half_a_db() -> None:
    # PRD 8: .db is documented as approximate, ~+-0.5 dB.
    for db in [x / 2 for x in range(-90, 21)]:
        raw = FADER_TAPER.to_raw(db)
        if raw == 0x00:
            continue
        assert FADER_TAPER.to_db(raw) == pytest.approx(db, abs=0.6)


def test_to_raw_clamps_above_the_top_anchor() -> None:
    assert FADER_TAPER.to_raw(99.0) == 0x7F


def test_linear_taper_with_an_asymmetric_centre() -> None:
    # PRD 8.4: 00-7F is 128 steps around a centre of 40, so the span is
    # asymmetric -- 64 steps below zero and 63 above. Anchor on 40 = 0 dB and
    # do not assume +24 dB lands exactly on 7F by symmetry.
    trim = linear_taper(-24.0, 24.0, centre_raw=0x40)
    assert trim.to_db(0x40) == pytest.approx(0.0)
    assert trim.to_db(0x00) == pytest.approx(-24.0)
    assert trim.to_db(0x7F) == pytest.approx(24.0)
    assert trim.to_db(0x20) == pytest.approx(-12.0, abs=0.01)
    # Below centre: 24/64 dB per step. Above: 24/63.
    assert abs(trim.to_db(0x41) - 0.0) == pytest.approx(24.0 / 63, abs=0.001)
    assert abs(trim.to_db(0x3F) - 0.0) == pytest.approx(24.0 / 64, abs=0.001)


def test_linear_taper_without_a_centre() -> None:
    delay_input = linear_taper(0.0, 85.0, hi_raw=0x40)
    assert delay_input.to_db(0x00) == pytest.approx(0.0)
    assert delay_input.to_db(0x40) == pytest.approx(85.0)
    assert delay_input.to_raw(42.5) == 0x20


def test_taper_rejects_unsorted_points() -> None:
    with pytest.raises(ValueError):
        Taper(points=((0x10, 0.0), (0x00, 1.0)))
