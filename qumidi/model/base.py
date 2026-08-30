"""Proxy objects for the ergonomic API (PRD 11.1).

CRITICAL INVARIANT (PRD 3): this layer contains zero protocol constants. Every
method here is a thin call into AsyncQuMixer.set/.get with a registry-supplied
parameter name. If you need a hex byte in this package, the registry is
missing an entry -- add it there instead.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from qumidi.errors import ValueUnknownError
from qumidi.protocol.channels import ChannelId
from qumidi.protocol.params import EnumDomain, FlaggedIndex, Interpolated
from qumidi.protocol.registry import REGISTRY
from qumidi.state import CachedValue, CacheKey, Freshness, MembershipSet, StateCache

if TYPE_CHECKING:  # pragma: no cover
    from qumidi.client_async import AsyncQuMixer


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

    @property
    def db(self) -> float:
        """The value in engineering units. Approximate; .raw is the truth."""
        spec = REGISTRY.lookup(self._param, self._channel.cls)
        if not isinstance(spec.domain, Interpolated):
            raise ValueUnknownError(
                f"{self._channel.label} {self._param} has no taper; read .raw instead"
            )
        return spec.domain.to_db(self.raw)

    @property
    def is_approximate(self) -> bool:
        """True where .db was extrapolated beyond the documented anchors."""
        spec = REGISTRY.lookup(self._param, self._channel.cls)
        if not isinstance(spec.domain, Interpolated):
            return False
        return spec.domain.taper.is_approximate(self.raw) or not spec.domain.taper.verified

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
