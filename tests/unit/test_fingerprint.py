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
