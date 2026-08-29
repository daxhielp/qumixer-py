"""Change notification (PRD 10.4).

Callbacks are dispatched synchronously on the event loop. Exceptions in user
callbacks are caught and logged, never allowed to kill the read loop, and a
callback that blocks for too long is reported -- it is holding up inbound
MIDI while it runs.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any

from qumidi.protocol import constants as c
from qumidi.protocol.channels import ChannelId
from qumidi.state import CachedValue
from qumidi.transport.base import ConnectionState

log = logging.getLogger(__name__)


class Source(Enum):
    #: This client wrote it.
    LOCAL = auto()
    #: The console reported it outside a sync burst.
    MIXER = auto()
    #: Part of a state-sync burst; no external_change is emitted for these.
    SYNC = auto()


@dataclass(frozen=True)
class ChangeEvent:
    channel: ChannelId
    param: str
    sub_index: int | None
    old: CachedValue
    new: CachedValue
    source: Source


@dataclass
class _Subscription:
    callback: Callable[[ChangeEvent], None]
    channel: ChannelId | None
    param: str | None

    def matches(self, event: ChangeEvent) -> bool:
        if self.channel is not None and self.channel != event.channel:
            return False
        return self.param is None or self.param == event.param


class EventBus:
    """Filtered subscriptions over the client's state changes."""

    def __init__(
        self,
        slow_callback_warn: float = c.SLOW_CALLBACK_WARN,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.slow_callback_warn = slow_callback_warn
        self._now = now
        self._changes: list[_Subscription] = []
        self._connection: list[Callable[[ConnectionState], None]] = []
        self._sync: list[Callable[[], None]] = []
        self._unconfirmed: list[Callable[[ChannelId, str, int | None], None]] = []
        self._scene: list[Callable[[int], None]] = []
        self._meter_layout: list[Callable[[str], None]] = []

    # -- subscription -----------------------------------------------------

    def on_change(
        self,
        callback: Callable[[ChangeEvent], None],
        channel: ChannelId | None = None,
        param: str | None = None,
    ) -> Callable[[], None]:
        """Subscribe to changes, optionally filtered. Returns an unsubscriber."""
        sub = _Subscription(callback, channel, param)
        self._changes.append(sub)

        def unsubscribe() -> None:
            if sub in self._changes:
                self._changes.remove(sub)

        return unsubscribe

    def on_connection_state(self, cb: Callable[[ConnectionState], None]) -> None:
        self._connection.append(cb)

    def on_sync_complete(self, cb: Callable[[], None]) -> None:
        self._sync.append(cb)

    def on_write_unconfirmed(self, cb: Callable[[ChannelId, str, int | None], None]) -> None:
        self._unconfirmed.append(cb)

    def on_scene_recalled(self, cb: Callable[[int], None]) -> None:
        self._scene.append(cb)

    def on_meter_layout_error(self, cb: Callable[[str], None]) -> None:
        self._meter_layout.append(cb)

    # -- emission ---------------------------------------------------------

    def emit_change(self, event: ChangeEvent) -> None:
        for sub in list(self._changes):
            if sub.matches(event):
                self._call(sub.callback, event)

    def emit_connection_state(self, state: ConnectionState) -> None:
        for cb in list(self._connection):
            self._call(cb, state)

    def emit_sync_complete(self) -> None:
        for cb in list(self._sync):
            self._call(cb)

    def emit_write_unconfirmed(self, ch: ChannelId, param: str, sub: int | None) -> None:
        for cb in list(self._unconfirmed):
            self._call(cb, ch, param, sub)

    def emit_scene_recalled(self, scene: int) -> None:
        for cb in list(self._scene):
            self._call(cb, scene)

    def emit_meter_layout_error(self, message: str) -> None:
        for cb in list(self._meter_layout):
            self._call(cb, message)

    # -- dispatch ---------------------------------------------------------

    def _call(self, cb: Callable[..., None], *args: Any) -> None:
        started = self._now()
        try:
            cb(*args)
        except Exception:
            log.exception("event callback %r raised; continuing", getattr(cb, "__name__", cb))
        elapsed = self._now() - started
        if elapsed > self.slow_callback_warn:
            log.warning(
                "slow event callback %r took %.0f ms; callbacks run on the read loop and "
                "must not await long operations",
                getattr(cb, "__name__", cb),
                elapsed * 1000,
            )
