"""The loop: lock, lease, run, deliver, ack -- against the queue replica and a fake docker.

Phase 7 Step 5. The queue is `tests/support/fake_wfqueue.py` over real HTTP on an ephemeral
port, driven through the queue's own client; the daemon is `tests/support/fake_docker.py`, a
dictionary. What is under test is everything the worker says to the queue and when, and every
one of Phase 7's three properties has a test here that fails if it is got wrong: a busy card
never costs an attempt, a long run is never redelivered, and a restarted agent finishes what it
started -- and acks it -- rather than starting it twice.

The card locks are real `flock`s under `tmp_path`, so "the card is somebody's" is a descriptor
this test holds, exactly as CARLA's script would.
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from pathlib import Path

import pytest

from scenariobank.agent.jobs import Roots
from scenariobank.agent.lock import CardLock
from scenariobank.agent.session import (
    LABEL_ATTEMPT,
    LABEL_GPU,
    LABEL_JOB,
    LABEL_MANAGED,
    MANAGED_BY,
)
from scenariobank.agent.wfqueue_client import QueueClient
from scenariobank.agent.worker import (
    STATUS_DIR,
    TOPIC,
    Agent,
    Timing,
    Worker,
    gpus_from_environment,
    queue_from_environment,
)
from scenariobank.bank import CategoryEntry, Manifest, ScenarioRow, write_manifest
from scenariobank.handedness import DRIVE_SIDE_LEFT
from scenariobank.results import JOB_SCHEMA_VERSION
from tests.support.fake_docker import FakeDocker, wrote
from tests.support.fake_wfqueue import FakeQueueServer

ROOT = Path(__file__).resolve().parents[2]
EXPERT = "scenariobank.policies:ExpertPolicy"

#: Milliseconds where the rig has seconds and minutes. Every wait the loop makes is in here.
FAST = Timing(
    lease_s=3.0,
    extend_every_s=0.05,
    handover_s=30.0,
    idle_sleep_s=0.02,
    busy_sleep_s=0.02,
    queue_down_sleep_s=0.02,
    retry_after_s=0.5,
    poll_s=0.01,
    status_every_s=0.0,
    sweep_every_s=0.0,
    sweep_age_s=0.0,
)


def test_the_runtime_client_is_the_vendored_one_byte_for_byte():
    # The colleague's client, kept verbatim under docs/ so it can be diffed against the copy a
    # running server serves; the package runs a copy of it, and this is what keeps the two one.
    vendored = (ROOT / "docs" / "queue-docs" / "queue-client-v0.py").read_bytes()
    runtime = (ROOT / "src" / "scenariobank" / "agent" / "wfqueue_client.py").read_bytes()
    assert runtime == vendored


# --- the bench --------------------------------------------------------------------------------


def write_bank(directory: Path, bank_id: str = "t-junction") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    rows = [
        ScenarioRow(
            scenario_id=f"t_junction_{index:04d}", seed=index, destination="d",
            spawn_lane_index=0, route_length_m=100.0, net_rotation_deg=0.0, turn_pairs="",
            thumbnail=None,
        )
        for index in range(2)
    ]  # fmt: skip
    write_manifest(
        directory,
        Manifest(
            schema_version="1.4",
            bank_id=bank_id,
            created_utc="2026-09-15T00:00:00Z",
            metadrive={"edition": None, "dist_version": None, "commit": None,
                       "asset_version": None},
            base_config={},
            drive_side=DRIVE_SIDE_LEFT,
            categories={
                "t_junction": CategoryEntry(
                    description="a T", block_seq="T", exit_rule="left", max_steps=30,
                    scenarios=rows,
                ),
            },
        ),
    )
    return directory


@pytest.fixture
def rig(tmp_path) -> Roots:
    write_bank(tmp_path / "banks" / "t-junction")
    return Roots(
        banks=tmp_path / "banks",
        models=tmp_path / "models",
        results=tmp_path / "results",
        out=tmp_path / "out",
        repo=Path("/host/checkout"),
    )


@pytest.fixture
def server():
    with FakeQueueServer() as running:
        yield running


@pytest.fixture
def client(server) -> QueueClient:
    queue = QueueClient(server.url, timeout=5, retries=0)
    queue.create_topic(TOPIC)
    return queue


def payload(job_id="j7", **fields) -> dict:
    base = {
        "schema_version": JOB_SCHEMA_VERSION,
        "job_id": job_id,
        "bank": {"id": "t-junction", "path": "/the/submitters/machine"},
        "scenarios": ["t_junction_0000"],
        "policy": EXPERT,
    }
    base.update(fields)
    return base


def finishes(*, exit_code=0, finished=None, n=1, done=None):
    """An `on_launch` that leaves a finished run behind the moment the container is created."""

    def on_launch(docker: FakeDocker) -> None:
        out_dir = Path(docker.launched["env"]["OUT_DIR"])
        wrote(out_dir, n=n, done=n if done is None else done, exit_code=exit_code,
              finished=finished)

    return on_launch


def runs_for(polls: int, *, exit_code=0):
    """A run that takes `polls` state queries to finish. The long run, in miniature."""
    seen = {"n": 0}

    def on_launch(docker: FakeDocker) -> None:
        wrote(Path(docker.launched["env"]["OUT_DIR"]), n=1, done=0, running="t_junction_0000")

    def on_state(docker: FakeDocker, name: str) -> None:
        seen["n"] += 1
        if seen["n"] >= polls and docker.launched is not None:
            out_dir = Path(docker.launched["env"]["OUT_DIR"])
            wrote(out_dir, n=1, done=1, exit_code=exit_code)
            docker.containers[name]["running"] = False
            docker.containers[name]["exit"] = exit_code

    return on_launch, on_state


def worker_for(rig, client, docker, tmp_path, *, gpu=0, timing=FAST, **options) -> Worker:
    return Worker(
        gpu, rig, client, commands=docker, timing=timing, lock_root=tmp_path / "locks",
        host="lap", **options,
    )


def message(client, job_id="j7") -> dict:
    """The one message in the topic whose payload names this job, as the server has it."""
    rows = client.list(TOPIC)["messages"]
    return next(row for row in rows if row["payload"].get("job_id") == job_id)


def in_thread(target, **kwargs) -> threading.Thread:
    thread = threading.Thread(target=target, kwargs=kwargs, daemon=True)
    thread.start()
    return thread


# --- the plain path ---------------------------------------------------------------------------


def test_a_job_is_leased_run_delivered_and_acked(rig, client, tmp_path):
    docker = FakeDocker(on_launch=finishes())
    client.put(TOPIC, payload())
    worker = worker_for(rig, client, docker, tmp_path)

    assert worker.run(max_jobs=1) == 1

    assert [item.what for item in worker.settled] == ["ack"]
    row = message(client)
    assert row["state"] == "done"
    # The label the studio's "what is running where" reads, and it survives the ack.
    assert row["consumer"] == "lap:gpu0"
    # Delivered under the job's own name, with the job it ran from beside the results.
    assert (rig.results / "j7" / "job.json").is_file()
    assert json.loads((rig.results / "j7" / "job.json").read_text())["bank"]["path"] == "/bank"
    # The card is free again and the container is gone.
    assert not (tmp_path / "locks" / ".scenariobank.gpu0.holder.json").exists()
    assert docker.containers == {}


def test_the_holder_record_names_the_lease_while_the_run_is_up(rig, client, tmp_path):
    seen = {}

    def on_state(docker, name):
        record = tmp_path / "locks" / ".scenariobank.gpu0.holder.json"
        if record.exists() and "holder" not in seen:
            seen["holder"] = json.loads(record.read_text())
            seen["leased"] = client.list(TOPIC, state="leased")["messages"]

    on_launch, finish = runs_for(3)

    def state(docker, name):
        on_state(docker, name)
        finish(docker, name)

    docker = FakeDocker(on_launch=on_launch, on_state=state)
    client.put(TOPIC, payload())
    worker_for(rig, client, docker, tmp_path).run(max_jobs=1)

    holder, leased = seen["holder"], seen["leased"]
    assert holder["schema_version"] == 2
    assert holder["job_id"] == "j7" and holder["container"] == "scenariobank-gpu0-j7-1"
    assert holder["message_id"] == leased[0]["id"]
    assert holder["lease_id"] == leased[0]["lease_id"]


def test_the_queues_attempt_count_is_the_attempt_the_run_carries(rig, client, tmp_path):
    # The payload says attempt 1; the queue has already handed this message out once and let
    # the lease expire, so this is attempt 2, and the container, the record and a failed run's
    # delivery name all say so.
    client.put(TOPIC, payload(attempt=1))
    first = client.get_one(TOPIC, visibility_timeout=0.1)
    time.sleep(0.2)
    docker = FakeDocker(on_launch=finishes(exit_code=1, finished={"permanent": False}))
    worker_for(rig, client, docker, tmp_path).run(max_jobs=1)

    assert docker.launched["env"]["ATTEMPT"] == "2"
    assert docker.launched["env"]["NAME"] == "scenariobank-gpu0-j7-2"
    assert (rig.results / "j7.attempt2").is_dir()
    assert client.get(first.id)["attempts"] == 2


# --- the three answers ------------------------------------------------------------------------


def test_a_payload_that_is_not_a_job_is_dead_lettered_and_nothing_launched(rig, client, tmp_path):
    client.put(TOPIC, {"schema_version": 1})
    docker = FakeDocker(on_launch=finishes())
    worker = worker_for(rig, client, docker, tmp_path)
    worker.run(max_jobs=1)

    assert [item.what for item in worker.settled] == ["dead"]
    rows = client.list(TOPIC, state="dead")["messages"]
    assert len(rows) == 1 and "not a job" in rows[0]["last_error"]
    assert docker.launched is None
    assert not (tmp_path / "locks" / ".scenariobank.gpu0.holder.json").exists()


def test_a_job_the_share_cannot_satisfy_is_dead_lettered_by_name(rig, client, tmp_path):
    # Property 3's other half: refused is not unlucky. The same job on the other rig would be
    # refused identically, so its remaining attempts are not spent finding that out.
    client.put(TOPIC, payload(scenarios=["t_junction_0000", "nope"]))
    docker = FakeDocker(on_launch=finishes())
    worker_for(rig, client, docker, tmp_path).run(max_jobs=1)

    row = message(client)
    assert row["state"] == "dead" and "no scenario named nope" in row["last_error"]
    assert docker.launched is None


def test_a_run_the_simulator_refuses_is_delivered_and_dead_lettered(rig, client, tmp_path):
    client.put(TOPIC, payload())
    docker = FakeDocker(
        on_launch=finishes(exit_code=1, finished={"permanent": True, "error": "no such policy"})
    )
    worker = worker_for(rig, client, docker, tmp_path)
    worker.run(max_jobs=1)

    assert [item.what for item in worker.settled] == ["dead"]
    row = message(client)
    assert row["state"] == "dead" and "no such policy" in row["last_error"]
    # Evidence, never the result: the job's own name stays free so a retry would not be told
    # "done".
    assert (rig.results / "j7.attempt1").is_dir() and not (rig.results / "j7").exists()


def test_a_run_that_broke_is_delivered_as_evidence_and_retried(rig, client, tmp_path):
    client.put(TOPIC, payload())
    docker = FakeDocker(on_launch=finishes(exit_code=1, finished={"permanent": False}))
    worker = worker_for(rig, client, docker, tmp_path)
    worker.run(max_jobs=1)

    assert [item.what for item in worker.settled] == ["retry"]
    row = message(client)
    assert row["state"] == "ready" and row["attempts"] == 1
    assert "failed" in row["last_error"]
    assert (rig.results / "j7.attempt1").is_dir() and not (rig.results / "j7").exists()
    # Hidden until `retry_after`, so the same rig does not spin on it.
    assert client.lease(TOPIC) == []


def test_a_rig_that_cannot_start_the_run_retries_the_job_and_keeps_the_log(rig, client, tmp_path):
    client.put(TOPIC, payload())
    docker = FakeDocker()
    docker.launch_creates_first = True
    docker.launch_code, docker.launch_output = 128, "could not select device driver"
    worker = worker_for(rig, client, docker, tmp_path)
    worker.run(max_jobs=1)

    assert [item.what for item in worker.settled] == ["retry"]
    assert "rig:" in worker.settled[0].detail
    assert message(client)["state"] == "ready"
    # What there was went to the share as an attempt, and the container did not linger.
    assert (rig.results / "j7.attempt1" / "job.json").is_file()
    assert docker.containers == {}


def test_a_job_already_delivered_is_acked_without_running(rig, client, tmp_path):
    # Property 1. At-least-once delivery means this message can arrive after the other rig has
    # finished it; `results/<job_id>` is the whole answer.
    (rig.results / "j7").mkdir(parents=True)
    client.put(TOPIC, payload())
    docker = FakeDocker(on_launch=finishes())
    worker = worker_for(rig, client, docker, tmp_path)
    worker.run(max_jobs=1)

    assert [item.what for item in worker.settled] == ["ack"]
    assert "already delivered" in worker.settled[0].detail
    assert docker.launched is None
    assert message(client)["state"] == "done"


# --- the card comes before the queue ---------------------------------------------------------


def hold_card(tmp_path, gpu=0):
    """Somebody else's exclusive lock on the card, as CARLA's script would take it."""
    root = tmp_path / "locks"
    root.mkdir(exist_ok=True)
    handle = os.open(root / f".wing-sim.gpu{gpu}.lock", os.O_RDWR | os.O_CREAT, 0o666)
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return handle


def test_a_busy_card_is_a_wait_and_never_a_lease(rig, client, tmp_path):
    # The single most likely wrong behaviour in the phase: a nack for busy spends an attempt,
    # and five busy rigs would dead-letter a perfectly good job. The lock comes first, so a card
    # that is somebody's costs the queue nothing at all.
    held = hold_card(tmp_path)
    client.put(TOPIC, payload())
    docker = FakeDocker(on_launch=finishes())
    worker = worker_for(rig, client, docker, tmp_path)
    thread = in_thread(worker.run, max_jobs=1)

    time.sleep(0.3)
    row = message(client)
    assert row["state"] == "ready" and row["attempts"] == 0
    assert worker.settled == [] and docker.launched is None

    os.close(held)
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert [item.what for item in worker.settled] == ["ack"]


def test_an_idle_worker_does_not_sit_on_the_rig_lock(rig, client, tmp_path):
    # An empty queue must not mean a rig CARLA cannot have. Between polls the card is released,
    # so an exclusive take from outside succeeds while the worker is polling.
    docker = FakeDocker()
    worker = worker_for(rig, client, docker, tmp_path)
    thread = in_thread(worker.run, max_jobs=1)
    time.sleep(0.1)

    taken = 0
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and taken < 3:
        try:
            handle = hold_card(tmp_path)
        except BlockingIOError:
            time.sleep(0.005)
            continue
        taken += 1
        os.close(handle)
        time.sleep(0.01)
    worker.stop.set()
    thread.join(timeout=5)
    assert taken == 3


# --- the lease is a clock ---------------------------------------------------------------------


def test_a_long_run_keeps_its_lease_and_is_never_redelivered(rig, client, tmp_path):
    # Property 2. The lease asked for is shorter than the run; the extend on every tick is what
    # keeps the message out of `ready` until the run is delivered and acked.
    short = Timing(**{**FAST.__dict__, "lease_s": 0.3})
    on_launch, on_state = runs_for(40)
    docker = FakeDocker(on_launch=on_launch, on_state=on_state)
    client.put(TOPIC, payload())
    worker = worker_for(rig, client, docker, tmp_path, timing=short)
    started = time.monotonic()
    worker.run(max_jobs=1)

    assert time.monotonic() - started > 0.4  # it really did outlive the lease
    row = message(client)
    assert row["state"] == "done" and row["attempts"] == 1
    assert [item.what for item in worker.settled] == ["ack"]


def test_a_lease_lost_mid_run_is_finished_delivered_and_reported(rig, client, tmp_path):
    # No extend at all, a lease shorter than the run: the message goes back to `ready` under
    # the worker's feet and somebody else acks it. The run is finished anyway -- stopping it
    # would turn a duplicate into a loss -- and the ack that meets 409 asks the queue what
    # became of the message: `done` is the first ack having landed, and that is still an ack.
    lost = Timing(**{**FAST.__dict__, "lease_s": 0.2, "extend_every_s": 1000.0})
    on_launch, on_state = runs_for(60)

    def other_worker(docker, name):
        on_state(docker, name)
        if "taken" not in seen:
            again = client.get_one(TOPIC)
            if again is not None:
                seen["taken"] = again
                again.ack()

    seen: dict = {}
    docker = FakeDocker(on_launch=on_launch, on_state=other_worker)
    client.put(TOPIC, payload())
    worker = worker_for(rig, client, docker, tmp_path, timing=lost)
    worker.run(max_jobs=1)

    assert "taken" in seen
    assert [item.what for item in worker.settled] == ["ack"]
    assert "lease lost" in worker.settled[0].detail
    assert (rig.results / "j7").is_dir()


# --- restart recovery -------------------------------------------------------------------------


def dead_agents_run(rig, client, docker, tmp_path, *, visibility=5.0):
    """What a worker killed mid-run leaves behind: a container, a job file, a holder record."""
    client.put(TOPIC, payload())
    leased = client.get_one(TOPIC, visibility_timeout=visibility, consumer="lap:gpu0")
    out_dir = rig.out / "j7"
    resolved = json.dumps(
        {**payload(), "attempt": 1, "bank": {"id": "t-junction", "path": "/bank"}}
    )
    wrote(out_dir, n=1, done=0, running="t_junction_0000")
    (out_dir / "job.json").write_text(resolved)
    docker.add(
        "scenariobank-gpu0-j7-1",
        running=True,
        labels={LABEL_MANAGED: MANAGED_BY, LABEL_JOB: "j7", LABEL_ATTEMPT: "1", LABEL_GPU: "0"},
    )
    held = CardLock(0, tmp_path / "locks").acquire(
        job_id="j7", attempt=1, container="scenariobank-gpu0-j7-1", out=str(out_dir),
        message_id=leased.id, lease_id=leased.lease_id,
    )
    held.abandon()
    return leased


def test_a_restarted_agent_adopts_the_run_and_acks_it_on_the_old_lease(rig, client, tmp_path):
    # The recovery path is the normal path with the launch skipped. The record beside the lock
    # names the lease; `extend` on a live lease is accepted; so the worker that finishes the run
    # is the one that acks it, and the other rig never sees the message at all.
    docker = FakeDocker()
    leased = dead_agents_run(rig, client, docker, tmp_path)
    polls = {"n": 0}

    def on_state(d, name):
        polls["n"] += 1
        if polls["n"] >= 3:
            wrote(rig.out / "j7", n=1, done=1, exit_code=0)
            d.containers[name]["running"] = False

    docker.on_state = on_state
    worker = worker_for(rig, client, docker, tmp_path)
    assert worker.run(max_jobs=1) == 1

    assert docker.launched is None  # no second container, which is the point
    assert [item.what for item in worker.settled] == ["ack"]
    assert worker.settled[0].message_id == leased.id
    row = message(client)
    assert row["state"] == "done" and row["attempts"] == 1
    assert (rig.results / "j7").is_dir()
    assert docker.containers == {}


def test_an_adopted_run_whose_lease_expired_is_delivered_and_the_redelivery_is_acked(
    rig, client, tmp_path
):
    # The lease is gone by the time the agent is back. The run is still finished and delivered
    # -- nothing the queue does changes what is on the card -- and the message, redelivered to
    # this same worker a moment later, meets `results/j7` and is acked without a run.
    docker = FakeDocker()
    dead_agents_run(rig, client, docker, tmp_path, visibility=0.1)
    time.sleep(0.2)
    polls = {"n": 0}

    def on_state(d, name):
        polls["n"] += 1
        if polls["n"] >= 3:
            wrote(rig.out / "j7", n=1, done=1, exit_code=0)
            d.containers[name]["running"] = False

    docker.on_state = on_state
    worker = worker_for(rig, client, docker, tmp_path)
    assert worker.run(max_jobs=2) == 2

    assert docker.launched is None
    assert [item.what for item in worker.settled] == ["ack"]
    assert "already delivered" in worker.settled[0].detail
    row = message(client)
    assert row["state"] == "done" and row["attempts"] == 2


def test_stopping_the_agent_mid_run_hands_the_run_over(rig, client, tmp_path):
    # The run is a sibling and outlives the agent by design. On the way out the worker buys the
    # next agent time -- one more extend -- and leaves the record it will read.
    on_launch, on_state = runs_for(10_000)
    docker = FakeDocker(on_launch=on_launch, on_state=on_state)
    client.put(TOPIC, payload())
    worker = worker_for(rig, client, docker, tmp_path)
    thread = in_thread(worker.run, max_jobs=1)
    deadline = time.monotonic() + 5
    while docker.launched is None and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.1)

    worker.stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()

    assert docker.containers["scenariobank-gpu0-j7-1"]["running"]  # still driving
    assert worker.settled == []  # nothing was said to the queue about the job itself
    record = json.loads((tmp_path / "locks" / ".scenariobank.gpu0.holder.json").read_text())
    row = message(client)
    assert record["message_id"] == row["id"] and record["lease_id"] == row["lease_id"]
    assert row["state"] == "leased"
    assert row["lease_expires_at"] - time.time() > FAST.lease_s + 1  # the handover extend
    # And the card can be taken again in this process: the descriptors were closed, the record
    # was not.
    os.close(hold_card(tmp_path))


# --- the agent, the topic, the queue being away ----------------------------------------------


def test_two_workers_take_one_job_each(rig, client, tmp_path):
    docker = FakeDocker(on_launch=finishes())
    client.put(TOPIC, payload("a"))
    client.put(TOPIC, payload("b"))
    agent = Agent(
        [0, 1], rig, client, commands=docker, timing=FAST, lock_root=tmp_path / "locks",
        host="lap",
    )
    assert agent.run(max_jobs=1) == 2

    done = client.list(TOPIC, state="done")["messages"]
    assert sorted(row["consumer"] for row in done) == ["lap:gpu0", "lap:gpu1"]
    launched = [env for argv, env in docker.calls if argv[0] == "bash" and "sim-run.sh" in argv[1]]
    assert sorted(env["GPU"] for env in launched) == ["0", "1"]
    assert (rig.results / "a").is_dir() and (rig.results / "b").is_dir()


def test_the_agent_creates_the_topic_rather_than_assume_the_studio_did(rig, server, tmp_path):
    fresh = QueueClient(server.url, timeout=5, retries=0)
    assert fresh.topics() == []
    stop = threading.Event()
    stop.set()
    Agent([0], rig, fresh, commands=FakeDocker(), timing=FAST, stop=stop,
          lock_root=tmp_path / "locks", host="lap").run()
    assert [row["name"] for row in fresh.topics()] == [TOPIC]


def test_a_queue_that_is_away_is_a_wait_and_not_a_crash(rig, tmp_path):
    nobody = QueueClient("http://127.0.0.1:9", timeout=0.5, retries=0)
    worker = worker_for(rig, nobody, FakeDocker(), tmp_path)
    assert worker.lease() is None and worker.queue_ok is False
    thread = in_thread(worker.run, max_jobs=1)
    time.sleep(0.2)
    worker.stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive() and worker.jobs_done == 0
    # And the card was not left held while it waited.
    os.close(hold_card(tmp_path))


# --- the status file and the sweep -----------------------------------------------------------


def test_the_status_file_says_what_the_card_is_doing(rig, client, tmp_path):
    seen = {}
    on_launch, finish = runs_for(3)

    def on_state(docker, name):
        path = rig.results / STATUS_DIR / "lap-gpu0.json"
        if path.exists() and "running" not in seen:
            record = json.loads(path.read_text())
            if record["state"] == "running":
                seen["running"] = record
        finish(docker, name)

    docker = FakeDocker(on_launch=on_launch, on_state=on_state)
    client.put(TOPIC, payload())
    worker_for(rig, client, docker, tmp_path).run(max_jobs=1)

    running = seen["running"]
    assert running["job_id"] == "j7" and running["gpu"] == 0 and running["host"] == "lap"
    assert running["progress"] == {"done": 0, "n": 1, "running": "t_junction_0000"}
    assert running["consumer"] == "lap:gpu0" and running["agent_version"]
    after = json.loads((rig.results / STATUS_DIR / "lap-gpu0.json").read_text())
    assert after["state"] == "stopped" and after["jobs_done"] == 1 and after["job_id"] is None


def test_the_sweep_deletes_only_what_is_delivered_and_old(rig, client, tmp_path):
    old = time.time() - 10 * 86400
    for name in ("delivered-old", "undelivered-old", "delivered-fresh", "delivered-live"):
        wrote(rig.out / name, n=1, done=1, exit_code=0)
    for name in ("delivered-old", "delivered-fresh", "delivered-live"):
        (rig.results / name).mkdir(parents=True)
    for name in ("delivered-old", "undelivered-old", "delivered-live"):
        os.utime(rig.out / name / "exit_code", (old, old))
    docker = FakeDocker()
    docker.add("scenariobank-gpu0-delivered-live-1", running=True,
               labels={LABEL_MANAGED: MANAGED_BY, LABEL_JOB: "delivered-live", LABEL_GPU: "0"})
    keep = Timing(**{**FAST.__dict__, "sweep_age_s": 86400.0})
    worker = worker_for(rig, client, docker, tmp_path, timing=keep)

    gone = worker.sweep(force=True)

    assert gone == [rig.out / "delivered-old"]
    assert not (rig.out / "delivered-old").exists()
    for name in ("undelivered-old", "delivered-fresh", "delivered-live"):
        assert (rig.out / name).is_dir(), name


def test_the_sweep_never_touches_the_results_root_inside_the_out_root(tmp_path, client):
    # The laptop's layout: results/ is out/results. It is delivered by definition and must not
    # be mistaken for a run directory called "results".
    roots = Roots(banks=tmp_path / "banks", models=tmp_path / "models",
                  results=tmp_path / "out" / "results", out=tmp_path / "out", repo=Path("/r"))
    (roots.results / "results").mkdir(parents=True)  # a job called "results", delivered
    old = time.time() - 10 * 86400
    os.utime(roots.results, (old, old))
    worker = Worker(0, roots, client, commands=FakeDocker(), timing=FAST,
                    lock_root=tmp_path / "locks", host="lap")
    assert worker.sweep(force=True) == []
    assert roots.results.is_dir()


# --- the environment --------------------------------------------------------------------------


def test_the_cards_come_from_the_environment_and_default_to_card_zero():
    assert gpus_from_environment({}) == [0]
    assert gpus_from_environment({"SCENARIOBANK_GPUS": "1,0, 1"}) == [0, 1]
    with pytest.raises(ValueError, match="SCENARIOBANK_GPUS"):
        gpus_from_environment({"SCENARIOBANK_GPUS": "zero"})


def test_the_queue_comes_from_the_flag_then_the_environment_then_localhost():
    assert queue_from_environment(None, {}).base_url == "http://localhost:9090"
    found = queue_from_environment(None, {"WFQUEUE_URL": "http://nas:9090/", "WFQUEUE_TOKEN": "t"})
    assert found.base_url == "http://nas:9090" and found.token == "t"
    assert queue_from_environment("http://x:1", {"WFQUEUE_URL": "http://y:2"}).base_url == "http://x:1"
