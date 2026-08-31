"""Meter frame layout and decoding (PRD 12.3). Tier: EXPERIMENTAL.

The block counts below are transcribed from the spec and are UNVERIFIED. One
of them is provably wrong: taken literally, a Qu-24 frame is larger than a
Qu-32 frame (PRD 14.12). Both Qu-24 candidates are encoded here, and the
runtime frame-size check -- not this table -- is the authority.

Frames decode lazily. A Qu-32 frame holds ~790 meters including two 31-band
RTAs; eagerly building 790 floats at frame rate is wasteful when the caller
wants six of them.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto

from qumidi.errors import MeterLayoutError
from qumidi.protocol.channels import ChannelId, Mix
from qumidi.protocol.sysex import decode_meter_db


class Meter(Enum):
    """A meter tap. Which of these a block has depends on the block type."""

    # Mono input block (10)
    POST_PREAMP = auto()
    POST_PEQ = auto()
    POST_COMP = auto()
    POST_DELAY = auto()
    GATE_SC = auto()
    COMP_SC = auto()
    DIRECT_OUT = auto()
    GATE_GR = auto()
    COMP_GR = auto()
    DUCKER_GR = auto()
    # Mono mix block (10). POST_PEQ/POST_COMP/COMP_SC/COMP_GR/DUCKER_GR are shared.
    TB_SIGGEN = auto()
    PRE_INSERT = auto()
    POST_GEQ = auto()
    POST_FADER = auto()
    POST_INSERT = auto()
    # FX block (18)
    SEND_L = auto()
    SEND_R = auto()
    SEND_MONO = auto()
    PRE_PEQ_L = auto()
    PRE_PEQ_R = auto()
    TAP_TEMPO_L = auto()
    TAP_TEMPO_R = auto()
    POST_PEQ_L = auto()
    POST_PEQ_R = auto()
    # Monitor block (78)
    PAFL_L = auto()
    PAFL_R = auto()
    PAFL_MONO = auto()
    TALKBACK = auto()
    SIGGEN = auto()
    MAIN_PRE_FADER_L = auto()
    MAIN_PRE_FADER_R = auto()
    MAIN_POST_FADER_L = auto()
    MAIN_POST_FADER_R = auto()
    MAIN_MONO_PRE_FADER = auto()
    MAIN_MONO_POST_FADER = auto()
    USB_RECORD_L = auto()
    USB_RECORD_R = auto()


MONO_INPUT_METERS: tuple[Meter, ...] = (
    Meter.POST_PREAMP, Meter.POST_PEQ, Meter.POST_COMP, Meter.POST_DELAY,
    Meter.GATE_SC, Meter.COMP_SC, Meter.DIRECT_OUT, Meter.GATE_GR,
    Meter.COMP_GR, Meter.DUCKER_GR,
)
MONO_MIX_METERS: tuple[Meter, ...] = (
    Meter.TB_SIGGEN, Meter.PRE_INSERT, Meter.POST_PEQ, Meter.POST_GEQ,
    Meter.POST_COMP, Meter.POST_FADER, Meter.POST_INSERT, Meter.COMP_SC,
    Meter.COMP_GR, Meter.DUCKER_GR,
)
MONITOR_METERS = (
    Meter.PAFL_L, Meter.PAFL_R, Meter.PAFL_MONO, Meter.TALKBACK, Meter.SIGGEN,
    Meter.MAIN_PRE_FADER_L, Meter.MAIN_PRE_FADER_R,
    Meter.MAIN_POST_FADER_L, Meter.MAIN_POST_FADER_R,
    Meter.MAIN_MONO_PRE_FADER, Meter.MAIN_MONO_POST_FADER,
    Meter.USB_RECORD_L, Meter.USB_RECORD_R,
)
#: 13 named + 3 unused + 31 RTA L + 31 RTA R = 78.
MONITOR_RTA_OFFSET = len(MONITOR_METERS) + 3
MONITOR_BLOCK_SIZE = 78
RTA_BANDS = 31

FX_METERS: tuple[Meter, ...] = (
    Meter.SEND_L, Meter.SEND_R, Meter.SEND_MONO,
    Meter.PRE_PEQ_L, Meter.PRE_PEQ_R,
    Meter.TAP_TEMPO_L, Meter.TAP_TEMPO_R,
    Meter.POST_PEQ_L, Meter.POST_PEQ_R,
)
FX_BLOCK_SIZE = 18   # 9 named + 9 unused


class BlockType(Enum):
    MONO_INPUT = auto()
    STEREO_INPUT = auto()
    UNUSED = auto()
    MONO_MIX = auto()
    STEREO_MIX = auto()
    MONITOR = auto()
    FX = auto()


@dataclass(frozen=True)
class MeterBlock:
    kind: BlockType
    #: Number of *channels* in this block; UNUSED carries its meter count here.
    count: int

    @property
    def meters(self) -> int:
        if self.kind is BlockType.MONO_INPUT:
            return self.count * 10
        if self.kind is BlockType.STEREO_INPUT:
            return self.count * 20
        if self.kind is BlockType.UNUSED:
            # PRD 12.3: "unused" counts are meter SLOTS, not blocks.
            return self.count
        if self.kind is BlockType.MONO_MIX:
            return self.count * 10
        if self.kind is BlockType.STEREO_MIX:
            return self.count * 20
        if self.kind is BlockType.MONITOR:
            return self.count * MONITOR_BLOCK_SIZE
        return self.count * FX_BLOCK_SIZE


@dataclass(frozen=True)
class MeterSlot:
    """One addressable meter. Constructed through the factories below."""

    kind: BlockType
    index: int          # 1-based channel within the block kind
    meter: Meter | None
    side: str = "L"     # "L" or "R" for stereo blocks
    band: int | None = None

    @staticmethod
    def input(n: int, meter: Meter) -> MeterSlot:
        return MeterSlot(BlockType.MONO_INPUT, n, meter)

    @staticmethod
    def stereo_input(n: int, meter: Meter, side: str = "L") -> MeterSlot:
        return MeterSlot(BlockType.STEREO_INPUT, n, meter, side)

    @staticmethod
    def mix(channel: ChannelId, meter: Meter, side: str = "L") -> MeterSlot:
        # NOTE: the index here must be 1-based *within the STEREO_MIX (or
        # MONO_MIX) block kind alone* -- offset_of walks blocks of a single
        # kind cumulatively and knows nothing about the other kind's count.
        # Numbering MIX56..LR as a continuation of MIX1..MIX4 (5, 6, 7, 8)
        # would overrun the real 4-wide "stereo mix" block and silently
        # land in whatever STEREO_MIX block (group, matrix) follows it --
        # or, on a layout with no such trailing block (Qu-16), raise.
        if channel in _MONO_MIXES:
            return MeterSlot(BlockType.MONO_MIX, _MONO_MIXES.index(channel) + 1, meter, side)
        if channel in _STEREO_MIXES:
            return MeterSlot(BlockType.STEREO_MIX, _STEREO_MIXES.index(channel) + 1, meter, side)
        raise MeterLayoutError(f"{channel.label} is not a mix channel")

    @staticmethod
    def fx(n: int, meter: Meter) -> MeterSlot:
        return MeterSlot(BlockType.FX, n, meter)

    @staticmethod
    def monitor(meter: Meter) -> MeterSlot:
        return MeterSlot(BlockType.MONITOR, 1, meter)

    @staticmethod
    def rta(channel: str, band: int) -> MeterSlot:
        return MeterSlot(BlockType.MONITOR, 1, None, side=channel, band=band)


_MONO_MIXES = (Mix.MIX1, Mix.MIX2, Mix.MIX3, Mix.MIX4)
#: PRD 12.3: "4 stereo mix" is Mix 5-6, 7-8, 9-10 AND LR. LR is metered here,
#: not in the monitor block. Getting this wrong shifts every later block by 20.
_STEREO_MIXES = (Mix.MIX56, Mix.MIX78, Mix.MIX910, Mix.LR)


@dataclass(frozen=True)
class MeterLayout:
    name: str
    blocks: tuple[MeterBlock, ...]

    @property
    def total_meters(self) -> int:
        return sum(block.meters for block in self.blocks)

    @property
    def payload_bytes(self) -> int:
        return self.total_meters * 2

    @property
    def encoded_bytes(self) -> int:
        """Bytes on the wire after 7-bit packing."""
        payload = self.payload_bytes
        groups, remainder = divmod(payload, 7)
        return groups * 8 + (remainder + 1 if remainder else 0)

    def offset_of(self, slot: MeterSlot) -> int:
        """The 0-based meter index of ``slot`` within a frame."""
        offset = 0
        seen = 0
        for block in self.blocks:
            if block.kind is not slot.kind:
                offset += block.meters
                continue
            if slot.index > seen + block.count:
                seen += block.count
                offset += block.meters
                continue
            within = slot.index - seen - 1
            return offset + within * (block.meters // block.count) + self._within(slot)
        raise MeterLayoutError(
            f"{self.name} has no meter block for {slot.kind.name} index {slot.index}"
        )

    def _within(self, slot: MeterSlot) -> int:
        if slot.kind in (BlockType.MONO_INPUT, BlockType.STEREO_INPUT):
            table, width = MONO_INPUT_METERS, 10
        elif slot.kind in (BlockType.MONO_MIX, BlockType.STEREO_MIX):
            table, width = MONO_MIX_METERS, 10
        elif slot.kind is BlockType.FX:
            table, width = FX_METERS, FX_BLOCK_SIZE
        else:
            return self._within_monitor(slot)
        side = width if slot.side == "R" else 0
        if slot.meter is None or slot.meter not in table:
            raise MeterLayoutError(
                f"{slot.kind.name} blocks have no {slot.meter} meter; valid: "
                f"{', '.join(m.name for m in table)}"
            )
        if slot.kind is BlockType.FX:
            return table.index(slot.meter)
        return side + table.index(slot.meter)

    def _within_monitor(self, slot: MeterSlot) -> int:
        if slot.band is not None:
            if not 0 <= slot.band < RTA_BANDS:
                raise MeterLayoutError(f"RTA band must be 0..{RTA_BANDS - 1}, got {slot.band}")
            side = RTA_BANDS if slot.side == "R" else 0
            return MONITOR_RTA_OFFSET + side + slot.band
        if slot.meter is None or slot.meter not in MONITOR_METERS:
            raise MeterLayoutError(f"the monitor block has no {slot.meter} meter")
        return MONITOR_METERS.index(slot.meter)


def _layout(name: str, *blocks: MeterBlock) -> MeterLayout:
    return MeterLayout(name, blocks)


B = MeterBlock

#: PRD 12.3. Qu-16: 16 mono input, 80 unused, 3 stereo input, 20 unused,
#: 4 mono mix, 4 stereo mix, 1 stereo monitor, 4 stereo FX = 590 meters.
#: The 80 unused pads 16 inputs out to a 24-input stride.
QU16 = _layout(
    "Qu-16",
    B(BlockType.MONO_INPUT, 16), B(BlockType.UNUSED, 80),
    B(BlockType.STEREO_INPUT, 3), B(BlockType.UNUSED, 20),
    B(BlockType.MONO_MIX, 4), B(BlockType.STEREO_MIX, 4),
    B(BlockType.MONITOR, 1), B(BlockType.FX, 4),
)

#: PRD 12.3 as written: 830 meters. PRD 14.12 flags this as internally
#: inconsistent -- a Qu-24 frame cannot plausibly be larger than a Qu-32's 790.
QU24_AS_WRITTEN = _layout(
    "Qu-24",
    B(BlockType.MONO_INPUT, 24), B(BlockType.STEREO_INPUT, 3),
    B(BlockType.UNUSED, 180),
    B(BlockType.MONO_MIX, 4), B(BlockType.STEREO_MIX, 4),
    B(BlockType.STEREO_MIX, 2), B(BlockType.STEREO_MIX, 2),
    B(BlockType.MONITOR, 1), B(BlockType.FX, 4),
)

#: PRD 14.12's hypothesis: the unused figure is 100, not 180, making the input
#: region a uniform 400 meters across Qu-24/32/Pac/SB. Total 750.
QU24_ALTERNATE = _layout(
    "Qu-24 (alternate)",
    B(BlockType.MONO_INPUT, 24), B(BlockType.STEREO_INPUT, 3),
    B(BlockType.UNUSED, 100),
    B(BlockType.MONO_MIX, 4), B(BlockType.STEREO_MIX, 4),
    B(BlockType.STEREO_MIX, 2), B(BlockType.STEREO_MIX, 2),
    B(BlockType.MONITOR, 1), B(BlockType.FX, 4),
)

#: PRD 12.3. Note the inputs are NOT contiguous: channels 25-32 appear after
#: the stereo inputs and a 20-meter gap. Total 790.
QU32 = _layout(
    "Qu-32",
    B(BlockType.MONO_INPUT, 24), B(BlockType.STEREO_INPUT, 3),
    B(BlockType.UNUSED, 20), B(BlockType.MONO_INPUT, 8),
    B(BlockType.MONO_MIX, 4), B(BlockType.STEREO_MIX, 4),
    B(BlockType.STEREO_MIX, 4), B(BlockType.STEREO_MIX, 2),
    B(BlockType.MONITOR, 1), B(BlockType.FX, 4),
)

LAYOUTS: dict[str, MeterLayout] = {
    "qu16": QU16,
    "qu24": QU24_AS_WRITTEN,
    "qu32": QU32,
    "qupac": QU32,
    "qusb": QU32,
}

QU24_CANDIDATES: tuple[MeterLayout, ...] = (QU24_AS_WRITTEN, QU24_ALTERNATE)


class MeterFrame:
    """One meter frame. Decoded lazily on access (PRD 12.3)."""

    __slots__ = ("_buffer", "layout")

    def __init__(self, payload: bytes, layout: MeterLayout) -> None:
        if len(payload) % 2:
            raise MeterLayoutError(
                f"meter payload length {len(payload)} is odd; meters are 16-bit, so the "
                f"frame was truncated or the packing is misunderstood"
            )
        if len(payload) != layout.payload_bytes:
            raise MeterLayoutError(
                f"meter payload is {len(payload)} bytes but the {layout.name} layout "
                f"computes {layout.payload_bytes}. The layout table is wrong for this "
                f"console -- use mixer.meters.set_layout() to correct it (PRD 12.3)."
            )
        self._buffer = memoryview(payload)
        self.layout = layout

    def raw(self, slot: MeterSlot) -> int:
        offset = self.layout.offset_of(slot) * 2
        return int.from_bytes(self._buffer[offset : offset + 2], "big")

    def db(self, slot: MeterSlot) -> float:
        return decode_meter_db(self.raw(slot))
