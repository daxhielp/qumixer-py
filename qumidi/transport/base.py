"""Transport interface (PRD 4.1).

A transport yields *parsed messages*, not bytes. TcpTransport owns a
MidiStreamParser internally; a future UsbTransport receives pre-framed
messages from mido and skips the parser. That asymmetry is why the parser is
not part of the client.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from enum import Enum, auto

from qumidi.midi.messages import MidiMessage

log = logging.getLogger(__name__)


class ConnectionState(Enum):
    DISCONNECTED = auto()
    CONNECTING = auto()
    CONNECTED = auto()
    SYNCING = auto()
    READY = auto()
    RECONNECTING = auto()


class Transport(ABC):
    """Bytes in, parsed messages out."""

    def __init__(self) -> None:
        self._state = ConnectionState.DISCONNECTED
        self._state_callbacks: list[Callable[[ConnectionState], None]] = []

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def send(self, data: bytes) -> None: ...

    @abstractmethod
    def messages(self) -> AsyncIterator[MidiMessage]: ...

    @property
    @abstractmethod
    def is_connected(self) -> bool: ...

    @property
    def state(self) -> ConnectionState:
        return self._state

    def on_state_change(self, cb: Callable[[ConnectionState], None]) -> None:
        self._state_callbacks.append(cb)

    def set_state(self, state: ConnectionState) -> None:
        """Record a transition and notify subscribers. Callback errors never propagate."""
        if state is self._state:
            return
        self._state = state
        for cb in list(self._state_callbacks):
            try:
                cb(state)
            except Exception:
                log.exception("connection-state callback raised; continuing")
