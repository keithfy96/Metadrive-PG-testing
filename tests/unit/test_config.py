"""The base config is a claim about what a policy can perceive. Test it as one."""

import pytest

from scenariobank.config import OBSERVATION_SHAPE, SENSOR_CONFIG, base_config
from scenariobank.doctor import has_simulator

needs_sim = pytest.mark.skipif(
    not has_simulator(),
    reason="needs_sim: MetaDrive is not installed (uv sync --group sim)",
)


@pytest.mark.parametrize("detector", ["lidar", "side_detector", "lane_line_detector"])
def test_every_ray_casting_detector_is_pinned_at_zero_lasers(detector):
    # Above zero, `side_detector` and `lane_line_detector` stop being geometric scalars and
    # become lidars -- the side door lidar comes back through.
    assert SENSOR_CONFIG[detector]["num_lasers"] == 0


def test_the_lidar_distance_is_zero_as_well_as_its_laser_count():
    # `LidarStateObservation` drops the term when either is zero; pinning both means a single
    # careless edit cannot switch it back on.
    assert SENSOR_CONFIG["lidar"]["distance"] == 0


@needs_sim
@pytest.mark.parametrize("axis", ["traffic_density", "accident_prob"])
def test_the_option_axes_start_at_their_floor_rather_than_at_a_metadrive_default(axis):
    assert base_config()[axis] == 0.0


@needs_sim
def test_random_traffic_is_off_because_it_never_reseeds_the_traffic_manager():
    assert base_config()["random_traffic"] is False


@needs_sim
def test_the_observation_class_is_named_explicitly_rather_than_inferred():
    from metadrive.obs.state_obs import StateObservation

    assert base_config()["agent_observation"] is StateObservation


@needs_sim
def test_overrides_replace_top_level_keys_and_leave_the_rest_pinned():
    config = base_config(num_scenarios=35, start_seed=0)
    assert config["num_scenarios"] == 35
    assert config["vehicle_config"]["lidar"]["num_lasers"] == 0


@needs_sim
def test_a_freshly_built_env_produces_exactly_the_pinned_observation():
    from metadrive.envs.metadrive_env import MetaDriveEnv

    env = MetaDriveEnv(base_config())
    try:
        env.reset(seed=0)
        assert tuple(env.observation_space.shape) == OBSERVATION_SHAPE
    finally:
        env.close()


def test_the_spawn_lane_draw_is_named_rather_than_inherited():
    # MetaDrive defaults `random_spawn_lane_index` to True (`metadrive_env.py:61`) -- the only
    # `random_*` key that does. It is left on deliberately, because for `X` it is the sole
    # difference between the five seeds. Naming it here is what makes that a decision rather
    # than an accident, so this test guards the *presence* of the key, not just its value.
    assert "random_spawn_lane_index" in base_config()
    assert base_config()["random_spawn_lane_index"] is True
