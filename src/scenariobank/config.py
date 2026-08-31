"""The canonical base config every scenario in this bank is built from.

Phase 0 owns only the part of it that is not scenario-specific: the settings that fix the
*observation*, and so fix what a policy can possibly perceive. Maps, destinations, and the
option axes arrive in Phases 1-3 and layer on top of this.

Nothing here imports MetaDrive at module scope, so this module stays importable without the
`sim` dependency group.
"""

from __future__ import annotations

import logging
from typing import Any

#: The observation this bank is defined against: 6 ego + 10 navigation + 3 detector scalars.
#:
#: One constant, four eventual call sites -- `doctor`, the runner's pre-flight, the runner's
#: post-episode check, and the Phase 4 expert-leak assert. They must never be able to disagree
#: about what shape this bank produces, so none of them writes the number itself.
OBSERVATION_SHAPE: tuple[int, ...] = (19,)

#: Lidar off, and both geometric detectors pinned at zero lasers.
#:
#: `num_lasers=0` is a real off switch, not a zero-length array: `LidarStateObservation`
#: (`obs/state_obs.py:176-178`) only adds lidar dims when `num_lasers > 0` **and**
#: `distance > 0`. The two detectors are *not* lidar at zero lasers -- they contribute 3
#: scalars computed geometrically (`state_obs.py:145-162`) -- but set either above 0 and they
#: become raycasting lidars and the observation grows. Pinned here so nobody reintroduces
#: lidar through the side door.
SENSOR_CONFIG: dict[str, Any] = {
    "lidar": {"num_lasers": 0, "distance": 0, "num_others": 0},
    "side_detector": {"num_lasers": 0, "distance": 50},
    "lane_line_detector": {"num_lasers": 0, "distance": 20},
}


def base_config(**overrides: Any) -> dict[str, Any]:
    """Return the canonical MetaDrive config, with `overrides` applied at the top level.

    `agent_observation` is set explicitly rather than left to MetaDrive's default choice.
    `base_env.py:674-678` checks it *first* and only falls through to `ImageStateObservation`
    vs `LidarStateObservation` when it is unset -- so pinning it means the observation stays a
    bare `Box(19,)` even once Phase 4 turns `image_observation` on to keep the camera rig
    alive. Belt-and-braces against the lidar block above, and it makes this config
    self-documenting.
    """
    from metadrive.obs.state_obs import StateObservation

    from scenariobank.handedness import install

    # Left-side traffic. This is the one function every env-building path in this package goes
    # through, and the mirror has to be in place before any map is built -- so it is installed
    # here rather than left to each caller to remember. Idempotent; see `handedness.install`.
    install()

    config: dict[str, Any] = {
        "use_render": False,
        "agent_observation": StateObservation,
        "vehicle_config": dict(SENSOR_CONFIG),
        # Neutral values for the axes Phase 3's options own. They are named here, at zero,
        # so that "the option was never applied" and "the option is at its floor" are the
        # same state rather than two different ones.
        "traffic_density": 0.0,
        "random_traffic": False,
        "accident_prob": 0.0,
        # One scenario at seed 0. Phases 1-2 replace this with the real seed range.
        "start_seed": 0,
        "num_scenarios": 1,
        "horizon": 1000,
        "log_level": logging.WARNING,
    }
    config.update(overrides)
    return config
