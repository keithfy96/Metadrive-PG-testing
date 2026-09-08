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
| observation | `OBSERVATION_SHAPE` (19) | `SCENARIO_OBSERVATION_SHAPE` (31) |

Both columns bound an **index** with `num_scenarios`, so `num_scenarios_for` sizes both:
`base_env.py:926` asserts `start <= seed < start + num_scenarios` whichever env it is.

**One env per entry, because one env carries one `horizon`.** `base_config` pins `horizon: 1000`
and until this module nothing mapped an entry onto it, which mattered: `t_junction` is 320 and
`CCS_only` is 1320. `horizon` is the config key (`metadrive_env.py:58`); `max_step` is a
`TerminationState` field and setting it does nothing. The loop enforces `entry.budget_for(row)`
on top, because that is *per row* and one env cannot carry two horizons -- see `runner.py`.

**The destination is pinned after the reset**, with `navigation.set_route`, the way `bank._measure`
and `variety.scan` already do -- not through `vehicle_config["destination"]`, which is read at
construction. Destinations vary inside a category (`StdTInterSection` exposes a different arm per
seed), and one env per entry cannot hold a construction-time destination that differs per row.
`set_route` raises on an unreachable node, which is the failure wanted.

**The step rate differs by kind and is known before anything is built.** `base_config` leaves
MetaDrive's `physics_world_step_size` x `decision_repeat` at 0.02 x 5, so one `env.step` is 10 Hz
on a procedural road; a recording is stepped one frame at a time, at the rate its tracks were
sampled at. `step_hz_for` says which, off the manifest.

Nothing here imports MetaDrive at module scope, so every refusal a caller makes off the manifest
still works on a machine without the simulator; the env classes are imported inside `build_env`.
"""

from __future__ import annotations

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
    them, `horizon` becomes the entry's `max_steps`, and the traffic axis becomes
    `traffic_density`, the one option knob stock MetaDrive reads. The other four numeric axes are
    counts for the managers Step 4b builds, and nothing consumes them here yet.

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
    )


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
    if isinstance(entry, RealWorldEntry):
        from metadrive.envs.scenario_env import ScenarioEnv

        return ScenarioEnv(config), _prepare_recorded

    from metadrive.envs.metadrive_env import MetaDriveEnv

    return MetaDriveEnv(config), _prepare_procedural


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
    "DEFAULT_DECISION_REPEAT",
    "DEFAULT_PHYSICS_STEP_S",
    "Entry",
    "Row",
    "build_config",
    "build_env",
    "expected_shape",
    "replay_config",
    "seed_for",
    "step_hz_for",
]
