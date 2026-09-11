"""`av3/av3_model.py` -- the six conversions into and out of the AV3 checkpoint.

Every one of them fails silently. A mirrored route, a swapped camera pair or a reversed frame
history all produce a model that loads, runs and returns twenty plausible waypoints, so
nothing downstream raises and the only symptom is a car that drives somewhere else. What can
be pinned here is pinned here; the rest is `scenariobank av3`, which needs an engine.

**Pinned against sources rather than against comments**: `preprocess` is checked pixel for
pixel against the fork's own `modifiers.py` where that checkout is present, and the rig's
camera names against the submitted config's `camera_order`. The forward pass itself is out of
reach -- it needs a 1.2 GB TensorRT engine and a GPU -- and so is anything that takes a
MetaDrive `agent`. Those go through stand-ins with exactly the attributes the conversion reads.
"""

from __future__ import annotations

import importlib.util
import math
from pathlib import Path

import numpy as np
import pytest

from scenariobank.av3 import av3_model
from scenariobank.av3.av3_model import (
    MODEL_HORIZON_S,
    MODELV2_OUTPUT_WIDTH,
    ROUTE_FEATURE_DIM,
    AV3Model,
    Config,
    FrameHistory,
    ModelError,
)
from scenariobank.av3.camera_rig import load_rig

#: The openpilot fork's own preprocessing, read as a file and executed, where the checkout is.
FORK_MODIFIERS = Path(
    "/home/keith/Desktop/work/wingfin/wingfin-openpilot-temp/assets/modifiers/modifiers.py"
)
RIG = Path("rigs/av3.txt")
SUBMITTED = Path("../models/model_dev.yml")


# ---------------------------------------------------------------------------------------
# Stand-ins. Only the attributes each conversion actually reads.
# ---------------------------------------------------------------------------------------


class FakeTrajectory:
    """A straight route along +x, or one bending left, sampled by arc length."""

    def __init__(self, curvature: float = 0.0, length: float = 200.0) -> None:
        self.curvature = curvature
        self.length = length

    def local_coordinates(self, position):
        return 0.0, 0.0

    def position(self, along, lateral):
        if abs(self.curvature) < 1e-12:
            return (along, 0.0)
        radius = 1.0 / self.curvature
        angle = along * self.curvature
        return (math.sin(angle) * radius, (1.0 - math.cos(angle)) * radius)

    def heading_theta_at(self, along):
        return along * self.curvature


class FakeAgent:
    """At the world origin, heading due east, so the ego frame is the world frame."""

    def __init__(self, velocity=(0.0, 0.0), heading: float = 0.0) -> None:
        self.velocity = velocity
        self.heading_theta = heading
        self.position = (0.0, 0.0)


def config(**overrides) -> Config:
    values = {
        "camera_order": ["a", "b"], "t_frames": 5, "frame_stride_s": 0.5,
        "ego_velocity_scale": [8.09, 0.27], "n_route": 20, "route_spacing_m": 2.0,
        "route_max_offset_m": 20.0, "expected_camera_image_width": 512,
        "expected_camera_image_height": 288,
    }
    values.update(overrides)
    return Config(values, path="fake.yml")


def _fork_modifier():
    if not FORK_MODIFIERS.exists():
        pytest.skip(f"the openpilot fork is not at {FORK_MODIFIERS}")
    spec = importlib.util.spec_from_file_location("fork_modifiers", FORK_MODIFIERS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------------------
# 1. pixels
# ---------------------------------------------------------------------------------------


def test_preprocess_is_pixel_identical_to_the_forks_own_modifier():
    """Read as a file and executed, not reproduced from its docstring.

    `preprocess` stops one step short of the fork's -- it returns uint8 rather than dividing by
    255 -- and undoing that one step has to give back the fork's array exactly, at every input
    size the rig can produce: 512x384 is what `rigs/av3.txt` renders, 1440x1080 what wing-sim's
    own cameras render, so both have to squash identically.
    """
    pytest.importorskip("cv2")
    fork = _fork_modifier()
    generator = np.random.default_rng(0)
    for shape in ((1080, 1440, 3), (384, 512, 3), (288, 512, 3)):
        frame = generator.integers(0, 256, shape, dtype=np.uint8)
        theirs = fork.Modifiers.camera_preprocessing(frame)
        ours = av3_model.preprocess(frame, 512, 288).astype(np.float32) / 255.0
        assert theirs.shape == (3, 288, 512)
        assert np.array_equal(theirs, ours), shape


def test_preprocess_squashes_four_three_to_sixteen_nine_and_swaps_the_channels():
    pytest.importorskip("cv2")
    frame = np.zeros((384, 512, 3), dtype=np.uint8)
    frame[..., 0] = 200  # blue, in BGR
    out = av3_model.preprocess(frame, 512, 288)
    assert out.shape == (3, 288, 512) and out.dtype == np.uint8
    assert out[2].max() == 200 and out[0].max() == 0, "BGR in, RGB out: blue lands last"


def test_a_float_frame_and_a_flat_frame_are_refused_by_name():
    with pytest.raises(ModelError, match="uint8"):
        av3_model.preprocess(np.zeros((384, 512, 3), dtype=np.float32), 512, 288)
    with pytest.raises(ModelError, match=r"\(H, W, 3\)"):
        av3_model.preprocess(np.zeros((384, 512), dtype=np.uint8), 512, 288)


# ---------------------------------------------------------------------------------------
# 2. camera order
# ---------------------------------------------------------------------------------------


@pytest.mark.skipif(not SUBMITTED.exists(), reason=f"needs the submitted config at {SUBMITTED}")
def test_the_rig_offers_exactly_the_cameras_the_submitted_model_reads():
    """`camera_order` is a contract with the weights, and `rigs/av3.txt` exists so these two
    lists can be compared directly."""
    loaded = av3_model.load_config(SUBMITTED)
    rig = load_rig(RIG, read_interval_s=None)
    assert sorted(rig.names) == sorted(loaded.camera_order)


def test_the_stack_is_in_model_order_by_name_and_a_missing_camera_is_refused_by_name():
    pytest.importorskip("cv2")
    model = AV3Model(config(camera_order=["b", "a"]), "nowhere.ep", 0.05)
    frames = {
        "a": np.full((48, 64, 3), 10, dtype=np.uint8),
        "b": np.full((48, 64, 3), 20, dtype=np.uint8),
    }
    stack = model.image_stack(frames)
    assert stack.shape == (2, 3, 288, 512)
    assert (stack[0].max(), stack[1].max()) == (20, 10), "b first, as camera_order says"
    with pytest.raises(ModelError) as raised:
        model.image_stack({"a": frames["a"]})
    assert "'b'" in str(raised.value) and "rigs/av3.txt" in str(raised.value)


# ---------------------------------------------------------------------------------------
# 3. the temporal ring
# ---------------------------------------------------------------------------------------


def test_the_ring_spans_the_training_stride_at_the_rate_it_is_read():
    ring = FrameHistory(5, 0.5, 0.05)
    assert (ring.stride, ring.depth) == (10, 41)
    assert ring.sample_index == [0, 10, 20, 30, 40]
    assert ring.spacing_note is None


def test_a_read_interval_that_cannot_divide_the_stride_says_so_rather_than_refusing():
    ring = FrameHistory(5, 0.5, 0.3)
    assert ring.spacing_note is not None and "0.6" in ring.spacing_note
    with pytest.raises(ModelError, match="read interval"):
        FrameHistory(5, 0.5, 0.0)


def test_the_ring_fills_on_the_first_observation_and_then_slides_newest_last():
    ring = FrameHistory(3, 0.2, 0.1)
    frame = np.full((2, 3, 4, 5), 7, dtype=np.uint8)
    ring.observe(frame, np.array([7.0, 0.0], dtype=np.float32))
    images, ego = ring.sampled()
    assert images.shape == (1, 3, 2, 3, 4, 5) and ego.shape == (1, 3, 2)
    assert np.allclose(images, 7 / 255.0)
    for value in (8, 9, 10, 11):
        ring.observe(
            np.full((2, 3, 4, 5), value, dtype=np.uint8),
            np.array([float(value), 0.0], dtype=np.float32),
        )
    images, ego = ring.sampled()
    # Index 0 of the sample is the OLDEST frame in a full ring: `av3_base`'s own ordering.
    assert [round(float(v) * 255) for v in images[0, :, 0, 0, 0, 0]] == [7, 9, 11]
    assert [float(v) for v in ego[0, :, 0]] == [7.0, 9.0, 11.0]
    ring.reset()
    with pytest.raises(ModelError, match="empty"):
        ring.sampled()


def test_the_ring_holds_uint8_and_sampling_creates_the_float():
    ring = FrameHistory(2, 0.1, 0.1)
    ring.observe(np.full((1, 3, 2, 2), 200, dtype=np.uint8), np.zeros(2, dtype=np.float32))
    images, _ = ring.sampled()
    assert images.dtype == np.float32 and np.allclose(images * 255.0, 200.0)


# ---------------------------------------------------------------------------------------
# 4 and 5. the mirror
# ---------------------------------------------------------------------------------------


def test_the_ego_states_lateral_is_right_positive():
    """The car faces due east, so world +y is its left; a velocity with +y in it must come
    back as a NEGATIVE lateral."""
    state = av3_model.ego_state(FakeAgent(velocity=(3.0, 4.0)), (1.0, 1.0))
    assert state[0] == pytest.approx(3.0) and state[1] == pytest.approx(-4.0)
    scaled = av3_model.ego_state(FakeAgent(velocity=(8.09, -0.27)), (8.09, 0.27))
    assert scaled[0] == pytest.approx(1.0) and scaled[1] == pytest.approx(1.0)
    assert state.dtype == np.float32


def test_a_route_bending_left_comes_out_as_negative_right():
    """The fake route curves toward world +y, which for a car facing east is LEFT. The model's
    second column is RIGHT-positive, so it must be negative, `sin(theta)` negative with it,
    `cos(theta)` positive, and curvature -- d(theta)/ds -- negative too."""
    block = av3_model.navigation(FakeAgent(), FakeTrajectory(1.0 / 40.0), 20, 2.0, 20.0)
    assert block.shape == (20, ROUTE_FEATURE_DIM) and block.dtype == np.float32
    assert block[-1, 0] > 0.0 and block[-1, 1] < 0.0
    assert block[-1, 2] > 0.0 and block[-1, 3] < 0.0 and block[-1, 4] < 0.0


def test_a_route_bending_right_mirrors_it_exactly():
    left = av3_model.navigation(FakeAgent(), FakeTrajectory(1.0 / 40.0), 20, 2.0, 20.0)
    right = av3_model.navigation(FakeAgent(), FakeTrajectory(-1.0 / 40.0), 20, 2.0, 20.0)
    assert np.allclose(left[:, 0], right[:, 0]) and np.allclose(left[:, 2], right[:, 2])
    for column in (1, 3, 4):
        assert np.allclose(left[:, column], -right[:, column])


def test_the_route_is_normalised_by_the_windows_own_length_and_flags_its_end():
    block = av3_model.navigation(FakeAgent(), FakeTrajectory(), 20, 2.0, 20.0)
    assert block[-1, 0] * 40.0 == pytest.approx(38.0)
    assert block[-1, 5] == pytest.approx(1.0) and block[-1, 6] == pytest.approx(1.0)
    short = av3_model.navigation(FakeAgent(), FakeTrajectory(length=10.0), 20, 2.0, 20.0)
    assert short[:6, 6].tolist() == [1.0] * 6 and not short[6:, 6].any(), "valid ends at 10 m"
    assert short[-1, 0] * 40.0 == pytest.approx(10.0), "clamped at the end of the route"


def test_a_car_off_its_route_is_fed_zeros_and_no_route_at_all_is_refused():
    class Wide(FakeTrajectory):
        def local_coordinates(self, position):
            return 0.0, 25.0

    assert not av3_model.navigation(FakeAgent(), Wide(), 20, 2.0, 20.0).any()
    with pytest.raises(ModelError, match="route"):
        av3_model.navigation(FakeAgent(), None, 20, 2.0, 20.0)


def test_the_synthetic_arc_bends_the_way_its_sign_says_in_the_models_frame():
    right = av3_model.synthetic_route(20, 2.0, 30.0)
    left = av3_model.synthetic_route(20, 2.0, -30.0)
    assert right.shape == (20, ROUTE_FEATURE_DIM)
    assert right[-1, 1] > 0 and left[-1, 1] < 0, "+radius bends RIGHT, the model's +y"
    assert np.allclose(right[:, 0], left[:, 0]) and np.allclose(right[:, 1], -left[:, 1])
    assert right[0].tolist() == pytest.approx([0.0, 0.0, 1.0, 0.0, 40.0 / 30.0, 0.0, 1.0])


# ---------------------------------------------------------------------------------------
# 6. what the bridge is sent
# ---------------------------------------------------------------------------------------


def test_modelv2_rows_are_the_shape_from_predicted_reads():
    prediction = np.arange(20 * 8, dtype=np.float32).reshape(20, 8)
    rows = av3_model.modelv2_rows(prediction)
    assert len(rows) == 20 and all(len(row) == 9 for row in rows)
    assert rows[0][:2] == [0.0, 1.0] and rows[0][2] == pytest.approx(0.1)
    assert rows[0][3:] == [2.0, 3.0, 4.0, 5.0, 6.0, 7.0]
    assert rows[-1][2] == pytest.approx(MODEL_HORIZON_S)


def test_the_model_output_is_not_flipped_on_the_way_out():
    """The one asymmetry: the model's frame is already the bridge's y-RIGHT. The converter
    measured it end to end with `--nav-sweep`."""
    prediction = np.zeros((4, 8), dtype=np.float32)
    prediction[:, 1] = [1.0, 2.0, 3.0, 4.0]
    assert [row[1] for row in av3_model.modelv2_rows(prediction)] == [1.0, 2.0, 3.0, 4.0]
    assert [p[1] for p in av3_model.waypoints(prediction)] == [1.0, 2.0, 3.0, 4.0]


def test_waypoints_are_sent_as_well_as_modelv2_and_carry_the_same_times():
    prediction = np.arange(20 * 8, dtype=np.float32).reshape(20, 8)
    rows = av3_model.modelv2_rows(prediction)
    points = av3_model.waypoints(prediction)
    assert len(points) == len(rows) == 20
    for point, row in zip(points, rows, strict=True):
        assert point == [row[0], row[1], row[2]]


def test_a_waypoints_only_output_width_is_refused_rather_than_reinterpreted():
    with pytest.raises(ModelError, match=str(MODELV2_OUTPUT_WIDTH)):
        av3_model.modelv2_rows(np.zeros((20, 2), dtype=np.float32))
    with pytest.raises(ModelError):
        av3_model.waypoint_times(0)
    assert av3_model.waypoint_times(4) == pytest.approx([0.5, 1.0, 1.5, 2.0])


# ---------------------------------------------------------------------------------------
# the model object, without a GPU
# ---------------------------------------------------------------------------------------


def test_the_model_refuses_to_predict_before_loading_and_names_a_missing_checkpoint(tmp_path):
    model = AV3Model(config(), tmp_path / "missing.ep", 0.05)
    assert model.n_waypoints is None
    with pytest.raises(ModelError, match="before load"):
        model.predict(FakeAgent(), FakeTrajectory())
    with pytest.raises(ModelError, match="before load"):
        model.predict_with_navigation(np.zeros((20, 7)))
    with pytest.raises(ModelError, match="no checkpoint at"):
        model.load()
    model.close()


def test_loading_a_checkpoint_without_torch_says_so(tmp_path):
    """The host has no torch and the sim image does; the refusal names the module and the
    machine rather than a traceback into an import."""
    pytest.importorskip("numpy")
    try:
        import torch_tensorrt  # noqa: F401
    except ImportError:
        pass
    else:
        pytest.skip("torch_tensorrt is importable here; the load would proceed")
    weights = tmp_path / "x.ep"
    weights.write_bytes(b"not an engine")
    with pytest.raises(ModelError, match="torch"):
        AV3Model(config(), weights, 0.05).load()
