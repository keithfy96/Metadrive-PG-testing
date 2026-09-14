"""`results.py` and `run_bank`: the record, and the batch that never aborts.

Nothing here needs a simulator. The bank is a manifest written into `tmp_path`, and the env is
a fake handed in through `runner.build_env`, scripted per seed: which step it ends at, what
`info` it ends with, and whether it raises. That is enough to pin every promise the batch makes
-- one file per scenario the moment it ends, an error is a row and not an abort, a stop lands
between two steps and the env is still closed -- without opening MetaDrive, which is what makes
the promises checkable on every machine rather than only on a rig.

The signal test really sends SIGTERM to this process. pytest runs tests on the main thread, which
is the only thread Python lets install a handler on, and `stop_on_signals` puts the old handler
back, so the process is as it was afterwards; the test checks that too.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import types
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

import scenariobank.runner as runner_module
from scenariobank.bank import CategoryEntry, Manifest, ScenarioRow, write_manifest
from scenariobank.events import (
    BATCH_FILE,
    EVENTS_FILE,
    STARTS_DIR,
    read_events,
    read_exit_code,
)
from scenariobank.handedness import DRIVE_SIDE_LEFT
from scenariobank.options import OptionError
from scenariobank.policies import PolicyError
from scenariobank.replay import ENDINGS
from scenariobank.results import (
    JOB_SCHEMA_VERSION,
    RESULTS_SCHEMA_VERSION,
    STOPPED,
    TERMINATIONS,
    Job,
    JobBank,
    JobOptions,
    Results,
    ScenarioResult,
    Summary,
    dump_json,
    failure_reason,
    summarize,
)
from scenariobank.runner import COLLISION_FLAGS, RunError, run_bank, select_rows, stop_on_signals

# --- the bank and the fake env ----------------------------------------------------------------


def row(scenario_id: str, seed: int, *, max_steps: int | None = None) -> ScenarioRow:
    return ScenarioRow(
        scenario_id=scenario_id,
        seed=seed,
        destination=f"dest-{seed}",
        spawn_lane_index=0,
        route_length_m=100.0,
        net_rotation_deg=0.0,
        turn_pairs="",
        thumbnail=None,
        max_steps=max_steps,
    )


def write_bank(tmp_path: Path) -> Path:
    """Two categories, four rows, one with its own budget. Written, so `read_manifest` reads it."""
    write_manifest(
        tmp_path,
        Manifest(
            schema_version="1.4",
            bank_id="fake-bank",
            created_utc="2026-09-08T00:00:00Z",
            metadrive={
                "edition": None, "dist_version": None, "commit": None, "asset_version": None,
            },
            base_config={"traffic_density": 0.0},
            drive_side=DRIVE_SIDE_LEFT,
            categories={
                "curve": CategoryEntry(
                    description="two curves",
                    block_seq="CC",
                    exit_rule="only",
                    max_steps=50,
                    scenarios=[row("curve_0000", 0), row("curve_0001", 1, max_steps=10)],
                ),
                "t_junction": CategoryEntry(
                    description="a T",
                    block_seq="T",
                    exit_rule="left",
                    max_steps=30,
                    scenarios=[row("t_junction_0000", 0), row("t_junction_0002", 2)],
                ),
            },
        ),
    )
    return tmp_path


class FakeSpace:
    shape = (2,)


class FakeEnv:
    """Scripted per seed: `ends[seed]` is `(step, info)`; `raises[seed]` is the step to raise at.

    A seed with no script runs until the loop caps it. `on_reset` and `on_step` are hooks the
    tests use to look at the world mid-batch -- which files exist, or to send a signal.
    """

    built: list[FakeEnv] = []

    def __init__(
        self, *, ends=None, raises=None, on_reset=None, on_step=None, widths=(19, 19)
    ):
        self.ends = ends or {}
        self.raises = raises or {}
        self.on_reset = on_reset
        self.on_step = on_step
        #: The observation's width at reset and after a step. Both 19 unless a test wants to
        #: see what the batch does when the shape moves.
        self.widths = widths
        self.action_space = FakeSpace()
        self.seed: int | None = None
        self.taken = 0
        self.closed = False
        self.seeds_seen: list[int] = []
        FakeEnv.built.append(self)

    def reset(self, *, seed: int):
        self.seed = seed
        self.taken = 0
        self.seeds_seen.append(seed)
        if self.on_reset is not None:
            self.on_reset(seed)
        return np.zeros(self.widths[0]), {}

    def step(self, action):
        del action
        self.taken += 1
        if self.on_step is not None:
            self.on_step(self.seed, self.taken)
        if self.raises.get(self.seed) == self.taken:
            raise RuntimeError(f"scripted failure on seed {self.seed} at step {self.taken}")
        end, info = self.ends.get(self.seed, (None, {}))
        observation = np.zeros(self.widths[1])
        if end is not None and self.taken >= end:
            return observation, 1.0, True, False, {"env_seed": self.seed, **info}
        return observation, 1.0, False, False, {"env_seed": self.seed}

    def close(self):
        self.closed = True


def prepare(env, row_):
    """`env.py`'s per-row step, faked: the destination read back is the row's own."""
    del env
    return row_.destination


def use_fake_env(monkeypatch, **script):
    """Swap `runner.build_env` for one that builds a scripted `FakeEnv` per row."""
    FakeEnv.built = []

    def build_env(bank_dir, entry, options, rig=None, step_hz=None):
        del bank_dir, entry, options, rig
        return FakeEnv(**script), prepare

    monkeypatch.setattr(runner_module, "build_env", build_env)


def job_for(bank_dir: Path, **fields) -> Job:
    defaults = dict(
        schema_version=JOB_SCHEMA_VERSION,
        bank=JobBank(id="fake-bank", path=str(bank_dir)),
        policy="scenariobank.policies:ConstantPolicy",
    )
    return Job(**{**defaults, **fields})


# --- offline: the two records -----------------------------------------------------------------


def test_a_job_round_trips_and_refuses_an_unknown_field(tmp_path):
    job = job_for(tmp_path, scenarios=["a"], options=JobOptions(tier="hard", raw={"traffic": 0.2}))
    again = Job.model_validate_json(dump_json(job))
    assert again == job
    with pytest.raises(ValidationError):
        Job.model_validate({**job.model_dump(), "gpu": 0})
    with pytest.raises(ValidationError, match="schema_version"):
        Job.model_validate({**job.model_dump(), "schema_version": 2})


def test_a_job_from_the_queue_carries_its_id_and_attempt_into_the_result(tmp_path, monkeypatch):
    use_fake_env(monkeypatch, ends={0: (3, {}), 1: (3, {}), 2: (3, {})})
    job = job_for(write_bank(tmp_path), job_id="msg-42", attempt=2, scenarios=["curve_0000"])
    report = run_bank(job, tmp_path / "out")
    assert (report.job_id, report.attempt) == ("msg-42", 2)


def test_results_round_trip_and_refuse_an_unknown_field(tmp_path, monkeypatch):
    use_fake_env(monkeypatch, ends={0: (5, {"arrive_dest": True})})
    report = run_bank(job_for(write_bank(tmp_path)), tmp_path / "out")
    written = json.loads((tmp_path / "out" / "results.json").read_text())
    assert Results.model_validate(written) == report
    assert report.schema_version == RESULTS_SCHEMA_VERSION
    assert (report.job_id, report.attempt) == (None, None), "the CLI is not the queue"
    with pytest.raises(ValidationError):
        Results.model_validate({**written, "gpu": 0})
    with pytest.raises(ValidationError):
        ScenarioResult.model_validate({**written["results"][0], "lidar": True})


@pytest.mark.parametrize(
    ("info", "kwargs", "expected"),
    [
        # `crash` and `env_seed` are written by the env and were missing from the old list;
        # `crash` outranks `max_step`, and `env_seed` is not an ending at all.
        ({"crash": True, "env_seed": 3, "max_step": True}, {}, "crash"),
        ({"crash_vehicle": True, "crash": True, "arrive_dest": True}, {}, "crash_vehicle"),
        ({"out_of_road": True, "max_step": True}, {}, "out_of_road"),
        ({"arrive_dest": True, "max_step": True}, {}, None),
        ({"max_step": True}, {}, "max_step"),
        # The loop's own cap, with the env still going, is the row's `max_steps` by another name.
        ({"env_seed": 3}, {"capped": True}, "max_step"),
        ({"env_seed": 3}, {}, None),
        # A stop outranks everything, because the loop checks it before the step.
        ({"crash_vehicle": True}, {"stopped": True}, STOPPED),
        ({"idle": True}, {}, None),
    ],
)
def test_failure_reason_reads_the_flags_in_a_fixed_precedence(info, kwargs, expected):
    assert failure_reason(info, **kwargs) == expected


def test_the_replay_phrases_and_the_result_reasons_are_one_list():
    # `replay.ENDINGS` is built from `TERMINATIONS`, so the diagnostic and the record rank an
    # ending the same way; this pins that nothing measured is left without a phrase.
    assert tuple(key for key, _ in ENDINGS) == TERMINATIONS
    assert "idle" not in TERMINATIONS, "defined by TerminationState, written by nothing"
    assert "env_seed" not in TERMINATIONS, "written every step, and not an ending"


def test_summarize_counts_rows_reasons_and_statuses():
    def result(scenario_id, *, status="ok", success=False, reason=None):
        return ScenarioResult(
            scenario_id=scenario_id, category="c", status=status, success=success,
            failure_reason=reason, steps=1, actions=1, reward=0.0, cost=0.0, wall_time_s=0.0,
        )

    summary = summarize([
        result("a", success=True),
        result("b", reason="max_step"),
        result("c", reason="max_step"),
        result("d", status="error"),
    ])
    assert summary == Summary(
        n=4, success_rate=0.25, by_failure_reason={"max_step": 2}, by_status={"error": 1, "ok": 3}
    )
    assert summarize([]) == Summary(n=0, success_rate=0.0)


# --- offline: the batch -----------------------------------------------------------------------


def test_every_row_is_written_the_moment_it_ends_and_the_batch_is_assembled_last(
    tmp_path, monkeypatch
):
    out = tmp_path / "out"
    seen_at_reset: list[list[str]] = []

    def on_reset(seed):
        del seed
        seen_at_reset.append(sorted(p.name for p in (out / "results").glob("*.json")))
        assert not (out / "results.json").exists(), "results.json is assembled last"

    use_fake_env(
        monkeypatch, ends={0: (5, {"arrive_dest": True}), 2: (4, {"out_of_road": True})},
        on_reset=on_reset,
    )
    report = run_bank(job_for(write_bank(tmp_path)), out)

    assert [r.scenario_id for r in report.results] == [
        "curve_0000", "curve_0001", "t_junction_0000", "t_junction_0002"
    ]
    assert sorted(p.name for p in (out / "results").glob("*.json")) == [
        f"{r.scenario_id}.json" for r in report.results
    ]
    # Each reset saw exactly the rows that had already ended, and no more.
    assert seen_at_reset == [
        [], ["curve_0000.json"], ["curve_0000.json", "curve_0001.json"],
        ["curve_0000.json", "curve_0001.json", "t_junction_0000.json"],
    ]
    per_row = ScenarioResult.model_validate_json((out / "results" / "curve_0001.json").read_text())
    assert per_row == report.results[1]
    assert report.summary == Summary(
        n=4, success_rate=0.5, by_failure_reason={"max_step": 1, "out_of_road": 1},
        by_status={"ok": 4},
    )


def test_the_cap_is_the_rows_own_budget_and_the_seed_selects_the_row(tmp_path, monkeypatch):
    use_fake_env(monkeypatch)
    report = run_bank(job_for(write_bank(tmp_path)), tmp_path / "out")
    by_id = {r.scenario_id: r for r in report.results}
    assert by_id["curve_0000"].steps == 50
    assert by_id["curve_0001"].steps == 10, "its own max_steps, not the category's"
    assert by_id["t_junction_0002"].steps == 30
    assert all(r.failure_reason == "max_step" for r in report.results)
    assert by_id["curve_0001"].seed == 1 and by_id["curve_0001"].scenario_index is None
    assert by_id["curve_0001"].destination == "dest-1"
    assert set(by_id["curve_0001"].collisions) == {name for _, name in COLLISION_FLAGS}
    # One env per row, each closed, and each saw only its own row.
    assert [env.seeds_seen for env in FakeEnv.built] == [[0], [1], [0], [2]]
    assert all(env.closed for env in FakeEnv.built)


def test_a_row_that_raises_is_an_error_row_and_the_next_row_runs(tmp_path, monkeypatch):
    use_fake_env(monkeypatch, raises={1: 3})
    report = run_bank(job_for(write_bank(tmp_path)), tmp_path / "out")
    assert report.summary.by_status == {"error": 1, "ok": 3}
    failed = report.results[1]
    assert (failed.scenario_id, failed.status, failed.success) == ("curve_0001", "error", False)
    assert failed.failure_reason is None and failed.steps == 0
    assert "scripted failure on seed 1 at step 3" in failed.traceback
    assert report.results[2].status == "ok", "the batch went on"
    assert all(env.closed for env in FakeEnv.built)


def test_a_policy_that_raises_is_recorded_by_name_in_every_row(tmp_path, monkeypatch):
    use_fake_env(monkeypatch)
    job = job_for(write_bank(tmp_path), policy="scenariobank.policies:RaisingPolicy")
    report = run_bank(job, tmp_path / "out")
    assert report.summary.by_status == {"error": 4}
    assert all("RaisingPolicy" in r.traceback for r in report.results)


def test_an_entry_whose_env_will_not_build_is_four_error_rows_and_the_next_entry_runs(
    tmp_path, monkeypatch
):
    FakeEnv.built = []

    def build_env(bank_dir, entry, options, rig=None, step_hz=None):
        del bank_dir, options, rig
        if entry.block_seq == "CC":
            raise RuntimeError("no such road")
        return FakeEnv(), prepare

    monkeypatch.setattr(runner_module, "build_env", build_env)
    report = run_bank(job_for(write_bank(tmp_path)), tmp_path / "out")
    statuses = [(r.scenario_id, r.status) for r in report.results]
    assert statuses == [
        ("curve_0000", "error"), ("curve_0001", "error"),
        ("t_junction_0000", "ok"), ("t_junction_0002", "ok"),
    ]
    assert "no such road" in report.results[0].traceback


def test_a_policy_with_bind_is_handed_each_rows_env_before_the_row_runs(tmp_path, monkeypatch):
    """The optional half of the policy protocol: one `bind` per env, in the bank's order, and
    before the reset of that env -- which is when the expert first needs the agent."""
    bound_at_reset: list[int] = []
    use_fake_env(monkeypatch, on_reset=lambda seed: bound_at_reset.append(len(Bound.bound)))
    bank = write_bank(tmp_path)

    class Bound:
        bound: list = []

        def bind(self, env):
            Bound.bound.append(env)

        def __call__(self, observation):
            del observation
            return (0.0, 0.0)

    module = types.ModuleType("fake_policies")
    module.Bound = Bound
    monkeypatch.setitem(sys.modules, "fake_policies", module)
    report = run_bank(job_for(bank, policy="fake_policies:Bound"), tmp_path / "out")
    assert Bound.bound == FakeEnv.built and len(FakeEnv.built) == 4, "one bind per row's env"
    assert bound_at_reset == [1, 2, 3, 4], "bound before the reset of each env"
    assert report.summary.by_status == {"ok": 4}


def test_a_moved_observation_shape_fails_the_run_after_the_record_is_written(
    tmp_path, monkeypatch
):
    """Step 4's expert-leak check. The record is on disk first, with both shapes, so the
    failure is diagnosable; then the run fails, because every row after the leak was scored
    against a different observation."""
    use_fake_env(monkeypatch, widths=(19, 31))
    bank = write_bank(tmp_path)
    with pytest.raises(RunError, match=r"observation shape moved.*\[19\].*\[31\]"):
        run_bank(job_for(bank), tmp_path / "out")
    written = json.loads((tmp_path / "out" / "results.json").read_text())
    assert (
        written["env"]["observation_shape_before"], written["env"]["observation_shape_after"]
    ) == ([19], [31])
    assert len(written["results"]) == 4, "every row was scored and written first"


def test_a_stop_ends_the_row_it_lands_in_writes_what_there_is_and_closes_the_env(
    tmp_path, monkeypatch
):
    use_fake_env(monkeypatch)
    total = {"steps": 0}

    def count(seed, taken):
        del seed, taken
        total["steps"] += 1

    FakeEnv.built = []
    original = runner_module.build_env

    def counting_build(bank_dir, entry, options, rig=None, step_hz=None):
        env, prep = original(bank_dir, entry, options, rig, step_hz)
        env.on_step = count
        return env, prep

    monkeypatch.setattr(runner_module, "build_env", counting_build)
    # curve_0000 is 50 steps and curve_0001 is 10; the flag goes up 7 steps into the second.
    report = run_bank(
        job_for(write_bank(tmp_path)), tmp_path / "out", stop=lambda: total["steps"] >= 57
    )

    assert report.stopped is True
    assert [(r.scenario_id, r.failure_reason, r.steps) for r in report.results] == [
        ("curve_0000", "max_step", 50), ("curve_0001", STOPPED, 7),
    ]
    assert report.results[1].status == "ok", "scored as far as it went"
    assert report.summary.by_failure_reason == {"max_step": 1, STOPPED: 1}
    assert (tmp_path / "out" / "results.json").exists()
    assert len(FakeEnv.built) == 2, "one env per row, and the t_junction rows were never built"
    assert all(env.closed for env in FakeEnv.built)


def test_sigterm_sets_the_flag_and_the_old_handler_is_put_back(tmp_path, monkeypatch):
    before = signal.getsignal(signal.SIGTERM)

    def send_at_five(seed, taken):
        if seed == 0 and taken == 5:
            os.kill(os.getpid(), signal.SIGTERM)

    use_fake_env(monkeypatch, on_step=send_at_five)
    report = run_bank(job_for(write_bank(tmp_path)), tmp_path / "out")

    assert report.stopped is True
    assert [(r.scenario_id, r.failure_reason, r.steps) for r in report.results] == [
        ("curve_0000", STOPPED, 5),
    ]
    assert FakeEnv.built[0].closed, "closed on the normal path, nothing raised into it"
    assert signal.getsignal(signal.SIGTERM) is before


def test_stop_on_signals_is_a_flag_until_a_signal_and_restores_afterwards():
    before = signal.getsignal(signal.SIGINT)
    with stop_on_signals((signal.SIGINT,)) as flag:
        assert flag() is False
        os.kill(os.getpid(), signal.SIGINT)
        assert flag() is True, "a Ctrl-C is a flag, never a KeyboardInterrupt into env.close()"
        assert flag.signal == signal.SIGINT
    assert signal.getsignal(signal.SIGINT) is before


def test_trajectories_are_written_only_when_asked(tmp_path, monkeypatch):
    use_fake_env(monkeypatch)
    out = tmp_path / "out"
    run_bank(job_for(write_bank(tmp_path), scenarios=["curve_0001"]), out)
    assert not (out / "trajectories").exists()

    run_bank(
        job_for(
            write_bank(tmp_path), scenarios=["curve_0001"], save_trajectories=True, decision_hz=5
        ),
        out,
    )
    written = json.loads((out / "trajectories" / "curve_0001.json").read_text())
    assert written["stride"] == 2
    assert written["actions"] == [[0.0, 0.0]] * 5, "10 steps at a stride of 2 is five decisions"


def test_the_actions_digest_tells_two_policies_apart_and_two_identical_runs_not(
    tmp_path, monkeypatch
):
    use_fake_env(monkeypatch)
    out = tmp_path / "out"
    bank = write_bank(tmp_path)
    same = [
        run_bank(job_for(bank, scenarios=["curve_0000"]), out).results[0].actions_digest
        for _ in range(2)
    ]
    assert same[0] == same[1]
    # `tests/` is not a package, so the other policy is a module built by hand.
    swerve = types.ModuleType("fake_policies")
    swerve.Swerve = lambda: (lambda _observation: (0.5, 0.5))
    monkeypatch.setitem(sys.modules, "fake_policies", swerve)
    other = run_bank(job_for(bank, scenarios=["curve_0000"], policy="fake_policies:Swerve"), out)
    assert other.results[0].actions_digest != same[0]


# --- offline: every refusal, before the simulator ---------------------------------------------


def test_the_wrong_bank_at_the_path_is_refused_by_id(tmp_path, monkeypatch):
    use_fake_env(monkeypatch)
    job = job_for(write_bank(tmp_path), bank=JobBank(id="other-bank", path=str(tmp_path)))
    with pytest.raises(RunError, match="is 'fake-bank', and the job names 'other-bank'"):
        run_bank(job, tmp_path / "out")
    assert FakeEnv.built == []


def test_a_job_that_does_not_name_the_bank_id_skips_the_check(tmp_path, monkeypatch):
    use_fake_env(monkeypatch)
    job = job_for(write_bank(tmp_path), bank=JobBank(path=str(tmp_path)))
    assert run_bank(job, tmp_path / "out").bank.id == "fake-bank"


def test_an_unknown_scenario_id_is_refused_by_name(tmp_path, monkeypatch):
    use_fake_env(monkeypatch)
    job = job_for(write_bank(tmp_path), scenarios=["curve_0000", "curve_9999", "nope"])
    with pytest.raises(RunError, match="no scenario named curve_9999, nope"):
        run_bank(job, tmp_path / "out")


def test_scenarios_run_in_the_banks_order_whatever_order_they_are_named_in(tmp_path, monkeypatch):
    use_fake_env(monkeypatch)
    job = job_for(write_bank(tmp_path), scenarios=["t_junction_0002", "curve_0000"])
    report = run_bank(job, tmp_path / "out")
    assert [r.scenario_id for r in report.results] == ["curve_0000", "t_junction_0002"]


def test_a_policy_that_will_not_load_and_a_level_that_does_not_exist_are_refused(
    tmp_path, monkeypatch
):
    use_fake_env(monkeypatch)
    bank = write_bank(tmp_path)
    with pytest.raises(PolicyError, match="has no 'Nope'"):
        run_bank(job_for(bank, policy="scenariobank.policies:Nope"), tmp_path / "out")
    with pytest.raises(OptionError, match="'enormous' is not a level"):
        run_bank(
            job_for(bank, options=JobOptions(levels={"traffic": "enormous"})), tmp_path / "out"
        )
    with pytest.raises(ValueError, match="faster than the env's 10 Hz"):
        run_bank(job_for(bank, decision_hz=20), tmp_path / "out")
    assert FakeEnv.built == [], "every refusal comes before an env is built"


def test_the_options_a_run_used_are_the_record_names_and_numbers(tmp_path, monkeypatch):
    use_fake_env(monkeypatch)
    job = job_for(
        write_bank(tmp_path),
        options=JobOptions(tier="hard", levels={"traffic": "low"}, raw={"cones": 2}),
    )
    report = run_bank(job, tmp_path / "out")
    assert report.options.kind == "pg"
    assert report.options.tier == "hard"
    assert report.options.levels["traffic"] == "low"
    assert report.options.origin == {
        "traffic": "flag", "cones": "raw", "barriers": "tier", "pedestrians": "tier",
        "cyclists": "tier", "lights": "tier",
    }
    assert report.options.values["cones"] == 2.0


def test_select_rows_needs_no_simulator_and_an_empty_bank_is_refused():
    manifest = Manifest(
        schema_version="1.4",
        bank_id="empty",
        created_utc="2026-09-08T00:00:00Z",
        metadrive={"edition": None, "dist_version": None, "commit": None, "asset_version": None},
        base_config={},
        drive_side=DRIVE_SIDE_LEFT,
        categories={},
    )
    with pytest.raises(RunError, match="holds no scenarios"):
        select_rows(manifest, None)


# --- offline: the command ---------------------------------------------------------------------


def _run(*args):
    from typer.testing import CliRunner

    from scenariobank import cli

    return CliRunner().invoke(cli.app, ["run", *args])


def test_the_command_builds_a_job_from_its_flags_and_prints_where_the_results_are(
    tmp_path, monkeypatch
):
    use_fake_env(monkeypatch)
    bank = write_bank(tmp_path)
    out = tmp_path / "out"
    result = _run(
        "--bank", str(bank), "--categories", "curve", "--scenarios", "curve_0001,t_junction_0000",
        "--tier", "easy", "--traffic", "low", "--out", str(out),
    )
    assert result.exit_code == 0, result.output
    written = out / "easy"  # the tier is a subdirectory of `--out`
    assert f"results written: {written / 'results.json'}" in result.stdout
    report = Results.model_validate_json((written / "results.json").read_text())
    # `--categories` and `--scenarios` intersect: one row is in both, the other is not.
    assert [r.scenario_id for r in report.results] == ["curve_0001"]
    assert report.options.tier == "easy" and report.options.levels["traffic"] == "low"
    assert report.policy == "scenariobank.policies:ConstantPolicy"


def test_a_relative_out_lands_under_out_and_a_tier_names_a_subdirectory(tmp_path, monkeypatch):
    """Fourteen verify-run directories had accumulated in the repository root, and been
    committed, before `run` learnt this (2026-09-10). `out/` is the path the container writes
    and the one git ignores; `--out out/x` is not doubled, and an absolute path is untouched.
    The same day, three tiers run into one `--out` had overwritten each other: a tier is now a
    subdirectory, on relative and absolute paths alike, and a job file's tier counts too."""
    from scenariobank.cli import under_out

    use_fake_env(monkeypatch)
    bank = write_bank(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert under_out(Path("easy1")) == Path("out/easy1")
    assert under_out(Path("out/easy1")) == Path("out/easy1")
    assert under_out(tmp_path / "easy1") == tmp_path / "easy1"
    assert under_out(Path("film"), "hard") == Path("out/film/hard")
    assert under_out(tmp_path / "film", "easy") == tmp_path / "film" / "easy"

    for tier in ("easy", "medium", "hard"):
        result = _run("--bank", str(bank), "--scenarios", "curve_0000", "--out", "film",
                      "--tier", tier)
        assert result.exit_code == 0, result.output
        assert f"results written: out/film/{tier}/results.json" in result.stdout
    assert sorted(p.name for p in (tmp_path / "out" / "film").iterdir() if p.is_dir()) == [
        "easy", "hard", "medium"
    ]
    # The two files a supervisor reads belong to the process, not to the batch, so they sit in
    # the directory `--out` named -- beside the three tiers rather than inside the last one.
    assert sorted(p.name for p in (tmp_path / "out" / "film").iterdir() if p.is_file()) == [
        "events.jsonl", "exit_code"
    ]
    job_file = tmp_path / "job.json"
    job_file.write_text(
        dump_json(job_for(bank, scenarios=["curve_0000"], options=JobOptions(tier="hard")))
    )
    result = _run("--job", str(job_file), "--out", "from-job")
    assert result.exit_code == 0, result.output
    assert (tmp_path / "out" / "from-job" / "hard" / "results.json").exists()

    result = _run("--bank", str(bank), "--scenarios", "curve_0000", "--out", "easy1")
    assert result.exit_code == 0, result.output
    assert "results written: out/easy1/results.json" in result.stdout
    assert (tmp_path / "out" / "easy1" / "results.json").exists()
    assert not (tmp_path / "easy1").exists()

    result = _run("--bank", str(bank), "--scenarios", "curve_0000", "--out", "out/easy2")
    assert result.exit_code == 0, result.output
    assert (tmp_path / "out" / "easy2" / "results.json").exists()
    assert not (tmp_path / "out" / "out").exists()


def test_a_job_file_is_the_same_run_and_takes_no_other_flags(tmp_path, monkeypatch):
    use_fake_env(monkeypatch)
    bank = write_bank(tmp_path)
    job_file = tmp_path / "job.json"
    job_file.write_text(dump_json(job_for(bank, job_id="studio-7", scenarios=["curve_0000"])))
    out = tmp_path / "out"

    refused = _run("--job", str(job_file), "--bank", str(bank), "--out", str(out))
    assert refused.exit_code == 2, "click's own code for a usage error"
    assert "--job carries the whole job; drop --bank" in refused.output
    # And even that is recorded: the agent looks for an exit code whatever it did wrong.
    assert read_exit_code(out) == 2

    result = _run("--job", str(job_file), "--out", str(out))
    assert result.exit_code == 0, result.output
    report = Results.model_validate_json((out / "results.json").read_text())
    assert report.job_id == "studio-7"
    assert [r.scenario_id for r in report.results] == ["curve_0000"]


@pytest.mark.parametrize(
    ("flags", "phrase"),
    [
        (("--categories", "nope"), "has no category named nope"),
        (("--scenarios", "nope"), "no scenario named nope"),
        (("--policy", "nowhere:Nothing"), "cannot import 'nowhere'"),
        (("--traffic", "enormous"), "'enormous' is not a level"),
        (("--traffic-density", "0.005"), "below 0.01"),
        (("--lights", "low"), "Phase 8"),
        (("--decision-hz", "20"), "faster than the env's 10 Hz"),
    ],
)
def test_every_refusal_leaves_the_command_at_exit_one_with_the_reason(
    tmp_path, monkeypatch, flags, phrase
):
    use_fake_env(monkeypatch)
    result = _run("--bank", str(write_bank(tmp_path)), "--out", str(tmp_path / "out"), *flags)
    assert result.exit_code != 0
    assert phrase in result.output
    assert FakeEnv.built == []


def test_the_command_needs_a_bank_or_a_job(tmp_path):
    result = _run("--out", str(tmp_path / "out"))
    assert result.exit_code != 0
    assert "name a bank to run, or a --job file" in result.output


# --- offline: the event stream, and the files a supervisor reads instead of a log --------------


def test_a_run_says_what_it_is_doing_in_order_and_leaves_a_bar_behind_it(tmp_path, monkeypatch):
    """Phase 7 Step 1: every moment of a run as one JSON object per line, and two of those
    moments as files. The bar is then the directory alone -- `batch.json` is the denominator,
    `results/` is the numerator, and the row in `starts/` with no result yet is the one running
    -- which is what an agent restarted mid-run reads, having kept nothing in memory."""
    use_fake_env(monkeypatch)
    bank = write_bank(tmp_path)
    job_file = tmp_path / "job.json"
    job_file.write_text(
        dump_json(
            job_for(bank, job_id="studio-7", attempt=2, scenarios=["curve_0000", "curve_0001"])
        )
    )
    out = tmp_path / "out"
    result = _run("--job", str(job_file), "--out", str(out))
    assert result.exit_code == 0, result.output

    stream = read_events(out / EVENTS_FILE)
    assert [item["event"] for item in stream] == [
        "run.started",
        "batch.started",
        "scenario.started",
        "scenario.finished",
        "scenario.started",
        "scenario.finished",
        "run.finished",
    ]
    # Every line stands alone: a log holding two attempts of one job is read by reading lines.
    assert {item["job_id"] for item in stream} == {"studio-7"}
    assert {item["attempt"] for item in stream} == {2}

    started = stream[0]
    assert started["out"] == str(out) and started["job_file"] == str(job_file)
    assert started["policy"] == "scenariobank.policies:ConstantPolicy"
    assert started["pid"] == os.getpid()

    batch = json.loads((out / BATCH_FILE).read_text())
    assert batch["event"] == "batch.started"
    assert (batch["bank_id"], batch["source"], batch["n"]) == ("fake-bank", "pg", 2)
    assert batch["scenarios"] == ["curve_0000", "curve_0001"]
    assert (batch["step_hz"], batch["stride"]) == (10.0, 1)

    assert sorted(path.name for path in (out / STARTS_DIR).iterdir()) == [
        "curve_0000.json", "curve_0001.json"
    ]
    start = json.loads((out / STARTS_DIR / "curve_0001.json").read_text())
    # The row's own budget, so a heartbeat's step count has a denominator without the bank.
    assert (start["index"], start["n"], start["max_steps"]) == (2, 2, 10)

    ended = [item for item in stream if item["event"] == "scenario.finished"]
    assert [(item["scenario_id"], item["index"], item["steps"]) for item in ended] == [
        ("curve_0000", 1, 50), ("curve_0001", 2, 10)
    ]
    assert all(item["status"] == "ok" for item in ended)

    last = stream[-1]
    assert (last["outcome"], last["exit_code"], last["permanent"]) == ("ok", 0, False)
    assert (last["n"], last["stopped"]) == (2, False)
    assert last["results"] == str(out / "results.json")
    assert read_exit_code(out) == 0

    assert batch["n"] == len(list((out / "results").iterdir())) == len(
        list((out / STARTS_DIR).iterdir())
    )


def test_a_refusal_leaves_an_exit_code_and_a_line_saying_it_will_never_run(tmp_path, monkeypatch):
    """The exit code is written whatever happened, so a supervisor that finds none knows the
    container was killed outright rather than that it refused. `permanent` is the judgement a
    dead-letter rests on: nothing ran, and nothing this machine does will change that."""
    use_fake_env(monkeypatch)
    bank = write_bank(tmp_path)
    out = tmp_path / "out"
    result = _run("--bank", str(bank), "--scenarios", "nope", "--out", str(out))

    assert result.exit_code == 1
    assert read_exit_code(out) == 1
    stream = read_events(out / EVENTS_FILE)
    assert [item["event"] for item in stream] == ["run.started", "run.finished"]
    assert stream[-1]["permanent"] is True
    assert "no scenario named nope" in stream[-1]["error"]
    assert stream[-1]["results"] is None
    assert not (out / BATCH_FILE).exists(), "the batch never started"
    assert FakeEnv.built == [], "and no simulator was opened"


def test_a_job_file_that_does_not_parse_still_records_an_exit_code(tmp_path):
    """No `run.started`: nothing was ever identified to run, and the stream says so by opening
    with the line that closes it."""
    out = tmp_path / "out"
    bad = tmp_path / "bad.json"
    bad.write_text('{"schema_version": 1}')
    result = _run("--job", str(bad), "--out", str(out))

    assert result.exit_code == 1
    assert read_exit_code(out) == 1
    stream = read_events(out / EVENTS_FILE)
    assert [item["event"] for item in stream] == ["run.finished"]
    assert stream[0]["permanent"] is True and stream[0]["job_id"] is None


def test_a_failure_after_the_batch_started_is_not_permanent(tmp_path, monkeypatch):
    """A run that got as far as building an env may have failed on the card, the driver or the
    bridge, and those are worth another rig. The observation shape moving is the failure at
    hand, and the record it wrote before failing is still named."""
    use_fake_env(monkeypatch, widths=(19, 21), ends={0: (3, {})})
    bank = write_bank(tmp_path)
    out = tmp_path / "out"
    result = _run("--bank", str(bank), "--scenarios", "curve_0000", "--out", str(out))

    assert result.exit_code == 1
    assert read_exit_code(out) == 1
    last = read_events(out / EVENTS_FILE)[-1]
    assert last["permanent"] is False
    assert "observation shape moved" in last["error"]
    assert last["results"] == str(out / "results.json") and (out / "results.json").exists()


def test_events_on_stdout_replace_the_summary_line(tmp_path, monkeypatch):
    """`--events` is what the container is run with: stdout is the stream, and the one prose
    line a person reads is not on it. The file is written either way, and holds the same lines."""
    use_fake_env(monkeypatch)
    bank = write_bank(tmp_path)
    out = tmp_path / "out"
    result = _run("--bank", str(bank), "--scenarios", "curve_0000", "--out", str(out), "--events")

    assert result.exit_code == 0, result.output
    assert "results written" not in result.stdout
    printed = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    assert [item["event"] for item in printed] == [item["event"] for item in
                                                   read_events(out / EVENTS_FILE)]


def test_a_stopped_run_exits_zero_and_the_last_line_says_so(tmp_path, monkeypatch):
    """The stop is `run_bank`'s, not the entrypoint's (Phase 7 Step 1): SIGTERM from
    `docker stop` reaches the flag, the row ends `stopped`, the record is written, and the
    process exits 0 -- a cancelled run that scored six of thirty-five is not a failed one."""
    def send_at_five(seed, taken):
        if seed == 0 and taken == 5:
            os.kill(os.getpid(), signal.SIGTERM)

    use_fake_env(monkeypatch, on_step=send_at_five)
    bank = write_bank(tmp_path)
    out = tmp_path / "out"
    result = _run("--bank", str(bank), "--scenarios", "curve_0000", "--out", str(out))

    assert result.exit_code == 0, result.output
    assert read_exit_code(out) == 0
    last = read_events(out / EVENTS_FILE)[-1]
    assert (last["outcome"], last["stopped"], last["exit_code"]) == ("ok", True, 0)
    assert FakeEnv.built[0].closed, "closed on the normal path, nothing raised into it"
