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
"""

import importlib.util
import json
import subprocess
import sys

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
                    points = [[float(p[0]), float(p[1])]
                              for p in (lane.position(s, 0) for s in steps)]
                    lanes["{}|{}|{}".format(start, end, index)] = [float(lane.length), points]
        out[seq] = lanes
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


def trace_map(block_seq):
    """Lane traces of `block_seq` as this process builds it -- that is, mirrored."""
    from metadrive.envs.metadrive_env import MetaDriveEnv

    from scenariobank.config import base_config

    env = MetaDriveEnv(base_config(map=block_seq, start_seed=0, num_scenarios=1))
    try:
        env.reset(seed=0)
        lanes = {}
        for start, tos in env.engine.current_map.road_network.graph.items():
            for end, group in tos.items():
                for index, lane in enumerate(group):
                    steps = np.linspace(0, lane.length, SAMPLES_PER_LANE)
                    points = np.array([lane.position(s, 0) for s in steps], dtype=float)
                    lanes[f"{start}|{end}|{index}"] = (float(lane.length), points)
        return lanes
    finally:
        env.close()


@needs_sim
@pytest.mark.parametrize("block_seq", BLOCK_SEQUENCES)
def test_the_mirror_is_an_exact_reflection_lane_by_lane(block_seq, unmirrored_maps):
    reference = unmirrored_maps[block_seq]
    mirrored = trace_map(block_seq)

    # A reflection cannot add, drop or rename a lane. If these differ the map was rebuilt
    # differently, not reflected.
    assert set(mirrored) == set(reference), "the mirror changed the road network's topology"

    for key, (length, points) in mirrored.items():
        expected_length, expected_points = reference[key]
        assert length == pytest.approx(expected_length, abs=1e-3), (
            f"{key}: mirrored length {length:.3f} != {expected_length:.3f}. A reflection is an "
            "isometry, so this lane is not a reflection of the original -- most likely its "
            "radius was offset to the wrong side."
        )
        reflected = np.asarray(expected_points, dtype=float) * np.array([1.0, -1.0])
        assert points == pytest.approx(reflected, abs=1e-2), f"{key}: geometry is not reflected"


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
