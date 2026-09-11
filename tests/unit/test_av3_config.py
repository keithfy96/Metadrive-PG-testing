"""`av3/av3_model.py`'s config: the submission's `model_dev.yml`, with nothing defaulted.

Two repos beside this one ship a file of that name with different schemas, and the AV3 loader
requires every field it reads and defaults none -- deliberately. A silently-defaulted
`frame_stride_s` is the exact failure the shipped file's own comment warns about: the model
runs, on history spaced differently to how it was trained, and the run still scores. So the
test that matters here is the one Phase 4 Step 7's verify block names: delete one field from a
copy of the submitted config and `load_config` raises naming it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scenariobank.av3 import av3_model
from scenariobank.av3.av3_model import (
    MODEL_CONFIG_VARIABLE,
    REQUIRED_KEYS,
    Config,
    ModelError,
    config_path,
    load_config,
)

#: The submitted config as it sits on this machine, beside the 1.2 GB checkpoint it names.
SUBMITTED = Path("../models/model_dev.yml")
needs_submission = pytest.mark.skipif(
    not SUBMITTED.exists(), reason=f"needs the submitted config at {SUBMITTED}"
)

#: The submitted config's `model:` block, values verbatim, so every test here runs on a machine
#: that does not have the submission.
MODEL_BLOCK = """\
model:
  type: av3_trt
  route_path: town10_route.parquet
  n_route: 20
  route_spacing_m: 2.0
  route_max_offset_m: 20
  checkpoint: step_440000_trt_direct_full.ep
  device: cuda
  ego_velocity_scale: [8.09, 0.27]
  waypoint_reference: axle_mid
  camera_order: [front_middle, front_right, rear_right, rear_middle, rear_left, front_left]
  expected_camera_image_width: 512
  expected_camera_image_height: 288
  t_frames: 5
  frame_stride_s: 0.5
"""


def write_config(tmp_path: Path, text: str = MODEL_BLOCK) -> Path:
    path = tmp_path / "model_dev.yml"
    path.write_text(text)
    return path


def test_the_submitted_block_loads_and_reads_as_the_weights_expect(tmp_path):
    config = load_config(write_config(tmp_path))
    assert config.camera_order == [
        "front_middle", "front_right", "rear_right", "rear_middle", "rear_left", "front_left"
    ]
    assert (config.t_frames, config.frame_stride_s) == (5, 0.5)
    assert config.ego_velocity_scale == (8.09, 0.27)
    assert (config.n_route, config.route_spacing_m, config.route_max_offset_m) == (20, 2.0, 20.0)
    assert (config.image_width, config.image_height) == (512, 288)
    assert config.waypoint_reference == "axle_mid"
    assert config.checkpoint_name == "step_440000_trt_direct_full.ep"
    assert config.path == str(tmp_path / "model_dev.yml")


@pytest.mark.parametrize("key", REQUIRED_KEYS)
def test_deleting_one_field_from_a_copy_of_the_config_is_refused_naming_it(tmp_path, key):
    """The verify block's test: nothing here is defaulted, and the refusal says which field."""
    lines = [line for line in MODEL_BLOCK.splitlines() if not line.startswith(f"  {key}:")]
    assert len(lines) == len(MODEL_BLOCK.splitlines()) - 1, key
    path = write_config(tmp_path, "\n".join(lines) + "\n")
    with pytest.raises(ModelError) as raised:
        load_config(path)
    assert key in str(raised.value)
    assert str(path) in str(raised.value)


def test_every_required_key_is_refused_by_name_on_the_block_itself():
    for key in REQUIRED_KEYS:
        values = {name: 1 for name in REQUIRED_KEYS}
        values.update(camera_order=["a"], ego_velocity_scale=[1.0, 1.0], n_route=2)
        del values[key]
        with pytest.raises(ModelError) as raised:
            Config(values)
        assert key in str(raised.value)


def test_a_scalar_velocity_scale_and_a_repeated_camera_are_refused():
    values = {name: 1 for name in REQUIRED_KEYS}
    values.update(camera_order=["a"], ego_velocity_scale=8.09, n_route=2)
    with pytest.raises(ModelError, match="ego_velocity_scale"):
        Config(values)
    values.update(camera_order=["a", "a"], ego_velocity_scale=[1.0, 1.0])
    with pytest.raises(ModelError, match="repeats"):
        Config(values)


def test_a_file_with_no_model_block_and_a_file_that_is_not_yaml_are_refused(tmp_path):
    with pytest.raises(ModelError, match="`model:` block"):
        load_config(write_config(tmp_path, "controller:\n  selected: learned\n"))
    with pytest.raises(ModelError, match="not YAML"):
        load_config(write_config(tmp_path, "model: [unclosed\n"))
    with pytest.raises(ModelError, match="no model config at"):
        load_config(tmp_path / "missing.yml")


def test_there_is_no_default_path_only_the_flag_and_the_environment(monkeypatch, tmp_path):
    """The converter defaulted to its own checkout's copy; here the file is a contract with one
    set of weights and the wrong one runs and scores, so silence is a refusal naming both ways
    to say which."""
    monkeypatch.delenv(MODEL_CONFIG_VARIABLE, raising=False)
    with pytest.raises(ModelError) as raised:
        config_path(None)
    assert "--model-config" in str(raised.value) and MODEL_CONFIG_VARIABLE in str(raised.value)
    with pytest.raises(ModelError, match="no model config"):
        load_config()
    path = write_config(tmp_path)
    monkeypatch.setenv(MODEL_CONFIG_VARIABLE, str(path))
    assert config_path(None) == str(path)
    assert load_config().t_frames == 5
    assert config_path("elsewhere.yml") == "elsewhere.yml", "the flag wins over the environment"


def test_the_checkpoint_is_the_flag_or_the_environment_and_never_the_configs_name(monkeypatch):
    monkeypatch.delenv(av3_model.MODEL_CHECKPOINT_VARIABLE, raising=False)
    with pytest.raises(ModelError, match="--checkpoint"):
        av3_model.checkpoint_path(None)
    monkeypatch.setenv(av3_model.MODEL_CHECKPOINT_VARIABLE, "/models/x.ep")
    assert av3_model.checkpoint_path(None) == "/models/x.ep"
    assert av3_model.checkpoint_path("given.ep") == "given.ep"


@needs_submission
def test_the_submitted_config_on_this_machine_is_the_block_pinned_above():
    """The file beside the checkpoint and the text this test carries are one config."""
    real = load_config(SUBMITTED)
    assert real.camera_order == Config(_block_values()).camera_order
    for name in ("t_frames", "frame_stride_s", "n_route", "route_spacing_m", "image_width",
                 "image_height", "ego_velocity_scale", "checkpoint_name"):
        assert getattr(real, name) == getattr(Config(_block_values()), name), name


def _block_values() -> dict:
    import yaml

    return yaml.safe_load(MODEL_BLOCK)["model"]
