"""Client-side surface mapping.

THE PROTOCOL IS ENTIRELY IDENTITY-ADDRESSED AND NEVER POSITION-ADDRESSED. No
message carries a physical strip position or a layer index, and there is no
way to query the layer configuration, to read which channels are assigned to
which custom-layer positions, or to learn which layer is currently selected.

This module is therefore pure client-side bookkeeping with no protocol
involvement of any kind, and it makes no attempt to infer a mapping from
traffic.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping

from qumixer_py.protocol.channels import ChannelId, parse_channel_ref


class SurfaceMap:
    """A position-to-identity map for the console's custom layer.

    This map is user-supplied configuration. The library cannot validate it
    against the console, because the protocol offers no way to read the layer
    configuration back.

    Recalling a scene can change the console's custom layer assignment without
    any notification, silently invalidating this map. Applications that care
    should re-load their map after a scene recall; registering the map with
    ``mixer.register_surface_map()`` makes the library mark it invalid
    automatically when it recalls a scene, but a recall made from the
    console's own surface cannot be detected at all.
    """

    def __init__(self, mapping: Mapping[int, ChannelId]) -> None:
        self._by_position = dict(mapping)
        self._by_channel = {ch: pos for pos, ch in mapping.items()}
        self._valid = True

    @classmethod
    def from_dict(cls, mapping: Mapping[int, ChannelId]) -> SurfaceMap:
        return cls(mapping)

    @classmethod
    def from_json(cls, path: str | os.PathLike[str]) -> SurfaceMap:
        """Load ``{"1": "input:6", "2": "dca:1"}`` from a JSON file."""
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return cls({int(pos): parse_channel_ref(ref) for pos, ref in data.items()})

    @property
    def valid(self) -> bool:
        return self._valid

    def invalidate(self) -> None:
        """Mark the map stale, typically because a scene was recalled."""
        self._valid = False

    def _check(self) -> None:
        if not self._valid:
            raise RuntimeError(
                "this SurfaceMap has been invalidated because a scene was recalled, "
                "which can change the console's custom layer without notification. "
                "Re-load the map from your configuration."
            )

    def channel_at(self, position: int) -> ChannelId | None:
        self._check()
        return self._by_position.get(position)

    def position_of(self, channel: ChannelId) -> int | None:
        self._check()
        return self._by_channel.get(channel)
