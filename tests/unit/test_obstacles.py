"""`ObstacleManager`: two counts on MetaDrive's own placement, and the trap made visible.

Everything here needs the simulator and the two procedural banks, because a manager is a
simulator object and the claims are about what lands on a road: that `cones` is a number of
corridors and `barriers` a number of barriers, with no broken-down vehicle behind either; that
an `X` road takes nothing at any level and the row says so; that two envs at one seed lay the
same scene; that the manager exists only when an axis is above `none`; and that traffic keeps
off a coned lane, which is the `accident_lanes` coupling `PGTrafficManager` reads.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest

pytest.importorskip(
    "metadrive", reason="needs_sim: MetaDrive is not installed (uv sync --group sim)"
)

from scenariobank.bank import read_manifest  # noqa: E402
from scenariobank.env import build_env, seed_for  # noqa: E402
from scenariobank.obstacles import ELIGIBLE_BLOCKS, ObstacleManager  # noqa: E402
from scenariobank.options import LEVELS, resolve_options  # noqa: E402
from scenariobank.results import JOB_SCHEMA_VERSION, Job, JobBank  # noqa: E402
from scenariobank.runner import placed_counts, run_bank  # noqa: E402

CURVE = Path("banks/curve")
LEFT = Path("banks/t-junction-left-intersection")


def needs_bank(bank: Path):
    return pytest.mark.skipif(
        not (bank / "manifest.json").exists(), reason=f"needs the bank at {bank}"
    )


#: Every numeric axis at its floor. Spelled out so a bank's pinned block cannot leak in.
NONE = {
    "traffic": "none",
    "cones": "none",
    "barriers": "none",
    "pedestrians": "none",
    "cyclists": "none",
}


@contextmanager
def scene(bank: Path, category: str, index: int = 0, **levels: str):
    """One row of a bank, built, reset and routed, at `NONE` plus `levels`. Closes after."""
    manifest = read_manifest(bank)
    entry = manifest.categories[category]
    row = entry.scenarios[index]
    env, prepare = build_env(bank, entry, resolve_options(manifest, levels={**NONE, **levels}))
    try:
        env.reset(seed=seed_for(row))
        prepare(env, row)
        yield env
    finally:
        env.close()


def positions(env, class_name: str) -> list[tuple[float, float]]:
    return sorted(
        tuple(round(float(v), 3) for v in placed.position[:2])
        for placed in env.engine.get_objects().values()
        if type(placed).__name__ == class_name
    )


@needs_bank(CURVE)
def test_cones_are_corridors_and_barriers_are_barriers_with_no_vehicle_behind_either():
    with scene(CURVE, "curve", cones="medium", barriers="medium") as env:
        manager = env.engine.object_manager
        assert isinstance(manager, ObstacleManager)
        corridors, barriers = LEVELS["cones"]["medium"], LEVELS["barriers"]["medium"]
        lane_width = env.engine.current_map.config["lane_width"]
        per_corridor = manager.corridor_length(lane_width) / manager.CONE_LONGITUDE
        placed = placed_counts(env)
        assert placed["TrafficCone"] == corridors * per_corridor
        assert placed["TrafficBarrier"] == barriers
        assert "TrafficWarning" not in placed, "the breakdown scene is not used"
        assert placed["DefaultVehicle"] == 1, "no broken-down car: traffic=none means no cars"
        kinds = [one.kind for one in manager.scenes]
        assert kinds == ["cones"] * corridors + ["barriers"] * barriers
        assert len(manager.accident_lanes) == corridors + barriers
        assert all(type(block) in ELIGIBLE_BLOCKS for block in env.engine.current_map.blocks[1:])


@needs_bank(LEFT)
def test_an_intersection_road_takes_no_obstacle_at_any_level_and_placed_says_so():
    """`object_manager.py:51-53`: only four block types take one, and `X` is not among them.
    The manager is registered, draws nothing, and the row shows no cone -- the documented
    trap as a measurement rather than a footnote."""
    with scene(LEFT, "intersection_left", cones="high", barriers="high") as env:
        assert isinstance(env.engine.object_manager, ObstacleManager)
        assert env.engine.object_manager.scenes == []
        placed = placed_counts(env)
        assert "TrafficCone" not in placed and "TrafficBarrier" not in placed
        assert placed == {"DefaultVehicle": 1}


@needs_bank(CURVE)
def test_two_envs_at_one_seed_lay_the_same_scene_and_another_seed_lays_a_different_one():
    with scene(CURVE, "curve", cones="high", barriers="high") as env:
        first = (env.engine.object_manager.scenes, positions(env, "TrafficCone"))
    with scene(CURVE, "curve", cones="high", barriers="high") as env:
        second = (env.engine.object_manager.scenes, positions(env, "TrafficCone"))
    with scene(CURVE, "curve", index=1, cones="high", barriers="high") as env:
        other = (env.engine.object_manager.scenes, positions(env, "TrafficCone"))
    assert first == second
    assert first[1] != other[1], "a digest that cannot differ measures nothing"


@needs_bank(CURVE)
def test_the_manager_exists_only_when_an_obstacle_axis_is_above_none():
    with scene(CURVE, "curve") as env:
        assert "object_manager" not in env.engine.managers
        assert placed_counts(env) == {"DefaultVehicle": 1}
    with scene(CURVE, "curve", barriers="low") as env:
        assert "object_manager" in env.engine.managers
        assert placed_counts(env) == {"DefaultVehicle": 1, "TrafficBarrier": 1}


@needs_bank(CURVE)
def test_traffic_keeps_off_a_coned_lane():
    """`traffic_manager.py:253` skips `engine.object_manager.accident_lanes`, which is why the
    manager keeps the stock name and publishes the list."""
    with scene(CURVE, "curve", traffic="high", cones="high") as env:
        coned = {tuple(lane.index) for lane in env.engine.object_manager.accident_lanes}
        traffic = env.engine.traffic_manager.spawned_objects.values()
        assert len(traffic) > 0 and len(coned) > 0
        assert all(tuple(vehicle.config["spawn_lane_index"]) not in coned for vehicle in traffic)


@needs_bank(CURVE)
def test_a_result_row_carries_what_was_placed(tmp_path):
    job = Job(
        schema_version=JOB_SCHEMA_VERSION,
        bank=JobBank(path=str(CURVE)),
        scenarios=["curve_0000"],
        options={"levels": {**NONE, "cones": "low"}},
        policy="scenariobank.policies:ConstantPolicy",
    )
    report = run_bank(job, tmp_path / "out")
    row = report.results[0]
    assert row.placed["TrafficCone"] > 0 and row.placed["DefaultVehicle"] == 1
    assert row.actor_layout_digest is None, "no actor manager was registered"
