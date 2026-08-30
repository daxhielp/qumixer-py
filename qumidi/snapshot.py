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
from qumidi.state import StateCache

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
        "names": {str(ch.value): name for ch, name in names.items() if name is not None},
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
    """Report every parameter whose current raw value differs from the snapshot."""
    from qumidi.protocol.channels import GLOBAL, channel_from_raw, dsnake_socket_channel

    out: list[SnapshotDiff] = []
    for entry in snapshot["values"]:
        cls = ChannelClass[entry["channel_class"]]
        if cls is ChannelClass.GLOBAL:
            ch = GLOBAL
        elif cls is ChannelClass.DSNAKE:
            ch = dsnake_socket_channel(int(entry["channel_raw"]))
        else:
            ch = channel_from_raw(int(entry["channel_raw"]))
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
