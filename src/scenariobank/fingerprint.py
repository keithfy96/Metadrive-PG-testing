"""Hashing primitives. One implementation, imported everywhere it is needed.

The house rule is that `_sha256` is written once and never reimplemented, and it matters more
here than usual: a map's identity is computed at generation, again in `verify`, again in
`selftest`, and again in the runner's refusal check. Four call sites that must never be able to
disagree about what a map is.

Phase 2's invariance tests build on this. Phase 1 needs only `lane_geometry_digest`, to answer a
question the manifest will ask later -- how many *distinct roads* a block sequence actually
produces across its seeds.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # numpy stays a run-time-lazy import, as everywhere else here
    import numpy as np

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


def road_shape(road_map) -> np.ndarray:
    """Describe a road's size: total drivable lane length, then bounding-box width and height.

    The companion to `lane_geometry_digest`, and the reason it exists: a digest answers *is this
    the same road* and nothing more. It cannot answer *is this a different scenario*, and for a
    while this package reported hash-equality classes as "distinct roads" -- which called `curve`
    seeds 0 and 4 distinct when their first blocks differ by 2% and their routes by 7%, and
    called `roundabout` seeds 0 and 4 distinct when they differ by nothing measurable at all.

    Deliberately category-independent: no route, no destination, no spawn. It describes the road,
    which is what a seed draws.
    """
    import numpy as np

    total = sum(
        lane.length
        for destinations in road_map.road_network.graph.values()
        for lanes in destinations.values()
        for lane in lanes
    )
    x_min, x_max, y_min, y_max = road_map.road_network.get_bounding_box()
    return np.array([float(total), float(x_max - x_min), float(y_max - y_min)])


def shape_gap(left, right) -> float:
    """How far apart two `road_shape` readings are: the worst relative difference among them.

    **A coarse measure, not a metric.** It catches near-twins, which is all it is asked to do:
    two roads of the same total length and the same extent are the same drive whatever their
    lane coordinates say. It will not notice two roads of identical size and different layout,
    and `lane_geometry_digest` is the right tool for that question.

    Returns 0.0 for identical readings. Equal digests imply a gap of 0.0; the converse does not
    hold, which is the entire point.
    """
    import numpy as np

    left, right = np.asarray(left, dtype=float), np.asarray(right, dtype=float)
    return float(np.max(np.abs(left - right) / np.maximum(np.maximum(left, right), 1e-9)))
