"""qumidi -- control and observe Allen & Heath Qu series mixers over TCP MIDI."""

from __future__ import annotations

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

__version__ = "0.1.0.dev0"

__all__ = [
    "MeterLayoutError",
    "NotSyncedError",
    "ParameterRangeError",
    "QuConnectionError",
    "QuConnectionLostError",
    "QuError",
    "ReadOnlyParameterError",
    "ShutdownNotPermittedError",
    "SyncTimeoutError",
    "UnknownParameterError",
    "UnsupportedOnModelError",
    "ValueUnknownError",
    "__version__",
]
