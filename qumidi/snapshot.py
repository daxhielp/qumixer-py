"""Snapshot and diff (PRD 10.5).

The cache is the only complete picture of console state the library holds, and
it is expensive to rebuild -- a full sync plus a paced name sweep. This makes
it serialisable.

Restoring is deliberately NOT "push this snapshot to the console". diff()
reports what differs and the application decides what to write. A blind
restore replaying hundreds of unpaced writes is the single most dangerous
operation this library could offer, and a partial failure halfway through
leaves the console in a state that is in neither snapshot.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from qumidi.protocol.channels import ChannelClass, ChannelId
from qumidi.state import StateCache, channel_from_key

SNAPSHOT_VERSION = 1


@dataclass(frozen=True)
class SnapshotDiff:
    channel: ChannelId
    param: str
    sub_index: int | None
    snapshot_raw: int | None
    current_raw: int | None
    snapshot_freshness: str
    current_freshness: str


def build_snapshot(
    *,
    box_id: int | None,
    model: str | None,
    firmware: tuple[int, int] | None,
    cache: StateCache,
    names: dict[ChannelId, str | None],
    now: float,
) -> dict[str, Any]:
    """Build the plain-dict snapshot.

    ``values`` carries every entry ``cache`` actually holds, UNCONFIRMED and
    STALE included, each tagged with its own freshness -- never silently
    upgraded or dropped (PRD 10.5). ``StateCache`` never materializes a row
    for a parameter that has simply never been observed (its ``.get()``
    returns the UNKNOWN sentinel only for the lookup, it never stores it), so
    such a parameter is absent from ``values`` here too -- there is no cache
    entry to iterate, so nothing here can synthesize a fake
    ``"freshness": "UNKNOWN"`` row for it.

    ``names`` carries every channel this client currently tracks a name for,
    ``None`` included -- e.g. every nameable channel goes back to ``None``
    on scene recall until the post-recall sweep re-populates it (PRD 9.4).
    Filtering those out would make "not yet re-swept" indistinguishable from
    "channel absent from this snapshot".
    """
    values = []
    for (raw_ch, cls, param, sub), value in cache.items():
        values.append(
            {
                "channel_raw": raw_ch,
                "channel_class": cls.name,
                "param": param,
                "sub_index": sub,
                "raw": value.raw,
                "freshness": value.freshness.name,
                "age": None if value.updated_at is None else round(now - value.updated_at, 3),
            }
        )
    return {
        "version": SNAPSHOT_VERSION,
        "box_id": box_id,
        "model": model,
        "firmware": None if firmware is None else list(firmware),
        "names": {str(ch.value): name for ch, name in names.items()},
        "values": sorted(
            values, key=lambda v: (v["channel_raw"], v["param"], v["sub_index"] or -1)
        ),
    }


def load_snapshot(path: str | os.PathLike[str]) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        data: dict[str, Any] = json.load(handle)
    if data.get("version") != SNAPSHOT_VERSION:
        raise ValueError(f"snapshot version {data.get('version')!r} is not {SNAPSHOT_VERSION}")
    return data


def diff_snapshot(snapshot: dict[str, Any], cache: StateCache) -> list[SnapshotDiff]:
    """Report every parameter whose current raw value differs from the snapshot.

    ``channel_raw``/``channel_class`` are the first two fields of the cache
    key verbatim (see ``build_snapshot``), so recovering the channel is
    exactly ``StateCache.key()``'s inverse -- ``channel_from_key`` in
    ``qumidi/state.py``, which lives next to ``key()`` itself and is shared
    with ``qumidi dump-state``'s text output rather than re-derived here.
    """
    out: list[SnapshotDiff] = []
    for entry in snapshot["values"]:
        cls = ChannelClass[entry["channel_class"]]
        ch = channel_from_key(int(entry["channel_raw"]), cls)
        current = cache.get(ch, entry["param"], entry["sub_index"])
        if current.raw == entry["raw"]:
            continue
        out.append(
            SnapshotDiff(
                channel=ch,
                param=entry["param"],
                sub_index=entry["sub_index"],
                snapshot_raw=entry["raw"],
                current_raw=current.raw,
                snapshot_freshness=entry["freshness"],
                current_freshness=current.freshness.name,
            )
        )
    return out
