"""Exception hierarchy for qumidi (PRD 11.3).

Every exception message must name the channel, parameter and value involved.
"Invalid value" with no context is useless when debugging a 40-channel console.
"""

from __future__ import annotations


class QuError(Exception):
    """Base class for every error raised by this library."""


class QuConnectionError(QuError):
    """Could not establish or maintain the TCP MIDI session.

    Most commonly raised because the mixer permits exactly one TCP MIDI
    connection at a time and something else (Qu-Pad, another script) holds it.
    """


class QuConnectionLostError(QuConnectionError):
    """A write was attempted after an unrecoverable disconnect."""


class SyncTimeoutError(QuError):
    """The console replied to the state request but never sent the end marker."""


class NotSyncedError(QuError):
    """An operation needing the model or the console's MIDI channel ran before sync."""


class UnsupportedOnModelError(QuError):
    """The addressed channel does not exist on the detected model."""


class ParameterRangeError(QuError):
    """A value or index is outside the range the parameter accepts."""


class UnknownParameterError(QuError):
    """No registry entry matches the requested parameter name."""


class ReadOnlyParameterError(QuError):
    """Attempted to write a Direction.RX_ONLY parameter."""


class ValueUnknownError(QuError):
    """Read of a value the console has never reported, with no default supplied."""


class ShutdownNotPermittedError(QuError):
    """Remote shutdown attempted without both guards satisfied."""


class MeterLayoutError(QuError):
    """A meter frame's length disagrees with the computed layout for the model."""
