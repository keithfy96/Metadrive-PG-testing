"""`av3/policy.py`, the run's `setup` hook, and `--step-hz`: the AV3 stack on a scored run.

Offline: every refusal `AV3Policy.setup` makes before an env is built, the bridge address, the
`Job` fields, the flags on `run` and `replay`, and the step-rate arithmetic (`env.step_hz_for`,
`env.budget_at`). Live: `BridgePolicy` drives `t_junction_0000` at `--step-hz 100
--decision-hz 20` through `StubBridge` -- the whole path from the bank's route to the bridge's
pedals, no model, no GPU -- and arrives, with `actions == ceil(steps / 5)`; and a road built at
100 Hz carries the two MetaDrive keys with the budget scaled, where the default leaves both
alone (the pin `test_env.py` holds).
"""

from __future__ import annotations

import math
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_av3_config import write_config
from test_env import pg_entry, pg_manifest, recorded_entry, row

from scenariobank.av3.policy import (
    AV3Policy,
    BridgePolicy,
    bridge_address,
)
from scenariobank.av3.probe import distance_to_path, ego_frame
from scenariobank.doctor import has_simulator
from scenariobank.env import DEFAULT_STEP_HZ, budget_at, build_config, step_hz_for
from scenariobank.policies import PolicyError, load_policy
from scenariobank.results import JOB_SCHEMA_VERSION, Job, JobBank, dump_json
from scenariobank.runner import RunSetup, close_policy, run_bank, setup_policy

needs_sim = pytest.mark.skipif(
    not has_simulator(), reason="needs_sim: MetaDrive is not installed (uv sync --group sim)"
)
T_JUNCTION = Path("banks/t-junction")
needs_t_junction = pytest.mark.skipif(
    not (T_JUNCTION / "manifest.json").exists(), reason=f"needs the bank at {T_JUNCTION}"
)
AV3 = Path("rigs/av3.txt")


class FakeRig:
    def __init__(self, names):
        self.names = list(names)
        self.path = "fake-rig.txt"
        self.tick_rate_s = 0.05


def setup_for(**overrides) -> RunSetup:
    fields = dict(step_hz=100.0, stride=5, rig=FakeRig(AV3_ORDER), ignore_rig_rate=False)
    fields.update(overrides)
    return RunSetup(**fields)


AV3_ORDER = ["front_middle", "front_right", "rear_right", "rear_middle", "rear_left", "front_left"]


# --- offline: what the policies refuse, and where the bridge is ------------------------------


def test_the_policies_load_by_spec_and_take_the_checkpoint_keyword(monkeypatch):
    monkeypatch.delenv("AV3_BRIDGE", raising=False)
    assert isinstance(load_policy("scenariobank.av3:AV3Policy"), AV3Policy)
    assert isinstance(load_policy("scenariobank.av3:BridgePolicy"), BridgePolicy)
    policy = load_policy("scenariobank.av3:AV3Policy", checkpoint_path="/models/x.ep")
    assert policy.checkpoint_given == "/models/x.ep"
    assert (policy.driver.bridge.host, policy.driver.bridge.port) == ("127.0.0.1", 5558)


def test_the_bridge_address_is_the_argument_then_the_environment_then_the_default(monkeypatch):
    monkeypatch.delenv("AV3_BRIDGE", raising=False)
    assert bridge_address() == ("127.0.0.1", 5558)
    monkeypatch.setenv("AV3_BRIDGE", "10.0.0.7:6000")
    assert bridge_address() == ("10.0.0.7", 6000)
    assert bridge_address(":7000") == ("127.0.0.1", 7000)
    with pytest.raises(PolicyError, match="host:port"):
        bridge_address("nonsense")
    monkeypatch.setenv("AV3_TARGET_SPEED_MPS", "7.5")
    monkeypatch.setenv("AV3_LONGITUDINAL", "pedal")
    policy = BridgePolicy()
    assert policy.driver.target_speed_mps == 7.5 and policy.driver.longitudinal == "pedal"
    monkeypatch.setenv("AV3_LONGITUDINAL", "table")
    with pytest.raises(PolicyError, match="longitudinal"):
        BridgePolicy()


def test_av3_refuses_a_run_with_no_rig_before_any_env(tmp_path, monkeypatch):
    monkeypatch.delenv("AV3_BRIDGE", raising=False)
    policy = AV3Policy(model_config=str(write_config(tmp_path)))
    with pytest.raises(PolicyError, match="--camera-rig"):
        policy.setup(setup_for(rig=None))


def test_av3_refuses_ignore_rig_rate_because_that_switch_is_for_a_film(tmp_path):
    policy = AV3Policy(model_config=str(write_config(tmp_path)))
    with pytest.raises(PolicyError) as raised:
        policy.setup(setup_for(ignore_rig_rate=True))
    assert "--ignore-rig-rate" in str(raised.value) and "--step-hz 100" in str(raised.value)


def test_av3_refuses_a_missing_config_a_missing_checkpoint_and_a_rig_short_of_a_camera(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("MODEL_CONFIG", raising=False)
    monkeypatch.delenv("MODEL_CHECKPOINT", raising=False)
    with pytest.raises(PolicyError, match="--model-config"):
        AV3Policy().setup(setup_for())
    config = str(write_config(tmp_path))
    with pytest.raises(PolicyError, match="--checkpoint"):
        AV3Policy(model_config=config).setup(setup_for())
    with pytest.raises(PolicyError, match="no checkpoint at"):
        AV3Policy(checkpoint_path=str(tmp_path / "x.ep"), model_config=config).setup(setup_for())
    weights = tmp_path / "x.ep"
    weights.write_bytes(b"engine")
    policy = AV3Policy(checkpoint_path=str(weights), model_config=config)
    with pytest.raises(PolicyError) as raised:
        policy.setup(setup_for(rig=FakeRig(AV3_ORDER[:-1])))
    assert "front_left" in str(raised.value) and "contract" in str(raised.value)
    # The run's config wins over the constructor's, the way `--model-config` reaches it.
    notes = policy.setup(setup_for(model_config=config))
    assert notes == [] and policy.model is not None and policy.model.n_waypoints is None
    assert policy.model.history.stride == 10, "0.5 s of training stride at 0.05 s reads"


def test_a_decision_rate_that_is_not_the_bridges_tick_is_a_note_not_a_refusal(tmp_path):
    bridge = BridgePolicy()
    assert bridge.setup(setup_for(step_hz=10.0, stride=1, rig=None)) == [
        note for note in bridge.notes
    ]
    assert len(bridge.notes) == 1 and "_DT_MDL" in bridge.notes[0]
    assert bridge.setup(setup_for()) == []
    weights = tmp_path / "x.ep"
    weights.write_bytes(b"engine")
    av3 = AV3Policy(checkpoint_path=str(weights), model_config=str(write_config(tmp_path)))
    notes = av3.setup(setup_for(step_hz=10.0, stride=1))
    assert any("_DT_MDL" in note for note in notes)
    assert not any("frame_stride_s" in note for note in notes), "0.1 s divides 0.5 s: no note"
    notes = av3.setup(setup_for(step_hz=10.0, stride=3))
    assert any("frame_stride_s" in note and "0.6" in note for note in notes), "0.3 s does not"


def test_a_policy_that_is_not_bound_or_not_set_up_says_so():
    with pytest.raises(PolicyError, match="not bound"):
        BridgePolicy()([0.0] * 19)
    policy = AV3Policy()
    policy.bind(SimpleNamespace(agent=None))
    with pytest.raises(PolicyError, match="setup"):
        policy([0.0] * 19)


def test_the_runners_hooks_are_optional_and_called_in_order():
    calls = []

    class Hooked:
        def setup(self, run):
            calls.append(("setup", run.decision_interval_s))
            return ["a note", 7]

        def __call__(self, observation):
            return (0.0, 0.0)

        def close(self):
            calls.append(("close",))

    policy = Hooked()
    assert setup_policy(policy, setup_for()) == ["a note", "7"]
    close_policy(policy)
    assert calls == [("setup", 0.05), ("close",)]
    assert setup_policy(lambda observation: (0.0, 0.0), setup_for()) == []
    close_policy(lambda observation: (0.0, 0.0))


# --- offline: the step rate ------------------------------------------------------------------


def test_a_road_steps_at_the_rate_asked_and_a_recording_only_at_its_own():
    entry = pg_entry([0])
    assert step_hz_for(entry) == DEFAULT_STEP_HZ == 10.0
    assert step_hz_for(entry, 100.0) == 100.0
    recorded = recorded_entry([row()])
    assert step_hz_for(recorded, 100.0) == 100.0, "restating the recording's rate is allowed"
    with pytest.raises(ValueError, match="recording"):
        step_hz_for(recorded, 50.0)
    with pytest.raises(ValueError, match="positive"):
        step_hz_for(entry, 0.0)


def test_a_budget_sized_at_ten_hertz_is_the_same_seconds_at_a_hundred():
    entry = pg_entry([0], max_steps=320)
    assert budget_at(320, entry) == 320
    assert budget_at(320, entry, 100.0) == 3200
    assert budget_at(321, entry, 25.0) == math.ceil(321 * 2.5), "rounded up, never cut short"
    assert budget_at(400, recorded_entry([row()]), 100.0) == 400, "a recording's frames"


def test_the_job_carries_the_rate_and_the_config_and_an_old_job_still_validates():
    job = Job(
        schema_version=JOB_SCHEMA_VERSION, bank=JobBank(path="banks/x"),
        policy="scenariobank.av3:AV3Policy", step_hz=100.0, decision_hz=20.0,
        model_config_path="sub/model_dev.yml", checkpoint_path="sub/model.ep",
    )
    again = Job.model_validate_json(dump_json(job))
    assert (again.step_hz, again.model_config_path) == (100.0, "sub/model_dev.yml")
    old = Job.model_validate({"schema_version": 1, "bank": {"path": "b"}, "policy": "p:P"})
    assert old.step_hz is None and old.model_config_path is None


def test_run_and_replay_take_step_hz_and_run_takes_the_model_config():
    from typer.testing import CliRunner

    from scenariobank.cli import app

    runner = CliRunner()
    run_help = runner.invoke(app, ["run", "--help"]).output
    assert "--step-hz" in run_help and "--model-config" in run_help
    assert "--step-hz" in runner.invoke(app, ["replay", "--help"]).output
    av3_help = runner.invoke(app, ["av3", "--help"]).output
    assert "--no-model" in av3_help and "--nav-sweep" in av3_help


def test_the_probes_geometry_helpers():
    assert ego_frame((1.0, 1.0), math.pi / 2, (1.0, 3.0)) == pytest.approx((2.0, 0.0))
    assert ego_frame((0.0, 0.0), 0.0, (2.0, 1.0)) == pytest.approx((2.0, 1.0))
    path = [(0.0, 0.0), (10.0, 0.0)]
    assert distance_to_path((5.0, 2.0), path) == pytest.approx(2.0)
    assert distance_to_path((12.0, 0.0), path) == pytest.approx(2.0), "past the end: the end"


# --- live: the bridge path on a road, and a road at 100 Hz -----------------------------------


@needs_sim
def test_a_road_built_at_a_hundred_hertz_sets_the_two_keys_and_scales_the_horizon():
    """The other half of `test_env.py`'s pin: the default leaves the keys alone, an asked rate
    sets them, one physics step per env.step, the way a recording is stepped."""
    entry = pg_entry([0], max_steps=320)
    config = build_config(Path("."), entry, _resolved(entry), 100.0)
    assert config["physics_world_step_size"] == pytest.approx(0.01)
    assert config["decision_repeat"] == 1
    assert config["horizon"] == 3200
    same = build_config(Path("."), entry, _resolved(entry), DEFAULT_STEP_HZ)
    assert "physics_world_step_size" not in same and same["horizon"] == 320


def _resolved(entry):
    from scenariobank.options import resolve_options

    return resolve_options(pg_manifest(entry))


@needs_sim
@needs_t_junction
def test_the_bridge_policy_drives_the_t_junction_through_the_stub_at_100_20(tmp_path, monkeypatch):
    """The whole path with the model taken out: the bank's route projected into the car's
    frame, mirrored into the bridge's, pure pursuit over a real socket, the reply negated back
    -- at the AV3 stack's own clock. The car arrives, one action per five steps, the record
    says 100 Hz and stride 5, and the bridge was told the car's real geometry."""
    from scenariobank.av3.openpilot_policy import StubBridge

    stub = StubBridge()
    monkeypatch.setenv("AV3_BRIDGE", f"{stub.host}:{stub.port}")
    notes: list[str] = []
    try:
        report = run_bank(
            Job(
                schema_version=JOB_SCHEMA_VERSION, bank=JobBank(path=str(T_JUNCTION)),
                scenarios=["t_junction_0000"], policy="scenariobank.av3:BridgePolicy",
                decision_hz=20.0, step_hz=100.0,
            ),
            tmp_path / "out",
            progress=notes.append,
        )
    finally:
        stub.close()
    result = report.results[0]
    assert result.status == "ok", result.traceback
    assert result.success and result.failure_reason is None
    assert result.actions == math.ceil(result.steps / 5)
    assert 800 < result.steps < 3200, "a 12 s drive in 100 Hz steps, under the scaled budget"
    assert (report.env.step_hz, report.env.decision_hz, report.env.stride) == (100.0, 20.0, 5)
    assert stub.steps == result.actions
    assert stub.inits[0]["max_steer_angle"] == 40.0
    assert stub.inits[0]["wheelbase_m"] == pytest.approx(1.05234 + 1.4166)
    assert not any(line.startswith("note:") for line in notes), "20 Hz is the bridge's tick"
