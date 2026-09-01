"""The job slot, and the one property everything else rests on: state lives on disk.

These drive `Jobs` directly with trivial Python subprocesses rather than through HTTP or the CLI.
The thing under test is the plumbing -- one slot, a log that fills, an exit file that appears when
and only when the job ends -- and a real command would only make it slower to prove.
"""

from __future__ import annotations

import json
import sys
import time

import pytest

from scenariobank.web.jobs import JobBusy, JobNotFound, Jobs

PRINT = [sys.executable, "-c", "print('hello from a job')"]
SLEEP = [sys.executable, "-c", "import time; time.sleep(30)"]


@pytest.fixture
def jobs(tmp_path):
    runner = Jobs(tmp_path / "jobs", workdir=tmp_path)
    yield runner
    running = runner.running()
    if running:
        runner.cancel(running["job_id"])


def settle(jobs, job_id, timeout=15.0):
    """Poll the way the page does, and return the job once it is no longer running."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = jobs.status(job_id)
        if status["state"] != "running":
            return status
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never finished")


def test_a_job_runs_and_its_output_lands_in_the_log(jobs):
    started = jobs.submit(PRINT, command="print")
    finished = settle(jobs, started["job_id"])
    assert finished["state"] == "finished"
    assert finished["exit_code"] == 0
    assert "hello from a job" in finished["log"]


def test_the_exit_file_appears_only_when_the_job_ends(jobs):
    started = jobs.submit(SLEEP, command="sleep")
    path = jobs.root / started["job_id"] / "exit"
    # Its presence is the definition of "finished", so it must never be written early: a job that
    # reads as done while its process is still holding a bank directory is the whole failure.
    assert not path.exists()
    assert jobs.status(started["job_id"])["state"] == "running"
    jobs.cancel(started["job_id"])
    settle(jobs, started["job_id"])
    assert path.exists()


def test_a_second_job_is_refused_and_the_refusal_names_the_first(jobs):
    running = jobs.submit(SLEEP, command="sleep")
    with pytest.raises(JobBusy) as raised:
        jobs.submit(PRINT, command="print")
    assert running["job_id"] in str(raised.value)
    assert "sleep" in str(raised.value)
    assert raised.value.holder["job_id"] == running["job_id"]


def test_a_restarted_studio_reads_the_same_answer_off_disk(jobs, tmp_path):
    finished = settle(jobs, jobs.submit(PRINT, command="print")["job_id"])
    # A brand new instance: no thread waiting on the process, nothing carried over in memory. If
    # any of this lived in a variable, a restarted studio would lose the job -- so ask a stranger.
    stranger = Jobs(tmp_path / "jobs", workdir=tmp_path)
    again = stranger.status(finished["job_id"])
    assert again["state"] == "finished"
    assert again["exit_code"] == 0
    assert again["log"] == finished["log"]
    assert [row["job_id"] for row in stranger.recent()] == [finished["job_id"]]


def test_a_job_whose_process_is_gone_without_an_exit_code_is_lost_not_running(jobs):
    # The studio was killed mid-job: no exit file was ever written, and the pid is long gone.
    # Reporting that as "running" would wedge the slot forever, which is worse than saying so.
    path = jobs.root / "20200101-000000-abcdef"
    path.mkdir(parents=True)
    (path / "meta.json").write_text(
        json.dumps(
            {
                "job_id": path.name,
                "command": "generate",
                "argv": ["generate"],
                "pid": 2**22 - 1,
                "start_time": "1",
                "studio_pid": 2**22 - 2,
                "studio_start_time": "1",
                "started": 0.0,
            }
        )
    )
    assert jobs.status(path.name)["state"] == "lost"
    assert jobs.running() is None


def test_the_log_can_be_read_from_an_offset(jobs):
    finished = settle(jobs, jobs.submit(PRINT, command="print")["job_id"])
    tail = jobs.status(finished["job_id"], since=finished["log_size"])
    assert tail["log"] == ""
    head = jobs.status(finished["job_id"], since=0)
    assert head["log"] == finished["log"]


def test_cancelling_stops_the_job(jobs):
    started = jobs.submit(SLEEP, command="sleep")
    jobs.cancel(started["job_id"])
    finished = settle(jobs, started["job_id"])
    assert finished["state"] == "finished"
    # Negative: killed by a signal rather than exiting on its own.
    assert finished["exit_code"] < 0


@pytest.mark.parametrize("job_id", ["../../etc", "nope", "20200101-000000-ABCDEF", ""])
def test_an_id_that_is_not_a_job_id_is_refused_before_it_reaches_the_disk(jobs, job_id):
    # The id arrives in a URL path. Reading `<root>/<id>` without checking its shape is how a
    # status endpoint becomes a file reader.
    with pytest.raises(JobNotFound):
        jobs.status(job_id)
