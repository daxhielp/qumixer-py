"""Write guard for the hardware suite (spec: docs/superpowers/specs/2026-09-19-hardware-suite-design.md)."""

from __future__ import annotations

from qumixer_py.errors import ParameterRangeError

DENYLIST: frozenset[str] = frozenset(
    {"shutdown", "transport", "local_48v", "preamp_source", "usb_source", "insert"}
)


def assert_writable(param: str, *, ar_rack: bool = False) -> None:
    """Refuse parameters the hardware suite must never write.

    ``dsnake_48v`` is covered by the ``dsnake_`` prefix rule; ``local_48v`` is
    listed explicitly. ``dsnake_*`` writes are allowed only with an AR rack.
    """
    if param in DENYLIST or (param.startswith("dsnake_") and not ar_rack):
        raise ParameterRangeError(f"{param} is on the hardware-suite denylist and is never written")
