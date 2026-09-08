"""Phase 2b, as the two tests it became: the option levels leave the map and the route alone.

Comparing a score at `traffic=none` with one at `traffic=high` means something only if the road
and the route were the same both times. Structurally they are -- `Randomizable.__init__` hands
every manager its own `np_random`, re-seeded per episode -- but the obstacle and actor managers
are ours, and this is the test that would catch one of them reaching into the map's stream.

Two tests, one comparison. `test_option_levels_do_not_move_the_map_or_route` asserts the map
digest and the checkpoints are identical across levels, with `assert_array_equal` and not
`allclose`, and that the object sets differ, or the loop compared nothing. The second runs the
same comparison under `random_traffic=True`, which leaves the traffic manager unseeded
(`traffic_manager.py:339-341`), and requires the traffic to differ between two resets at one
seed: the one setting known to break invariance must be seen breaking it, or a green run
proves only that two things were compared that were never going to differ.

Never run during generation. Needs the simulator and the two procedural banks.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from scenariobank.bank import read_manifest
from scenariobank.doctor import has_simulator
from scenariobank.env import build_config, build_env, procedural_env_class, seed_for
from scenariobank.fingerprint import lane_geometry_digest
from scenariobank.options import resolve_options
from scenariobank.runner import placed_counts

needs_sim = pytest.mark.skipif(
    not has_simulator(),
    reason="needs_sim: MetaDrive is not installed (uv sync --group sim)",
)

CURVE = Path("banks/curve")
LEFT = Path("banks/t-junction-left-intersection")

NONE = {
    "traffic": "none",
    "cones": "none",
    "barriers": "none",
    "pedestrians": "none",
    "cyclists": "none",
}
HIGH = {axis: "high" for axis in NONE}

#: The levels the map is compared across: nothing, traffic alone, everything.
LADDER = (NONE, {**NONE, "traffic": "high"}, HIGH)

def needs_bank(bank: Path):
    return pytest.mark.skipif(
        not (bank / "manifest.json").exists(), reason=f"needs the bank at {bank}"
    )


BANKS = [
    pytest.param(CURVE, "curve", marks=needs_bank(CURVE)),
    pytest.param(LEFT, "intersection_left", marks=needs_bank(LEFT)),
]


def observe(env) -> tuple[str, np.ndarray, list[tuple[float, float]]]:
    """What one reset looks like: the road, the route, and where the traffic is."""
    digest = lane_geometry_digest(env.engine.current_map)
    checkpoints = np.array(list(env.agent.navigation.checkpoints))
    traffic = sorted(
        tuple(round(float(v), 3) for v in vehicle.position[:2])
        for vehicle in env.engine.traffic_manager.spawned_objects.values()
    )
    return digest, checkpoints, traffic


def assert_same_road_and_route(first, second) -> None:
    assert first[0] == second[0], "the lane geometry moved"
    np.testing.assert_array_equal(first[1], second[1])


@needs_sim
@pytest.mark.parametrize(("bank", "category"), BANKS)
def test_option_levels_do_not_move_the_map_or_route(bank, category):
    manifest = read_manifest(bank)
    entry = manifest.categories[category]
    row = entry.scenarios[0]
    seen = []
    placed = []
    for levels in LADDER:
        env, prepare = build_env(bank, entry, resolve_options(manifest, levels=levels))
        try:
            env.reset(seed=seed_for(row))
            prepare(env, row)
            seen.append(observe(env))
            placed.append(placed_counts(env))
        finally:
            env.close()
    for other in seen[1:]:
        assert_same_road_and_route(seen[0], other)
    assert placed[0] == {"DefaultVehicle": 1}
    assert placed[-1] != placed[0] and placed[1] != placed[0], "nothing was placed: an empty loop"
    assert seen[1][2] != seen[0][2], "traffic=high put no vehicle on the road"


@needs_sim
@pytest.mark.parametrize(("bank", "category"), BANKS)
def test_random_traffic_breaks_invariance(bank, category):
    manifest = read_manifest(bank)
    entry = manifest.categories[category]
    row = entry.scenarios[0]
    options = resolve_options(manifest, levels={**NONE, "traffic": "high"})
    for random_traffic in (False, True):
        config = build_config(bank, entry, options)
        config["random_traffic"] = random_traffic
        env = procedural_env_class()(config)
        try:
            resets = []
            for _ in range(2):
                env.reset(seed=seed_for(row))
                resets.append(observe(env))
        finally:
            env.close()
        assert_same_road_and_route(*resets)
        if random_traffic:
            assert resets[0][2] != resets[1][2], "unseeded traffic came out the same twice"
        else:
            assert resets[0][2] == resets[1][2], "seeded traffic moved between two resets"
