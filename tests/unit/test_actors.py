"""`VRUManager`: the one placement code that is ours, so its reproducibility is tested here.

Needs the simulator and the `X` bank, the road on which the actors are the only thing the hard
tier changes. Two envs at one seed and level lay the actors out identically, and another seed
does not; stepping consumes none of the manager's randomness; pedestrians cross at their speed
and turn round, cyclists hold their lane and their speed; cyclists on one lane get disjoint
stretches; hitting a person scores the object pair and ends the episode.
"""

from __future__ import annotations

import math
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip(
    "metadrive", reason="needs_sim: MetaDrive is not installed (uv sync --group sim)"
)

from metadrive.component.traffic_participants.pedestrian import Pedestrian  # noqa: E402

from scenariobank.actors import (  # noqa: E402
    CYCLIST_SPEED,
    LANE_MARGIN,
    PEDESTRIAN_SPEED,
    SEGMENT_GAP,
    VRUManager,
    _stretches,
)
from scenariobank.bank import read_manifest  # noqa: E402
from scenariobank.env import (  # noqa: E402
    CRASH_HUMAN_COST,
    CRASH_HUMAN_PENALTY,
    build_env,
    seed_for,
)
from scenariobank.fingerprint import sha256_hex  # noqa: E402
from scenariobank.options import resolve_options  # noqa: E402
from scenariobank.runner import actor_layout_digest, placed_counts  # noqa: E402

LEFT = Path("banks/t-junction-left-intersection")
CURVE = Path("banks/curve")

needs_left = pytest.mark.skipif(
    not (LEFT / "manifest.json").exists(), reason=f"needs the bank at {LEFT}"
)
needs_curve = pytest.mark.skipif(
    not (CURVE / "manifest.json").exists(), reason=f"needs the bank at {CURVE}"
)

NONE = {
    "traffic": "none",
    "cones": "none",
    "barriers": "none",
    "pedestrians": "none",
    "cyclists": "none",
}

#: One env step is five physics steps of 0.02 s.
STEP_S = 0.1


@contextmanager
def scene(bank: Path, category: str, index: int = 0, **levels: str):
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


def rng_state(manager):
    kind, keys, position, has_gauss, gauss = manager.np_random.get_state()
    return kind, keys.tolist(), position, has_gauss, gauss


@needs_left
def test_two_envs_at_one_seed_and_level_lay_the_actors_out_identically_and_another_seed_does_not():
    with scene(LEFT, "intersection_left", pedestrians="high", cyclists="high") as env:
        manager = env.engine.vru_manager
        assert isinstance(manager, VRUManager)
        first = (manager.layout(), manager.layout_digest())
        assert len(first[0]) == 6 + 4
        assert first[1] == sha256_hex("\n".join(first[0])) == actor_layout_digest(env)
        assert placed_counts(env) == {"Cyclist": 4, "DefaultVehicle": 1, "Pedestrian": 6}
    with scene(LEFT, "intersection_left", pedestrians="high", cyclists="high") as env:
        second = (env.engine.vru_manager.layout(), env.engine.vru_manager.layout_digest())
    with scene(LEFT, "intersection_left", index=1, pedestrians="high", cyclists="high") as env:
        other = env.engine.vru_manager.layout_digest()
    assert first == second
    assert other != first[1], "a digest that cannot differ measures nothing"


@needs_left
def test_stepping_consumes_none_of_the_managers_randomness():
    with scene(LEFT, "intersection_left", pedestrians="medium", cyclists="low") as env:
        manager = env.engine.vru_manager
        before = rng_state(manager)
        for _ in range(60):
            env.step([0.0, 0.0])
        assert rng_state(manager) == before


@needs_left
def test_pedestrians_cross_at_their_speed_on_their_line_and_turn_round():
    """The speed pins the push: through the transform it would be 80% of this (see
    `actors.py`). The line pins the crossing; the turn pins the endpoint comparison."""
    with scene(LEFT, "intersection_left", pedestrians="high") as env:
        manager = env.engine.vru_manager
        tracks = {name: [] for name in manager.patrols}
        turned = set()
        heading = dict(manager._toward_end)
        for _ in range(200):
            env.step([0.0, 0.0])
            for name, actor in manager.spawned_objects.items():
                tracks[name].append(np.array(actor.position[:2], dtype=float))
                if manager._toward_end[name] != heading[name]:
                    turned.add(name)
                    heading[name] = manager._toward_end[name]
        for name, patrol in manager.patrols.items():
            track = np.array(tracks[name])
            steps = np.linalg.norm(np.diff(track, axis=0), axis=1)
            assert steps.mean() == pytest.approx(PEDESTRIAN_SPEED * STEP_S, rel=0.03), name
            start, end = np.array(patrol.start), np.array(patrol.end)
            along = (end - start) / np.linalg.norm(end - start)
            offsets = track - start
            off_line = np.linalg.norm(offsets - np.outer(offsets @ along, along), axis=1)
            assert off_line.max() < 0.1, name
        assert turned, "200 steps at 1.2 m/s is 24 m; a crossing is shorter than that"


@needs_curve
def test_cyclists_hold_their_lane_and_their_speed_on_an_arc():
    with scene(CURVE, "curve", cyclists="high") as env:
        manager = env.engine.vru_manager
        network = env.engine.current_map.road_network
        errors = {name: [] for name in manager.patrols}
        tracks = {name: [] for name in manager.patrols}
        for _ in range(150):
            env.step([0.0, 0.0])
            for name, actor in manager.spawned_objects.items():
                patrol = manager.patrols[name]
                lane = network.get_lane(patrol.lane_index)
                _, lateral = lane.local_coordinates(actor.position[:2])
                errors[name].append(abs(lateral - patrol.lateral))
                tracks[name].append(np.array(actor.position[:2], dtype=float))
        arcs = 0
        for name, patrol in manager.patrols.items():
            arcs += type(network.get_lane(patrol.lane_index)).__name__ == "CircularLane"
            assert max(errors[name]) < 0.25, name
            steps = np.linalg.norm(np.diff(np.array(tracks[name]), axis=0), axis=1)
            assert steps.mean() > 0.9 * CYCLIST_SPEED * STEP_S, name
        assert arcs > 0, "a `CC` road with no cyclist on an arc tested nothing about arcs"


@dataclass
class FakeLane:
    index: tuple
    length: float


def test_cyclists_drawn_onto_one_lane_get_disjoint_stretches_in_draw_order():
    a, b = [FakeLane(("x", "y", 1), 50.0)], [FakeLane(("y", "z", 1), 30.0)]
    rides = [(a, 0.0, 0.9), (b, 0.5, 0.1), (a, 1.0, 0.6)]
    out = _stretches(rides)
    assert [lanes for lanes, *_ in out] == [a, b, a]
    share = (50.0 - 2 * LANE_MARGIN - SEGMENT_GAP) / 2
    (_, first, at_first, fwd_first), (_, alone, at_alone, _), (_, second, at_second, _) = out
    assert first == (LANE_MARGIN, LANE_MARGIN + share)
    assert second == (first[1] + SEGMENT_GAP, first[1] + SEGMENT_GAP + share)
    assert alone == (LANE_MARGIN, 30.0 - LANE_MARGIN)
    assert (at_first, at_alone, at_second) == (first[0], 15.0, second[1])
    assert fwd_first is True


@needs_curve
@needs_curve
def test_an_actor_a_vehicle_touches_is_not_driven_and_traffic_keeps_its_lane():
    """The first film's finding: cars knocked off the road by a pedestrian that would not
    stop walking into them. Traffic high with people and no obstacles, the ego idle. Before the
    fix four of forty vehicles were more than a lane off their centreline by step 300."""
    with scene(CURVE, "curve", traffic="high", pedestrians="medium", cyclists="low") as env:
        vru = env.engine.vru_manager
        traffic = env.engine.traffic_manager
        worst = 0.0
        for step in range(300):
            env.step([0.0, 0.0])
            if step % 10:
                continue
            for vehicle in traffic.spawned_objects.values():
                if vehicle.lane is not None:
                    worst = max(worst, abs(vehicle.lane.local_coordinates(vehicle.position)[1]))
        assert vru.struck, "a vehicle touched an actor, or this checked nothing"
        assert worst < 1.75, f"a traffic vehicle drifted {worst:.2f} m off its lane centre"


@needs_curve
def test_actors_carry_the_lane_they_are_on_so_traffic_brakes_instead_of_going_blind():
    """`IDMPolicy.act` reads `obj.lane` off everything its lidar sees inside a bare `except`
    whose fallback is no front object. Actors without a lane made every car near them drive
    blind: thirty-one of forty traffic cars crashed with actors alone on this row. With the
    lane set, the expert drives the row at `hard` and the traffic neither crashes nor leaves
    the road."""
    from scenariobank.policies import load_policy
    from scenariobank.runner import bind_policy

    levels = dict(traffic="high", cones="medium", barriers="medium", pedestrians="medium",
                  cyclists="low")
    with scene(CURVE, "curve", **levels) as env:
        act = load_policy("scenariobank.policies:ExpertPolicy")
        bind_policy(act, env)
        vru = env.engine.vru_manager
        traffic = env.engine.traffic_manager.spawned_objects
        network = env.engine.current_map.road_network
        for _ in range(300):
            for name, patrol in vru.patrols.items():
                actor = vru.spawned_objects[name]
                lanes = network.graph[patrol.lane_index[0]][patrol.lane_index[1]]
                assert actor.lane in lanes, "an actor's lane is one of its road's"
                nearest = min(abs(lane.local_coordinates(actor.position[:2])[1]) for lane in lanes)
                assert abs(actor.lane.local_coordinates(actor.position[:2])[1]) == nearest
            for vehicle in traffic.values():
                index = network.get_closest_lane_index(vehicle.position)
                index = index[0] if not isinstance(index[0], str) else index
                lateral = abs(network.get_lane(index).local_coordinates(vehicle.position)[1])
                assert lateral < 2.5, f"a traffic car is {lateral:.1f} m from any lane centre"
            _, _, terminated, truncated, _ = env.step(act(None))  # the expert reads the agent
            if terminated or truncated:
                break
        crashed = sum(1 for vehicle in traffic.values() if vehicle.crash_vehicle)
        assert crashed <= 2, f"{crashed} traffic cars crashed into each other"


def test_hitting_a_person_scores_the_object_pair_and_ends_the_episode():
    """`crash_human_done` was already true; the reward and cost were the missing half."""
    with scene(CURVE, "curve") as env:
        assert env.config["crash_human_penalty"] == CRASH_HUMAN_PENALTY == 5.0
        assert env.config["crash_human_cost"] == CRASH_HUMAN_COST == 1.0
        ego = env.agent
        along, _ = ego.lane.local_coordinates(ego.position)
        ahead = ego.lane.position(along + 12, 0)
        env.engine.spawn_object(
            Pedestrian, position=list(ahead), heading_theta=ego.heading_theta + math.pi / 2
        )
        for _ in range(200):
            _, reward, terminated, _, info = env.step([0.0, 1.0])
            if info.get("crash_human"):
                break
        assert info["crash_human"] and terminated
        assert (reward, info["cost"]) == (-5.0, 1.0)
        assert not info["crash_object"] and not info["crash_vehicle"]
