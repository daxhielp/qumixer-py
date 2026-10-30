"""Channel facades. No protocol constants live here."""

from __future__ import annotations

from typing import TYPE_CHECKING

from qumixer_py.errors import ValueUnknownError
from qumixer_py.model.base import (
    BoolProxy,
    EnumProxy,
    MembershipProxy,
    ParamProxy,
    SectionProxy,
)
from qumixer_py.protocol.channels import ChannelId, Mix
from qumixer_py.protocol.registry import geq_band_for_freq, send_target_vx

if TYPE_CHECKING:  # pragma: no cover
    from qumixer_py.client_async import AsyncQuMixer


class PeqBand(SectionProxy):
    @property
    def gain(self) -> ParamProxy:
        return self._param("gain")

    @property
    def freq(self) -> ParamProxy:
        return self._param("freq")

    @property
    def width(self) -> ParamProxy:
        return self._param("width")

    async def set_gain_db(self, db: float) -> None:
        await self.gain.set_db(db)

    async def set_freq_hz(self, hz: float) -> None:
        # Not a typo: ParamProxy.set_db writes whatever engineering unit the
        # parameter's taper uses (Taper.unit), and freq's taper is in Hz.
        await self.freq.set_db(hz)


class PeqShelvingBand(PeqBand):
    @property
    def type(self) -> EnumProxy:
        return EnumProxy(self._mixer, self._channel, f"{self._prefix}.type")


class Peq(BoolProxy):
    """The channel's parametric EQ. Reads/writes peq_in; bands hang off it."""

    def __init__(self, mixer: AsyncQuMixer, channel: ChannelId) -> None:
        super().__init__(mixer, channel, "peq_in")

    @property
    def lf(self) -> PeqShelvingBand:
        return PeqShelvingBand(self._mixer, self._channel, "peq.lf")

    @property
    def lm(self) -> PeqBand:
        return PeqBand(self._mixer, self._channel, "peq.lm")

    @property
    def hm(self) -> PeqBand:
        return PeqBand(self._mixer, self._channel, "peq.hm")

    @property
    def hf(self) -> PeqShelvingBand:
        return PeqShelvingBand(self._mixer, self._channel, "peq.hf")


class Gate(BoolProxy):
    def __init__(self, mixer: AsyncQuMixer, channel: ChannelId) -> None:
        super().__init__(mixer, channel, "gate_in")

    def _leaf(self, name: str) -> ParamProxy:
        return ParamProxy(self._mixer, self._channel, f"gate.{name}")

    @property
    def attack(self) -> ParamProxy:
        return self._leaf("attack")

    @property
    def release(self) -> ParamProxy:
        return self._leaf("release")

    @property
    def hold(self) -> ParamProxy:
        return self._leaf("hold")

    @property
    def threshold(self) -> ParamProxy:
        return self._leaf("threshold")

    @property
    def depth(self) -> ParamProxy:
        return self._leaf("depth")


class Comp(BoolProxy):
    def __init__(self, mixer: AsyncQuMixer, channel: ChannelId) -> None:
        super().__init__(mixer, channel, "comp_in")

    def _leaf(self, name: str) -> ParamProxy:
        return ParamProxy(self._mixer, self._channel, f"comp.{name}")

    @property
    def type(self) -> EnumProxy:
        return EnumProxy(self._mixer, self._channel, "comp.type")

    async def set_type(self, label: str) -> None:
        await self.type.set_label(label)

    @property
    def attack(self) -> ParamProxy:
        return self._leaf("attack")

    @property
    def release(self) -> ParamProxy:
        return self._leaf("release")

    @property
    def knee(self) -> EnumProxy:
        return EnumProxy(self._mixer, self._channel, "comp.knee")

    @property
    def ratio(self) -> ParamProxy:
        return self._leaf("ratio")

    @property
    def threshold(self) -> ParamProxy:
        return self._leaf("threshold")

    @property
    def makeup(self) -> ParamProxy:
        return self._leaf("makeup")


class Geq(BoolProxy):
    """The 28-band graphic EQ on mixes and LR. Band indices are 0-based."""

    def __init__(self, mixer: AsyncQuMixer, channel: ChannelId) -> None:
        super().__init__(mixer, channel, "geq_in")

    def band(self, index: int) -> ParamProxy:
        return ParamProxy(self._mixer, self._channel, "geq_gain", sub=index, vx=index)

    async def set_band_db(
        self, db: float, band: int | None = None, freq: float | None = None
    ) -> None:
        """Set one band's gain, by 0-based index or by frequency.

        Band indices are 0-based throughout to match the wire encoding. Band 14
        is 800 Hz and band 15 is 1 kHz. ``freq=`` resolves through the band
        table, which is what most callers actually want.
        """
        if (band is None) == (freq is None):
            raise ValueError("pass exactly one of band= or freq=")
        index = band if band is not None else geq_band_for_freq(float(freq))  # type: ignore[arg-type]
        await self.band(index).set_db(db)


class Preamp:
    """The channel's preamp, dispatching between local and dSNAKE."""

    def __init__(self, mixer: AsyncQuMixer, channel: ChannelId) -> None:
        self._mixer = mixer
        self._channel = channel

    async def set_gain_db(self, db: float) -> None:
        """Set preamp gain on whichever preamp this channel is using.

        Dispatches to local_gain or dsnake_gain based on the cached
        preamp_source, and raises rather than guessing if that is unknown
.
        """
        source = self._mixer.get(self._channel, "preamp_source")
        if source.raw is None:
            raise ValueUnknownError(
                f"{self._channel.label}: the console has not reported whether this "
                f"channel uses its local or dSNAKE preamp, so the gain write cannot be "
                f"routed. Set local_gain or dsnake_gain explicitly, or resync."
            )
        if not source.raw:
            await self._mixer.set(self._channel, "local_gain", db=db)
            return
        socket = self._mixer.get(self._channel, "dsnake_patch")
        if socket.raw is None:
            raise ValueUnknownError(
                f"{self._channel.label} uses its dSNAKE preamp but the console has not "
                f"reported which socket it is patched to (dsnake_patch is receive-only)"
            )
        await self._mixer.set(self._channel, "dsnake_gain", db=db, vx=socket.raw)

    async def set_phantom(self, on: bool) -> None:
        """Set 48V phantom power on whichever preamp this channel is using.

        Dispatches the same way set_gain_db() does, and raises rather than
        guessing if the routing is unknown -- phantom power is
        safety-relevant, so this must never fall back to the local preamp
        just because the routing hasn't been reported yet.
        """
        source = self._mixer.get(self._channel, "preamp_source")
        if source.raw is None:
            raise ValueUnknownError(
                f"{self._channel.label}: the console has not reported whether this "
                f"channel uses its local or dSNAKE preamp, so the phantom-power write "
                f"cannot be routed. Set local_48v or dsnake_48v explicitly, or resync."
            )
        if not source.raw:
            await self._mixer.set(self._channel, "local_48v", raw=1 if on else 0)
            return
        socket = self._mixer.get(self._channel, "dsnake_patch")
        if socket.raw is None:
            raise ValueUnknownError(
                f"{self._channel.label} uses its dSNAKE preamp but the console has not "
                f"reported which socket it is patched to (dsnake_patch is receive-only)"
            )
        await self._mixer.set(self._channel, "dsnake_48v", raw=1 if on else 0, vx=socket.raw)


class Send:
    """One send from a channel to one destination."""

    def __init__(self, mixer: AsyncQuMixer, channel: ChannelId, target: ChannelId) -> None:
        self._mixer = mixer
        self._channel = channel
        self._target = target

    @property
    def _sub(self) -> int:
        """The cache sub-index a send-scoped write to this target lands under.

        SendTarget.sub_index() (qumixer_py/protocol/params.py) resolves to the
        target's *wire* VX byte, not None -- the same byte AsyncQuMixer.set()
        computes via send_target_vx() before storing the write. A read must
        resolve the identical byte or it will always miss the cache entry its
        own write just populated.
        """
        return send_target_vx(self._target, self._mixer.caps)

    @property
    def level(self) -> ParamProxy:
        return ParamProxy(self._mixer, self._channel, "send_level", sub=self._sub, vx=self._target)

    @property
    def pan(self) -> ParamProxy:
        return ParamProxy(self._mixer, self._channel, "pan", sub=self._sub, vx=self._target)

    async def set_level_db(self, db: float) -> None:
        await self._mixer.set(self._channel, "send_level", db=db, vx=self._target)

    async def set_pre(self, pre: bool) -> None:
        await self._mixer.set(self._channel, "mix_pre", raw=1 if pre else 0, vx=self._target)

    async def set_assigned(self, on: bool) -> None:
        param = "lr_assign" if self._target == Mix.LR else "mix_assign"
        vx = None if param == "lr_assign" else self._target
        await self._mixer.set(self._channel, param, raw=1 if on else 0, vx=vx)


class BaseStrip:
    """Anything with a fader, a mute and a name."""

    def __init__(self, mixer: AsyncQuMixer, channel: ChannelId) -> None:
        self._mixer = mixer
        self.id = channel

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.id.label}>"

    @property
    def name(self) -> str | None:
        return self._mixer.name(self.id)

    async def set_name(self, name: str) -> None:
        await self._mixer.set_name(self.id, name)

    @property
    def fader(self) -> ParamProxy:
        return ParamProxy(self._mixer, self.id, "fader")

    @property
    def pafl(self) -> BoolProxy:
        return BoolProxy(self._mixer, self.id, "pafl")

    async def mute(self, muted: bool) -> None:
        await self._mixer.mute(self.id, muted)


class SendSource(BaseStrip):
    """A strip that can send to mixes, groups, matrices and FX."""

    def send(self, target: ChannelId) -> Send:
        return Send(self._mixer, self.id, target)

    @property
    def pan(self) -> ParamProxy:
        """The channel's LR pan.

        Sugar for send(Mix.LR).pan -- there is no separate channel-pan
        parameter in the protocol.
        """
        return self.send(Mix.LR).pan

    @property
    def dca_assign(self) -> MembershipProxy:
        return MembershipProxy(self._mixer, self.id, "dca_assign")

    @property
    def mute_group_assign(self) -> MembershipProxy:
        return MembershipProxy(self._mixer, self.id, "mute_group_assign")

    @property
    def peq(self) -> Peq:
        return Peq(self._mixer, self.id)

    @property
    def comp(self) -> Comp:
        return Comp(self._mixer, self.id)

    @property
    def delay(self) -> ParamProxy:
        return ParamProxy(self._mixer, self.id, "delay_time")


class InputChannel(SendSource):
    @property
    def preamp(self) -> Preamp:
        return Preamp(self._mixer, self.id)

    @property
    def gate(self) -> Gate:
        return Gate(self._mixer, self.id)

    @property
    def hpf(self) -> BoolProxy:
        return BoolProxy(self._mixer, self.id, "hpf_in")

    @property
    def hpf_freq(self) -> ParamProxy:
        return ParamProxy(self._mixer, self.id, "hpf_freq")

    @property
    def polarity(self) -> BoolProxy:
        return BoolProxy(self._mixer, self.id, "polarity")


class MixMaster(SendSource):
    @property
    def geq(self) -> Geq:
        return Geq(self._mixer, self.id)


class DcaMaster(BaseStrip):
    pass


class MuteGroupMaster(BaseStrip):
    async def fire(self, on: bool) -> None:
        """Fire the whole mute group.

        Identical to mute() -- a Note On to the mute-group master is the only
        way to fire a mute group; there is no dedicated message.
        """
        await self.mute(on)
