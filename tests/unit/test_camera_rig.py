"""Phase 4 Step 6: the camera rig, six cameras alive on `DefaultVehicle`.

Split the way the rest of the suite is. The **offline** half is the spec: `rigs/av3.txt` reads
into six cameras whose mounts are the CARLA numbers swapped and whose headings are the yaws
negated, every refusal `_parse` makes, the buffer ceiling, and `mount`'s refusal of an env whose
cameras MetaDrive has already deleted. It runs with no simulator. The **`needs_sim`** half builds
the rig onto `banks/curve` once and reads it: the six sensors and no `rgb_camera`, six buffers,
the observation still 19 wide, six frames of the spec's size that are pictures and not each
other, the vehicle frame the conversion rests on re-measured on the mirrored road, and the
expert's actions identical with the rig mounted and without -- the cameras never enter the
observation, so a rig changes nothing a run scores.

The live half is expensive on purpose -- an env with `image_observation` on opens an offscreen
window and costs about 15 s to reset against a quarter of a second without -- so it builds
exactly two rig envs: one through `replay.drive`, which is the command, and one by hand, which
is everything else.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scenariobank.av3.camera_rig import (
    CAMERA_FILTER,
    MAX_IMAGE_BUFFERS,
    Camera,
    CameraRig,
    FrameCheck,
    RigError,
    RigReport,
    _parse,
    format_frame,
    load_rig,
    report_for,
)
from scenariobank.doctor import has_simulator

needs_sim = pytest.mark.skipif(
    not has_simulator(),
    reason="needs_sim: MetaDrive is not installed (uv sync --group sim)",
)

AV3 = Path("rigs/av3.txt")
CURVE = Path("banks/curve")

needs_curve = pytest.mark.skipif(
    not (CURVE / "manifest.json").exists(), reason=f"needs the bank at {CURVE}"
)

#: `model_dev.yml`'s `camera_order`: a contract with the weights, kept exactly.
AV3_ORDER = ["front_middle", "front_right", "rear_right", "rear_middle", "rear_left", "front_left"]

#: The AV3 stack's own rate, which `--step-hz 100 --decision-hz 20` (Step 7) is what produces.
AV3_TICK_S = 0.05


def spec(*cameras: str) -> str:
    return "sensors:\n" + "\n".join(cameras)


def camera(name: str, **overrides: object) -> str:
    fields = {
        "x": 1.0, "y": 0.0, "z": 1.5, "pitch": 0, "yaw": 0, "roll": 0,
        "width": 64, "height": 48, "fov": 60, "type": "rgb_camera",
    }
    fields.update(overrides)
    transform = "\n".join(
        f"      {key}: {fields.pop(key)}" for key in ("x", "y", "z", "pitch", "yaw", "roll")
    )
    rest = "\n".join(f"    {key}: {value}" for key, value in fields.items())
    return f"  - name: {name}\n    transform:\n{transform}\n{rest}"


# --- offline: the spec ---------------------------------------------------------------------


def test_the_av3_spec_reads_into_six_cameras_in_the_weights_own_order():
    rig = load_rig(AV3)
    assert rig.names == AV3_ORDER
    assert len(rig) == 6
    assert rig.tick_rate_s == AV3_TICK_S
    assert rig.image_source() == "front_middle", "the first camera, so no rgb_camera is added"
    assert all((c.width, c.height) == (512, 384) for c in rig.cameras), "4:3, squashed later"
    assert rig.megabytes == pytest.approx(6 * 512 * 384 * 3 / 1e6)


def test_the_conversion_is_a_swap_and_a_flip_and_not_a_rename():
    """CARLA x forward / y right / +yaw right -> MetaDrive x right / y forward / +heading left.
    Pitch passes through: both quote nose-up positive, measured (`check_frame`)."""
    by_name = {c.name: c for c in load_rig(AV3).cameras}
    front = by_name["front_middle"]
    assert front.position == (0.0, 0.2244, 1.393)
    assert front.hpr == (0.0, -5.0, 0.0)
    assert str(front.hpr[0]) == "0.0", "a yaw of 0 does not come back as -0.0"
    right = by_name["front_right"]
    assert right.position == (0.468, 0.1844, 1.392), "CARLA's y (right) is MetaDrive's x"
    assert right.hpr == (-53.7, -10.0, 0.0), "CARLA's +yaw (right) is a negative heading"
    left = by_name["rear_left"]
    assert left.position == (-0.412, -1.3016, 1.462)
    assert left.hpr == (116.9, -10.0, 0.0)
    assert by_name["rear_middle"].hpr[0] == -180.0


def test_every_av3_camera_aims_where_its_name_says():
    """The converter's older spec had two side cameras named backwards; av3.txt was generated so
    the yaw column and the names agree, and `aim` is what keeps that visible."""
    aims = {c.name: c.aim for c in load_rig(AV3).cameras}
    assert aims["front_middle"] == "straight ahead"
    assert aims["rear_middle"] == "straight behind"
    for name in ("front_right", "rear_right", "rear_left", "front_left"):
        quarter, side = name.split("_")
        assert aims[name].endswith(f"{quarter}-{side}"), (name, aims[name])


def test_describe_prints_one_line_per_camera_with_its_aim_and_the_declared_rate():
    lines = load_rig(AV3).describe()
    assert lines[0] == "6 camera(s) from rigs/av3.txt"
    assert len(lines) == 2 + 6 + 2
    assert "front_left" in lines[7] and "front-left" in lines[7] and "H  +53.7" in lines[7]
    assert "tick_rate 0.05 s (20 Hz)" in lines[8]
    assert lines[9].endswith("MB of uint8 image per read")


def test_the_rate_check_compares_the_spec_with_the_interval_it_is_read_at():
    load_rig(AV3, read_interval_s=AV3_TICK_S)
    load_rig(AV3, read_interval_s=None)
    with pytest.raises(RigError, match=r"read every 0\.1 s \(10 Hz\)"):
        load_rig(AV3, read_interval_s=0.1)


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("cameras:\n" + camera("a"), "expected `sensors:` at the top level"),
        ("  - name: a\n", "content before `sensors:`"),
        ("# nothing\n", "no `sensors:` key"),
        (spec(camera("a", type="depth_camera")), "type is 'depth_camera'; only rgb_camera"),
        (spec(camera("a").replace("    width: 64\n", "")), "a: no width"),
        (spec(camera("a").replace("      z: 1.5\n", "")), "a: transform has no z"),
        (spec(camera("a", roll=2)), "a: roll 2.0. Only yaw and pitch are converted"),
        (spec(camera("a", fov="wide")), "a: fov is 'wide', which is not a number"),
        (spec(camera("a"), camera("a")), "duplicate camera name\\(s\\): a"),
        (spec(camera("a", tick_rate=0.1), camera("b", tick_rate=0.05)), "cannot ask for two"),
        (spec(camera("a", tick_rate=0.05)).replace("\n    transform:", "\n    transform: {}"),
         "inline `transform:` is not supported"),
        ("sensors:\n  - name a\n", "is not `key: value`"),
        ("sensors:\n    name: a\n", "is not inside a `- ` list item"),
        ("sensors:\n", "the spec defines no cameras"),
    ],
)
def test_a_spec_that_is_not_the_one_shape_is_refused_by_name(text, reason):
    with pytest.raises(RigError, match=reason):
        _parse(text, read_interval_s=0.1)


def test_a_declared_rate_that_is_not_the_read_interval_is_refused_and_says_both():
    with pytest.raises(RigError, match=r"tick_rate 0\.05 s \(20 Hz\).*read every 0\.1 s"):
        _parse(spec(camera("a", tick_rate=0.05)), read_interval_s=0.1)
    assert _parse(spec(camera("a", tick_rate=0.05)), read_interval_s=0.05).tick_rate_s == 0.05


def test_more_cameras_than_panda3d_is_reliable_for_is_refused_at_parse_time():
    nine = spec(*(camera(f"c{i}") for i in range(MAX_IMAGE_BUFFERS)))
    assert len(_parse(nine)) == MAX_IMAGE_BUFFERS == 9
    ten = spec(*(camera(f"c{i}") for i in range(MAX_IMAGE_BUFFERS + 1)))
    with pytest.raises(RigError, match="10 cameras, and panda3d is reliable to 9"):
        _parse(ten)


def test_a_missing_spec_is_named():
    with pytest.raises(RigError, match="no rig spec at"):
        load_rig("rigs/nowhere.txt")


def test_a_comment_and_a_blank_line_are_not_content():
    rig = _parse("# a header\n\nsensors:\n" + camera("a", fov="70   # fisheye fallback"))
    assert rig.cameras[0].fov == 70.0


# --- offline: the rig on an env ------------------------------------------------------------


def one_camera_rig() -> CameraRig:
    return CameraRig([Camera("cam", (0.0, 1.0, 1.5), (0.0, 0.0, 0.0), 64, 48, 60.0, 0.0)])


def test_mounting_on_an_env_whose_cameras_metadrive_deleted_is_refused_naming_the_line():
    """`base_env.py:343-346` filters every camera out of a headless env's sensors when
    `image_observation` is off. The refusal comes before `get_sensor` would raise."""
    env = SimpleNamespace(config={"image_observation": False})
    with pytest.raises(RigError, match=CAMERA_FILTER):
        one_camera_rig().mount(env)
    assert CAMERA_FILTER == "base_env.py:343"


def test_reading_before_mounting_is_refused():
    with pytest.raises(RigError, match="read\\(\\) before mount"):
        one_camera_rig().read()


def test_the_report_round_trips_through_json_and_carries_the_probe_rows():
    rows = [FrameCheck(label="local +y is 1 m forward", ok=True, detail="ahead +1.000 m")]
    report = report_for(load_rig(AV3), rows)
    again = RigReport.model_validate(json.loads(report.model_dump_json()))
    assert again == report
    assert [c.name for c in again.cameras] == AV3_ORDER
    assert again.cameras[1].position == (0.468, 0.1844, 1.392)
    assert again.frame == rows
    assert report_for(load_rig(AV3)).frame is None


def test_a_failed_probe_row_is_printed_as_a_failure_with_the_consequence():
    rows = [
        FrameCheck(label="H=+55 turns left", ok=True, detail="+55.00 deg"),
        FrameCheck(label="P=+10 tilts up", ok=False, detail="-10.00 deg"),
    ]
    lines = format_frame(rows)
    assert lines[0].startswith("  ok  ") and lines[1].startswith("  FAIL")
    assert "Every rig mount and aim is wrong" in lines[-1]
    assert len(format_frame(rows[:1])) == 1


# --- offline: the commands -----------------------------------------------------------------


def run_cli(*argv):
    from typer.testing import CliRunner

    from scenariobank.cli import app

    return CliRunner().invoke(app, list(argv))


def test_the_rig_command_describes_a_spec_without_a_simulator():
    result = run_cli("rig", "--camera-rig", str(AV3))
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[0] == "6 camera(s) from rigs/av3.txt"
    assert "front_left" in result.output and "aims 54 deg to the left" in result.output
    as_json = run_cli("rig", "--camera-rig", str(AV3), "--json")
    assert RigReport.model_validate(json.loads(as_json.output)).frame is None


def test_the_rig_command_refuses_a_bad_spec_and_a_probe_with_no_bank(tmp_path):
    bad = tmp_path / "bad.txt"
    bad.write_text(spec(camera("a", roll=5)))
    result = run_cli("rig", "--camera-rig", str(bad))
    assert result.exit_code == 1
    assert "rig spec rejected: a: roll 5.0" in result.output
    result = run_cli("rig", "--camera-rig", str(AV3), "--check-frame")
    assert result.exit_code == 2
    assert "--check-frame needs --bank" in result.output


def test_replay_refuses_the_av3_rig_on_a_10_hz_road_before_building_anything(tmp_path):
    """The AV3 rig declares 0.05 s and a road reads it every 0.1 s at most; nothing resamples,
    so the refusal is off the manifest and needs no simulator. `--ignore-rig-rate` is the
    switch for looking anyway, and `run` has no such switch."""
    from test_replay import procedural

    from scenariobank.bank import write_manifest

    write_manifest(tmp_path, procedural())
    result = run_cli("replay", "--bank", str(tmp_path), "--camera-rig", str(AV3))
    assert result.exit_code == 1
    assert "tick_rate 0.05 s (20 Hz), but these cameras are read every 0.1 s" in result.output
    assert "--ignore-rig-rate" in run_cli("replay", "--help").output
    assert "--ignore-rig-rate" not in run_cli("run", "--help").output


# --- live: six cameras alive ----------------------------------------------------------------


@needs_sim
@needs_curve
def test_the_rig_replay_reports_six_live_cameras_and_no_rgb_camera():
    """`replay --camera-rig`, the command: the env's sensors are the six and never the
    default `rgb_camera`; six buffers, one under the ceiling by three; the observation is
    still 19 wide; every camera returned a frame of the spec's size at every decision."""
    from scenariobank.bank import read_manifest
    from scenariobank.config import OBSERVATION_SHAPE
    from scenariobank.replay import drive

    episode = drive(CURVE, read_manifest(CURVE), steps=10, camera_rig=AV3, ignore_rig_rate=True)
    report = episode.env
    assert report is not None
    assert report.sensors == sorted([*AV3_ORDER, "lane_line_detector", "lidar", "side_detector"])
    assert "rgb_camera" not in report.sensors
    assert (report.image_observation, report.image_source) == (True, "front_middle")
    assert report.image_buffers == 6 <= MAX_IMAGE_BUFFERS
    assert (report.rig, report.rig_cameras) == (str(AV3), AV3_ORDER)
    assert (report.rig_tick_rate_s, report.read_interval_s) == (AV3_TICK_S, 0.1)
    assert report.frames == {name: (384, 512, 3) for name in AV3_ORDER}
    assert report.reads == episode.steps + 1 == 11, "once per decision, the reset included"
    assert report.ms_per_read > 0
    assert episode.observation_shape == episode.observation_shape_end == OBSERVATION_SHAPE
    assert episode.steps == 10 and episode.ended_by == "capped short"
    # MetaDrive's offscreen mode writes `PYTHONUTF8=on` into the environment
    # (`asset_loader.py:116`),
    # a value CPython refuses at startup, so every subprocess after a rig env would die; the seam
    # puts the variable back (`env._restore_utf8_variable`). Checked here, on the process that
    # just built one, and by the subprocess tests that run after this file in the suite.
    assert os.environ.get("PYTHONUTF8") != "on"
    subprocess.run([sys.executable, "-c", "pass"], check=True)


@needs_sim
@needs_curve
def test_the_mounted_rig_reads_six_pictures_and_the_frame_is_what_the_conversion_assumes():
    """One rig env by hand: the frames are pictures and not each other, `check_frame`'s six
    rows hold on the mirrored road, and the expert drives `curve_0000` identically with the
    rig on and off -- the cameras are read off the engine, never through the observation."""
    import numpy as np

    from scenariobank.av3.camera_rig import check_frame, image_buffers
    from scenariobank.bank import read_manifest
    from scenariobank.env import build_env, seed_for
    from scenariobank.options import resolve_options
    from scenariobank.policies import load_policy
    from scenariobank.runner import bind_policy, run_episode

    manifest = read_manifest(CURVE)
    entry = manifest.categories["curve"]
    row = entry.scenarios[0]
    options = resolve_options(manifest)
    cap = 60

    def expert_actions(rig):
        env, prepare = build_env(CURVE, entry, options, rig=rig)
        try:
            act = load_policy("scenariobank.policies:ExpertPolicy")
            bind_policy(act, env)
            drive = run_episode(
                env, seed=seed_for(row), prepare=lambda built: prepare(built, row),
                cap=cap, stride=1, act=act,
            )
            return env, drive
        except BaseException:
            env.close()
            raise

    rig = load_rig(AV3, read_interval_s=None)
    env, with_rig = expert_actions(rig)
    try:
        assert image_buffers(env) == 6
        frames = rig.read()
        assert list(frames) == AV3_ORDER
        for name, frame in frames.items():
            assert frame.shape == (384, 512, 3) and frame.dtype == np.uint8, name
            assert frame.std() > 10, f"{name} is a blank frame"
        pairs = [(a, b) for i, a in enumerate(AV3_ORDER) for b in AV3_ORDER[i + 1 :]]
        for a, b in pairs:
            assert np.abs(frames[a].astype(int) - frames[b].astype(int)).mean() > 5, (a, b)
        rows = check_frame(env)
        assert [r.label for r in rows] == [
            "local +y is 1 m forward", "local +x is 1 m right", "H=+55 turns left",
            "H=-55 turns right", "P=+10 tilts up", "P=-10 tilts down",
        ]
        assert all(r.ok for r in rows), format_frame(rows)
    finally:
        env.close()

    plain_env, without = expert_actions(None)
    plain_env.close()
    assert with_rig.issued_actions == without.issued_actions
    assert with_rig.steps == without.steps == cap
    assert with_rig.observation_shape == without.observation_shape == (19,)
