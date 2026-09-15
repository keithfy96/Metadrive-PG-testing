"""The run session: launch, supervise, harvest, deliver -- with a fake docker under it.

Phase 7 Step 3. Every subprocess the session starts goes through one seam (`Commands`), so these
drive the real code with a fake daemon: containers are a dictionary, `sim-run.sh` is a recorded
argv, and a run's output is whatever the test wrote into the out directory. What is under test is
the supervision and the arithmetic of delivery, and a real container would only make it slower to
prove -- the real one is run by hand and recorded in the plan.

The two things the fake cannot prove are named where they are measured instead: that `sim-run.sh`
does what the environment tells it (`test_images.py`), and that a container actually appears
(the by-hand check). The fake itself is `tests/support/fake_docker.py`, shared with Step 5's
`test_worker.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scenariobank.agent.jobs import Resolved, Roots
from scenariobank.agent.session import (
    BRIDGE_BASE_PORT,
    BRIDGE_POLICIES,
    JOB_FILE,
    LABEL_GPU,
    LABEL_JOB,
    LABEL_MANAGED,
    LOG_FILE,
    MANAGED_BY,
    Outcome,
    Progress,
    RunSession,
    SessionError,
)
from scenariobank.results import JOB_SCHEMA_VERSION, Job, JobBank, JobOptions
from tests.support.fake_docker import FakeDocker, wrote

EXPERT = "scenariobank.policies:ExpertPolicy"
AV3 = "scenariobank.av3:AV3Policy"


def job(**fields) -> Job:
    base = {
        "schema_version": JOB_SCHEMA_VERSION,
        "job_id": "j7",
        "attempt": 1,
        "bank": JobBank(id="t-junction", path="/bank"),
        "scenarios": ["t_junction_0000", "t_junction_0001"],
        "policy": EXPERT,
    }
    base.update(fields)
    return Job(**base)


@pytest.fixture
def rig(tmp_path) -> Roots:
    return Roots(
        banks=tmp_path / "banks",
        models=tmp_path / "models",
        results=tmp_path / "results",
        out=tmp_path / "out",
        repo=Path("/host/checkout"),
    )


def session_for(rig, docker, *, policy=EXPERT, gpu=0, tier=None, **kwargs) -> RunSession:
    resolved = Resolved(
        job=job(policy=policy, options=JobOptions(tier=tier) if tier else JobOptions()),
        job_id="j7",
        attempt=1,
        bank_dir=rig.banks / "t-junction",
        models_dir=None,
        out_dir=rig.out / "j7",
        gpu=gpu,
    )
    return RunSession(resolved, rig, commands=docker, **kwargs)


# --- what the launcher is told --------------------------------------------------------------


def test_the_launch_hands_over_host_paths_and_the_containers_own_paths(rig):
    docker = FakeDocker()
    session = session_for(rig, docker)
    session.launch()
    argv, env = docker.launched["argv"], docker.launched["env"]
    # The container's own paths on the command line: /out is the mount, and the job file inside
    # it is the one the agent wrote there.
    assert argv[2:] == ["run", "--job", f"/out/{JOB_FILE}", "--out", "/out", "--events"]
    # The daemon's paths in the environment. REPO_DIR is the trap this exists for: inside the
    # agent container `pwd` is /work and the daemon has never heard of /work.
    assert env["REPO_DIR"] == "/host/checkout"
    assert env["OUT_DIR"] == str(rig.out / "j7")
    assert env["BANK_DIR"] == str(rig.banks / "t-junction")
    # Detached, so restarting the agent cannot kill a twenty-minute drive.
    assert env["DETACH"] == "1"
    assert env["NAME"] == "scenariobank-gpu0-j7-1"
    assert (env["JOB_ID"], env["ATTEMPT"], env["GPU"]) == ("j7", "1", "0")


def test_nothing_is_inherited_from_the_environment_wholesale(rig, monkeypatch):
    # wing-sim's reason, and it is the one that matters: a developer's exported setting
    # otherwise silently changes what a model is scored on.
    monkeypatch.setenv("QUALITY", "Low")
    monkeypatch.setenv("AV3_TARGET_SPEED_MPS", "3")
    env = session_for(rig, FakeDocker()).child_environment()
    assert "QUALITY" not in env and "AV3_TARGET_SPEED_MPS" not in env
    assert set(env) == {
        "PATH", "HOME", "REPO_DIR", "OUT_DIR", "BANK_DIR",
        "JOB_ID", "ATTEMPT", "GPU", "NAME", "DETACH",
    }


def test_a_card_is_named_even_when_the_container_gets_no_gpu(rig):
    # `sim-run.sh` reads NO_GPU first and GPU only for the label, so a laptop run still carries
    # the label the adopt query filters on.
    env = session_for(rig, FakeDocker(), gpu=1, no_gpu=True).child_environment()
    assert env["NO_GPU"] == "1" and env["GPU"] == "1"


def test_the_job_written_for_the_container_is_the_resolved_one(rig):
    docker = FakeDocker()
    session = session_for(rig, docker)
    path = session.write_job()
    assert path == rig.out / "j7" / JOB_FILE
    assert json.loads(path.read_text())["bank"]["path"] == "/bank"


def test_launching_onto_a_name_that_already_exists_is_refused(rig):
    docker = FakeDocker()
    docker.add("scenariobank-gpu0-j7-1", running=True)
    with pytest.raises(SessionError, match="already exists"):
        session_for(rig, docker).launch()


def test_a_launcher_that_fails_is_a_rig_fault_and_not_a_refusal(rig):
    docker = FakeDocker()
    docker.launch_code, docker.launch_output = 1, "sim image is not ready"
    with pytest.raises(SessionError, match="could not start the run container"):
        session_for(rig, docker).launch()


def test_a_failed_launch_takes_away_the_container_it_may_have_created(rig):
    # `docker run` can fail having CREATED the container: a one-card rig asked for
    # `--gpus device=1` exits 128 with it sitting there (measured on the rig). Left alone it is
    # adopted by the next attempt and reported as a run that vanished -- two wrong answers, since
    # the run never started and the reason is in a log nobody kept.
    docker = FakeDocker()
    docker.launch_creates_first = True
    docker.launch_code, docker.launch_output = 128, "could not select device driver"
    session = session_for(rig, docker, gpu=1)
    (rig.out / "j7").mkdir(parents=True)
    with pytest.raises(SessionError, match="could not start the run container"):
        session.run(poll_s=0)
    assert session.name not in docker.containers
    assert (session.out_dir / LOG_FILE).exists()


# --- the bridge -------------------------------------------------------------------------------


def test_a_policy_that_needs_no_bridge_starts_none(rig):
    docker = FakeDocker()
    session = session_for(rig, docker)
    assert session.needs_bridge is False
    assert session.ensure_bridge() is None
    assert docker.containers == {}


def test_the_bridge_is_this_cards_own_port_and_name(rig):
    docker = FakeDocker()
    session = session_for(rig, docker, policy=AV3, gpu=1)
    assert session.bridge_port == BRIDGE_BASE_PORT + 1 == 5601
    assert session.ensure_bridge() == "bridge-gpu1"
    _, env = docker.calls[-1]
    assert env["BRIDGE_PORT"] == "5601" and env["BRIDGE_NAME"] == "bridge-gpu1"
    assert session.child_environment()["BRIDGE_PORT"] == "5601"


def test_a_bridge_that_is_already_up_is_left_alone(rig):
    docker = FakeDocker()
    docker.add("bridge-gpu0", running=True)
    before = len(docker.calls)
    session_for(rig, docker, policy=AV3).ensure_bridge()
    assert all("start" not in argv for argv, _ in docker.calls[before:])


def test_a_stopped_bridge_holding_the_name_is_removed_first(rig):
    # `bridge.sh start` refuses a name a stopped container holds, with advice for a person. A
    # supervisor has to act rather than be advised.
    docker = FakeDocker()
    docker.add("bridge-gpu0", running=False, exit_code=1)
    session_for(rig, docker, policy=AV3).ensure_bridge()
    scripts = [argv[2] for argv, _ in docker.calls if argv[0] == "bash"]
    assert scripts == ["stop", "start"]


def test_no_bridge_means_no_bridge_whatever_the_policy(rig):
    session = session_for(rig, FakeDocker(), policy=AV3, bridge=False)
    assert session.needs_bridge is False
    assert "BRIDGE_PORT" not in session.child_environment()


def test_the_named_bridge_policies_are_every_bridge_policy_there_is():
    # Named rather than imported in `session.py`, because importing `scenariobank.av3` pulls
    # torch into the supervisor. This is the guard that keeps the two in step: a third policy
    # that talks to the bridge fails here rather than running against one nobody started.
    import scenariobank.av3 as av3

    subclasses = {
        f"scenariobank.av3:{name}"
        for name in av3.__all__
        if isinstance(getattr(av3, name), type)
        and issubclass(getattr(av3, name), av3.BridgePolicy)
    }
    assert subclasses == set(BRIDGE_POLICIES)


# --- progress, derived and never stored ---------------------------------------------------------


def test_progress_is_the_record_directory_and_nothing_else(rig):
    out = rig.out / "j7"
    assert Progress.read(out) == Progress()  # nothing written yet is no progress
    wrote(out, n=2, done=1, running="t_junction_0001")
    progress = Progress.read(out)
    assert (progress.started, progress.n, progress.done) == (True, 2, 1)
    assert progress.running == "t_junction_0001"
    assert progress.sentence() == "1/2, running t_junction_0001"


def test_progress_follows_the_tier_subdirectory(rig):
    docker = FakeDocker()
    session = session_for(rig, docker, tier="hard")
    wrote(rig.out / "j7" / "hard", n=2, done=2)
    assert session.progress().done == 2


# --- supervision ------------------------------------------------------------------------------


def running_session(rig, docker, **kwargs) -> RunSession:
    """A session whose container is already up, labelled the way `sim-run.sh` labels it."""
    session = session_for(rig, docker, **kwargs)
    docker.add(
        session.name,
        running=True,
        labels={LABEL_MANAGED: MANAGED_BY, LABEL_JOB: "j7", LABEL_GPU: str(session.resolved.gpu)},
    )
    return session


def test_a_run_that_ended_at_zero_is_completed(rig):
    docker = FakeDocker()
    session = running_session(rig, docker)
    wrote(session.out_dir, done=2, exit_code=0, finished={"outcome": "ok", "exit_code": 0})
    result = session.supervise(poll_s=0)
    assert result.outcome is Outcome.COMPLETED and result.ok
    assert (result.exit_code, result.progress.done) == (0, 2)


def test_a_stopped_run_exits_zero_and_is_not_a_completed_one(rig):
    # The difference is in `run.finished` and nowhere else: a stopped run keeps every row it
    # scored and exits 0 exactly as a whole batch does.
    docker = FakeDocker()
    session = running_session(rig, docker)
    wrote(session.out_dir, done=1, exit_code=0, finished={"exit_code": 0, "stopped": True})
    result = session.supervise(poll_s=0)
    assert result.outcome is Outcome.STOPPED and result.ok
    assert result.progress.done == 1


def test_a_permanent_failure_is_refused_and_is_the_one_to_dead_letter(rig):
    docker = FakeDocker()
    session = running_session(rig, docker)
    wrote(
        session.out_dir,
        n=0,
        exit_code=1,
        finished={"exit_code": 1, "permanent": True, "error": "no scenario named nope"},
    )
    result = session.supervise(poll_s=0)
    assert result.outcome is Outcome.REFUSED and result.permanent
    assert result.detail == "no scenario named nope"


def test_a_failure_after_the_batch_started_is_worth_another_rig(rig):
    docker = FakeDocker()
    session = running_session(rig, docker)
    wrote(session.out_dir, done=1, exit_code=1, finished={"exit_code": 1, "permanent": False})
    result = session.supervise(poll_s=0)
    assert result.outcome is Outcome.FAILED and not result.permanent


def test_a_container_that_is_gone_with_no_exit_code_vanished(rig):
    # SIGKILL, the OOM killer, the machine going down. Distinguished from every code by ABSENCE,
    # which is why the absence is not an error.
    docker = FakeDocker()
    session = session_for(rig, docker)
    (rig.out / "j7").mkdir(parents=True)
    result = session.supervise(poll_s=0)
    assert result.outcome is Outcome.VANISHED and result.exit_code is None


def test_a_run_that_finished_in_the_gap_is_classified_and_not_vanished(rig):
    # The run writes the exit code and then exits, so the two happen in that order -- but the
    # read and the liveness check do not. wing-sim reported a job failed with "lock released
    # with no exit code" after every one of its presets passed; the second read is the fix.
    docker = FakeDocker()
    session = session_for(rig, docker)
    (rig.out / "j7").mkdir(parents=True)

    def stop_and_write(fake, name=None):
        fake.containers[session.name]["running"] = False
        fake.containers[session.name]["exit"] = 0
        wrote(session.out_dir, done=2, exit_code=0, finished={"exit_code": 0})

    docker.add(session.name, running=True)
    docker.on_state = stop_and_write
    # The state query is what flips it, so the first exit-code read misses and the second finds.
    original = session.state

    def state(name):
        found = original(name)
        if docker.on_state is not None:
            docker.on_state(docker)
            docker.on_state = None
            return original(name)
        return found

    session.state = state  # type: ignore[method-assign]
    result = session.supervise(poll_s=0)
    assert result.outcome is Outcome.COMPLETED


def test_the_supervisor_offers_every_poll_to_its_caller(rig):
    # This is where Step 5 extends the lease: a lease is a clock and our work is longer than it.
    docker = FakeDocker()
    session = running_session(rig, docker)
    wrote(session.out_dir, done=2, exit_code=0)
    seen: list[Progress] = []
    session.supervise(on_tick=seen.append, poll_s=0)
    assert seen and seen[-1].done == 2


# --- adoption ---------------------------------------------------------------------------------


def test_a_run_already_going_for_this_job_on_this_card_is_adopted(rig):
    docker = FakeDocker()
    session = session_for(rig, docker)
    docker.add(
        "scenariobank-gpu0-j7-1",
        running=True,
        labels={LABEL_MANAGED: MANAGED_BY, LABEL_JOB: "j7", LABEL_GPU: "0"},
    )
    assert session.adopt() == "scenariobank-gpu0-j7-1"


def test_another_jobs_container_on_this_card_is_not_adopted(rig):
    docker = FakeDocker()
    session = session_for(rig, docker)
    docker.add(
        "scenariobank-gpu0-other-1",
        running=True,
        labels={LABEL_MANAGED: MANAGED_BY, LABEL_JOB: "other", LABEL_GPU: "0"},
    )
    assert session.adopt() is None
    assert session.ours() == [("scenariobank-gpu0-other-1", "other")]


def test_the_same_job_on_another_card_is_not_ours(rig):
    docker = FakeDocker()
    docker.add(
        "scenariobank-gpu1-j7-1",
        running=True,
        labels={LABEL_MANAGED: MANAGED_BY, LABEL_JOB: "j7", LABEL_GPU: "1"},
    )
    assert session_for(rig, docker, gpu=0).adopt() is None


def test_run_adopts_rather_than_launching_a_second_container(rig):
    docker = FakeDocker()
    session = session_for(rig, docker)
    docker.add(
        session.name,
        running=False,
        exit_code=0,
        labels={LABEL_MANAGED: MANAGED_BY, LABEL_JOB: "j7", LABEL_GPU: "0"},
    )
    wrote(session.out_dir, done=2, exit_code=0)
    result = session.run(poll_s=0)
    assert docker.launched is None  # nothing was started
    assert result.outcome is Outcome.COMPLETED
    assert result.delivered == rig.results / "j7"


# --- harvest and delivery -----------------------------------------------------------------------


def test_harvest_keeps_the_log_before_it_removes_the_container(rig):
    # The whole reason the run is not `--rm`: its output outlives it, and a removal that
    # succeeded with the log unread is the diagnosis gone.
    docker = FakeDocker()
    session = session_for(rig, docker)
    (rig.out / "j7").mkdir(parents=True)
    docker.add(session.name, running=False, exit_code=1, log="Traceback: boom\n")
    path = session.harvest()
    assert path == session.out_dir / LOG_FILE
    assert "boom" in path.read_text()
    assert session.name not in docker.containers


def test_harvesting_a_container_that_is_already_gone_says_so_quietly(rig):
    session = session_for(rig, FakeDocker())
    assert session.harvest() is None


def test_delivery_is_a_copy_then_a_rename_and_leaves_no_partial(rig):
    session = session_for(rig, FakeDocker())
    wrote(session.out_dir, done=2, exit_code=0)
    final = session.deliver()
    assert final == rig.results / "j7"
    assert (final / "batch.json").exists() and (final / "results").is_dir()
    assert not (rig.results / "j7.partial").exists()
    # And the run's own record is still on local disk, untouched.
    assert (session.out_dir / "batch.json").exists()


def test_a_result_already_on_the_share_means_done_and_is_never_overwritten(rig):
    # The redelivery guard. The queue is at-least-once by its own documentation, so a message
    # can arrive while another rig is still running it.
    session = session_for(rig, FakeDocker())
    (rig.results / "j7").mkdir(parents=True)
    assert session.already_delivered() is True
    wrote(session.out_dir, done=2, exit_code=0)
    with pytest.raises(SessionError, match="already exists"):
        session.deliver()


def test_a_stale_partial_from_an_interrupted_delivery_is_replaced(rig):
    session = session_for(rig, FakeDocker())
    staging = rig.results / "j7.partial"
    staging.mkdir(parents=True)
    (staging / "half-copied.json").write_text("{}")
    wrote(session.out_dir, done=2, exit_code=0)
    final = session.deliver()
    assert not (final / "half-copied.json").exists()


def test_delivering_nothing_is_an_error_and_not_an_empty_directory(rig):
    with pytest.raises(SessionError, match="nothing to deliver"):
        session_for(rig, FakeDocker()).deliver()


def test_a_failed_run_is_delivered_beside_the_jobs_name_and_never_under_it(rig):
    # Evidence first -- but NOT under the name the redelivery guard reads. `results/<job_id>`
    # existing means "done, ack without running", so a failed run delivered there would tell the
    # next worker that a job which must be retried is finished. Found on the rig by a job whose
    # card did not exist: it vanished, was delivered, and the next attempt said already delivered.
    docker = FakeDocker()
    session = running_session(rig, docker)
    wrote(session.out_dir, done=1, exit_code=1, finished={"exit_code": 1})
    result = session.run(poll_s=0)
    assert result.outcome is Outcome.FAILED
    assert result.delivered == rig.results / "j7.attempt1"
    assert not (rig.results / "j7").exists()
    assert session.already_delivered() is False  # so the retry runs


def test_a_vanished_run_does_not_look_finished_either(rig):
    # An exited container with no exit code beside it: killed outright, and the job must be
    # tried again somewhere.
    docker = FakeDocker()
    session = session_for(rig, docker)
    (rig.out / "j7").mkdir(parents=True)
    docker.add(
        session.name,
        running=False,
        exit_code=137,
        labels={LABEL_MANAGED: MANAGED_BY, LABEL_JOB: "j7", LABEL_GPU: "0"},
    )
    result = session.run(poll_s=0)
    assert result.outcome is Outcome.VANISHED
    assert result.delivered == rig.results / "j7.attempt1"
    assert session.already_delivered() is False


def test_a_stopped_run_does_take_the_jobs_name(rig):
    # It ran: the rows before the stop are real, and the job is finished as far as the queue is
    # concerned. Only `completed` and `stopped` are runs that ran.
    docker = FakeDocker()
    session = running_session(rig, docker)
    wrote(session.out_dir, done=1, exit_code=0, finished={"exit_code": 0, "stopped": True})
    assert session.run(poll_s=0).delivered == rig.results / "j7"


def test_no_deliver_leaves_the_run_on_local_disk(rig):
    docker = FakeDocker()
    session = running_session(rig, docker)
    wrote(session.out_dir, done=2, exit_code=0)
    result = session.run(poll_s=0, deliver=False)
    assert result.delivered is None
    assert not rig.results.exists()


# --- stopping ---------------------------------------------------------------------------------


def test_stop_asks_docker_and_records_that_we_asked(rig):
    # The intent is the only evidence: the exit code afterwards is a 0 like any other.
    docker = FakeDocker()
    session = running_session(rig, docker)
    session.stop(grace_s=5)
    argv = [argv for argv, _ in docker.calls if argv[:2] == ["docker", "stop"]][-1]
    assert argv == ["docker", "stop", "--timeout", "5", session.name]
    assert session.stop_requested is True
    wrote(session.out_dir, done=1, exit_code=0)
    assert session.supervise(poll_s=0).outcome is Outcome.STOPPED
