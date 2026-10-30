"""Protocol constants and tuned defaults.

Every timing default here was tuned for a reason (keepalive deadlines, the
console's rate limit, reconnect backoff). Read the comment beside a constant
before changing it.
"""

from __future__ import annotations

from typing import Final

# --- Transport ---------------------------------------

QU_TCP_PORT: Final = 51325
CONNECT_TIMEOUT: Final = 5.0

#: Sent unconditionally on this interval whenever the link is up. Safe under
#: both keepalive regimes and satisfies the 5s iPadFlag=1 deadline.
KEEPALIVE_INTERVAL: Final = 2.0

#: No bytes of any kind for this long means the link is dead. Do not wait for
#: the socket to notice.
INBOUND_TIMEOUT: Final = 10.0

#: Roughly the 31.25 kbaud DIN-MIDI rate the console's parser is built around.
MAX_SEND_RATE: Final = 1000  # bytes per second
OUTBOUND_QUEUE_MAXSIZE: Final = 256

RECONNECT_BACKOFF_MIN: Final = 0.5
RECONNECT_BACKOFF_MAX: Final = 30.0
RECONNECT_BACKOFF_FACTOR: Final = 2.0
RECONNECT_JITTER: Final = 0.2

# --- MIDI status bytes ---------------------------------------------------

NOTE_OFF: Final = 0x80
NOTE_ON: Final = 0x90
CONTROL_CHANGE: Final = 0xB0
PROGRAM_CHANGE: Final = 0xC0
SYSEX_START: Final = 0xF0
SYSEX_END: Final = 0xF7
ACTIVE_SENSING: Final = 0xFE

CC_NRPN_MSB: Final = 0x63
CC_NRPN_LSB: Final = 0x62
CC_DATA_MSB: Final = 0x06
CC_DATA_LSB: Final = 0x26
CC_BANK_SELECT_MSB: Final = 0x00
CC_BANK_SELECT_LSB: Final = 0x20

MAX_SYSEX_BYTES: Final = 65536

# --- Qu sysex framing -------------------------------------

#: F0 00 00 1A 50 11 01 00 -- the channel byte follows, then the payload.
QU_SYSEX_PREFIX: Final = bytes([0xF0, 0x00, 0x00, 0x1A, 0x50, 0x11, 0x01, 0x00])
ALL_CALL_CHANNEL_BYTE: Final = 0x7F

CMD_GET_NAME: Final = 0x01
CMD_NAME_REPLY: Final = 0x02
CMD_SET_NAME: Final = 0x03
CMD_GET_SYSTEM_STATE: Final = 0x10
CMD_SYSTEM_STATE_REPLY: Final = 0x11
CMD_METERS: Final = 0x12
CMD_METER_DATA: Final = 0x13
CMD_END_OF_SYNC: Final = 0x14

# --- Sync and naming --------------------------------------

DEFAULT_IPAD_FLAG: Final = 1
REPLY_TIMEOUT: Final = 5.0
SYNC_TIMEOUT: Final = 15.0
NAME_SWEEP_INTERVAL: Final = 0.020
NAME_REQUEST_TIMEOUT: Final = 0.5
NAME_REQUEST_RETRIES: Final = 2

#: Provisional -- to be measured on hardware, and the
#: measured value substituted here.
MAX_NAME_LENGTH: Final = 6

#: The minimum firmware this registry was transcribed against.
MIN_FIRMWARE: Final = (1, 9)

# --- NRPN decode and state --------------------------

PENDING_FLUSH_TIMEOUT: Final = 0.050
ECHO_WINDOW: Final = 1.0
SLOW_CALLBACK_WARN: Final = 0.100
SCENE_SETTLE_DELAY: Final = 0.500

# --- Metering ----------------------------------------------------

#: Bounded so a slow consumer drops OLD frames rather than growing without
#: limit -- a stale meter reading has no value.
METER_QUEUE_SIZE: Final = 4
