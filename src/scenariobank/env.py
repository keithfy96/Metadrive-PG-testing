"""Build the env a manifest describes, and the per-row step that readies it: the seam.

A bank is a recipe and not a saved scenario, so something has to read the recipe. This module is
that reader, and it is the **one place** that knows there are two kinds of entry. Above it the
step loop (`runner.py`), `replay` and the result record handle "an env, a seed, a prepare step,
a cap" and never learn which kind they are driving. Written out, the seam is:

| | procedural (`CategoryEntry`) | recorded (`RealWorldEntry`) |
|---|---|---|
| env | `MetaDriveEnv(base_config(...))` | `ScenarioEnv(replay_config(...))` |
| `horizon` | `entry.max_steps` | `entry.max_steps` |
| select a row | `reset(seed=row.seed)` | `reset(seed=row.scenario_index)` |
| prepare | `set_route(lane_index, row.destination)` | nothing; the recording has its route |
| options | the six axes, `resolve_options` | the three replay switches, pinned |
| managers | `ObstacleManager`, `VRUManager`, off the counts | the recording's |
| observation | `OBSERVATION_SHAPE` (19) | `SCENARIO_OBSERVATION_SHAPE` (31) |

Both columns bound an **index** with `num_scenarios`, so `num_scenarios_for` sizes both:
`base_env.py:926` asserts `start <= seed < start + num_scenarios` whichever env it is.

**One env carries one `horizon`, and the runner builds one env per row.** `base_config` pins
`horizon: 1000` and until this module nothing mapped an entry onto it, which mattered:
`t_junction` is 320 and `CCS_only` is 1320. `horizon` is the config key (`metadrive_env.py:58`);
`max_step` is a `TerminationState` field and setting it does nothing. The loop enforces
`entry.budget_for(row)` on top, because that is *per row* and one env cannot carry two horizons
-- see `runner.py`, which also says why an env is built and closed around every row rather than
shared across an entry: an episode run after another one in the same env is not the episode run
alone.

**The lidar reports its objects in a pinned order.** MetaDrive's `Lidar.get_surrounding_objects`
returns a `set` of the objects near a vehicle, and a set of objects iterates in the order of
their addresses. The IDM policy every traffic vehicle drives with hands that set to
`FrontBackObjects.get_find_front_back_objs` (`idm_policy.py:83`), which keeps the nearest object
ahead and behind on each lane with a strict comparison -- so when two objects sit at one
longitude, which one wins is which one the set yields first. A cone corridor puts cones at equal
longitudes by construction. Measured on `banks/curve` at `hard`: one row, alone, in a fresh
process, ended at 339 steps or at 348 depending on nothing but the size of the process's
environment block (`PYTHONHASHSEED=8` in the environment was enough to move it, and so was any
other one-digit value; `0`, `100` and unset agreed with each other), and the drift began at step
two of the expert's actions. `pinned_lidar_class()` is the stock lidar with both object sets
returned as lists sorted by `object_order` -- class name, then position, then heading -- and
`build_env` registers it through the `sensors` config on both kinds of env, so every consumer
of the set, the IDM policy and the expert's own observation alike, sees one order everywhere.

**The destination is pinned after the reset**, with `navigation.set_route`, the way `bank._measure`
and `variety.scan` already do -- not through `vehicle_config["destination"]`, which is read at
construction. Destinations vary inside a category (`StdTInterSection` exposes a different arm per
seed), and one env per entry cannot hold a construction-time destination that differs per row.
`set_route` raises on an unreachable node, which is the failure wanted.

**The step rate differs by kind and is known before anything is built.** `base_config` leaves
MetaDrive's `physics_world_step_size` x `decision_repeat` at 0.02 x 5, so one `env.step` is 10 Hz
on a procedural road; a recording is stepped one frame at a time, at the rate its tracks were
sampled at. `step_hz_for` says which, off the manifest.

**The procedural env is a subclass, built on first use.** Four of the six axes are counts for
managers MetaDrive does not register -- `ObstacleManager` for cones and barriers, `VRUManager`
for pedestrians and cyclists -- and `MetaDriveEnv` registers its managers in `setup_engine`,
which only a subclass can extend. `procedural_env_class()` is that subclass: it knows the four
counts as config keys, registers each manager only when its axis is above zero (the way
`metadrive_env.py:296-300` registers the stock object manager only above `accident_prob`'s
floor), and carries the `crash_human_penalty` / `crash_human_cost` pair that MetaDrive
terminates on but never scores (`metadrive_env.py:74-83` has the vehicle and object pairs and
no human one). The class is made inside a function because its base is the simulator's.

Nothing here imports MetaDrive at module scope, so every refusal a caller makes off the manifest
still works on a machine without the simulator; the env classes are imported inside `build_env`,
`procedural_env_class` and `pinned_lidar_class`.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from scenariobank.bank import (
    CategoryEntry,
    RealWorldEntry,
    RealWorldRow,
    ScenarioRow,
    num_scenarios_for,
)
from scenariobank.config import (
    OBSERVATION_SHAPE,
    SCENARIO_OBSERVATION_SHAPE,
    SENSOR_CONFIG,
    base_config,
)
from scenariobank.options import REPLAY_FLAGS, ResolvedOptions

Entry = CategoryEntry | RealWorldEntry
Row = ScenarioRow | RealWorldRow

#: MetaDrive's own defaults, the two keys `base_config` leaves alone (`base_env.py:190-191`).
#: Named so `step_hz_for` can say what a procedural env steps at without importing the simulator.
DEFAULT_PHYSICS_STEP_S = 0.02
DEFAULT_DECISION_REPEAT = 5

#: The four option axes that are counts for our managers, as the config keys the procedural
#: env class registers. `traffic` is not here: it is `traffic_density`, stock MetaDrive's.
COUNT_AXES: tuple[str, ...] = ("cones", "barriers", "pedestrians", "cyclists")

#: What hitting a person costs, mirroring `crash_object_penalty` / `crash_object_cost`
#: (`metadrive_env.py:75`, `:83`). MetaDrive ends the episode on `crash_human` and scores it
#: nothing; these are the missing pair, registered by `procedural_env_class`.
CRASH_HUMAN_PENALTY = 5.0
CRASH_HUMAN_COST = 1.0


def seed_for(row: Row) -> int:
    """What `reset(seed=...)` is handed to select this row. An index on both kinds."""
    return row.scenario_index if isinstance(row, RealWorldRow) else row.seed


def expected_shape(entry: Entry) -> tuple[int, ...]:
    """The observation width this entry's kind produces, from the two `config.py` constants.

    Asserted by kind rather than merely reported, because the two widths are the reason a policy
    trained against one bank kind cannot be handed the other.
    """
    return SCENARIO_OBSERVATION_SHAPE if isinstance(entry, RealWorldEntry) else OBSERVATION_SHAPE


def step_hz_for(entry: Entry) -> float:
    """How many times a second `env.step` advances for this entry. Answerable off the manifest.

    One `env.step` is `decision_repeat` physics steps of `physics_world_step_size` each. A
    recording pins both to step one recorded frame at a time, so this is its `step_hz`; a
    procedural config leaves both at MetaDrive's defaults, which is 10 Hz. `test_env.py` pins
    that `build_config` does not touch either key, so the number here is the number the env runs
    at -- and it is known before the env is built, which is what lets `replay` refuse an
    impossible decision rate without the simulator.
    """
    if isinstance(entry, RealWorldEntry):
        return entry.step_hz
    return 1 / (DEFAULT_PHYSICS_STEP_S * DEFAULT_DECISION_REPEAT)


def replay_config(bank_dir: Path, entry: RealWorldEntry, row: RealWorldRow) -> dict[str, Any]:
    """The `ScenarioEnv` config for one recorded row: that row alone, at its own length.

    Phase 3 Step 6's diagnostic shape, kept because `test_replay.py` pins it and because it is
    what one row costs. `build_config` is the same dict sized for the whole entry.
    """
    return _recorded_config(
        bank_dir, entry, first=row.scenario_index, count=1, horizon=entry.budget_for(row)
    )


def _recorded_config(
    bank_dir: Path, entry: RealWorldEntry, *, first: int, count: int, horizon: int
) -> dict[str, Any]:
    """The `ScenarioEnv` config for `count` recordings of `entry` from index `first`.

    **Deliberately not `config.base_config()`.** That one pins `start_seed`, `num_scenarios: 1`,
    `horizon: 1000` and `random_spawn_lane_index` for a *generated* road, and it calls
    `handedness.install()`, which mirrors MetaDrive's PG lane geometry -- a stored map has no
    generated geometry to mirror, and it already drives on whichever side it was recorded on.
    What the two do share is the observation, so `agent_observation` and `SENSOR_CONFIG` come
    from `config.py` rather than being restated here: a recording and a PG scenario must be
    perceived through the same rig even though they end up different widths.

    `data_directory` is made absolute. It is stored relative to the bank root because a bank is
    copied and mounted, and MetaDrive resolves it against the process's working directory.
    """
    from metadrive.obs.state_obs import StateObservation

    return {
        "use_render": False,
        "agent_observation": StateObservation,
        "vehicle_config": dict(SENSOR_CONFIG),
        "data_directory": str((Path(bank_dir) / entry.dataset_dir).resolve()),
        # The index is the recording's own position rather than 0 -- see
        # `RealWorldRow.scenario_index` on why the two are separate.
        "start_scenario_index": first,
        "num_scenarios": count,
        # One recorded frame per step. MetaDrive's default 0.02 x 5 is 10 Hz, and opening a 100 Hz
        # recording at it replays every actor at a tenth of its speed while nothing raises.
        "physics_world_step_size": 1 / entry.step_hz,
        "decision_repeat": 1,
        # Without it the env replays past the last frame forever -- measured, see `replay.py`.
        "horizon": horizon,
        # The recording's contents, replayed as recorded. One dict, shared with
        # `resolve_options`, so the config and the result record cannot say different things
        # about the same switches.
        **REPLAY_FLAGS,
        "log_level": logging.WARNING,
    }


def build_config(bank_dir: Path, entry: Entry, options: ResolvedOptions) -> dict[str, Any]:
    """The env config for every row of one entry: one branch per kind, the seam's first half.

    On the procedural side the three `_PER_RUN_KEYS` are filled the way `variety.scan` fills
    them, `horizon` becomes the entry's `max_steps`, the traffic axis becomes `traffic_density`,
    the one option knob stock MetaDrive reads, and the four `COUNT_AXES` become the keys
    `procedural_env_class` registers -- which is why this config fits that class and not a
    stock `MetaDriveEnv`, whose `Config` refuses a key it does not know.

    Needs the simulator: both branches name `StateObservation`.
    """
    if isinstance(entry, RealWorldEntry):
        indices = [row.scenario_index for row in entry.scenarios]
        return _recorded_config(
            bank_dir,
            entry,
            first=min(indices),
            count=num_scenarios_for(indices),
            horizon=entry.max_steps,
        )
    if options.kind != "pg":
        raise ValueError(
            f"options resolved for a {options.kind!r} bank cannot build a procedural env"
        )
    seeds = [row.seed for row in entry.scenarios]
    return base_config(
        map=entry.block_seq,
        start_seed=min(seeds),
        num_scenarios=num_scenarios_for(seeds),
        horizon=entry.max_steps,
        traffic_density=float(options.values["traffic"]),
        **{axis: int(options.values[axis]) for axis in COUNT_AXES},
    )


@functools.cache
def procedural_env_class() -> type:
    """`MetaDriveEnv` with the count axes, the two managers and the crash-human pair. Cached.

    `default_config` is where MetaDrive's `Config` learns a key; anything else passed to the
    constructor is refused by name (`base_env.py:293`). `setup_engine` is where managers are
    registered, after the engine exists and before the first reset; a manager whose axis is at
    zero is not registered at all, so a run at `none` has exactly the managers a stock env has
    and `placed` shows nothing it did not put there. The reward and cost overrides slot
    `crash_human` in behind `crash_object`, at the same numbers.
    """
    from metadrive.envs.metadrive_env import MetaDriveEnv

    from scenariobank.actors import VRUManager
    from scenariobank.obstacles import ObstacleManager

    class ScenarioBankEnv(MetaDriveEnv):
        @classmethod
        def default_config(cls):
            config = super().default_config()
            config.update(
                {
                    **{axis: 0 for axis in COUNT_AXES},
                    "crash_human_penalty": CRASH_HUMAN_PENALTY,
                    "crash_human_cost": CRASH_HUMAN_COST,
                }
            )
            return config

        def setup_engine(self) -> None:
            super().setup_engine()
            if self.config["cones"] or self.config["barriers"]:
                # The stock name: `PGTrafficManager` reads `engine.object_manager.accident_lanes`.
                self.engine.register_manager("object_manager", ObstacleManager())
            if self.config["pedestrians"] or self.config["cyclists"]:
                self.engine.register_manager("vru_manager", VRUManager())

        def reward_function(self, vehicle_id: str):
            reward, step_info = super().reward_function(vehicle_id)
            vehicle = self.agents[vehicle_id]
            outranked = (
                self._is_arrive_destination(vehicle)
                or self._is_out_of_road(vehicle)
                or vehicle.crash_vehicle
                or vehicle.crash_object
            )
            if vehicle.crash_human and not outranked:
                reward = -self.config["crash_human_penalty"]
            return reward, step_info

        def cost_function(self, vehicle_id: str):
            cost, step_info = super().cost_function(vehicle_id)
            if not cost and self.agents[vehicle_id].crash_human:
                cost = step_info["cost"] = self.config["crash_human_cost"]
            return cost, step_info

    return ScenarioBankEnv


def object_order(obj: Any) -> tuple[str, float, float, float]:
    """The order the pinned lidar reports objects in: class, then position, then heading.

    Two objects of one class at one position and heading would tie, and physically cannot: a
    cone and a barrier are distinct classes, and nothing else is spawned into another object.
    """
    return (
        type(obj).__name__,
        float(obj.position[0]),
        float(obj.position[1]),
        float(obj.heading_theta),
    )


@functools.cache
def pinned_lidar_class() -> type:
    """MetaDrive's `Lidar`, with both of its object sets returned as lists in `object_order`.

    Cached, so the class is one object and the `sensors` config compares equal across builds.
    """
    from metadrive.component.sensors.lidar import Lidar

    class PinnedLidar(Lidar):
        """The stock lidar, reporting objects in one order whatever the heap looks like."""

        def get_surrounding_objects(self, vehicle: Any, radius: float = 50) -> list[Any]:
            return sorted(super().get_surrounding_objects(vehicle, radius), key=object_order)

        @staticmethod
        def get_surrounding_vehicles(detected_objects: Any) -> list[Any]:
            return sorted(Lidar.get_surrounding_vehicles(detected_objects), key=object_order)

    return PinnedLidar


def build_env(
    bank_dir: Path, entry: Entry, options: ResolvedOptions
) -> tuple[Any, Callable[[Any, Row], str | None]]:
    """Build the env for one entry and return it with its `prepare(env, row)` step.

    `prepare` runs after `reset` and before the first step. On a procedural row it pins the
    route to the row's destination and returns the node the navigation now ends at, read back
    off the env so the report says what was set rather than what was asked; on a recorded row it
    does nothing and returns `None`, because the recording already has its route.

    The caller owns `env.close()`.
    """
    config = build_config(bank_dir, entry, options)
    # The stock `sensors` entry is `(Lidar,)`; the pinned one keeps the other two sensors.
    config["sensors"] = {"lidar": (pinned_lidar_class(),)}
    if isinstance(entry, RealWorldEntry):
        from metadrive.envs.scenario_env import ScenarioEnv

        return ScenarioEnv(config), _prepare_recorded

    return procedural_env_class()(config), _prepare_procedural


def _prepare_recorded(env: Any, row: Row) -> None:
    """Nothing to do: a recording carries its own route."""
    del env, row


def _prepare_procedural(env: Any, row: Row) -> str:
    """Pin the route after the reset, and read back where it ends."""
    assert isinstance(row, ScenarioRow)
    navigation = env.agent.navigation
    navigation.set_route(env.agent.lane_index, row.destination)
    return str(navigation.final_road.end_node)


__all__ = [
    "COUNT_AXES",
    "CRASH_HUMAN_COST",
    "CRASH_HUMAN_PENALTY",
    "DEFAULT_DECISION_REPEAT",
    "DEFAULT_PHYSICS_STEP_S",
    "Entry",
    "Row",
    "build_config",
    "build_env",
    "expected_shape",
    "object_order",
    "pinned_lidar_class",
    "procedural_env_class",
    "replay_config",
    "seed_for",
    "step_hz_for",
]
