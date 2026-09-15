"""The results store: the tree on the share is the truth, the index is a cache of it.

Every tree here is built in `tmp_path` from the same models the runner writes with, so the store
is tested against what `deliver()` produces and never against a hand-typed JSON. No simulator,
no agent: the store reads directories.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scenariobank.options import ResolvedOptions
from scenariobank.results import (
    BankInfo,
    EnvInfo,
    Results,
    ScenarioResult,
    summarize,
    write_json,
)
from scenariobank.web.results import (
    PARTIAL_SUFFIX,
    STATUS_DIR,
    Ingested,
    ResultsStore,
    UnknownJob,
)


def scored(
    scenario_id: str,
    *,
    success: bool = True,
    steps: int = 40,
    category: str = "t_junction",
    wall_time_s: float = 0.25,
) -> ScenarioResult:
    return ScenarioResult(
        scenario_id=scenario_id,
        category=category,
        seed=7,
        status="ok",
        success=success,
        failure_reason=None if success else "out_of_road",
        steps=steps,
        actions=steps,
        reward=12.5,
        cost=0.0 if success else 1.0,
        wall_time_s=wall_time_s,
        collisions={"vehicle": 0, "human": 0},
        route_completion=1.0 if success else 0.4,
        actor_layout_digest="abc123",
    )


def record(
    job_id: str,
    rows: list[ScenarioResult],
    *,
    stopped: bool = False,
    policy: str = "scenariobank.policies:ExpertPolicy",
    levels: dict[str, str] | None = None,
) -> Results:
    """A `Results` the way `run_bank` assembles one, for a queue job."""
    return Results(
        schema_version=1,
        started_utc="2026-09-15T10:00:00Z",
        finished_utc="2026-09-15T10:00:09Z",
        job_id=job_id,
        attempt=1,
        stopped=stopped,
        bank=BankInfo(path="/bank", id="t-junction", source="pg", schema_version="1.1"),
        policy=policy,
        options=ResolvedOptions(kind="pg", levels=levels or {}),
        env=EnvInfo(
            observation_shape_before=(19,),
            observation_shape_after=(19,),
            step_hz=10.0,
            decision_hz=None,
            stride=1,
        ),
        results=rows,
        summary=summarize(rows),
    )


def deliver(
    root: Path,
    name: str,
    results: Results,
    *,
    checkpoint: str | None = None,
    host: str | None = None,
) -> Path:
    """What `RunSession.deliver` leaves under `results/`: the record, the job beside it, and
    with `host` the runner's `events.jsonl`, whose `run.started` line names the machine."""
    directory = root / name
    write_json(directory / "results.json", results)
    if host is not None:
        started = {"schema_version": 1, "event": "run.started", "job_id": name, "host": host}
        (directory / "events.jsonl").write_text(
            json.dumps(started) + "\n" + json.dumps({"event": "batch.started"}) + "\n"
        )
    (directory / "job.json").write_text(
        json.dumps({"job_id": results.job_id, "checkpoint_path": checkpoint}) + "\n"
    )
    (directory / "exit_code").write_text("0\n")
    return directory


def attempt(root: Path, name: str, *, code: int = 1) -> Path:
    """A run that did not run: `<job_id>.attempt<N>`, a log and an exit code, no record."""
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "exit_code").write_text(f"{code}\n")
    (directory / "container.log").write_text("Traceback: the card is not there\n")
    return directory


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "share" / "results"
    root.mkdir(parents=True)
    return root


@pytest.fixture
def store(tmp_path: Path, tree: Path) -> ResultsStore:
    # The index is under the studio's state directory, not under the tree: local disk, never
    # the share. The fixture keeps the two apart the way the deployment does.
    return ResultsStore(tmp_path / ".studio" / "results.sqlite", tree)


def test_ingesting_the_same_tree_twice_moves_nothing(store, tree):
    # The step's own "verify alone": the same results.json read twice, the row count still.
    deliver(tree, "j1", record("j1", [scored("t_junction_0000"), scored("t_junction_0001")]))
    deliver(tree, "j2", record("j2", [scored("t_junction_0000", success=False)]))

    first = store.ingest()
    assert first == Ingested(added=2, skipped=0, invalid=0, total=2)
    assert sorted(len(store.rows(name)) for name in ("j1", "j2")) == [1, 2]

    second = store.ingest()
    assert second == Ingested(added=0, skipped=2, invalid=0, total=2)
    assert [job["name"] for job in store.jobs()] == ["j2", "j1"]
    assert len(store.rows("j1")) == 2 and len(store.rows("j2")) == 1

    # And the same file read again from scratch, which is REPLACE on the key rather than a skip.
    assert store.rebuild() == Ingested(added=2, skipped=0, invalid=0, total=2)
    assert len(store.rows("j1")) == 2 and len(store.rows("j2")) == 1


def test_the_key_is_the_job_and_the_scenario_not_the_scenario_alone(store, tree):
    # The same scenario scores under many jobs -- every model, every option set -- so REPLACE on
    # the scenario id alone would keep one score for all of them.
    deliver(tree, "expert", record("expert", [scored("t_junction_0000", steps=40)]))
    deliver(tree, "av3", record("av3", [scored("t_junction_0000", success=False, steps=90)]))
    store.ingest()
    assert [row["steps"] for row in store.rows("expert")] == [40]
    assert [row["steps"] for row in store.rows("av3")] == [90]
    assert store.rows("av3")[0]["success"] is False
    assert store.rows("av3")[0]["failure_reason"] == "out_of_road"


def test_a_row_carries_the_outcome_fields_and_only_those(store, tree):
    # The fields the plan's notes say are comparable across machines. `actions_digest` and
    # `reward` differ between CPUs and are deliberately not columns.
    deliver(tree, "j1", record("j1", [scored("t_junction_0000")]))
    store.ingest()
    (row,) = store.rows("j1")
    assert row == {
        "job_id": "j1",
        "scenario_id": "t_junction_0000",
        "category": "t_junction",
        "status": "ok",
        "success": True,
        "steps": 40,
        "route_completion": 1.0,
        "cost": 0.0,
        "collisions": {"human": 0, "vehicle": 0},
        "failure_reason": None,
        "wall_time_s": 0.25,
        "actor_layout_digest": "abc123",
    }
    (job,) = store.jobs()
    assert (job["kind"], job["status"], job["bank_id"], job["n"]) == (
        "result", "complete", "t-junction", 1
    )
    assert job["policy"] == "scenariobank.policies:ExpertPolicy"
    assert job["summary"]["success_rate"] == 1.0
    assert job["options"]["kind"] == "pg"
    assert job["attempt"] == 1 and job["job_id"] == "j1" and job["error"] is None
    assert job["host"] is None, "no events.jsonl beside the record, so no host claimed"


def test_a_stopped_batch_is_listed_as_stopped(store, tree):
    deliver(tree, "j1", record("j1", [scored("t_junction_0000")], stopped=True))
    store.ingest()
    assert store.job("j1")["status"] == "stopped"


def test_the_model_is_read_off_the_job_beside_the_record(store, tree):
    deliver(tree, "j1", record("j1", [scored("t_junction_0000")]), checkpoint="av3-2026-09.pt")
    deliver(tree, "j2", record("j2", [scored("t_junction_0000")]))
    store.ingest()
    assert store.job("j1")["model"] == "av3-2026-09.pt"
    assert store.job("j2")["model"] is None


def test_a_delivery_in_flight_is_not_read_and_is_read_once_it_lands(store, tree):
    # `deliver()` copies to `<name>.partial` and renames. Until the rename, the directory may
    # be half a copy and the store must not open it.
    staged = deliver(tree, "j1" + PARTIAL_SUFFIX, record("j1", [scored("t_junction_0000")]))
    assert store.ingest() == Ingested(added=0, skipped=0, invalid=0, total=0)
    assert store.jobs() == []

    os.replace(staged, tree / "j1")
    assert store.ingest() == Ingested(added=1, skipped=0, invalid=0, total=1)
    assert store.job("j1")["status"] == "complete"


def test_an_attempt_is_evidence_and_not_a_score(store, tree):
    # `<job_id>.attempt<N>` is a run that did not run, delivered beside the job's own name so
    # the redelivery guard cannot mistake it for the result. Listed, with what it says about
    # itself, and with no rows.
    attempt(tree, "j1.attempt1", code=2)
    deliver(tree, "j1", record("j1", [scored("t_junction_0000")]))
    assert store.ingest() == Ingested(added=2, skipped=0, invalid=0, total=2)

    failed = store.job("j1.attempt1")
    assert (failed["kind"], failed["status"], failed["job_id"], failed["attempt"]) == (
        "attempt", "failed", "j1", 1
    )
    assert failed["error"].startswith("exit code 2; container.log ")
    assert failed["n"] is None and failed["summary"] is None
    assert store.rows("j1.attempt1") == []
    assert store.job("j1")["status"] == "complete" and len(store.rows("j1")) == 1


def test_a_record_that_does_not_validate_is_one_invalid_row_and_the_scan_goes_on(store, tree):
    # This is the `validate --results` Step 5 deferred to the store: a bad file is listed with
    # its error, never dropped, and never a reason to stop reading the directories after it.
    good = deliver(tree, "good", record("good", [scored("t_junction_0000")]))
    wrong = deliver(tree, "wrong-field", record("wrong-field", [scored("t_junction_0000")]))
    text = json.loads((wrong / "results.json").read_text())
    text["gpu"] = 0
    (wrong / "results.json").write_text(json.dumps(text))
    broken = tree / "broken"
    broken.mkdir()
    (broken / "results.json").write_text("{not json")
    empty = tree / "empty"
    empty.mkdir()

    assert store.ingest() == Ingested(added=4, skipped=0, invalid=3, total=4)
    assert store.job("good")["status"] == "complete"
    assert "gpu" in store.job("wrong-field")["error"]
    assert store.job("wrong-field")["kind"] == "result"
    assert store.rows("wrong-field") == []
    assert store.job("broken")["status"] == "invalid"
    assert store.job("empty")["error"] == "no results.json"
    assert good.is_dir(), "the store reads the tree and never writes it"
    # Fixed on the share and rebuilt: the row goes from invalid to complete, and stays one row.
    del text["gpu"]
    (wrong / "results.json").write_text(json.dumps(text))
    assert store.ingest().added == 0, "a name already indexed is skipped, invalid or not"
    assert store.rebuild() == Ingested(added=4, skipped=0, invalid=2, total=4)
    assert store.job("wrong-field")["status"] == "complete"


def test_rebuild_drops_what_the_tree_no_longer_has_and_ingest_alone_does_not(store, tree):
    deliver(tree, "j1", record("j1", [scored("t_junction_0000")]))
    deliver(tree, "j2", record("j2", [scored("t_junction_0000")]))
    store.ingest()
    for path in sorted((tree / "j2").rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    (tree / "j2").rmdir()

    assert store.ingest() == Ingested(added=0, skipped=1, invalid=0, total=2)
    assert [job["name"] for job in store.jobs()] == ["j2", "j1"]
    assert store.rebuild() == Ingested(added=1, skipped=0, invalid=0, total=1)
    assert [job["name"] for job in store.jobs()] == ["j1"]
    assert store.rows("j2") == []
    with pytest.raises(UnknownJob):
        store.job("j2")


def test_the_status_directory_is_the_rigs_and_never_a_job(store, tree):
    from scenariobank.agent import worker

    assert STATUS_DIR == worker.STATUS_DIR, "the store reads where the worker writes"
    status = tree / STATUS_DIR
    status.mkdir()
    (status / "sim-gpu0.json").write_text(
        json.dumps({"host": "sim", "gpu": 0, "consumer": "sim:gpu0", "state": "running",
                    "job_id": "j9", "progress": {"done": 3, "n": 35, "running": "x"},
                    "updated": "2026-09-15T10:00:00Z"})
    )
    (status / "sim-gpu1.json").write_text("{half a write")
    (status / ".status.abc.tmp").write_text("{}")
    deliver(tree, "j1", record("j1", [scored("t_junction_0000")]))

    assert store.ingest() == Ingested(added=1, skipped=0, invalid=0, total=1)
    rigs = store.rigs()
    assert [rig["file"] for rig in rigs] == ["sim-gpu0.json", "sim-gpu1.json"]
    assert rigs[0]["consumer"] == "sim:gpu0" and rigs[0]["progress"]["done"] == 3
    assert "error" in rigs[1], "a card whose file is broken is listed, not dropped"


def test_a_tree_that_is_not_there_yet_is_empty_rather_than_an_error(tmp_path):
    store = ResultsStore(tmp_path / "i.sqlite", tmp_path / "nowhere")
    assert store.ingest() == Ingested(added=0, skipped=0, invalid=0, total=0)
    assert store.jobs() == [] and store.rigs() == []


def test_the_index_is_a_file_where_it_was_asked_for_and_survives_reopening(tmp_path, tree):
    index = tmp_path / "deep" / "er" / "results.sqlite"
    deliver(tree, "j1", record("j1", [scored("t_junction_0000")]))
    ResultsStore(index, tree).ingest()
    assert index.is_file()
    again = ResultsStore(index, tree)
    assert again.ingest() == Ingested(added=0, skipped=1, invalid=0, total=1)
    assert len(again.rows("j1")) == 1


def test_the_host_is_read_off_the_run_started_event_and_nothing_else(store, tree):
    # The record has no host field; the runner's first event carries `socket.gethostname()`,
    # which under `--network host` is the rig's own name. A directory with no events, or one
    # whose events are not JSON, is a delivered result all the same.
    deliver(tree, "j1", record("j1", [scored("t_junction_0000")]), host="sim")
    deliver(tree, "j2", record("j2", [scored("t_junction_0000")]))
    j3 = deliver(tree, "j3", record("j3", [scored("t_junction_0000")]))
    (j3 / "events.jsonl").write_text("not json\n{\"event\": \"batch.started\"}\n")
    attempt(tree, "j4.attempt1")
    (tree / "j4.attempt1" / "events.jsonl").write_text(
        json.dumps({"event": "run.started", "host": "rig-b"}) + "\n"
    )
    store.ingest()
    hosts = {job["name"]: job["host"] for job in store.jobs()}
    assert hosts == {"j1": "sim", "j2": None, "j3": None, "j4.attempt1": "rig-b"}


def test_an_index_of_another_version_is_started_over_on_open(tmp_path, tree):
    import sqlite3

    from scenariobank.web.results import INDEX_VERSION

    deliver(tree, "j1", record("j1", [scored("t_junction_0000")]))
    index = tmp_path / ".studio" / "results.sqlite"
    assert ResultsStore(index, tree).ingest().added == 1

    # A Step 6 index: the same tables without the columns this version added, and no stamp.
    with sqlite3.connect(index) as connection:
        connection.executescript(
            "DROP TABLE rows; DROP TABLE jobs; PRAGMA user_version = 0;"
            "CREATE TABLE jobs (name TEXT PRIMARY KEY, delivered_at TEXT NOT NULL);"
            "INSERT INTO jobs VALUES ('stale', '2026-01-01T00:00:00Z');"
        )
    reopened = ResultsStore(index, tree)
    assert reopened.ingest() == Ingested(added=1, skipped=0, invalid=0, total=1), (
        "the old index was dropped and the tree read again; the stale row is gone"
    )
    assert [job["name"] for job in reopened.jobs()] == ["j1"]
    with sqlite3.connect(index) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == INDEX_VERSION
    # And the version stamp is what makes reopening at this version keep the index.
    assert ResultsStore(index, tree).ingest().skipped == 1


def test_samples_are_the_scored_rows_of_one_category_under_one_policy_newest_first(store, tree):
    expert = "scenariobank.policies:ExpertPolicy"
    deliver(
        tree,
        "old",
        record(
            "old",
            [
                scored("curve_0000", category="curve", wall_time_s=1.0),
                scored("curve_0001", category="curve", wall_time_s=2.0),
                scored("t_junction_0000", wall_time_s=9.0),
            ],
            levels={"traffic": "high"},
        ),
        host="rig-a",
    )
    os.utime(tree / "old", (1_700_000_000, 1_700_000_000))
    # An errored row's wall time is the time to a traceback, not to a drive.
    errored = scored("curve_0001", category="curve", wall_time_s=50.0, success=False)
    errored = errored.model_copy(update={"status": "error", "traceback": "boom"})
    deliver(
        tree,
        "new",
        record("new", [scored("curve_0000", category="curve", wall_time_s=3.0), errored]),
        host="rig-b",
    )
    deliver(
        tree,
        "other",
        record(
            "other",
            [scored("curve_0000", category="curve", wall_time_s=100.0)],
            policy="scenariobank.policies:StraightPolicy",
        ),
    )
    attempt(tree, "gone.attempt1")
    store.ingest()

    samples = store.samples(expert, "curve")
    assert [(s.wall_time_s, s.host, s.levels) for s in samples] == [
        (3.0, "rig-b", {}),
        (2.0, "rig-a", {"traffic": "high"}),
        (1.0, "rig-a", {"traffic": "high"}),
    ], "newest delivery first and last row first, the errored row and the other policy out"
    assert store.samples(expert, "t_junction")[0].wall_time_s == 9.0
    assert store.samples(expert, "roundabout") == []
    assert store.samples(expert, "curve", limit=1) == samples[:1]


def test_the_results_command_runs_on_a_machine_with_no_web_group(tmp_path, tree, monkeypatch):
    # A rig has the agent image and no FastAPI. Found there on 2026-09-15: the first run of
    # `scenariobank results` died importing the studio's api module for one constant.
    import subprocess
    import sys

    deliver(tree, "j1", record("j1", [scored("t_junction_0000")]))
    code = (
        "import sys; sys.modules['fastapi'] = None; sys.modules['uvicorn'] = None\n"
        "from typer.testing import CliRunner\n"
        "from scenariobank.cli import app\n"
        f"r = CliRunner().invoke(app, ['results', '--results-root', {str(tree)!r}, "
        f"'--index', {str(tmp_path / 'i.sqlite')!r}])\n"
        "print(r.output); sys.exit(r.exit_code)"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "added 1, skipped 0, invalid 0; 1 in" in done.stdout
    assert "j1" in done.stdout
