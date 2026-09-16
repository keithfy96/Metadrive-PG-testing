"""`TrafficLightManager`: lights at every approach lane of every junction, on one clock.

Needs the simulator and the junction bank. The schedule is tested as arithmetic first; then a
crossroads and a T get their lights where the stop lines are, two envs at one seed cycle
identically and another seed does not, stepping draws nothing further, a car driven through a
red is scored `run_red_light` and ended, one driven through a green is not, and a second reset
in the same env leaves no light of the first behind.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from scenariobank.results import RUN_RED_LIGHT, TERMINATIONS, failure_reason

pytest.importorskip(
    "metadrive", reason="needs_sim: MetaDrive is not installed (uv sync --group sim)"
)

from scenariobank.bank import read_manifest  # noqa: E402
from scenariobank.env import (  # noqa: E402
    RUN_RED_LIGHT_COST,
    RUN_RED_LIGHT_PENALTY,
    build_config,
    build_env,
    procedural_env_class,
    seed_for,
)
from scenariobank.lights import (  # noqa: E402
    GREEN,
    RED,
    STOP_LINE_SETBACK,
    YELLOW,
    YELLOW_S,
    LightsError,
    Phase,
    plan,
    same_road,
)
from scenariobank.options import LEVELS, resolve_options  # noqa: E402
from scenariobank.runner import placed_counts  # noqa: E402

LEFT = Path("banks/t-junction-left-intersection")
needs_left = pytest.mark.skipif(
    not (LEFT / "manifest.json").exists(), reason=f"needs the bank at {LEFT}"
)

#: One env step is five physics steps of 0.02 s.
STEP_S = 0.1


# --- the schedule, as arithmetic -----------------------------------------------------------


@pytest.mark.parametrize("level", ["low", "medium", "high"])
def test_every_level_plans_two_groups_that_are_never_green_together(level):
    schedule = LEVELS["lights"][level]
    major, minor = plan(schedule["cycle"], schedule["green"])
    assert major.green_s == schedule["green"] and major.offset_s == 0.0
    assert minor.offset_s == major.green_s + major.yellow_s
    assert major.yellow_s == minor.yellow_s == YELLOW_S
    # Red is the remainder on both, so each group's three colours fill the cycle exactly.
    for group in (major, minor):
        assert group.green_s + group.yellow_s + group.red_s == pytest.approx(schedule["cycle"])
    for tenth in range(0, schedule["cycle"] * 10):
        seconds = tenth / 10
        assert not (major.colour(seconds) == GREEN and minor.colour(seconds) == GREEN), seconds
    # And each group shows green, then yellow, then red, once per cycle.
    seen = [major.colour(t / 10) for t in range(0, schedule["cycle"] * 10)]
    changes = [c for i, c in enumerate(seen) if i == 0 or c != seen[i - 1]]
    assert changes == [GREEN, YELLOW, RED]


def test_the_harder_the_level_the_less_green_the_ego_gets():
    share = [
        LEVELS["lights"][level]["green"] / LEVELS["lights"][level]["cycle"]
        for level in ("low", "medium", "high")
    ]
    assert share == sorted(share, reverse=True)


def test_a_schedule_that_would_put_both_roads_on_green_is_refused():
    with pytest.raises(LightsError, match="green together"):
        plan(30, 25)
    with pytest.raises(LightsError, match="positive"):
        plan(0, 0)
    # The offset slides the whole plan without changing its shape.
    shifted = Phase(cycle_s=30, green_s=10, yellow_s=3, offset_s=7)
    assert shifted.colour(7) == GREEN and shifted.colour(6.9) == RED
    assert shifted.colour(17) == YELLOW and shifted.colour(20) == RED
    assert shifted.colour(37) == GREEN


def test_same_road_means_parallel_either_way_round():
    assert same_road(0.0, math.pi) and same_road(0.0, 0.1) and same_road(1.0, 1.0 - math.pi)
    assert not same_road(0.0, math.pi / 2) and not same_road(0.3, -1.3)


def test_run_red_light_is_a_failure_reason_below_the_crashes_and_above_leaving_the_road():
    assert RUN_RED_LIGHT in TERMINATIONS
    assert TERMINATIONS.index("crash") < TERMINATIONS.index(RUN_RED_LIGHT)
    assert TERMINATIONS.index(RUN_RED_LIGHT) < TERMINATIONS.index("out_of_road")
    assert failure_reason({RUN_RED_LIGHT: True, "out_of_road": True}) == RUN_RED_LIGHT
    assert failure_reason({RUN_RED_LIGHT: True, "crash_vehicle": True}) == "crash_vehicle"


# --- lights on a road -----------------------------------------------------------------------


def _scene(category: str, index: int = 0, **override):
    """One env at one row with lights=high, every other axis at none; `override` edits the
    config the way a test of the manager needs to (a held red, the ending switched off)."""
    manifest = read_manifest(LEFT)
    entry = manifest.categories[category]
    row = entry.scenarios[index]
    if override:
        config = build_config(LEFT, entry, resolve_options(manifest, levels={"lights": "high"}))
        config.update(override)
        env = procedural_env_class()(config)

        def prepare(env, row):
            env.agent.navigation.set_route(env.agent.lane_index, row.destination)

    else:
        env, prepare = build_env(LEFT, entry, resolve_options(manifest, levels={"lights": "high"}))
    env.reset(seed=seed_for(row))
    prepare(env, row)
    return env, row


@needs_left
@pytest.mark.parametrize(("category", "arms"), [("intersection_left", 4), ("t_junction", 3)])
def test_a_junction_gets_one_light_per_approach_lane_at_the_stop_line(category, arms):
    env, _ = _scene(category)
    try:
        manager = env.engine.light_manager
        placed = manager.placed()
        lanes_per_arm = 3
        assert len(placed) == arms * lanes_per_arm
        assert placed_counts(env)["PGTrafficLight"] == arms * lanes_per_arm
        # The ego's arm and the one facing it are the major road; the rest the cross road.
        by_group = {group: [p for p in placed if p.group == group] for group in ("major", "minor")}
        assert len(by_group["minor"]) == (arms - 2) * lanes_per_arm
        assert len(by_group["major"]) == 2 * lanes_per_arm
        # At the stop line: each light sits `STOP_LINE_SETBACK` before its lane ends.
        network = env.engine.current_map.road_network
        for light in manager.spawned_objects.values():
            assert light.longitude == pytest.approx(light.lane.length - STOP_LINE_SETBACK)
            end = light.lane.position(light.lane.length, 0)
            gap = math.dist(light.position[:2], end[:2])
            assert gap == pytest.approx(STOP_LINE_SETBACK, abs=0.05)
        assert network is not None
        # The ego's route runs through a lit approach: the node a major light's lane ends at is
        # one of the route's checkpoints, so the car meets its own light before the junction.
        checkpoints = set(env.agent.navigation.checkpoints)
        assert any(p.lane_index[1] in checkpoints for p in by_group["major"])
    finally:
        env.close()


@needs_left
def test_two_envs_at_one_seed_cycle_identically_and_another_seed_does_not():
    def observe(index):
        env, _ = _scene("intersection_left", index)
        try:
            manager = env.engine.light_manager
            colours = [manager.colour_at(step, "major") for step in range(0, 300, 5)]
            layout = [(p.lane_index, p.group, p.position) for p in manager.placed()]
            return manager.episode_offset_s, colours, layout
        finally:
            env.close()

    first, second, other = observe(0), observe(0), observe(1)
    assert first == second, "the same seed lit the junction differently twice"
    assert first[0] != other[0], "two seeds drew the same offset"


@needs_left
def test_stepping_consumes_none_of_the_managers_randomness():
    env, _ = _scene("intersection_left")
    try:
        manager = env.engine.light_manager
        before = manager.np_random.get_state()[2]
        for _ in range(50):
            env.step([0.0, 0.0])
        assert manager.np_random.get_state()[2] == before
    finally:
        env.close()


@needs_left
def test_the_colour_changes_on_the_clock_and_traffic_shows_it():
    env, _ = _scene("intersection_left")
    try:
        manager = env.engine.light_manager
        seen = []
        for step in range(1, 301):
            env.step([0.0, 0.0])
            shown = {light.shown for light in manager.spawned_objects.values()
                     if manager._driven[light.id] is manager.phases[0]}
            assert len(shown) == 1, "the major group's lights disagree"
            (colour,) = shown
            assert colour == manager.colour_at(step, "major")
            if not seen or seen[-1] != colour:
                seen.append(colour)
        assert set(seen) == {GREEN, YELLOW, RED}, "a 30 s cycle showed every colour in 30 s"
    finally:
        env.close()


@needs_left
def test_driving_through_a_red_is_scored_run_red_light_and_ends_the_episode():
    # The ego's road held red for all but one second in three hundred, full throttle.
    env, _ = _scene("intersection_left", lights_cycle_s=300.0, lights_green_s=1.0)
    try:
        manager = env.engine.light_manager
        assert manager.colour_at(0, "major") == RED
        for _ in range(200):
            _, reward, terminated, _, info = env.step([0.0, 1.0])
            if terminated:
                break
        assert info[RUN_RED_LIGHT] is True and terminated
        assert reward == -RUN_RED_LIGHT_PENALTY and info["cost"] == RUN_RED_LIGHT_COST
        assert failure_reason(info) == RUN_RED_LIGHT
        assert not info["crash"], "a red wall is a sensor line, not a collision"
        # Before the stop line: nowhere near the junction box yet.
        assert float(env.agent.position[0]) < 49.0
    finally:
        env.close()


@needs_left
def test_the_red_wall_is_not_solid_and_the_flag_clears_once_the_car_is_through():
    env, _ = _scene(
        "intersection_left", lights_cycle_s=300.0, lights_green_s=1.0, run_red_light_done=False
    )
    try:
        flagged = []
        for step in range(1, 90):
            _, _, terminated, _, info = env.step([0.0, 1.0])
            if info[RUN_RED_LIGHT]:
                flagged.append(step)
            if terminated:
                break
        assert flagged and len(flagged) <= 4, flagged
        assert float(env.agent.position[0]) > 60.0, "the car did not pass the line"
        assert not info[RUN_RED_LIGHT], "the flag stayed up after the crossing"
    finally:
        env.close()


@needs_left
def test_driving_through_a_green_is_not_a_violation():
    env, _ = _scene("intersection_left", lights_cycle_s=300.0, lights_green_s=290.0)
    try:
        manager = env.engine.light_manager
        assert manager.colour_at(0, "major") == GREEN
        for _ in range(120):
            _, _, terminated, _, info = env.step([0.0, 1.0])
            assert not info[RUN_RED_LIGHT]
            if terminated:
                break
        assert float(env.agent.position[0]) > 60.0
    finally:
        env.close()


@needs_left
def test_a_second_reset_leaves_no_light_of_the_first_behind():
    env, row = _scene("intersection_left")
    try:
        first = {light.id for light in env.engine.light_manager.spawned_objects.values()}
        assert placed_counts(env)["PGTrafficLight"] == 12
        env.reset(seed=seed_for(row))
        second = env.engine.light_manager.spawned_objects
        assert placed_counts(env)["PGTrafficLight"] == 12, "lights accumulated across a reset"
        assert not (first & set(second)), "a light was recycled rather than destroyed"
        for light in second.values():
            assert light.lane is not None and light.lane.length > 0
    finally:
        env.close()


@needs_left
def test_a_road_without_a_junction_gets_no_lights_and_no_manager_at_none():
    manifest = read_manifest(LEFT)
    entry = manifest.categories["intersection_left"]
    row = entry.scenarios[0]
    env, prepare = build_env(LEFT, entry, resolve_options(manifest, levels={"lights": "none"}))
    try:
        env.reset(seed=seed_for(row))
        prepare(env, row)
        assert not hasattr(env.engine, "light_manager")
        assert "PGTrafficLight" not in placed_counts(env)
    finally:
        env.close()
