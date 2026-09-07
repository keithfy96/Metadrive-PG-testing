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
#:
#: A `MetaDriveEnv` fact, and only that. The 10 navigation scalars are
#: `NodeNetworkNavigation`'s, which needs a PG road network to navigate; a stored scenario has
#: none and gets a different navigation and a different width. See
#: `SCENARIO_OBSERVATION_SHAPE`.
OBSERVATION_SHAPE: tuple[int, ...] = (19,)

#: What the same `StateObservation` and the same `SENSOR_CONFIG` measure to on a *stored*
#: scenario: 6 ego + 22 navigation + 3 detector scalars.
#:
#: The 12-wide difference from `OBSERVATION_SHAPE` is entirely the navigation. `ScenarioEnv`
#: gives the ego a `TrajectoryNavigation`, which follows the recorded ego's own path instead of
#: a road graph, and reports `NUM_WAY_POINT * CHECK_POINT_INFO_DIM + 2` = 22 where
#: `NodeNetworkNavigation` reports 10. Nothing about the sensor rig differs -- lidar is off in
#: both and both detectors contribute their 3 geometric scalars.
#:
#: **Measured, not derived.** `scenariobank replay --bank banks/junction-1` reads it off a real
#: env at reset and again after the last step of the recording, and `test_replay.py` asserts
#: this constant against it. It is written down here because the two bank kinds produce
#: different-width observations and a policy trained against one cannot be handed the other --
#: which is a fact Phase 4 has to refuse on, and it needs a number to refuse against.
SCENARIO_OBSERVATION_SHAPE: tuple[int, ...] = (31,)

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
        # Neutral values for the axes Phase 4's options own. They are named here, at zero,
        # so that "the option was never applied" and "the option is at its floor" are the
        # same state rather than two different ones.
        "traffic_density": 0.0,
        "random_traffic": False,
        "accident_prob": 0.0,
        # Spawn lane is a seeded draw, and it is deliberately left on. It is the only `random_*`
        # key MetaDrive defaults to `True` (`metadrive_env.py:61`), and it is the *only* thing
        # that differs between the five seeds of an `X` category -- `StdInterSection` builds one
        # identical road at all five, so without this the five runs would coincide at option
        # level zero. `agent_manager.py:111-119` draws `randint(lane_num)` once per reset.
        # Named here at its own default so that keeping it is a decision on the record rather
        # than an inherited one. Measured invariant under `traffic_density` and `accident_prob`
        # and across env rebuilds, so it does not threaten the invariance tests; the drawn lane is
        # recorded per scenario rather than left implicit. See `sockets.measure_route`.
        "random_spawn_lane_index": True,
        # One scenario at seed 0. Phases 1-2 replace this with the real seed range.
        "start_seed": 0,
        "num_scenarios": 1,
        "horizon": 1000,
        "log_level": logging.WARNING,
    }
    config.update(overrides)
    return config
