"""The local state cache (PRD 10.1, 10.1a).

Never synthesize defaults. "I have not been told the value" is categorically
different from "the fader is at -inf", and conflating them will cause a user
to unmute something they shouldn't.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import Enum, auto

from qumidi.errors import ValueUnknownError
from qumidi.protocol.channels import (
    GLOBAL,
    ChannelClass,
    ChannelId,
    channel_from_raw,
    dsnake_socket_channel,
)
from qumidi.protocol.params import FlaggedIndex, ParamSpec, VxMode


class Freshness(Enum):
    """Four states, not three (PRD 10.1)."""

    #: Never received.
    UNKNOWN = auto()
    #: Confirmed by the mixer.
    KNOWN = auto()
    #: We wrote it; the mixer never confirmed it. The only signal a user gets
    #: that the library sent bytes into the void (PRD 10.3).
    UNCONFIRMED = auto()
    #: Was known; the connection has dropped since.
    STALE = auto()


@dataclass(frozen=True)
class CachedValue:
    raw: int | None
    freshness: Freshness
    #: A time.monotonic() reading, never wall clock (PRD 10.1).
    updated_at: float | None


UNKNOWN_VALUE = CachedValue(raw=None, freshness=Freshness.UNKNOWN, updated_at=None)

#: (channel identifier, channel class, param name, sub index).
#: The class is part of the key so the synthetic GLOBAL and DSNAKE channels,
#: which share the wire value -1 (and, for GLOBAL, also share index 0 with
#: dsnake_socket_channel(0)), cannot collide with each other or with real
#: channels.
CacheKey = tuple[int, ChannelClass, str, int | None]


def channel_from_key(ident: int, cls: ChannelClass) -> ChannelId:
    """Recover the ChannelId a CacheKey was built from.

    The exact inverse of ``StateCache.key()``'s channel substitution, and the
    only correct way to turn a key (or a snapshot row, which stores the same
    two fields verbatim) back into a channel. Anything that reaches for
    ``channel_from_raw(key[0])`` instead is wrong for the two synthetic
    classes: ``key()`` stores ``ch.index`` for GLOBAL and DSNAKE rather than
    the wire value, so GLOBAL's ident is 0 -- which ``channel_from_raw``
    happily decodes as the entirely unrelated FX Send 1.

    Both readers of a key (``qumidi/snapshot.py``'s diff, and
    ``qumidi dump-state``'s text output) call this rather than keeping their
    own copy; ``key()`` and its inverse live side by side so a future change
    to one is impossible to make without seeing the other.
    """
    if cls is ChannelClass.GLOBAL:
        return GLOBAL
    if cls is ChannelClass.DSNAKE:
        return dsnake_socket_channel(ident)
    return channel_from_raw(ident)


def sub_index_for(spec: ParamSpec, vx: int | None, value: int) -> int | None:
    """The parameter's *addressing* index (PRD 10.1a).

    This is not simply ``vx``. It comes from one of three places depending on
    the VxMode/Domain -- see the table in PRD 10.1a. The third row is the
    correction that PRD revision 1.1 exists for: a FlaggedIndex domain's
    sub-index lives in the *value*'s low bits, regardless of what VX is doing
    (VX is typically a fixed filler for these parameters), so the domain check
    must take priority over the VxMode dispatch below.
    """
    if isinstance(spec.domain, FlaggedIndex):
        return spec.domain.sub_index_from_value(value)
    if vx is None:
        return None
    return _vx_sub_index(spec.vx, vx, value)


def _vx_sub_index(mode: VxMode, vx: int, value: int) -> int | None:
    return mode.sub_index(vx, value)


@dataclass(frozen=True)
class MembershipSet:
    """The result of reading a set-valued parameter (PRD 10.1a).

    A cache read must not fabricate a set from partial knowledge: if DCA 1 is
    KNOWN and 2-4 are UNKNOWN, this reports that rather than returning {1}.
    """

    param: str
    channel_label: str
    known_on: frozenset[int]
    known_off: frozenset[int]
    unknown: frozenset[int]

    @property
    def known(self) -> frozenset[int]:
        return self.known_on

    @property
    def complete(self) -> bool:
        return not self.unknown

    def require(self) -> frozenset[int]:
        """Return the membership set, or explain what is missing.

        Group numbers in the message are 1-based (what the console displays
        and what a user would type); the indices stored internally stay
        0-based.
        """
        if self.unknown:
            missing = ", ".join(str(i + 1) for i in sorted(self.unknown))
            known = ", ".join(str(i + 1) for i in sorted(self.known_on)) or "none"
            raise ValueUnknownError(
                f"{self.channel_label} {self.param}: membership of group(s) {missing} has "
                f"never been reported by the console, so the set is not knowable. "
                f"Groups known on: {known}."
            )
        return self.known_on


class StateCache:
    """A dict of CachedValue keyed by (channel, param, sub index)."""

    def __init__(self, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._values: dict[CacheKey, CachedValue] = {}

    @staticmethod
    def key(ch: ChannelId, param: str, sub: int | None) -> CacheKey:
        #: GLOBAL and DSNAKE both carry a wire value of -1 (PRD 7.2), so the
        #: wire value can't disambiguate them or the different DSNAKE sockets
        #: from each other. Substitute the channel's index instead (the
        #: socket number for DSNAKE, always 0 for the singleton GLOBAL) --
        #: distinct DSNAKE sockets get distinct keys, and GLOBAL vs. DSNAKE
        #: socket 0 (both index 0) are still kept apart because ch.cls is
        #: also part of the tuple.
        #:
        #: ``channel_from_key()`` above is the inverse; keep the two in step.
        synthetic = ch.cls in (ChannelClass.GLOBAL, ChannelClass.DSNAKE)
        ident = ch.index if synthetic else ch.value
        return (ident, ch.cls, param, sub)

    def get(self, ch: ChannelId, param: str, sub: int | None = None) -> CachedValue:
        return self._values.get(self.key(ch, param, sub), UNKNOWN_VALUE)

    def set(
        self,
        ch: ChannelId,
        param: str,
        sub: int | None,
        raw: int,
        freshness: Freshness,
    ) -> tuple[CachedValue, CachedValue]:
        """Store a value, returning ``(old, new)``."""
        key = self.key(ch, param, sub)
        old = self._values.get(key, UNKNOWN_VALUE)
        new = CachedValue(raw=raw, freshness=freshness, updated_at=self._now())
        self._values[key] = new
        return old, new

    def downgrade(self, ch: ChannelId, param: str, sub: int | None, freshness: Freshness) -> None:
        """Change only the freshness of an existing entry (PRD 10.3)."""
        key = self.key(ch, param, sub)
        current = self._values.get(key)
        if current is None:
            return
        self._values[key] = CachedValue(
            raw=current.raw, freshness=freshness, updated_at=current.updated_at
        )

    def mark_all_stale(self) -> None:
        """PRD 4.4: on every reconnect, before re-running the full sync.

        Preserves ``raw`` on every entry -- the last known value is still the
        best guess we have -- and only ever downgrades ``freshness``. Entries
        that are already UNKNOWN are left alone: there is nothing to downgrade
        from "never known".
        """
        for key, value in list(self._values.items()):
            if value.freshness is not Freshness.UNKNOWN:
                self._values[key] = CachedValue(
                    raw=value.raw, freshness=Freshness.STALE, updated_at=value.updated_at
                )

    def clear(self) -> None:
        self._values.clear()

    def keys(self) -> Iterator[CacheKey]:
        return iter(self._values.keys())

    def items(self) -> Iterator[tuple[CacheKey, CachedValue]]:
        return iter(self._values.items())

    def __len__(self) -> int:
        return len(self._values)

    def membership(
        self, ch: ChannelId, param: str, count: int, on_flag: int = 0x40
    ) -> MembershipSet:
        """Read a FlaggedIndex parameter as a set, honestly (PRD 10.1a)."""
        on: set[int] = set()
        off: set[int] = set()
        unknown: set[int] = set()
        for index in range(count):
            value = self.get(ch, param, index)
            if value.raw is None or value.freshness is Freshness.UNKNOWN:
                unknown.add(index)
            elif value.raw & on_flag:
                on.add(index)
            else:
                off.add(index)
        return MembershipSet(
            param=param,
            channel_label=ch.label,
            known_on=frozenset(on),
            known_off=frozenset(off),
            unknown=frozenset(unknown),
        )
