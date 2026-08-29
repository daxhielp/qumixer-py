import pytest

from qumidi.errors import ValueUnknownError
from qumidi.protocol.channels import Input, Mix
from qumidi.protocol.params import Fixed, FlaggedIndex, SendTarget
from qumidi.protocol.registry import REGISTRY
from qumidi.state import (
    UNKNOWN_VALUE,
    CachedValue,
    Freshness,
    StateCache,
    sub_index_for,
)


def clock():  # type: ignore[no-untyped-def]
    t = {"now": 1000.0}
    def now() -> float:
        return t["now"]
    return t, now


def test_an_unseen_value_is_unknown_not_a_default() -> None:
    # PRD 10.1: "I have not been told the value" is categorically different
    # from "the fader is at -inf". Never synthesize defaults.
    cache = StateCache()
    got = cache.get(Input(6), "fader")
    assert got == UNKNOWN_VALUE
    assert got.raw is None
    assert got.freshness is Freshness.UNKNOWN


def test_set_and_get_round_trip_with_a_timestamp() -> None:
    _t, now = clock()
    cache = StateCache(now=now)
    old, new = cache.set(Input(6), "fader", None, 0x62, Freshness.KNOWN)
    assert old is UNKNOWN_VALUE
    assert new == CachedValue(raw=0x62, freshness=Freshness.KNOWN, updated_at=1000.0)
    assert cache.get(Input(6), "fader") == new


def test_the_clock_is_monotonic_not_wall_clock() -> None:
    # PRD 10.1: echo windows and staleness must not break when NTP steps the
    # system clock.
    import time

    cache = StateCache()
    _, new = cache.set(Input(6), "fader", None, 0x62, Freshness.KNOWN)
    assert new.updated_at is not None
    assert abs(new.updated_at - time.monotonic()) < 1.0


def test_send_level_to_different_mixes_are_independent_entries() -> None:
    cache = StateCache()
    cache.set(Input(6), "send_level", 0x02, 0x50, Freshness.KNOWN)
    cache.set(Input(6), "send_level", 0x03, 0x20, Freshness.KNOWN)
    assert cache.get(Input(6), "send_level", 0x02).raw == 0x50
    assert cache.get(Input(6), "send_level", 0x03).raw == 0x20


def test_the_same_param_on_different_channels_is_independent() -> None:
    cache = StateCache()
    cache.set(Input(6), "fader", None, 0x62, Freshness.KNOWN)
    cache.set(Mix.MIX3, "fader", None, 0x10, Freshness.KNOWN)
    assert cache.get(Input(6), "fader").raw == 0x62
    assert cache.get(Mix.MIX3, "fader").raw == 0x10


def test_mark_all_stale_preserves_the_values_but_downgrades_freshness() -> None:
    # PRD 4.4: never serve pre-disconnect values as current.
    cache = StateCache()
    cache.set(Input(6), "fader", None, 0x62, Freshness.KNOWN)
    cache.mark_all_stale()
    got = cache.get(Input(6), "fader")
    assert got.raw == 0x62
    assert got.freshness is Freshness.STALE


def test_clear_forgets_everything() -> None:
    cache = StateCache()
    cache.set(Input(6), "fader", None, 0x62, Freshness.KNOWN)
    cache.clear()
    assert cache.get(Input(6), "fader") is UNKNOWN_VALUE


# --- the PRD 10.1a sub-index rule ---------------------------------------


def test_fixed_filler_vx_contributes_no_sub_index() -> None:
    spec = REGISTRY.lookup("fader")
    assert isinstance(spec.vx, Fixed)
    assert sub_index_for(spec, vx=0x07, value=0x62) is None


def test_send_target_vx_is_the_sub_index() -> None:
    spec = REGISTRY.lookup("send_level")
    assert isinstance(spec.vx, SendTarget)
    assert sub_index_for(spec, vx=0x02, value=0x50) == 0x02


def test_flagged_index_takes_its_sub_index_from_the_value_not_vx() -> None:
    # PRD 14.10: this is the correction. The naive key loses three quarters
    # of the information.
    from qumidi.protocol.channels import ChannelClass
    from qumidi.protocol.params import ParamSpec

    spec = ParamSpec(
        name="dca_assign",
        nrpn_id=0x40,
        vx=Fixed(0x07),
        domain=FlaggedIndex(on_flag=0x40, count=4),
        applies_to=frozenset({ChannelClass.INPUT}),
    )
    assert sub_index_for(spec, vx=0x07, value=0x40) == 0
    assert sub_index_for(spec, vx=0x07, value=0x42) == 2
    assert sub_index_for(spec, vx=0x07, value=0x02) == 2


def test_two_dca_memberships_are_independently_known() -> None:
    # PRD 16.10 and the whole reason 10.1a was rewritten: assigning one input
    # to DCA 1 and DCA 3 must leave both readable, and neither write must
    # clear the other.
    cache = StateCache()
    cache.set(Input(6), "dca_assign", 0, 0x40, Freshness.KNOWN)
    cache.set(Input(6), "dca_assign", 2, 0x42, Freshness.KNOWN)
    assert cache.get(Input(6), "dca_assign", 0).raw == 0x40
    assert cache.get(Input(6), "dca_assign", 2).raw == 0x42


def test_membership_reports_partial_knowledge_rather_than_fabricating_a_set() -> None:
    # PRD 10.1a: if DCA 1 is KNOWN and 2-4 are UNKNOWN, the read must never
    # return {1} as though 2-4 were known to be off.
    cache = StateCache()
    cache.set(Input(6), "dca_assign", 0, 0x40, Freshness.KNOWN)
    membership = cache.membership(Input(6), "dca_assign", count=4, on_flag=0x40)
    assert membership.known == frozenset({0})
    assert membership.unknown == frozenset({1, 2, 3})
    assert membership.complete is False
    with pytest.raises(ValueUnknownError) as excinfo:
        membership.require()
    assert "dca_assign" in str(excinfo.value)


def test_membership_returns_a_set_once_every_bit_is_known() -> None:
    cache = StateCache()
    cache.set(Input(6), "dca_assign", 0, 0x40, Freshness.KNOWN)
    cache.set(Input(6), "dca_assign", 1, 0x01, Freshness.KNOWN)
    cache.set(Input(6), "dca_assign", 2, 0x42, Freshness.KNOWN)
    cache.set(Input(6), "dca_assign", 3, 0x03, Freshness.KNOWN)
    membership = cache.membership(Input(6), "dca_assign", count=4, on_flag=0x40)
    assert membership.complete is True
    assert membership.require() == frozenset({0, 2})


def test_items_enumerates_every_entry_for_snapshotting() -> None:
    cache = StateCache()
    cache.set(Input(6), "fader", None, 0x62, Freshness.KNOWN)
    cache.set(Input(6), "send_level", 0x02, 0x50, Freshness.UNCONFIRMED)
    assert len(list(cache.items())) == 2
