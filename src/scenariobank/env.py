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
| managers | `ObstacleManager`, `VRUManager`, `TrafficLightManager`, per level | the recording's |
| observation | `OBSERVATION_SHAPE` (19) | `SCENARIO_OBSERVATION_SHAPE` (31) |
| cameras | a `CameraRig`'s, mounted by `prepare` | the same |

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
sampled at. `step_hz_for` says which, off the manifest. **A road can be asked to step faster**
(Phase 4 Step 7): `step_hz` on `build_config` sets `physics_world_step_size = 1 / step_hz` with
`decision_repeat = 1`, which is what lets the AV3 rig be read at its own 0.05 s and the bridge
be ticked at its 20 Hz (`--step-hz 100 --decision-hz 20`). It changes what a step is, so every
step budget is scaled with it (`budget_at`) and every number measured at 10 Hz is a different
number at 100; a recording refuses any rate but its own, because replay advances one recorded
frame per step.

**The procedural env is a subclass, built on first use.** Five of the six axes drive managers
MetaDrive does not register -- `ObstacleManager` for cones and barriers, `VRUManager` for
pedestrians and cyclists, `TrafficLightManager` for the lights -- and `MetaDriveEnv` registers
its managers in `setup_engine`, which only a subclass can extend. `procedural_env_class()` is
that subclass: it knows the four counts and the two light numbers as config keys, registers
each manager only when its axis is above zero (the way
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
import math
import os
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
from scenariobank.results import RUN_RED_LIGHT

Entry = CategoryEntry | RealWorldEntry
Row = ScenarioRow | RealWorldRow

#: MetaDrive's own defaults, the two keys `base_config` leaves alone (`base_env.py:190-191`).
#: Named so `step_hz_for` can say what a procedural env steps at without importing the simulator.
DEFAULT_PHYSICS_STEP_S = 0.02
DEFAULT_DECISION_REPEAT = 5

#: The four option axes that are counts for our managers, as the config keys the procedural
#: env class registers. `traffic` is not here: it is `traffic_density`, stock MetaDrive's.
COUNT_AXES: tuple[str, ...] = ("cones", "barriers", "pedestrians", "cyclists")

#: The lights axis's two numbers as config keys: the junction cycle and the ego road's green,
#: in seconds. Both zero at `none`, and `procedural_env_class` registers the light manager only
#: above zero, the way it does the other managers.
LIGHT_KEYS: tuple[str, ...] = ("lights_cycle_s", "lights_green_s")

#: What hitting a person costs, mirroring `crash_object_penalty` / `crash_object_cost`
#: (`metadrive_env.py:75`, `:83`). MetaDrive ends the episode on `crash_human` and scores it
#: nothing; these are the missing pair, registered by `procedural_env_class`.
CRASH_HUMAN_PENALTY = 5.0
CRASH_HUMAN_COST = 1.0

#: Running a red: the same numbers as a crash, for the same reason `crash_human` mirrors
#: `crash_object` -- a violation the env ends the episode on is scored like the others.
RUN_RED_LIGHT_PENALTY = 5.0
RUN_RED_LIGHT_COST = 1.0


def seed_for(row: Row) -> int:
    """What `reset(seed=...)` is handed to select this row. An index on both kinds."""
    return row.scenario_index if isinstance(row, RealWorldRow) else row.seed


def expected_shape(entry: Entry) -> tuple[int, ...]:
    """The observation width this entry's kind produces, from the two `config.py` constants.

    Asserted by kind rather than merely reported, because the two widths are the reason a policy
    trained against one bank kind cannot be handed the other.
    """
    return SCENARIO_OBSERVATION_SHAPE if isinstance(entry, RealWorldEntry) else OBSERVATION_SHAPE


#: What a procedural road steps at unless a run asks otherwise: MetaDrive's 0.02 x 5.
DEFAULT_STEP_HZ = 1 / (DEFAULT_PHYSICS_STEP_S * DEFAULT_DECISION_REPEAT)


def step_hz_for(entry: Entry, step_hz: float | None = None) -> float:
    """How many times a second `env.step` advances for this entry. Answerable off the manifest.

    One `env.step` is `decision_repeat` physics steps of `physics_world_step_size` each. A
    recording pins both to step one recorded frame at a time, so this is its `step_hz`, and a
    `step_hz` asked for that is not the recording's own is refused here. A procedural config
    leaves both at MetaDrive's defaults, which is 10 Hz, unless `step_hz` asks for another rate,
    in which case `build_config` sets the two keys to it. `test_env.py` pins both halves, so the
    number here is the number the env runs at -- and it is known before the env is built, which
    is what lets `replay` refuse an impossible decision rate without the simulator.
    """
    if step_hz is not None and step_hz <= 0:
        raise ValueError(f"--step-hz must be positive, not {step_hz:g}")
    if isinstance(entry, RealWorldEntry):
        if step_hz is not None and abs(step_hz - entry.step_hz) > 1e-9:
            raise ValueError(
                f"--step-hz {step_hz:g} on a recording sampled at {entry.step_hz:g} Hz: replay "
                "advances one recorded frame per env.step, so a recording steps at its own rate "
                "and no other"
            )
        return entry.step_hz
    return DEFAULT_STEP_HZ if step_hz is None else float(step_hz)


def budget_at(budget: int, entry: Entry, step_hz: float | None = None) -> int:
    """A step budget measured at the kind's own rate, counted in this run's steps.

    A category's `max_steps` was sized at 10 Hz (`categories.step_budget`, 0.1 s a step), so a
    road stepped at 100 Hz needs ten times as many steps to cover the same seconds. Rounded up:
    a budget that cuts a route off is worse than one a step long. A recording's budget is its
    own frame count and never scales.
    """
    if isinstance(entry, RealWorldEntry) or step_hz is None:
        return int(budget)
    return math.ceil(int(budget) * step_hz_for(entry, step_hz) / DEFAULT_STEP_HZ)


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


def build_config(
    bank_dir: Path, entry: Entry, options: ResolvedOptions, step_hz: float | None = None
) -> dict[str, Any]:
    """The env config for every row of one entry: one branch per kind, the seam's first half.

    On the procedural side the three `_PER_RUN_KEYS` are filled the way `variety.scan` fills
    them, `horizon` becomes the entry's `max_steps`, the traffic axis becomes `traffic_density`,
    the one option knob stock MetaDrive reads, the four `COUNT_AXES` become the keys
    `procedural_env_class` registers, and the lights schedule becomes the two `LIGHT_KEYS`
    (zero at `none`) -- which is why this config fits that class and not a stock
    `MetaDriveEnv`, whose `Config` refuses a key it does not know. `step_hz`, when it is
    not the road's own 10 Hz, sets `physics_world_step_size` and `decision_repeat` and scales
    `horizon` with them; on a recording it may only restate the recording's rate.

    Needs the simulator: both branches name `StateObservation`.
    """
    rate = step_hz_for(entry, step_hz)
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
    lights = options.values.get("lights") or {}
    config = base_config(
        map=entry.block_seq,
        start_seed=min(seeds),
        num_scenarios=num_scenarios_for(seeds),
        horizon=budget_at(entry.max_steps, entry, step_hz),
        traffic_density=float(options.values["traffic"]),
        **{axis: int(options.values[axis]) for axis in COUNT_AXES},
        lights_cycle_s=float(lights.get("cycle", 0.0)),
        lights_green_s=float(lights.get("green", 0.0)),
    )
    if step_hz is not None and abs(rate - DEFAULT_STEP_HZ) > 1e-9:
        # One physics step per env.step, at the asked rate: the same shape a recording is
        # stepped in (`_recorded_config`), so a road at 100 Hz and a 100 Hz recording are one
        # step size apart from nothing. Left untouched at the default so the pinned-default
        # test and every number measured at 10 Hz still hold.
        config["physics_world_step_size"] = 1 / rate
        config["decision_repeat"] = 1
    return config


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
    from scenariobank.lights import TrafficLightManager
    from scenariobank.obstacles import ObstacleManager

    class ScenarioBankEnv(MetaDriveEnv):
        @classmethod
        def default_config(cls):
            config = super().default_config()
            config.update(
                {
                    **{axis: 0 for axis in COUNT_AXES},
                    **{key: 0.0 for key in LIGHT_KEYS},
                    "crash_human_penalty": CRASH_HUMAN_PENALTY,
                    "crash_human_cost": CRASH_HUMAN_COST,
                    "run_red_light_penalty": RUN_RED_LIGHT_PENALTY,
                    "run_red_light_cost": RUN_RED_LIGHT_COST,
                    "run_red_light_done": True,
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
            if self.config["lights_cycle_s"]:
                self.engine.register_manager("light_manager", TrafficLightManager())

        def done_function(self, vehicle_id: str):
            """The stock endings plus `run_red_light`, off the flag the contact test sets.

            `vehicle.red_light` is set when the ego's chassis meets a light's wall while it
            is red (`base_vehicle.py:_state_check`), which on green is not there to meet. So
            the flag is the violation, and the episode ends on it like it does on a crash.
            """
            done, done_info = super().done_function(vehicle_id)
            ran_red = bool(self.agents[vehicle_id].red_light)
            done_info[RUN_RED_LIGHT] = ran_red
            if ran_red and self.config["run_red_light_done"]:
                done = True
            return done, done_info

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
            elif vehicle.red_light and not outranked:
                reward = -self.config["run_red_light_penalty"]
            return reward, step_info

        def cost_function(self, vehicle_id: str):
            cost, step_info = super().cost_function(vehicle_id)
            vehicle = self.agents[vehicle_id]
            if not cost and vehicle.crash_human:
                cost = step_info["cost"] = self.config["crash_human_cost"]
            elif not cost and vehicle.red_light:
                cost = step_info["cost"] = self.config["run_red_light_cost"]
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
    bank_dir: Path,
    entry: Entry,
    options: ResolvedOptions,
    rig: Any | None = None,
    step_hz: float | None = None,
) -> tuple[Any, Callable[[Any, Row], str | None]]:
    """Build the env for one entry and return it with its `prepare(env, row)` step.

    `prepare` runs after `reset` and before the first step. On a procedural row it pins the
    route to the row's destination and returns the node the navigation now ends at, read back
    off the env so the report says what was set rather than what was asked; on a recorded row it
    does nothing and returns `None`, because the recording already has its route.

    `rig`, a `CameraRig`, puts its cameras on the env -- the same way on both kinds -- and
    `prepare` then also mounts them on the ego after every reset. Four config keys carry it
    (Phase 4 Step 6): the cameras join `sensors`; `image_observation` goes on, **not for the
    observation** -- `agent_observation` is pinned and wins (`base_env.py:674-678`), so the
    policy still sees 19 -- but because `base_env.py:343-346` deletes every camera from a
    headless env's sensors when it is off, and says nothing; `image_source` names a rig
    camera so MetaDrive's default `rgb_camera` is never registered as a buffer nothing reads;
    and `preload_models` goes off, because a render-mode env otherwise warms objects into the
    pool that a headless env never sees, and the drive moves (Step 6b).

    `step_hz` is `build_config`'s: the rate a road is stepped at, MetaDrive's 10 Hz when `None`.

    The caller owns `env.close()`.
    """
    config = build_config(bank_dir, entry, options, step_hz)
    # The stock `sensors` entry is `(Lidar,)`; the pinned one keeps the other two sensors.
    config["sensors"] = {"lidar": (pinned_lidar_class(),)}
    if rig is not None:
        config["sensors"].update(rig.sensors())
        config["image_observation"] = True
        config["vehicle_config"]["image_source"] = rig.image_source()
        # `preload_models` (default True) runs only in a render mode (`base_engine.py:749`): it
        # spawns a pedestrian, a traffic light, a barrier and a cone at [0, 0], steps them, and
        # hands them back to the object pool. The row then gets those warmed objects where a
        # headless env gets fresh ones, and the expert's throttle parted from the headless
        # drive in the seventh decimal at decision 60 on `curve_0000` at `hard` -- reproducibly.
        # Measured 2026-09-10 (Step 6b): with it off, a rig env drives the headless row exactly.
        config["preload_models"] = False
    if isinstance(entry, RealWorldEntry):
        from metadrive.envs.scenario_env import ScenarioEnv

        return ScenarioEnv(config), _with_rig(_prepare_recorded, rig)
    return procedural_env_class()(config), _with_rig(_prepare_procedural, rig)


#: What MetaDrive writes into the process environment when it opens an offscreen window, and
#: what CPython says about it: `asset_loader.py:116` sets `PYTHONUTF8=on` for "load model file
#: in utf-8" (only from `engine_core.py:250-252`, so only with a rig here, and only at the first
#: `reset`, where the engine is built), and the value is not one CPython accepts -- every child
#: process started afterwards dies at startup with `Fatal Python error: preconfig_init_utf8_mode:
#: invalid PYTHONUTF8 environment variable value`. The variable does nothing for the process
#: that set it; it is read at interpreter start. Found by the suite: the subprocess a handedness
#: test spawns failed only after a rig env had been reset in the same process.
_UTF8_KEY = "PYTHONUTF8"
_UTF8_BAD_VALUE = "on"


def _restore_utf8_variable(before: str | None) -> None:
    """Put `PYTHONUTF8` back to what it was before the env was built, if MetaDrive broke it."""
    if os.environ.get(_UTF8_KEY) != _UTF8_BAD_VALUE:
        return
    if before is None:
        del os.environ[_UTF8_KEY]
    else:
        os.environ[_UTF8_KEY] = before


def _with_rig(
    prepare: Callable[[Any, Row], str | None], rig: Any | None
) -> Callable[[Any, Row], str | None]:
    """`prepare`, then the rig mounted on the ego. `prepare` itself when there is no rig.

    Runs after every reset, which is where a rig env's engine comes to exist, so it is also
    where MetaDrive's `PYTHONUTF8` is put back (`_restore_utf8_variable`).
    """
    if rig is None:
        return prepare
    utf8_before = os.environ.get(_UTF8_KEY)

    def prepare_and_mount(env: Any, row: Row) -> str | None:
        destination = prepare(env, row)
        _restore_utf8_variable(utf8_before)
        rig.mount(env)
        return destination

    return prepare_and_mount


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
    "DEFAULT_STEP_HZ",
    "Entry",
    "Row",
    "budget_at",
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
