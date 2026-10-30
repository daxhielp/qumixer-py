import pytest

from qumixer_py.errors import ValueUnknownError
from qumixer_py.protocol.channels import (
    GLOBAL,
    FxSend,
    Input,
    Mix,
    channel_from_raw,
    dsnake_socket_channel,
)
from qumixer_py.protocol.params import Fixed, FlaggedIndex, SendTarget
from qumixer_py.protocol.registry import REGISTRY
from qumixer_py.state import (
    UNKNOWN_VALUE,
    CachedValue,
    Freshness,
    StateCache,
    channel_from_key,
    sub_index_for,
)


def clock():  # type: ignore[no-untyped-def]
    t = {"now": 1000.0}
    def now() -> float:
        return t["now"]
    return t, now


def test_an_unseen_value_is_unknown_not_a_default() -> None:
    # "I have not been told the value" is categorically different
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
    # echo windows and staleness must not break when NTP steps the
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
    # never serve pre-disconnect values as current.
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


# --- the sub-index rule ---------------------------------------


def test_fixed_filler_vx_contributes_no_sub_index() -> None:
    spec = REGISTRY.lookup("fader")
    assert isinstance(spec.vx, Fixed)
    assert sub_index_for(spec, vx=0x07, value=0x62) is None


def test_send_target_vx_is_the_sub_index() -> None:
    spec = REGISTRY.lookup("send_level")
    assert isinstance(spec.vx, SendTarget)
    assert sub_index_for(spec, vx=0x02, value=0x50) == 0x02


def test_flagged_index_takes_its_sub_index_from_the_value_not_vx() -> None:
    # this is the correction. The naive key loses three quarters
    # of the information.
    from qumixer_py.protocol.channels import ChannelClass
    from qumixer_py.protocol.params import ParamSpec

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
    # Set-valued parameters: assigning one input
    # to DCA 1 and DCA 3 must leave both readable, and neither write must
    # clear the other.
    cache = StateCache()
    cache.set(Input(6), "dca_assign", 0, 0x40, Freshness.KNOWN)
    cache.set(Input(6), "dca_assign", 2, 0x42, Freshness.KNOWN)
    assert cache.get(Input(6), "dca_assign", 0).raw == 0x40
    assert cache.get(Input(6), "dca_assign", 2).raw == 0x42


def test_membership_reports_partial_knowledge_rather_than_fabricating_a_set() -> None:
    # if DCA 1 is KNOWN and 2-4 are UNKNOWN, the read must never
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


# -- channel_from_key: the inverse of StateCache.key() ------------------------


@pytest.mark.parametrize(
    "channel",
    [
        Input(6),
        Input(1),
        Mix.LR,
        Mix.MIX3,
        GLOBAL,
        dsnake_socket_channel(0x00),
        dsnake_socket_channel(0x27),
    ],
)
def test_channel_from_key_is_the_exact_inverse_of_key(channel) -> None:  # type: ignore[no-untyped-def]
    # The property that matters: whatever key() stored, channel_from_key()
    # must hand back the identical ChannelId -- for the two synthetic classes
    # as much as for a real wire channel.
    ident, cls, _param, _sub = StateCache.key(channel, "fader", None)
    assert channel_from_key(ident, cls) == channel


def test_channel_from_key_does_not_mistake_global_for_the_channel_at_wire_zero() -> None:
    # The regression this helper exists for: key() stores ch.index (0) for
    # GLOBAL, and channel_from_raw(0) is the entirely unrelated FX Send 1.
    # A reader that reaches for channel_from_raw on a key's first element
    # silently mislabels every GLOBAL parameter.
    ident, cls, _param, _sub = StateCache.key(GLOBAL, "group_mode", None)
    assert ident == 0
    assert channel_from_raw(ident) == FxSend(1)
    assert channel_from_key(ident, cls) is GLOBAL


def test_channel_from_key_keeps_global_and_dsnake_socket_zero_apart() -> None:
    # Both carry wire value -1 and both have index 0; only the class in the
    # key tells them apart, and the inverse has to honour that.
    global_key = StateCache.key(GLOBAL, "group_mode", None)
    socket_key = StateCache.key(dsnake_socket_channel(0x00), "dsnake_gain", None)
    assert global_key[:2] != socket_key[:2]
    assert channel_from_key(*global_key[:2]) is GLOBAL
    assert channel_from_key(*socket_key[:2]) == dsnake_socket_channel(0x00)
