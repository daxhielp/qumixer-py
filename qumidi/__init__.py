"""qumidi -- control and observe Allen & Heath Qu series mixers over TCP MIDI.

The public surface is re-exported here so that ordinary use never has to
import from a submodule::

    from qumidi import AsyncQuMixer, Mix, Input

Everything not listed in ``__all__`` is an implementation detail and may
change without notice.
"""

from __future__ import annotations

from qumidi.client_async import AsyncQuMixer, MmcCommand
from qumidi.client_sync import QuMixer
from qumidi.errors import (
    MeterLayoutError,
    NotSyncedError,
    ParameterRangeError,
    QuConnectionError,
    QuConnectionLostError,
    QuError,
    ReadOnlyParameterError,
    ShutdownNotPermittedError,
    SyncTimeoutError,
    UnknownParameterError,
    UnsupportedOnModelError,
    ValueUnknownError,
)
from qumidi.events import ChangeEvent, Source
from qumidi.layout import SurfaceMap
from qumidi.protocol.channels import (
    GLOBAL,
    ChannelClass,
    ChannelId,
    Dca,
    FxReturn,
    FxSend,
    Group,
    Input,
    Matrix,
    Mix,
    Model,
    ModelCaps,
    MuteGroup,
    StereoInput,
    parse_channel_ref,
)
from qumidi.protocol.meters import Meter, MeterFrame, MeterLayout, MeterSlot
from qumidi.protocol.params import Direction, Tier
from qumidi.protocol.strips import Strip, StripId, StripKey
from qumidi.snapshot import SnapshotDiff
from qumidi.state import CachedValue, Freshness, MembershipSet
from qumidi.transport.base import ConnectionState

__version__ = "0.1.0.dev0"

__all__ = [
    "GLOBAL",
    "AsyncQuMixer",
    "CachedValue",
    "ChangeEvent",
    "ChannelClass",
    "ChannelId",
    "ConnectionState",
    "Dca",
    "Direction",
    "Freshness",
    "FxReturn",
    "FxSend",
    "Group",
    "Input",
    "Matrix",
    "MembershipSet",
    "Meter",
    "MeterFrame",
    "MeterLayout",
    "MeterLayoutError",
    "MeterSlot",
    "Mix",
    "MmcCommand",
    "Model",
    "ModelCaps",
    "MuteGroup",
    "NotSyncedError",
    "ParameterRangeError",
    "QuConnectionError",
    "QuConnectionLostError",
    "QuError",
    "QuMixer",
    "ReadOnlyParameterError",
    "ShutdownNotPermittedError",
    "SnapshotDiff",
    "Source",
    "StereoInput",
    "Strip",
    "StripId",
    "StripKey",
    "SurfaceMap",
    "SyncTimeoutError",
    "Tier",
    "UnknownParameterError",
    "UnsupportedOnModelError",
    "ValueUnknownError",
    "__version__",
    "parse_channel_ref",
]
