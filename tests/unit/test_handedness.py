"""The mirror that turns MetaDrive's right-side traffic into this bank's left-side traffic.

The load-bearing test here is `test_the_mirror_is_an_exact_reflection_lane_by_lane`. It builds
each map twice -- once in this process, where the mirror is installed, and once in a
**subprocess that never imports `scenariobank`**, where MetaDrive is its unmodified self -- and
asserts the two agree lane for lane once one of them is reflected. That is the only way to
check a monkey-patch of a global geometry layer honestly: the patch cannot be uninstalled, so
the unpatched reference has to come from somewhere the patch never reached.

It is also the test that found the real bug. An earlier mirror flipped `is_clockwise()` for the
arcs but not for the lateral arithmetic that places the *sibling* lanes of a multi-lane curve.
Every map still built, node names all matched, lane counts all matched, and the pictures looked
plausibly left-side -- the only symptom was that curved lanes came out the wrong length.

It did not find the second one, because it sampled centrelines only. A mirrored arc's lateral
axis pointed the other way from a mirrored straight's, so every centreline reflected exactly
while every sidewalk and lane line on an arc landed on the wrong side, and a vehicle's offset
in its lane changed sign at every bend. That was found from the driver's seat (Phase 4 Step 4a).
So the test now samples both lane edges, asks each lane where a reflected point is, and compares
every sidewalk polygon -- the whole map, not the line down the middle of it.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from scenariobank.categories import CATEGORIES
from scenariobank.doctor import has_simulator
from scenariobank.handedness import (
    DRIVE_SIDE_LEFT,
    HandednessError,
    _mirror_block_utils,
    drive_side,
    install,
)

needs_sim = pytest.mark.skipif(
    not has_simulator(),
    reason="needs_sim: MetaDrive is not installed (uv sync --group sim)",
)

#: One per category, plus the plain straight road that has no curvature to get wrong.
BLOCK_SEQUENCES = ("SS", "CC", "X", "T", "O", "rS")

SAMPLES_PER_LANE = 12

#: Run in a subprocess with `scenariobank` off `sys.path`, so `handedness.install()` cannot
#: have run and MetaDrive builds the maps it was written to build.
_REFERENCE_SCRIPT = """
import json, logging, sys
import numpy as np
from metadrive.envs.metadrive_env import MetaDriveEnv

target, samples, sequences = sys.argv[1], int(sys.argv[2]), sys.argv[3:]
out = {}
for seq in sequences:
    env = MetaDriveEnv({
        "use_render": False, "map": seq, "start_seed": 0, "num_scenarios": 1,
        "traffic_density": 0.0, "accident_prob": 0.0, "log_level": logging.WARNING,
    })
    try:
        env.reset(seed=0)
        lanes = {}
        for start, tos in env.engine.current_map.road_network.graph.items():
            for end, group in tos.items():
                for index, lane in enumerate(group):
                    steps = np.linspace(0, lane.length, samples)
                    half = lane.width_at(0) / 2
                    trace = lambda lat: [[float(p[0]), float(p[1])]
                                         for p in (lane.position(s, lat) for s in steps)]
                    probe = lane.position(lane.length / 3, half / 2)
                    lanes["{}|{}|{}".format(start, end, index)] = [
                        float(lane.length), trace(0), trace(half), trace(-half),
                        [float(probe[0]), float(probe[1])],
                    ]
        sidewalks = {}
        for block in env.engine.current_map.blocks:
            for name, sidewalk in block.sidewalks.items():
                sidewalks[name] = [[float(p[0]), float(p[1])] for p in sidewalk["polygon"]]
        out[seq] = {"lanes": lanes, "sidewalks": sidewalks}
    finally:
        env.close()
with open(target, "w") as handle:
    json.dump(out, handle)
"""


@pytest.fixture(scope="module")
def unmirrored_maps(tmp_path_factory):
    """Lane traces of every block sequence, as stock MetaDrive builds them."""
    target = tmp_path_factory.mktemp("reference") / "maps.json"
    result = subprocess.run(
        [
            sys.executable, "-c", _REFERENCE_SCRIPT,
            str(target), str(SAMPLES_PER_LANE), *BLOCK_SEQUENCES,
        ],
        capture_output=True,
        text=True,
        # `-c` puts "" on sys.path, not the repo's src/, so `scenariobank` is unimportable and
        # the mirror provably never ran in there.
        cwd=tmp_path_factory.mktemp("elsewhere"),
    )
    if result.returncode != 0:
        pytest.fail(f"reference MetaDrive run failed:\n{result.stderr[-2000:]}")
    return json.loads(target.read_text())


REFLECT = np.array([1.0, -1.0])


def reflect(points):
    return np.asarray(points, dtype=float) * REFLECT


def trace_map(block_seq, reference):
    """`block_seq` as this process builds it -- that is, mirrored -- traced the way the reference
    was, plus what each mirrored lane says the *reference's* probe point is once reflected."""
    from metadrive.envs.metadrive_env import MetaDriveEnv

    from scenariobank.config import base_config

    env = MetaDriveEnv(base_config(map=block_seq, start_seed=0, num_scenarios=1))
    try:
        env.reset(seed=0)
        lanes = {}
        for start, tos in env.engine.current_map.road_network.graph.items():
            for end, group in tos.items():
                for index, lane in enumerate(group):
                    key = f"{start}|{end}|{index}"
                    steps = np.linspace(0, lane.length, SAMPLES_PER_LANE)
                    half = lane.width_at(0) / 2
                    edges = {
                        lat: np.array([lane.position(s, lat) for s in steps], dtype=float)
                        for lat in (0, half, -half)
                    }
                    located = None
                    if key in reference["lanes"]:
                        probe = reflect(reference["lanes"][key][4])
                        located = tuple(float(v) for v in lane.local_coordinates(probe))
                    lanes[key] = (float(lane.length), edges, located, half)
        sidewalks = {
            name: np.asarray(sidewalk["polygon"], dtype=float)
            for block in env.engine.current_map.blocks
            for name, sidewalk in block.sidewalks.items()
        }
        return lanes, sidewalks
    finally:
        env.close()


def as_point_set(points, decimals=2):
    return {tuple(p) for p in np.round(np.asarray(points, dtype=float), decimals).tolist()}


@needs_sim
@pytest.mark.parametrize("block_seq", BLOCK_SEQUENCES)
def test_the_mirror_is_an_exact_reflection_lane_by_lane(block_seq, unmirrored_maps):
    reference = unmirrored_maps[block_seq]
    mirrored, sidewalks = trace_map(block_seq, reference)

    # A reflection cannot add, drop or rename a lane. If these differ the map was rebuilt
    # differently, not reflected.
    assert set(mirrored) == set(reference["lanes"]), "the mirror changed the network's topology"

    for key, (length, edges, located, half) in mirrored.items():
        expected_length, centre, plus, minus, _probe = reference["lanes"][key]
        assert length == pytest.approx(expected_length, abs=1e-3), (
            f"{key}: mirrored length {length:.3f} != {expected_length:.3f}. A reflection is an "
            "isometry, so this lane is not a reflection of the original -- most likely its "
            "radius was offset to the wrong side."
        )
        assert edges[0] == pytest.approx(reflect(centre), abs=1e-2), f"{key}: centreline"
        # The lateral axis: the mirrored lane's +w/2 edge must be the mirror of the original's
        # +w/2 edge, not of its -w/2 edge. The centreline cannot tell the two apart; an arc
        # whose sweep was inverted without its lateral term is exactly that failure.
        assert edges[half] == pytest.approx(reflect(plus), abs=1e-2), (
            f"{key}: the +w/2 edge is not the mirror of the original's +w/2 edge; the lane's "
            "lateral axis points the wrong way"
        )
        assert edges[-half] == pytest.approx(reflect(minus), abs=1e-2), f"{key}: -w/2 edge"
        # And the inverse: asked where the reflected probe point is, the mirrored lane must
        # answer with the coordinates the original gave it -- a third of the way along, a
        # quarter width to the positive side.
        assert located == pytest.approx((expected_length / 3, half / 2), abs=1e-2), (
            f"{key}: local_coordinates of the reflected probe is {located}"
        )

    # Everything built off a lane edge -- here, every sidewalk -- must reflect as a whole.
    assert set(sidewalks) == set(reference["sidewalks"]), "the mirror changed the sidewalks"
    for name, polygon in sidewalks.items():
        assert as_point_set(polygon) == as_point_set(reflect(reference["sidewalks"][name])), (
            f"sidewalk {name} is not the mirror of the original's; a lane edge on an arc is on "
            "the wrong side"
        )


@needs_sim
@pytest.mark.parametrize("block_seq", BLOCK_SEQUENCES)
def test_oncoming_traffic_is_on_the_egos_right(block_seq):
    """The whole point, stated in the one term a driver would use."""
    from metadrive.envs.metadrive_env import MetaDriveEnv

    from scenariobank.config import base_config
    from scenariobank.doctor import measure_drive_side

    env = MetaDriveEnv(base_config(map=block_seq, start_seed=0, num_scenarios=1))
    try:
        env.reset(seed=0)
        assert measure_drive_side(env) == DRIVE_SIDE_LEFT
    finally:
        env.close()


@needs_sim
@pytest.mark.skipif(
    not Path("banks/curve/manifest.json").exists(), reason="needs the bank at banks/curve"
)
def test_idm_traffic_keeps_its_lane_on_the_mirrored_map():
    """Change 4, from the driver's seat of every traffic car: the expert drives `curve_0000`
    through `traffic=high` and no car mounts the sidewalk. Before the change eight of
    thirty-five did in 146 steps, and 13 % of vehicle-steps were more than a metre off centre;
    stock MetaDrive on its own map gives none and 4 %."""
    from scenariobank.bank import read_manifest
    from scenariobank.env import build_env, seed_for
    from scenariobank.options import resolve_options
    from scenariobank.policies import load_policy
    from scenariobank.runner import bind_policy

    bank = Path("banks/curve")
    manifest = read_manifest(bank)
    entry = manifest.categories["curve"]
    row = entry.scenarios[0]
    levels = {
        "traffic": "high", "cones": "none", "barriers": "none",
        "pedestrians": "none", "cyclists": "none",
    }
    env, prepare = build_env(bank, entry, resolve_options(manifest, levels=levels))
    act = load_policy("scenariobank.policies:ExpertPolicy")
    bind_policy(act, env)
    try:
        observation, _ = env.reset(seed=seed_for(row))
        prepare(env, row)
        network = env.engine.current_map.road_network
        traffic = env.engine.traffic_manager.spawned_objects
        assert len(traffic) > 20, "dense enough that lane changes happen"
        off_centre = total = 0
        for _ in range(300):
            for vehicle in traffic.values():
                assert not vehicle.crash_sidewalk, "a traffic car mounted the sidewalk"
                index = network.get_closest_lane_index(vehicle.position)
                index = index[0] if not isinstance(index[0], str) else index
                lateral = abs(network.get_lane(index).local_coordinates(vehicle.position)[1])
                off_centre += lateral > 1.0
                total += 1
            observation, _, terminated, truncated, _ = env.step(act(observation))
            if terminated or truncated:
                break
        assert off_centre / total < 0.07, f"{off_centre / total:.3f} of vehicle-steps off centre"
    finally:
        env.close()


@needs_sim
def test_the_roundabout_circulates_clockwise():
    """Left-side traffic goes round a roundabout clockwise. Right-side traffic does not.

    Measured as the signed area swept by the circulating lanes about the roundabout's centre:
    negative means clockwise in MetaDrive's counter-clockwise-positive frame.
    """
    from metadrive.envs.metadrive_env import MetaDriveEnv

    from scenariobank.config import base_config

    env = MetaDriveEnv(base_config(map="O", start_seed=0, num_scenarios=1))
    try:
        env.reset(seed=0)
        from metadrive.component.lane.circular_lane import CircularLane

        network = env.engine.current_map.road_network
        arcs = [
            lane
            for tos in network.graph.values()
            for group in tos.values()
            for lane in group
            if isinstance(lane, CircularLane)
        ]
        assert arcs, "no circular lanes in a roundabout"
        # `direction` is -1 clockwise, +1 counter-clockwise. The circulating carriageway is the
        # majority of the arcs; entry and exit slip lanes curve the other way.
        swept = sum(lane.direction * lane.length for lane in arcs)
        assert swept < 0, f"roundabout circulates counter-clockwise (swept={swept:.1f})"
    finally:
        env.close()


@needs_sim
@pytest.mark.parametrize("name", sorted(CATEGORIES))
def test_every_category_still_resolves_a_destination_after_the_mirror(name):
    """A mirrored T junction offers its arms on the other side; the rules must still find one."""
    from scenariobank.sockets import resolve_destination

    assert resolve_destination(CATEGORIES[name], 0).node


@needs_sim
def test_the_drive_side_is_left_once_anything_has_built_a_config():
    from scenariobank.config import base_config

    base_config()
    assert drive_side() == DRIVE_SIDE_LEFT


@needs_sim
def test_installing_twice_does_not_mirror_twice():
    """Not idempotent would mean the second call flips the world back to right-side traffic."""
    from scenariobank.config import base_config

    base_config()
    install()
    install()
    assert drive_side() == DRIVE_SIDE_LEFT


def test_the_mirror_refuses_a_metadrive_whose_source_it_does_not_recognise(tmp_path):
    """The failure this guards is silent: an unapplied patch still yields a working bank.

    It just yields a right-side-traffic one, with every manifest in it claiming otherwise. So a
    MetaDrive whose `create_pg_block_utils` no longer contains the lines being rewritten has to
    be an error, not a fallback.
    """

    source = tmp_path / "create_pg_block_utils.py"
    source.write_text("def CreateRoadFrom():\n    pass\n")
    spec = importlib.util.spec_from_file_location("not_metadrive_utils", source)
    stand_in = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(stand_in)

    with pytest.raises(HandednessError, match="cannot mirror MetaDrive"):
        _mirror_block_utils(stand_in)
