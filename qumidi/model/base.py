"""Proxy objects for the ergonomic API (PRD 11.1).

CRITICAL INVARIANT (PRD 3): this layer contains zero protocol constants. Every
method here is a thin call into AsyncQuMixer.set/.get with a registry-supplied
parameter name. If you need a hex byte in this package, the registry is
missing an entry -- add it there instead.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any

from qumidi.errors import ValueUnknownError
from qumidi.protocol.channels import ChannelId
from qumidi.protocol.params import EnumDomain, FlaggedIndex, Interpolated
from qumidi.protocol.registry import REGISTRY
from qumidi.state import CachedValue, CacheKey, Freshness, MembershipSet, StateCache

if TYPE_CHECKING:  # pragma: no cover
    from qumidi.client_async import AsyncQuMixer


def _in_loop_thread(loop: asyncio.AbstractEventLoop) -> bool:
    """True if the *current* thread is the one presently running ``loop``.

    ``asyncio.get_running_loop()`` is thread-local and only returns a loop
    that is actually executing on the calling thread right now -- for the
    entire duration of its ``run_forever()``, not just inside a coroutine
    step -- so comparing it to ``loop`` is a direct, race-free way to answer
    "am I on the event loop's own thread". No separate thread-id bookkeeping
    is needed.
    """
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        return False
    return running is loop


#: How often ParamProxy._blocking's wait re-checks loop liveness while
#: polling, rather than making one unbounded .result() call -- not a ceiling
#: on how long a legitimate write may itself take (see _blocking's own
#: docstring).
_BLOCKING_POLL_INTERVAL = 0.2


class ParamProxy:
    """One parameter on one channel (and optionally one sub-index)."""

    def __init__(
        self,
        mixer: AsyncQuMixer,
        channel: ChannelId,
        param: str,
        sub: int | None = None,
        vx: ChannelId | int | None = None,
    ) -> None:
        self._mixer = mixer
        self._channel = channel
        self._param = param
        self._sub = sub
        self._vx = vx

    # -- ramp tracking ------------------------------------------------------
    #
    # A fresh ParamProxy is constructed on every property access (see e.g.
    # BaseStrip.fader below), so a ramp task cannot live on `self` -- two
    # `mixer.input(6).fader` accesses produce two unrelated ParamProxy
    # instances and a second ramp would never find the first to cancel it.
    # Ramps are tracked on the *mixer* instead, keyed by the same
    # (channel, param, sub) tuple the state cache itself uses, so any two
    # proxies addressing the same logical parameter share one slot.

    @property
    def _ramp_key(self) -> CacheKey:
        return StateCache.key(self._channel, self._param, self._sub)

    # -- reads ------------------------------------------------------------

    @property
    def value(self) -> CachedValue:
        """The cached value with its freshness. Never raises."""
        return self._mixer.get(self._channel, self._param, self._sub)

    @property
    def raw(self) -> int:
        """The exact 7-bit value the console reported. Raises if unknown."""
        value = self.value
        if value.raw is None or value.freshness is Freshness.UNKNOWN:
            raise ValueUnknownError(
                f"{self._channel.label} {self._param}: the console has never reported "
                f"this value. Await wait_synced(), and note that not every parameter "
                f"appears in the state push."
            )
        return value.raw

    @raw.setter
    def raw(self, value: int) -> None:
        """Blocking sugar for set_raw(). Only valid from the sync facade.

        The setter blocks until the write is flushed -- the same guarantee as
        await in the async API -- and raises the same exceptions (PRD 11.2).
        """
        self._blocking(self.set_raw(value), value)

    @property
    def db(self) -> float:
        """The value in engineering units. Approximate; .raw is the truth."""
        spec = REGISTRY.lookup(self._param, self._channel.cls)
        if not isinstance(spec.domain, Interpolated):
            raise ValueUnknownError(
                f"{self._channel.label} {self._param} has no taper; read .raw instead"
            )
        return spec.domain.to_db(self.raw)

    @db.setter
    def db(self, value: float) -> None:
        """Blocking sugar for set_db(). Only valid from the sync facade.

        The setter blocks until the write is flushed -- the same guarantee as
        await in the async API -- and raises the same exceptions (PRD 11.2).
        """
        self._blocking(self.set_db(value), value)

    @property
    def is_approximate(self) -> bool:
        """True where .db was extrapolated beyond the documented anchors."""
        spec = REGISTRY.lookup(self._param, self._channel.cls)
        if not isinstance(spec.domain, Interpolated):
            return False
        return spec.domain.taper.is_approximate(self.raw) or not spec.domain.taper.verified

    def _blocking(self, coro: Coroutine[Any, Any, None], value: float) -> None:
        """Run ``coro`` to completion, blocking the *calling* thread.

        This is what makes ``proxy.db = -6.0`` behave like ``await
        proxy.set_db(-6.0)`` from the sync facade (PRD 11.2): the mixer
        records the loop it connected on as ``_owner_loop``, and this
        marshals ``coro`` onto that loop with ``run_coroutine_threadsafe``
        and blocks until it's done -- the write is on the wire (or the
        exception has been raised) before the assignment returns.

        Refuses -- rather than deadlocking -- in the two cases where that
        cannot work: no owner loop is known yet (never connected, or already
        closed), or this is itself running on the owner loop's own thread (an
        `await`-based caller assigning to the property instead of using
        `set_db`/`set_raw`; blocking here would freeze the very loop the
        write needs to run on). This is the same reentrancy guard
        ``qumidi.client_sync.QuMixer._run`` reuses for the explicit-method
        call path, via this exact ``_in_loop_thread`` function.

        Waits in short polled slices rather than one unbounded ``.result()``
        call, so a write racing a concurrent close (the submission loses the
        race against the loop actually stopping, and the coroutine is queued
        but never runs) fails with a clear error within one poll interval
        instead of hanging forever -- without imposing any ceiling on how
        long a legitimate write may itself take.
        """
        loop = getattr(self._mixer, "_owner_loop", None)
        if loop is None or (loop.is_running() and _in_loop_thread(loop)):
            coro.close()
            raise RuntimeError(
                f"{self._channel.label} {self._param}: cannot assign {value!r} -- "
                f"assignment is the synchronous API's sugar; from async code use "
                f"`await proxy.set_db(...)` / `await proxy.set_raw(...)` instead. (This "
                f"also fires if the mixer isn't connected yet, or has been closed.)"
            )
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        while True:
            try:
                future.result(timeout=_BLOCKING_POLL_INTERVAL)
                return
            except concurrent.futures.TimeoutError:
                if not loop.is_running() and not future.done():
                    future.cancel()
                    raise RuntimeError(
                        f"{self._channel.label} {self._param}: cannot assign {value!r} -- "
                        f"the mixer's event loop stopped while this write was still in "
                        f"flight (the mixer may have been closed concurrently); it may "
                        f"or may not have reached the console"
                    ) from None

    # -- writes -----------------------------------------------------------

    async def set_raw(self, value: int) -> None:
        await self._mixer.set(self._channel, self._param, raw=value, vx=self._vx)

    async def set_db(self, value: float) -> None:
        await self._mixer.set(self._channel, self._param, db=value, vx=self._vx)

    # -- ramps (PRD 11.1) --------------------------------------------------

    async def ramp_to_raw(self, target: int, duration: float, steps: int | None = None) -> None:
        """Move smoothly to ``target`` over ``duration`` seconds.

        Cancellable, paced through the outbound queue, and approximate in
        timing. A second ramp on the same parameter cancels the first rather
        than interleaving with it.
        """
        self.cancel_ramp()
        key = self._ramp_key
        task = asyncio.create_task(self._run_ramp(target, duration, steps))
        self._mixer._ramps[key] = task
        try:
            await task
        finally:
            if self._mixer._ramps.get(key) is task:
                del self._mixer._ramps[key]

    async def ramp_to_db(self, target: float, duration: float, steps: int | None = None) -> None:
        spec = REGISTRY.lookup(self._param, self._channel.cls)
        if not isinstance(spec.domain, Interpolated):
            raise ValueUnknownError(f"{self._param} has no taper; use ramp_to_raw")
        await self.ramp_to_raw(spec.domain.to_raw(target), duration, steps)

    def cancel_ramp(self) -> None:
        key = self._ramp_key
        existing = self._mixer._ramps.get(key)
        if existing is not None:
            if not existing.done():
                existing.cancel()
            del self._mixer._ramps[key]

    async def _run_ramp(self, target: int, duration: float, steps: int | None) -> None:
        start = self.value.raw
        if start is None:
            # Nothing to interpolate from; jump and be honest about it.
            await self.set_raw(target)
            return
        span = abs(target - start)
        count = span if steps is None else max(1, steps)  # default: one per raw step
        if count == 0:
            return
        interval = duration / count
        for i in range(1, count + 1):
            await self.set_raw(round(start + (target - start) * i / count))
            await asyncio.sleep(interval)


class BoolProxy(ParamProxy):
    @property
    def enabled(self) -> bool:
        return bool(self.raw)

    async def set_enabled(self, on: bool) -> None:
        spec = REGISTRY.lookup(self._param, self._channel.cls)
        domain = spec.domain
        await self.set_raw(getattr(domain, "on" if on else "off"))


class EnumProxy(ParamProxy):
    @property
    def label(self) -> str:
        spec = REGISTRY.lookup(self._param, self._channel.cls)
        assert isinstance(spec.domain, EnumDomain)
        result = spec.domain.label(self.raw)
        if result is None:
            raise ValueUnknownError(
                f"{self._channel.label} {self._param}: console reported undocumented "
                f"value {self.raw:#04x}"
            )
        return result

    async def set_label(self, label: str) -> None:
        spec = REGISTRY.lookup(self._param, self._channel.cls)
        assert isinstance(spec.domain, EnumDomain)
        await self.set_raw(spec.domain.from_label(label))


class MembershipProxy:
    """A set-valued parameter (DCA / mute-group assign). Groups are 1-based."""

    def __init__(self, mixer: AsyncQuMixer, channel: ChannelId, param: str) -> None:
        self._mixer = mixer
        self._channel = channel
        self._param = param

    @property
    def groups(self) -> frozenset[int]:
        """The 1-based groups this channel belongs to.

        Raises if any membership bit is unknown -- the library never fabricates
        a set from partial knowledge (PRD 10.1a).
        """
        membership = self._mixer.membership(self._channel, self._param)
        return frozenset(i + 1 for i in membership.require())

    @property
    def partial(self) -> MembershipSet:
        """The MembershipSet, including what is still unknown."""
        return self._mixer.membership(self._channel, self._param)

    async def set(self, group: int, on: bool) -> None:
        spec = REGISTRY.lookup(self._param, self._channel.cls)
        assert isinstance(spec.domain, FlaggedIndex)
        await self._mixer.set(self._channel, self._param, raw=spec.domain.encode(group - 1, on))

    async def add(self, group: int) -> None:
        await self.set(group, True)

    async def remove(self, group: int) -> None:
        await self.set(group, False)


class SectionProxy:
    """A named group of parameters sharing a prefix (peq, gate, comp)."""

    def __init__(self, mixer: AsyncQuMixer, channel: ChannelId, prefix: str) -> None:
        self._mixer = mixer
        self._channel = channel
        self._prefix = prefix

    def _param(self, leaf: str) -> ParamProxy:
        return ParamProxy(self._mixer, self._channel, f"{self._prefix}.{leaf}")
