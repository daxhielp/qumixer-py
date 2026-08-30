"""The parameter description types (PRD 7.1).

A ParamSpec describes one controllable parameter completely: its NRPN id, what
its VX byte means, what its value means, and which channel classes accept it.
Both the flat command API and the object facade are generated from these, so
protocol knowledge lives in exactly one place.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum, auto

from qumidi.errors import ParameterRangeError
from qumidi.protocol.channels import ChannelClass
from qumidi.protocol.units import Taper

log = logging.getLogger(__name__)


class Direction(Enum):
    TX_ONLY = auto()
    #: Writing raises ReadOnlyParameterError rather than emitting bytes the
    #: mixer ignores (PRD 7.1). Group Mode 5E and dSNAKE Patch 5D.
    RX_ONLY = auto()
    BIDIRECTIONAL = auto()


class Tier(Enum):
    #: Confirmed against hardware in M6. Only the calibration run may set this,
    #: never a hand edit (PRD 7.1).
    VERIFIED = auto()
    SUPPORTED = auto()
    EXPERIMENTAL = auto()


# --- VX modes -------------------------------------------------------------


class VxMode:
    """What the Data Entry LSB byte means for one parameter."""

    def validate(self, vx: int | None, param: str) -> int:
        raise NotImplementedError

    def sub_index(self, vx: int, value: int) -> int | None:
        """The cache addressing index this parameter shape contributes."""
        raise NotImplementedError


@dataclass(frozen=True)
class Fixed(VxMode):
    """A constant filler byte.

    PRD 7.1: the filler value is inconsistent across parameters and must be
    transcribed literally, not inferred. Getting it wrong produces messages
    the mixer silently ignores.
    """

    value: int

    def validate(self, vx: int | None, param: str) -> int:
        if vx is not None and vx != self.value:
            raise ParameterRangeError(
                f"{param} takes no index; its VX is the fixed filler {self.value:#04x}, "
                f"but {vx:#04x} was supplied"
            )
        return self.value

    def sub_index(self, vx: int, value: int) -> int | None:
        return None


@dataclass(frozen=True)
class _RangedVx(VxMode):
    lo: int
    hi: int
    what: str

    def validate(self, vx: int | None, param: str) -> int:
        if vx is None:
            raise ParameterRangeError(f"{param} requires a {self.what} index in VX")
        if not self.lo <= vx <= self.hi:
            raise ParameterRangeError(
                f"{param}: {self.what} index must be {self.lo:#04x}..{self.hi:#04x}, "
                f"got {vx:#04x}"
            )
        return vx

    def sub_index(self, vx: int, value: int) -> int | None:
        return vx


@dataclass(frozen=True)
class GeqBand(_RangedVx):
    """28 GEQ bands, index 0x00-0x1B (PRD 7.2)."""

    lo: int = 0x00
    hi: int = 0x1B
    what: str = "GEQ band"


@dataclass(frozen=True)
class DsnakeSocket(_RangedVx):
    """dSNAKE socket 0x00-0x27 (PRD 7.2)."""

    lo: int = 0x00
    hi: int = 0x27
    what: str = "dSNAKE socket"


@dataclass(frozen=True)
class SendTarget(VxMode):
    """A send destination.

    The valid bytes are *not* a dense range (PRD 7.2: note the gap at 0E-0F),
    so VX must come from SEND_TARGET_VX by lookup, never from arithmetic on a
    mix index. Validation against that table lives in the registry, which owns
    it; this class only enforces presence and 7-bit range.
    """

    def validate(self, vx: int | None, param: str) -> int:
        if vx is None:
            raise ParameterRangeError(f"{param} requires a send target in VX")
        if not 0x00 <= vx <= 0x7F:
            raise ParameterRangeError(f"{param}: send target VX must be 7-bit, got {vx:#04x}")
        return vx

    def sub_index(self, vx: int, value: int) -> int | None:
        return vx


#: FX delay tap VX values (PRD 7.2, 14.5). Module-level so the 0x48 collision
#: resolution in registry.py can reference them without instantiating FxTap.
FX_TAP_LEFT = 0x05
FX_TAP_LINK = 0x06
FX_TAP_RIGHT = 0x07


@dataclass(frozen=True)
class FxTap(VxMode):
    """FX delay tap: 0x05 left, 0x07 right, 0x06 link (PRD 7.2)."""

    LEFT: int = FX_TAP_LEFT
    LINK: int = FX_TAP_LINK
    RIGHT: int = FX_TAP_RIGHT

    def validate(self, vx: int | None, param: str) -> int:
        if vx not in (self.LEFT, self.LINK, self.RIGHT):
            raise ParameterRangeError(
                f"{param}: FX tap VX must be 0x05 (left), 0x06 (link) or 0x07 (right), "
                f"got {vx!r}"
            )
        assert vx is not None
        return vx

    def sub_index(self, vx: int, value: int) -> int | None:
        return vx


# --- Domains --------------------------------------------------------------


class Domain:
    """What a parameter's 7-bit value means."""

    def validate_raw(self, value: int, param: str, ch_label: str) -> int:
        raise NotImplementedError

    def clamp_inbound(self, value: int, param: str, ch_label: str) -> int:
        """Coerce a value received from the console. Never raises (PRD 7.1)."""
        return value

    def sub_index_from_value(self, value: int) -> int | None:
        return None


@dataclass(frozen=True)
class Boolean(Domain):
    off: int = 0x00
    on: int = 0x01

    def validate_raw(self, value: int, param: str, ch_label: str) -> int:
        if value not in (self.off, self.on):
            raise ParameterRangeError(
                f"{ch_label} {param}: value must be {self.off:#04x} or {self.on:#04x}, "
                f"got {value:#04x}"
            )
        return value

    def clamp_inbound(self, value: int, param: str, ch_label: str) -> int:
        if value not in (self.off, self.on):
            log.warning(
                "%s %s: received %#04x, expected %#04x or %#04x; treating non-zero as on",
                ch_label, param, value, self.off, self.on,
            )
            return self.on if value else self.off
        return value


@dataclass(frozen=True)
class Raw7Bit(Domain):
    """A bounded 7-bit value. Bounds are explicit, never assumed to be 0-0x7F."""

    lo: int = 0x00
    hi: int = 0x7F

    def validate_raw(self, value: int, param: str, ch_label: str) -> int:
        if not self.lo <= value <= self.hi:
            raise ParameterRangeError(
                f"{ch_label} {param}: raw value must be {self.lo:#04x}..{self.hi:#04x}, "
                f"got {value:#04x}"
            )
        return value

    def clamp_inbound(self, value: int, param: str, ch_label: str) -> int:
        if value < self.lo or value > self.hi:
            clamped = min(max(value, self.lo), self.hi)
            log.warning(
                "%s %s: console reported %#04x outside %#04x..%#04x; clamping to %#04x",
                ch_label, param, value, self.lo, self.hi, clamped,
            )
            return clamped
        return value


@dataclass(frozen=True)
class Interpolated(Raw7Bit):
    """A bounded raw value with a dB/Hz/ms taper attached (PRD 8)."""

    taper: Taper = field(default_factory=lambda: Taper(points=((0, 0.0), (127, 1.0))))

    def to_db(self, raw: int) -> float:
        return self.taper.to_db(raw)

    def to_raw(self, db: float) -> int:
        return self.taper.to_raw(db)


@dataclass(frozen=True)
class EnumDomain(Domain):
    #: Stored as a tuple of pairs so the spec stays hashable and frozen.
    mapping: tuple[tuple[int, str], ...]

    # dataclasses' _set_new_attribute does not overwrite a name already in the
    # class dict, so this hand-written __init__ wins over the generated one.
    # That is what lets callers pass a plain dict while the field stays a tuple.
    def __init__(self, mapping: dict[int, str]) -> None:
        object.__setattr__(self, "mapping", tuple(mapping.items()))

    @property
    def as_dict(self) -> dict[int, str]:
        return dict(self.mapping)

    def validate_raw(self, value: int, param: str, ch_label: str) -> int:
        if value not in self.as_dict:
            valid = ", ".join(f"{k:#04x} ({v})" for k, v in self.mapping)
            raise ParameterRangeError(
                f"{ch_label} {param}: {value:#04x} is not a valid value; valid: {valid}"
            )
        return value

    def clamp_inbound(self, value: int, param: str, ch_label: str) -> int:
        if value not in self.as_dict:
            log.warning(
                "%s %s: console reported undocumented value %#04x", ch_label, param, value
            )
        return value

    def label(self, value: int) -> str | None:
        return self.as_dict.get(value)

    def from_label(self, label: str) -> int:
        for raw, name in self.mapping:
            if name == label:
                return raw
        valid = ", ".join(name for _, name in self.mapping)
        raise ParameterRangeError(f"unknown value {label!r}; valid: {valid}")


@dataclass(frozen=True)
class FlaggedIndex(Domain):
    """A set-valued parameter: "channel X is a member of group i".

    PRD 14.10: the group index lives in the *value*, not in VX. One NRPN
    message carries one bit of a four-bit set, so the cache must key on the
    index extracted from the value -- see PRD 10.1a.
    """

    on_flag: int = 0x40
    count: int = 4

    def encode(self, index: int, on: bool) -> int:
        if not 0 <= index < self.count:
            raise ParameterRangeError(f"group index must be 0..{self.count - 1}, got {index}")
        return (self.on_flag | index) if on else index

    def is_on(self, value: int) -> bool:
        return bool(value & self.on_flag)

    def sub_index_from_value(self, value: int) -> int | None:
        return value & (self.on_flag - 1)

    def validate_raw(self, value: int, param: str, ch_label: str) -> int:
        index = value & (self.on_flag - 1)
        if value & ~(self.on_flag | (self.on_flag - 1)) or index >= self.count:
            raise ParameterRangeError(
                f"{ch_label} {param}: {value:#04x} is not a valid membership value; "
                f"expected 0x00-{self.count - 1:#04x} (off) or "
                f"{self.on_flag:#04x}-{self.on_flag + self.count - 1:#04x} (on)"
            )
        return value


# --- ParamSpec ------------------------------------------------------------


@dataclass(frozen=True)
class ParamSpec:
    """One row of the parameter registry (PRD 7.1)."""

    name: str
    nrpn_id: int
    vx: VxMode
    domain: Domain
    applies_to: frozenset[ChannelClass]
    direction: Direction = Direction.BIDIRECTIONAL
    tier: Tier = Tier.SUPPORTED
    notes: str = ""
