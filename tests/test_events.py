import logging

from qumixer_py.events import ChangeEvent, EventBus, Source
from qumixer_py.protocol.channels import Input, Mix
from qumixer_py.state import CachedValue, Freshness

KNOWN = CachedValue(raw=0x62, freshness=Freshness.KNOWN, updated_at=1.0)
OLD = CachedValue(raw=0x50, freshness=Freshness.KNOWN, updated_at=0.0)
DEFAULT_CHANNEL = Input(6)


def event(channel=DEFAULT_CHANNEL, param="fader", sub=None, source=Source.MIXER) -> ChangeEvent:  # type: ignore[no-untyped-def]
    return ChangeEvent(channel=channel, param=param, sub_index=sub, old=OLD, new=KNOWN,
                       source=source)


def test_unfiltered_subscription_receives_everything() -> None:
    bus = EventBus()
    seen: list[ChangeEvent] = []
    bus.on_change(seen.append)
    bus.emit_change(event())
    bus.emit_change(event(channel=Mix.MIX3, param="pafl"))
    assert len(seen) == 2


def test_channel_filter() -> None:
    bus = EventBus()
    seen: list[ChangeEvent] = []
    bus.on_change(seen.append, channel=Input(6))
    bus.emit_change(event(channel=Input(6)))
    bus.emit_change(event(channel=Input(7)))
    assert [e.channel for e in seen] == [Input(6)]


def test_param_filter() -> None:
    bus = EventBus()
    seen: list[ChangeEvent] = []
    bus.on_change(seen.append, param="fader")
    bus.emit_change(event(param="fader"))
    bus.emit_change(event(param="pafl"))
    assert [e.param for e in seen] == ["fader"]


def test_unsubscribe() -> None:
    bus = EventBus()
    seen: list[ChangeEvent] = []
    unsubscribe = bus.on_change(seen.append)
    bus.emit_change(event())
    unsubscribe()
    bus.emit_change(event())
    assert len(seen) == 1


def test_a_raising_callback_does_not_stop_the_others(caplog) -> None:  # type: ignore[no-untyped-def]
    # exceptions in user callbacks are caught and logged, never
    # allowed to kill the read loop.
    bus = EventBus()
    seen: list[ChangeEvent] = []

    def boom(_: ChangeEvent) -> None:
        raise RuntimeError("user code")

    bus.on_change(boom)
    bus.on_change(seen.append)
    with caplog.at_level(logging.ERROR):
        bus.emit_change(event())
    assert len(seen) == 1
    assert any("user code" in r.message or r.exc_info for r in caplog.records)


def test_a_slow_callback_logs_a_warning(caplog) -> None:  # type: ignore[no-untyped-def]
    ticks = iter([0.0, 0.5])
    bus = EventBus(slow_callback_warn=0.1, now=lambda: next(ticks))
    bus.on_change(lambda _: None)
    with caplog.at_level(logging.WARNING):
        bus.emit_change(event())
    assert any("slow" in r.message.lower() for r in caplog.records)


def test_the_other_subscription_kinds_fire() -> None:
    from qumixer_py.transport.base import ConnectionState

    bus = EventBus()
    calls: dict[str, object] = {}
    bus.on_connection_state(lambda s: calls.__setitem__("state", s))
    bus.on_sync_complete(lambda: calls.__setitem__("sync", True))
    bus.on_write_unconfirmed(lambda ch, p, s: calls.__setitem__("unconfirmed", (ch, p, s)))
    bus.on_scene_recalled(lambda n: calls.__setitem__("scene", n))
    bus.emit_connection_state(ConnectionState.READY)
    bus.emit_sync_complete()
    bus.emit_write_unconfirmed(Input(6), "fader", None)
    bus.emit_scene_recalled(12)
    assert calls == {
        "state": ConnectionState.READY,
        "sync": True,
        "unconfirmed": (Input(6), "fader", None),
        "scene": 12,
    }


def test_change_event_carries_the_sub_index() -> None:
    # the payload includes the sub-index.
    e = event(param="dca_assign", sub=2)
    assert e.sub_index == 2
