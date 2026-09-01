"""The one hash, and the road-variety fact the bank is designed around."""

import pytest

from scenariobank.doctor import has_simulator
from scenariobank.fingerprint import lane_geometry_digest, sha256_hex

needs_sim = pytest.mark.skipif(
    not has_simulator(),
    reason="needs_sim: MetaDrive is not installed (uv sync --group sim)",
)


def test_the_same_payload_always_hashes_to_the_same_value():
    assert sha256_hex("scenariobank") == sha256_hex("scenariobank")


def test_truncation_is_a_prefix_of_the_full_digest():
    assert sha256_hex("x", length=10) == sha256_hex("x")[:10]


def test_different_payloads_do_not_collide():
    assert sha256_hex("1X0_1_") != sha256_hex("1X1_1_")


@needs_sim
@pytest.mark.parametrize(
    ("block_seq", "expected_distinct"),
    [("X", 1), ("T", 2), ("O", 5), ("CC", 5), ("rS", 5)],
)
def test_how_many_distinct_roads_each_block_sequence_produces_over_the_five_seeds(
    block_seq, expected_distinct
):
    # Measured, and pinned here on purpose. `X` producing one road for all five seeds is a fact
    # the bank is designed around -- the intersection categories vary the scene, not the road.
    # If a MetaDrive bump changes it, that is a change to what the bank means, and it should
    # fail here rather than quietly alter Phase 2's map_id count.
    from metadrive.envs.metadrive_env import MetaDriveEnv

    from scenariobank.config import base_config

    env = MetaDriveEnv(base_config(map=block_seq, start_seed=0, num_scenarios=5))
    try:
        digests = []
        for seed in range(5):
            env.reset(seed=seed)
            digests.append(lane_geometry_digest(env.engine.current_map, length=16))
    finally:
        env.close()
    assert len(set(digests)) == expected_distinct


@needs_sim
def test_the_seed_alone_does_not_change_a_fingerprint_the_way_get_meta_data_would():
    # `get_meta_data()` carries `map_config`, which carries the seed -- so it reports two
    # identical X roads as different maps. This is why map_id cannot be built on it wholesale.
    import json

    from metadrive.envs.metadrive_env import MetaDriveEnv

    from scenariobank.config import base_config

    env = MetaDriveEnv(base_config(map="X", start_seed=0, num_scenarios=2))
    try:
        geometry, metadata = [], []
        for seed in (0, 1):
            env.reset(seed=seed)
            road_map = env.engine.current_map
            geometry.append(lane_geometry_digest(road_map))
            metadata.append(sha256_hex(json.dumps(road_map.get_meta_data(), default=str)))
    finally:
        env.close()
    assert geometry[0] == geometry[1], "the road is identical"
    assert metadata[0] != metadata[1], "but get_meta_data() says otherwise"


def test_shape_gap_is_zero_for_a_reading_compared_with_itself():
    from scenariobank.fingerprint import shape_gap

    reading = [1234.5, 214.0, 216.0]
    assert shape_gap(reading, reading) == 0.0


def test_shape_gap_reports_the_worst_component_not_an_average():
    # A road the same total length and width but half the height is not "8% different"; the
    # measure has to surface the component that moved, or a near-twin check would miss it.
    from scenariobank.fingerprint import shape_gap

    assert shape_gap([1000.0, 200.0, 200.0], [1000.0, 200.0, 100.0]) == pytest.approx(0.5)


def test_shape_gap_catches_the_near_twin_a_digest_calls_distinct():
    # The measured `curve` seeds 0 and 4: different geometry hashes, 7% apart in shape. This is
    # the case that made `road_shape` necessary -- the digest reported them as two roads.
    from scenariobank.fingerprint import shape_gap
    from scenariobank.variety import NEAR_DUPLICATE

    seed_0 = [4130.0, 214.0, 216.0]
    seed_4 = [3860.0, 204.0, 213.0]
    assert shape_gap(seed_0, seed_4) < NEAR_DUPLICATE


@needs_sim
def test_road_shape_agrees_with_the_digest_on_an_identical_road():
    # `X` builds one identical road at every seed. Equal digests must imply a zero gap; the
    # converse is exactly what does not hold, and is why both measures exist.
    from metadrive.envs.metadrive_env import MetaDriveEnv

    from scenariobank.config import base_config
    from scenariobank.fingerprint import lane_geometry_digest, road_shape, shape_gap

    env = MetaDriveEnv(base_config(map="X", start_seed=0, num_scenarios=2))
    try:
        env.reset(seed=0)
        first = road_shape(env.engine.current_map)
        first_digest = lane_geometry_digest(env.engine.current_map)
        env.reset(seed=1)
        second = road_shape(env.engine.current_map)
        second_digest = lane_geometry_digest(env.engine.current_map)
    finally:
        env.close()

    assert first_digest == second_digest
    assert shape_gap(first, second) == 0.0
