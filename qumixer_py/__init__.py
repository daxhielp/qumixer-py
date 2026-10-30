"""qumixer-py -- control and observe Allen & Heath Qu series mixers over TCP MIDI.

The public surface is re-exported here so that ordinary use never has to
import from a submodule::

    from qumixer_py import AsyncQuMixer, Mix, Input

Everything not listed in ``__all__`` is an implementation detail and may
change without notice.
"""

from __future__ import annotations

from qumixer_py.client_async import AsyncQuMixer, MmcCommand
from qumixer_py.client_sync import QuMixer
from qumixer_py.errors import (
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
from qumixer_py.events import ChangeEvent, Source
from qumixer_py.layout import SurfaceMap
from qumixer_py.protocol.channels import (
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
from qumixer_py.protocol.meters import Meter, MeterFrame, MeterLayout, MeterSlot
from qumixer_py.protocol.params import Direction, Tier
from qumixer_py.protocol.strips import Strip, StripId, StripKey
from qumixer_py.snapshot import SnapshotDiff
from qumixer_py.state import CachedValue, Freshness, MembershipSet
from qumixer_py.transport.base import ConnectionState

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
