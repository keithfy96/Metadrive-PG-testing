"""What a run is doing, as one JSON object per line: the supervisor's half of a run's output.

The container prints prose for a person and this for a machine, and the two are never the same
bytes. The reason is the reference implementation: `rig/progress.py` in wing-sim scrapes four
prose patterns out of CARLA's log with unanchored regexes, and its own comments say why every
one of them is fragile -- loguru's formatter and `docker compose`'s per-container prefix each
contribute a `|`, embedded newlines mean only the first line of a record carries a prefix at
all, and an em dash in a source string is load-bearing. That file has no choice. We do: the run
is ours, so it emits the events rather than leaving them to be recovered.

**One object per line, and a line is independently readable.** Every event carries the schema
version, its name, the UTC second, and the job id and attempt it belongs to -- so a log holding
two attempts of one job, or one attempt's stdout interleaved with MetaDrive's own chatter, is
still legible by reading the lines that parse and ignoring the ones that do not. Field order is
pydantic's, not sorted, so the name is the second key a reader's eye lands on; the file the
house writes with `dump_json` (indented, sorted) is the other shape and is for reading, not for
streaming.

**Where they go** (`Emitter`): appended to `<out>/events.jsonl` always, and printed on stdout
with `run --events`. The file is the durable copy -- a pipe dies with its reader, and the agent
that launched the container may be restarted mid-run -- and it is appended, never truncated, so
a second attempt writing into a directory the first one used does not erase what the first one
said. The events a reader needs to tell those two apart are on every line.

**Two of these events are also files, and that is the progress signal.** `batch.started` is
written to `<out>/batch.json` and `scenario.started` to `<out>/starts/<scenario_id>.json`, each
the same model that goes down the stream; `<out>/results/<scenario_id>.json` (a `ScenarioResult`,
written by `run_bank` since Phase 4 Step 3) is the end of the same scenario. So a bar moves
without reading a log at all: the denominator is `batch.json`'s `n`, the numerator is the count
of files in `results/`, and the row running now is the one in `starts/` with no result yet.
Nothing here is state a process holds -- every answer is read back off disk, which is the
property Phase 7 rests on and the reason an agent restarted mid-run still describes the run
correctly.

**`exit_code` is written last, atomically, and always.** `write_exit_code` stages a `.tmp` and
renames it, because a half-written file read as `0` is a failed run reported as a success. The
entrypoint writes it in a `finally`, so a refusal that never opened the simulator leaves one
too; the only way to find none is a process that was killed outright (SIGKILL, OOM, the machine
going down), and that absence is itself the answer -- wing-sim calls that outcome VANISHED.

Nothing in this module imports the simulator, the runner or the CLI, so a supervisor can read a
run's events with `scenariobank` installed and nothing else.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Literal

from pydantic import BaseModel, ConfigDict, Field

#: Bumped when an event is added, removed or changes meaning. On every line, for the same reason
#: `Results` carries one: a reader that validates against the wrong version fails rather than
#: coercing.
EVENTS_SCHEMA_VERSION = 1

#: The four names the run writes under `--out`. Constants because the agent (Step 3) reads them
#: and a string typed twice is a string that will one day differ.
EVENTS_FILE = "events.jsonl"
EXIT_CODE_FILE = "exit_code"
BATCH_FILE = "batch.json"
STARTS_DIR = "starts"


def utc_now() -> str:
    """The UTC second, in the format every record in this project stamps."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Event(BaseModel):
    """What every event carries. `extra="forbid"`, like the two records it sits beside."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = EVENTS_SCHEMA_VERSION
    event: str
    #: When the event was made, not when it was written.
    utc: str = Field(default_factory=utc_now)
    #: The queue message's id and the lease's `attempts`, copied from the job onto every line so
    #: no line depends on the one before it. `None` from the CLI, which mints neither.
    job_id: str | None = None
    attempt: int | None = None

    def line(self) -> str:
        """The event as the one line it is written as, without the newline."""
        return json.dumps(self.model_dump(mode="json"))


class RunStarted(Event):
    """The process read a job and is about to run it. The first line of a run that got that far.

    Emitted after the job parses and before anything is validated against a bank, so a supervisor
    reading only the stream knows what it is watching before any refusal can happen. A job file
    that does not parse produces no `run.started` at all -- the stream opens with `run.finished`,
    `permanent: true`, which is the honest shape: nothing was ever identified to run.
    """

    event: Literal["run.started"] = "run.started"
    #: Where the batch writes, after `--out`'s tier subdirectory is applied. `events.jsonl` and
    #: `exit_code` are NOT in here when the job names a tier: they belong to the process and live
    #: in the directory the supervisor named, which is this one's parent.
    out: str
    #: The job file, when the job came from one.
    job_file: str | None = None
    bank: str
    bank_id: str | None = None
    policy: str
    pid: int
    #: `socket.gethostname()`. With `--network host` that is the rig's name, which is the one
    #: thing a delivered result cannot otherwise say about where it was scored.
    host: str


class BatchStarted(Event):
    """Every refusal passed; the first env is about to be built. Also `<out>/batch.json`.

    This is the bar's denominator and it is written before the simulator opens, so a progress
    bar is correct from the first second of a run rather than from the first finished scenario.
    """

    event: Literal["batch.started"] = "batch.started"
    bank_id: str
    #: `Manifest.source`: `pg`, or the recording's dataset name.
    source: str
    policy: str
    #: How many scenarios this run scores, and which, in the order they will run.
    n: int
    scenarios: list[str]
    step_hz: float
    decision_hz: float | None
    #: Env steps per action.
    stride: int


class ScenarioStarted(Event):
    """One scenario is about to be built. Also `<out>/starts/<scenario_id>.json`.

    `max_steps` is that row's own budget, scaled to the run's step rate -- the denominator a
    heartbeat's `step` counts against, so a row's own progress is readable without knowing the
    bank.
    """

    event: Literal["scenario.started"] = "scenario.started"
    scenario_id: str
    category: str
    #: 1-based, out of `n`. The same `n` `batch.started` gave.
    index: int
    n: int
    max_steps: int


class ScenarioFinished(Event):
    """One scenario ended, however it ended. `<out>/results/<scenario_id>.json` is the record.

    A summary, not the record: the fields here are the ones a queue's progress line shows. An
    error row is `status: "error"` with its traceback in the file and not on the line, because a
    traceback in a stream a supervisor parses is the thing that makes it parse prose again.
    """

    event: Literal["scenario.finished"] = "scenario.finished"
    scenario_id: str
    index: int
    n: int
    status: Literal["ok", "error"]
    success: bool
    failure_reason: str | None
    steps: int
    wall_time_s: float


class Heartbeat(Event):
    """The car is still moving, or is not. One per `--heartbeat` interval of wall time.

    The same reading `runner.Heartbeat` prints as prose on stderr, and the reason it exists at
    all: a scored row with a model on the car takes about a second per decision and prints
    nothing until it ends, which reads exactly like a hang. A supervisor extends its lease on a
    clock and not on this, but a run whose last heartbeat is twenty minutes old is a run to look
    at.
    """

    event: Literal["heartbeat"] = "heartbeat"
    scenario_id: str
    #: Seconds since the row's reset, not since the run began.
    elapsed_s: float
    step: int
    decision: int
    #: `None` where the env has no body to ask. Not `nan`: `json.dumps` writes that as the
    #: bare token `NaN`, which is not JSON and which a strict reader refuses the whole line
    #: over. The prose line still prints `nan`, where a person is the reader.
    speed_mps: float | None = None
    #: Metres between this reading and the one before it. Zero over several readings is the
    #: shape of a car that has stopped, which is the AV3 model's own failure and not the rig's.
    moved_m: float
    route_completion: float | None = None
    #: The action being held at the moment of the reading.
    action: list[float] | None = None


class RunFinished(Event):
    """The last line, whatever happened. Always emitted, and always the last event of a run.

    `permanent` is the one judgement in this file: nothing ran, and nothing this machine does
    will change that -- the bank at that path is a different bank, a scenario id is not in it,
    the policy will not import, an option level does not exist. A supervisor dead-letters that
    rather than spending the job's remaining attempts on it. Anything that got as far as
    `batch.started` is not permanent: a failure after the first env was built may be the card,
    the driver or the bridge, and those are worth another rig.
    """

    event: Literal["run.finished"] = "run.finished"
    outcome: Literal["ok", "failed"]
    exit_code: int
    #: The batch was told to stop and did. The rows it scored before the stop are in the record.
    stopped: bool = False
    #: The summary, when there is one to give.
    n: int | None = None
    success_rate: float | None = None
    results: str | None = None
    #: One line, the same text the prose failure prints. `None` on the way out of a good run.
    error: str | None = None
    permanent: bool = False


class Emitter:
    """Where an event goes: appended to a file, printed on a stream, or both.

    Opened and closed per event rather than held: a run emits a few hundred of these against a
    forward pass that costs a second, so the syscalls are free, and a line already on disk cannot
    be lost in a buffer by a process that is killed. `seen` is what the entrypoint reads to know
    whether the batch ever started, which is how `run.finished` decides `permanent`.
    """

    def __init__(self, path: Path | None = None, stream: IO[str] | None = None) -> None:
        self.path = path
        self.stream = stream
        self.seen: set[str] = set()
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, event: Event) -> None:
        line = event.line()
        if self.path is not None:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        if self.stream is not None:
            self.stream.write(line + "\n")
            self.stream.flush()
        self.seen.add(event.event)


def write_exit_code(directory: Path, code: int) -> Path:
    """Write `code` into `<directory>/exit_code`, atomically, and return the path.

    Staged and renamed for the reason wing-sim's launch script does the same: a reader that
    catches a half-written file parses `0` and reports a failed run as a success. The rename is
    the commit.
    """
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / EXIT_CODE_FILE
    staged = directory / f"{EXIT_CODE_FILE}.tmp"
    staged.write_text(f"{int(code)}\n")
    staged.replace(final)
    return final


def read_exit_code(directory: Path) -> int | None:
    """The code a finished run left, or `None` when there is none to read.

    `None` is not a failure: it is "this run did not get to say", which a supervisor treats as
    the container having been killed outright rather than as any exit code in particular.
    """
    try:
        return int((directory / EXIT_CODE_FILE).read_text().strip())
    except (OSError, ValueError):
        return None


def read_events(path: Path) -> list[dict]:
    """Every line of an event stream that parses as an object, in order; the rest dropped.

    A container's stdout carries panda3d's and torch's own lines as well as ours, so a reader
    that refuses a file over one foreign line is a reader that cannot read the log it was given.
    `events.jsonl` holds only ours and loses nothing here.
    """
    parsed: list[dict] = []
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and "event" in item:
            parsed.append(item)
    return parsed


__all__ = [
    "BATCH_FILE",
    "EVENTS_FILE",
    "EVENTS_SCHEMA_VERSION",
    "EXIT_CODE_FILE",
    "STARTS_DIR",
    "BatchStarted",
    "Emitter",
    "Event",
    "Heartbeat",
    "RunFinished",
    "RunStarted",
    "ScenarioFinished",
    "ScenarioStarted",
    "read_events",
    "read_exit_code",
    "utc_now",
    "write_exit_code",
]
