"""`events.py`: the lines a supervisor reads, and the two files it reads when there is no log.

Nothing here needs a simulator, a bank or a CLI -- the module imports none of them, which is the
point: an agent on a rig reads a run's events with `scenariobank` installed and nothing else.
What `run_bank` and `run` actually emit, in order, is pinned in `test_results.py`, where the
fake env and the bank live.

The one rule worth stating twice: a line is JSON or it is nothing. A container's stdout carries
panda3d's and torch's own chatter, so a reader drops what does not parse -- and an event must
never be the thing that does not parse, which is why a speed the env cannot give is `null` and
never `nan`.
"""

from __future__ import annotations

import json
from io import StringIO

import pytest
from pydantic import ValidationError

from scenariobank.events import (
    EVENTS_SCHEMA_VERSION,
    BatchStarted,
    Emitter,
    Heartbeat,
    RunFinished,
    RunStarted,
    ScenarioFinished,
    ScenarioStarted,
    read_events,
    read_exit_code,
    write_exit_code,
)


def a_run_started(**fields) -> RunStarted:
    defaults = dict(
        out="/out/j7", bank="/work/banks/t-junction", policy="p:Q", pid=1, host="rig-1"
    )
    return RunStarted(**{**defaults, **fields})


EVERY_EVENT = (
    a_run_started(job_id="j7", attempt=1),
    BatchStarted(
        bank_id="t-junction", source="pg", policy="p:Q", n=2, scenarios=["a", "b"],
        step_hz=10.0, decision_hz=None, stride=1,
    ),
    ScenarioStarted(scenario_id="a", category="t_junction", index=1, n=2, max_steps=300),
    Heartbeat(scenario_id="a", elapsed_s=10.0, step=100, decision=20, speed_mps=3.2, moved_m=9.9),
    ScenarioFinished(
        scenario_id="a", index=1, n=2, status="ok", success=True, failure_reason=None,
        steps=120, wall_time_s=11.5,
    ),
    RunFinished(outcome="ok", exit_code=0, n=2, success_rate=0.5),
)


@pytest.mark.parametrize("event", EVERY_EVENT, ids=lambda event: event.event)
def test_every_event_is_one_line_of_json_that_says_which_it_is(event):
    """One object per line, and the line carries its own version. A reader that has to know
    which event it is looking at before it can parse the line is a reader that has to be told
    the order things happen in."""
    line = event.line()
    assert "\n" not in line
    parsed = json.loads(line)
    assert parsed["event"] == event.event
    assert parsed["schema_version"] == EVENTS_SCHEMA_VERSION
    # The name is the second key, before any payload: a person reading a tail sees it first.
    assert list(parsed)[:2] == ["schema_version", "event"]


def test_an_event_refuses_a_field_nobody_reads():
    # `extra="forbid"`, the same rule `Job` and `Results` are held to: a field written here and
    # not read, or read and not written, fails rather than passing as a blank.
    with pytest.raises(ValidationError):
        a_run_started(gpu=0)


def test_a_speed_the_env_cannot_give_is_null_and_never_nan():
    """`json.dumps(float("nan"))` writes the bare token `NaN`, which is not JSON: a strict
    reader refuses the whole line over it, and the line it refuses is the one saying the car is
    not moving. The prose line still prints `nan`, where a person is the reader."""
    line = Heartbeat(scenario_id="a", elapsed_s=1.0, step=1, decision=1, moved_m=0.0).line()
    assert '"speed_mps": null' in line
    assert "NaN" not in line

    def refuse(token: str) -> float:
        raise AssertionError(f"not JSON: {token}")

    json.loads(line, parse_constant=refuse)


def test_the_emitter_appends_so_a_second_attempt_never_erases_the_first(tmp_path):
    """The queue is at-least-once, so a job's second attempt can land in the directory the
    first one used. Its results overwrite; its events do not, and every line says which attempt
    it belongs to."""
    path = tmp_path / "deep" / "events.jsonl"
    first = Emitter(path)
    first(a_run_started(job_id="j7", attempt=1))
    second = Emitter(path)
    second(a_run_started(job_id="j7", attempt=2))

    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert [line["attempt"] for line in lines] == [1, 2]
    assert second.seen == {"run.started"}


def test_the_emitter_prints_exactly_what_it_appends(tmp_path):
    path = tmp_path / "events.jsonl"
    stream = StringIO()
    emitter = Emitter(path, stream=stream)
    emitter(a_run_started())
    emitter(RunFinished(outcome="ok", exit_code=0))
    assert stream.getvalue() == path.read_text()
    assert emitter.seen == {"run.started", "run.finished"}


def test_an_emitter_with_no_file_writes_none(tmp_path):
    # `run_bank` is called by the studio's job engine and by `calibrate` with no sink at all.
    stream = StringIO()
    Emitter(stream=stream)(a_run_started())
    assert stream.getvalue().count("\n") == 1
    assert list(tmp_path.iterdir()) == []


def test_an_exit_code_is_written_whole_or_not_at_all(tmp_path):
    """Staged and renamed, because a reader that catches a half-written file parses `0` and
    reports a failed run as a success. Nothing is left behind to be read by mistake."""
    written = write_exit_code(tmp_path / "j7", 1)
    assert written.read_text() == "1\n"
    assert [path.name for path in (tmp_path / "j7").iterdir()] == ["exit_code"]
    assert read_exit_code(tmp_path / "j7") == 1
    assert read_exit_code(write_exit_code(tmp_path / "j7", 0).parent) == 0


def test_no_exit_code_is_not_an_exit_code(tmp_path):
    """`None` is "this run did not get to say" -- a container killed outright, an OOM, the
    machine going down -- and a supervisor treats that as its own outcome rather than as any
    code in particular. It is never read as a zero."""
    assert read_exit_code(tmp_path / "never-ran") is None
    (tmp_path / "exit_code").write_text("killed\n")
    assert read_exit_code(tmp_path) is None


def test_reading_a_stream_keeps_our_lines_and_drops_the_simulators(tmp_path):
    """What a container's stdout actually looks like. `events.jsonl` holds only ours and loses
    nothing here; the log holds both, and the reader is the same one."""
    path = tmp_path / "container.log"
    path.write_text(
        "No OpenGL_accelerate module loaded: No module named 'OpenGL_accelerate'\n"
        + a_run_started(job_id="j7").line() + "\n"
        + ":device(error): Error opening directory /dev/input: No such file or directory\n"
        + "{not json at all\n"
        + '{"this": "parses but is not an event"}\n'
        + RunFinished(job_id="j7", outcome="ok", exit_code=0).line() + "\n"
    )
    assert [item["event"] for item in read_events(path)] == ["run.started", "run.finished"]
