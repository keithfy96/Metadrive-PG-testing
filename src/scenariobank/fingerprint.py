"""Hashing primitives. One implementation, imported everywhere it is needed.

The house rule is that `_sha256` is written once and never reimplemented, and it matters more
here than usual: a map's identity is computed at generation, again in `verify`, again in
`selftest`, and again in the runner's refusal check. Four call sites that must never be able to
disagree about what a map is.

Phase 2 builds `map_id` on top of this. Phase 1 needs only `lane_geometry_digest`, to answer a
question the manifest will ask later -- how many *distinct roads* a block sequence actually
produces across its seeds.
"""

from __future__ import annotations

import hashlib

#: Points sampled along each lane centreline. High enough that an arc is distinguishable from
#: the chord across it, low enough that a 78-lane roundabout stays cheap.
SAMPLES_PER_LANE = 24


def sha256_hex(payload: str, *, length: int | None = None) -> str:
    """Return the SHA-256 of `payload`, optionally truncated. The one hash in this package."""
    digest = hashlib.sha256(payload.encode()).hexdigest()
    return digest if length is None else digest[:length]


def lane_geometry_digest(road_map, *, length: int | None = None) -> str:
    """Fingerprint the drivable surface: every lane centreline, sampled and sorted.

    Deliberately *not* `map.get_meta_data()`. That dictionary carries `map_config`, and
    `map_config` carries the seed -- so two byte-identical roads built at different seeds
    fingerprint differently, and a map identity built on it would certify variety that does not
    exist. Sampling the geometry answers the question actually being asked: is this the same
    road?
    """
    import numpy as np

    parts = []
    for start, destinations in sorted(road_map.road_network.graph.items()):
        for end, lanes in sorted(destinations.items()):
            for index, lane in enumerate(lanes):
                stations = np.linspace(0, lane.length, SAMPLES_PER_LANE)
                points = (lane.position(s, 0) for s in stations)
                trace = ";".join(f"{point[0]:.3f},{point[1]:.3f}" for point in points)
                parts.append(f"{start}|{end}|{index}|{trace}")
    return sha256_hex("\n".join(sorted(parts)), length=length)
