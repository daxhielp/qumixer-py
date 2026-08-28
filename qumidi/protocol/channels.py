"""Channel identity (PRD 6.1, 6.3).

Core invariant: the Qu MIDI protocol is entirely identity-addressed and never
position-addressed. No message carries a physical strip position or a layer
index. Nothing in this module knows what a layer is, and nothing added to it
ever should -- see PRD 6.3 and qumidi/layout.py.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, IntEnum, auto

from qumidi.errors import ParameterRangeError, UnsupportedOnModelError


class ChannelClass(IntEnum):
    """One member per row of the PRD 6.1 channel table, plus two synthetics.

    MIX_MONO/MIX_STEREO/LR are deliberately separate: pan and delay behave
    differently on them and the meter layout treats them as separate blocks.
    """

    FX_SEND = auto()
    FX_RETURN = auto()
    DCA = auto()
    INPUT = auto()
    STEREO_INPUT = auto()
    MUTE_GROUP = auto()
    MIX_MONO = auto()
    MIX_STEREO = auto()
    LR = auto()
    GROUP = auto()
    MATRIX = auto()
    #: Console-global settings with no documented CH (Group Mode, PRD 7.2).
    GLOBAL = auto()
    #: Socket-addressed dSNAKE preamp state, keyed per socket (PRD 7.2).
    DSNAKE = auto()


@dataclass(frozen=True, order=True)
class ChannelId:
    """A channel identity. ``value`` is the wire ``CH`` byte; -1 for synthetics.

    Deliberately *not* an int subclass: PRD 14.4 requires that ChannelId and
    ParamId cannot be passed for one another, and PRD 14.7 requires the same
    of ChannelId and StripId.
    """

    value: int
    cls: ChannelClass
    index: int
    label: str


def _make(
    base: int, count: int, cls: ChannelClass, fmt: str, kind: str
) -> Callable[[int], ChannelId]:
    def factory(n: int) -> ChannelId:
        if not 1 <= n <= count:
            raise ParameterRangeError(f"{kind} number must be 1..{count}, got {n}")
        return ChannelId(value=base + n - 1, cls=cls, index=n, label=fmt.format(n=n))

    factory.__name__ = kind.replace(" ", "")
    return factory


Input = _make(0x20, 32, ChannelClass.INPUT, "Input {n}", "input")
StereoInput = _make(0x40, 3, ChannelClass.STEREO_INPUT, "ST{n}", "stereo input")
Dca = _make(0x10, 4, ChannelClass.DCA, "DCA {n}", "DCA")
MuteGroup = _make(0x50, 4, ChannelClass.MUTE_GROUP, "Mute Group {n}", "mute group")
FxSend = _make(0x00, 4, ChannelClass.FX_SEND, "FX Send {n}", "FX send")
FxReturn = _make(0x08, 4, ChannelClass.FX_RETURN, "FX Return {n}", "FX return")


def Group(n: int) -> ChannelId:
    """Audio group 1-2 .. 7-8, numbered 1..4 (PRD 6.1)."""
    if not 1 <= n <= 4:
        raise ParameterRangeError(f"group number must be 1..4, got {n}")
    lo = 2 * n - 1
    return ChannelId(0x68 + n - 1, ChannelClass.GROUP, n, f"Group {lo}-{lo + 1}")


def Matrix(n: int) -> ChannelId:
    """Matrix 1-2 or 3-4, numbered 1..2 (PRD 6.1)."""
    if not 1 <= n <= 2:
        raise ParameterRangeError(f"matrix number must be 1..2, got {n}")
    lo = 2 * n - 1
    return ChannelId(0x6C + n - 1, ChannelClass.MATRIX, n, f"Matrix {lo}-{lo + 1}")


class Mix:
    """The seven mix identities and LR.

    PRD 6.1: "Mix 1 to 10" occupies only seven ids because 5-6, 7-8 and 9-10
    are stereo pairs. Model that explicitly rather than pretending there are ten.
    """

    MIX1 = ChannelId(0x60, ChannelClass.MIX_MONO, 1, "Mix 1")
    MIX2 = ChannelId(0x61, ChannelClass.MIX_MONO, 2, "Mix 2")
    MIX3 = ChannelId(0x62, ChannelClass.MIX_MONO, 3, "Mix 3")
    MIX4 = ChannelId(0x63, ChannelClass.MIX_MONO, 4, "Mix 4")
    MIX56 = ChannelId(0x64, ChannelClass.MIX_STEREO, 1, "Mix 5-6")
    MIX78 = ChannelId(0x65, ChannelClass.MIX_STEREO, 2, "Mix 7-8")
    MIX910 = ChannelId(0x66, ChannelClass.MIX_STEREO, 3, "Mix 9-10")
    LR = ChannelId(0x67, ChannelClass.LR, 1, "LR")


#: Console-global pseudo-channel for parameters with no documented CH (PRD 7.2).
GLOBAL = ChannelId(-1, ChannelClass.GLOBAL, 0, "GLOBAL")


def dsnake_socket_channel(socket: int) -> ChannelId:
    """A synthetic per-socket identity used only as a cache key (PRD 7.2)."""
    if not 0x00 <= socket <= 0x27:
        raise ParameterRangeError(f"dSNAKE socket must be 0x00..0x27, got {socket:#04x}")
    return ChannelId(-1, ChannelClass.DSNAKE, socket, f"dSNAKE socket {socket}")


_ALL: list[ChannelId] = [
    *(FxSend(n) for n in range(1, 5)),
    *(FxReturn(n) for n in range(1, 5)),
    *(Dca(n) for n in range(1, 5)),
    *(Input(n) for n in range(1, 33)),
    *(StereoInput(n) for n in range(1, 4)),
    *(MuteGroup(n) for n in range(1, 5)),
    Mix.MIX1, Mix.MIX2, Mix.MIX3, Mix.MIX4, Mix.MIX56, Mix.MIX78, Mix.MIX910, Mix.LR,
    *(Group(n) for n in range(1, 5)),
    *(Matrix(n) for n in range(1, 3)),
]

_BY_RAW: dict[int, ChannelId] = {ch.value: ch for ch in _ALL}

ALL_CHANNELS: tuple[ChannelId, ...] = tuple(_ALL)


def channel_from_raw(raw: int) -> ChannelId:
    """Decode a wire ``CH`` byte. Raises for bytes the table does not assign."""
    try:
        return _BY_RAW[raw]
    except KeyError:
        raise ParameterRangeError(f"no channel is assigned to CH {raw:#04x}") from None


_REF_FACTORIES = {
    "input": Input,
    "in": Input,
    "st": StereoInput,
    "stereo": StereoInput,
    "dca": Dca,
    "mutegroup": MuteGroup,
    "mg": MuteGroup,
    "group": Group,
    "grp": Group,
    "matrix": Matrix,
    "mtx": Matrix,
    "fxsend": FxSend,
    "fxreturn": FxReturn,
}

_MIX_REFS = {
    "1": Mix.MIX1, "2": Mix.MIX2, "3": Mix.MIX3, "4": Mix.MIX4,
    "5-6": Mix.MIX56, "56": Mix.MIX56,
    "7-8": Mix.MIX78, "78": Mix.MIX78,
    "9-10": Mix.MIX910, "910": Mix.MIX910,
}


def parse_channel_ref(text: str) -> ChannelId:
    """Parse a CLI channel reference such as ``input:6``, ``mix:5-6`` or ``lr``."""
    token = text.strip().lower()
    if token == "lr":
        return Mix.LR
    if ":" not in token:
        raise ParameterRangeError(
            f"channel reference {text!r} must look like 'input:6', 'mix:3', 'dca:2' or 'lr'"
        )
    kind, _, arg = token.partition(":")
    if kind == "mix":
        try:
            return _MIX_REFS[arg]
        except KeyError:
            raise ParameterRangeError(
                f"unknown mix {arg!r}; valid: {', '.join(sorted(_MIX_REFS))}"
            ) from None
    factory = _REF_FACTORIES.get(kind)
    if factory is None:
        raise ParameterRangeError(
            f"unknown channel kind {kind!r} in {text!r}; valid: "
            f"{', '.join(sorted({*_REF_FACTORIES, 'mix', 'lr'}))}"
        )
    try:
        return factory(int(arg))
    except ValueError:
        raise ParameterRangeError(f"channel number in {text!r} must be an integer") from None


# --- Model capabilities (PRD 6.2) ----------------------------------------

import logging  # grouped with the model section for readability

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelCaps:
    """Counts, not booleans (PRD 6.2).

    ``mono_mixes``/``stereo_mixes``/``groups``/``matrices`` are *stereo pair*
    counts matching the channel-id table: mono_mixes=4, stereo_mixes=3 is the
    seven channel ids covering "Mix 1 to 10", not ten.
    """

    name: str
    box_id: int
    mono_inputs: int
    stereo_inputs: int
    mono_mixes: int
    stereo_mixes: int
    groups: int
    matrices: int
    fx_units: int
    #: True where the count is inferred rather than verified on hardware.
    groups_provisional: bool = False


class Model(Enum):
    QU16 = 1
    QU24 = 2
    QU32 = 3
    QUPAC = 4
    QUSB = 5

    @property
    def box_id(self) -> int:
        return int(self.value)

    @classmethod
    def from_box_id(cls, box_id: int) -> Model | None:
        try:
            return cls(box_id)
        except ValueError:
            return None


CAPS_BY_BOX_ID: dict[int, ModelCaps] = {
    1: ModelCaps("Qu-16", 1, 16, 3, 4, 3, 0, 0, 2),
    # PRD 6.2 dagger note: the 2 is inferred from the meter block table while
    # the channel-id table implies 4. M6 hardware item; provisional until then.
    2: ModelCaps("Qu-24", 2, 24, 3, 4, 3, 2, 2, 4, groups_provisional=True),
    3: ModelCaps("Qu-32", 3, 32, 3, 4, 3, 4, 2, 4),
    4: ModelCaps("Qu-Pac", 4, 32, 3, 4, 3, 4, 2, 4),
    5: ModelCaps("Qu-SB", 5, 32, 3, 4, 3, 4, 2, 4),
}

#: Used before sync and for unrecognised BoxIDs (PRD 6.2).
PERMISSIVE_CAPS = ModelCaps("unknown", -1, 32, 3, 4, 3, 4, 2, 4, groups_provisional=True)


def caps_for_box_id(box_id: int) -> ModelCaps:
    """Resolve a BoxID, falling back permissively for unrecognised consoles."""
    caps = CAPS_BY_BOX_ID.get(box_id)
    if caps is None:
        _log.warning(
            "unrecognised BoxID %d; falling back to the permissive capability set. "
            "Refusing to connect to hardware that would otherwise work is the worse failure.",
            box_id,
        )
        return PERMISSIVE_CAPS
    return caps


def _limit(caps: ModelCaps, ch: ChannelId) -> tuple[int, bool]:
    """Return (max valid 1-based index for this class, is_provisional)."""
    if ch.cls is ChannelClass.INPUT:
        return caps.mono_inputs, False
    if ch.cls is ChannelClass.STEREO_INPUT:
        return caps.stereo_inputs, False
    if ch.cls is ChannelClass.MIX_MONO:
        return caps.mono_mixes, False
    if ch.cls is ChannelClass.MIX_STEREO:
        return caps.stereo_mixes, False
    if ch.cls is ChannelClass.GROUP:
        return caps.groups, caps.groups_provisional
    if ch.cls is ChannelClass.MATRIX:
        return caps.matrices, False
    if ch.cls in (ChannelClass.FX_SEND, ChannelClass.FX_RETURN):
        return caps.fx_units, False
    # DCA, mute group, LR and the synthetics exist on every model.
    return ch.index, False


def check_channel(caps: ModelCaps | None, ch: ChannelId) -> None:
    """Raise UnsupportedOnModelError if ``ch`` does not exist on this model.

    Before sync (``caps is None``) checks are permissive with a warning, and a
    provisional count warns rather than raising (PRD 6.2).
    """
    if caps is None:
        _log.warning(
            "capability check for %s ran before sync; permitting it. Call "
            "wait_synced() first to get a real check.",
            ch.label,
        )
        return
    limit, provisional = _limit(caps, ch)
    if ch.index <= limit:
        return
    message = (
        f"{ch.label} does not exist on {caps.name} "
        f"(this model has {limit} of that channel class)"
    )
    if provisional:
        _log.warning(
            "%s -- but that count is provisional and unverified on hardware, so this is "
            "permitted rather than refused (PRD 6.2)",
            message,
        )
        return
    raise UnsupportedOnModelError(message)
