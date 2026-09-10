"""The two records everything downstream reads: a `Job`, which says what to run, and `Results`.

Designed once, here, before any policy worth scoring exists -- so the record is a schema that
Steps 4-5, Phase 5 and Phase 7 all `jq` the same fields out of, rather than whatever the first
run happened to emit. Both models are `extra="forbid"`: a field written here and not read, or
read and not written, fails validation instead of passing as a blank.

**`Job` is the one input.** The CLI's `run` flags build one, the container's entrypoint reads one
from a file, and the queue message payload *is* one -- the same model at all three, which is why
`run_bank(job, out)` is the only entry point and why the studio can submit a run without a
schema of its own. It names the bank by id *and* path: the path is where a mounted bank is, the
id is what refuses the wrong bank at that path. The options travel as names (a tier, levels, raw
numbers) and are resolved against the bank on the machine that runs it, by `resolve_options`.

**`Results` carries two queue facts and no more.** `job_id` and `attempt` are what let a result
delivered twice -- the queue is at-least-once -- be recognised as the same run, and they are
`null` from the CLI. Below the orchestrator the queue is otherwise invisible.

**`failure_reason` is a string, taken from the `TerminationState` keys actually present in
`info`, in a fixed precedence.** The list this project used to carry included `idle`, which
`TerminationState` defines (`constants.py:34`) and nothing in `metadrive_env.py`, `base_env.py`
or `scenario_env.py` ever writes; `crash` (the aggregate) and `env_seed` *are* written and were
not in it. `TERMINATIONS` below is the measured list, worst first, and `replay.ENDINGS` phrases
the same keys in the same order so the diagnostic and the record cannot rank an ending
differently. One reason is ours rather than the env's: `stopped`, for an episode the batch was
told to end. Nothing here imports the simulator.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from scenariobank.options import ResolvedOptions
from scenariobank.workspace import Provenance

#: Bumped when a field is added, removed or changes meaning. A reader that validates against the
#: wrong version fails rather than coercing, which is the point of `Literal` with no default.
RESULTS_SCHEMA_VERSION = 1
JOB_SCHEMA_VERSION = 1

#: The `TerminationState` keys the two envs actually write into `info`, worst first. The order
#: is the precedence `failure_reason` reads them in: a crash on the last frame of a recording
#: sets `crash_vehicle`, `crash` *and* `max_step`, and the crash is the half that matters.
#: `arrive_dest` sits above `max_step` and below the crashes, so arriving while hitting something
#: still reads as the collision; `success` is its own field and records the arrival regardless.
#: Measured against `metadrive_env.py:138-157` and `scenario_env.py:164-173`.
TERMINATIONS: tuple[str, ...] = (
    "crash_vehicle",
    "crash_object",
    "crash_building",
    "crash_human",
    "crash_sidewalk",
    "crash",
    "out_of_road",
    "arrive_dest",
    "max_step",
)

#: The `info` key that means the episode succeeded.
SUCCESS_KEY = "arrive_dest"

#: The one reason the env never writes: the batch was told to stop, and the loop did. A stopped
#: episode is neither capped nor budgeted, and it is not a failure of the policy either -- which
#: is why it is a reason of its own rather than `max_step` by another name.
STOPPED = "stopped"

#: What the loop's own cap reads as. The cap is the row's `max_steps` (`entry.budget_for(row)`),
#: so an episode the loop capped with the env still going ended for the same reason one the env
#: capped did, and the record says so with the env's own word for it.
CAPPED = "max_step"


def failure_reason(
    info: Mapping[str, Any], *, stopped: bool = False, capped: bool = False
) -> str | None:
    """The one reason an episode did not succeed, or `None` if it did.

    `stopped` wins outright: the loop checks its stop before a step, so a stopped episode has no
    ending of the env's to report. Then the env's flags in `TERMINATIONS` order, then the loop's
    own cap. An episode that ended with none of these is one the env terminated without saying
    why, and that reads as `None` -- "not a failure the env named" -- rather than as a guess.
    """
    if stopped:
        return STOPPED
    for key in TERMINATIONS:
        if info.get(key):
            return None if key == SUCCESS_KEY else key
    return CAPPED if capped else None


class ScenarioResult(BaseModel):
    """One scenario, scored. `status: "error"` rows carry a traceback and nothing measured."""

    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    category: str
    #: The seed a procedural row is built from, or the index a recorded one is opened at. One is
    #: set and the other `None`, the way `replay.Episode` reports them.
    seed: int | None = None
    scenario_index: int | None = None
    #: Where the route ended, read back off the navigation. `None` on a recording.
    destination: str | None = None
    #: `ok` if the episode ran to an end the loop or the env chose; `error` if anything raised.
    #: A stopped episode is `ok`: it was scored as far as it went.
    status: Literal["ok", "error"]
    #: `info["arrive_dest"]` on the last step. False on an error row.
    success: bool
    #: `failure_reason()`'s answer. `None` on success and on an error row.
    failure_reason: str | None
    steps: int
    #: How many actions were issued: `ceil(steps / stride)`.
    actions: int
    reward: float
    cost: float
    wall_time_s: float
    #: Rising-edge counts by `runner.COLLISION_FLAGS` name. Every name present, at zero if unseen,
    #: because an omitted count reads as "not measured" and that is a different claim from "none".
    collisions: dict[str, int] = Field(default_factory=dict)
    route_completion: float | None = None
    #: `fingerprint.sha256_hex` over the per-decision action stream. Two runs of one policy on one
    #: scenario that diff here differ in what they did, whatever their summaries say.
    actions_digest: str | None = None
    #: Every object in the scene after the reset, by MetaDrive class name: the ego, the traffic,
    #: the cones, the people. What the option managers actually placed on this road -- which on
    #: an `X`, `T` or `O` road is no cone or barrier at any level. Empty on an error row.
    placed: dict[str, int] = Field(default_factory=dict)
    #: `VRUManager.layout_digest()`: where every pedestrian and cyclist was put and walks
    #: between. `None` when no actor manager was registered. A sibling of the bank's
    #: `lane_geometry_digest`, measured on the run rather than stored.
    actor_layout_digest: str | None = None
    traceback: str | None = None


class Summary(BaseModel):
    """The batch in four numbers. `n` counts every row, errors and stopped rows included."""

    model_config = ConfigDict(extra="forbid")

    n: int
    #: Successes over `n`. 0.0 for an empty batch rather than a division error.
    success_rate: float
    #: Rows per `failure_reason`, over the rows that have one. Successes and error rows are
    #: not in it; they are in `success_rate` and `by_status`.
    by_failure_reason: dict[str, int] = Field(default_factory=dict)
    by_status: dict[str, int] = Field(default_factory=dict)


class BankInfo(BaseModel):
    """Which bank was run. Enough to find it again and to say what kind it is."""

    model_config = ConfigDict(extra="forbid")

    #: The path the run opened, as given. A mounted bank's path is the rig's, not the NAS's.
    path: str
    id: str
    #: `Manifest.source`: `pg`, or the recording's dataset name.
    source: str
    #: The manifest's `schema_version`. (`schema` is a pydantic name, so it cannot be the field.)
    schema_version: str
    #: On a recorded bank, the first run entry's chain and licence line; both must survive into
    #: a result. `None` on a procedural bank, which has neither.
    provenance: Provenance | None = None
    attribution: str | None = None


class EnvInfo(BaseModel):
    """The env every row was driven through, as the run saw it."""

    model_config = ConfigDict(extra="forbid")

    #: At the first reset and after the last step of the run. Two numbers because "it stayed 19"
    #: is the claim, and Step 4's expert-leak check is that the two agree.
    observation_shape_before: tuple[int, ...] | None
    observation_shape_after: tuple[int, ...] | None
    #: The rate one `env.step` advances at, from `env.step_hz_for`. One per run.
    step_hz: float
    decision_hz: float | None
    #: Env steps per action, from `runner.stride_for`.
    stride: int
    #: The simulator's commit, read off the installed distribution. `None` where it cannot be.
    metadrive_commit: str | None = None
    #: The camera rig on the ego, as the spec's path (`run --camera-rig`). `None` without one.
    camera_rig: str | None = None
    #: The rate that spec declared, in seconds; `None` without a rig or a declaration. Beside
    #: `step_hz` and `stride` it says whether the rig was read at its own rate.
    rig_tick_rate_s: float | None = None


class Results(BaseModel):
    """A batch, scored: what was run, with what, and what each scenario did."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    started_utc: str
    finished_utc: str
    #: The queue message's id, or the studio-minted one. `None` from the CLI.
    job_id: str | None = None
    #: The lease's `attempts`, so a redelivered job's second result says it is one. `None` from
    #: the CLI.
    attempt: int | None = None
    #: The batch was told to stop and did. The rows present are every scenario that ended before
    #: the stop, plus the one it landed in at `failure_reason: "stopped"`; nothing after it ran.
    stopped: bool = False
    bank: BankInfo
    #: The `load_policy` spec that was run, as given.
    policy: str
    options: ResolvedOptions
    env: EnvInfo
    results: list[ScenarioResult]
    summary: Summary


class JobBank(BaseModel):
    """The bank a job names: where it is, and which one it must be."""

    model_config = ConfigDict(extra="forbid")

    #: `Manifest.bank_id`. Checked against the manifest at `path`, so a job for one bank cannot
    #: quietly run against another that was mounted at the same place. `None` skips the check.
    id: str | None = None
    #: Where the runner opens it. Relative paths are relative to the process's working directory.
    path: str


class JobOptions(BaseModel):
    """`resolve_options`' three arguments, as names. Resolved on the machine that runs the job."""

    model_config = ConfigDict(extra="forbid")

    tier: str | None = None
    levels: dict[str, str] = Field(default_factory=dict)
    raw: dict[str, float] = Field(default_factory=dict)


class Job(BaseModel):
    """What to run. The CLI's input, the container's input and the queue's payload, one model."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    #: Minted by whoever submits -- the studio, before `put()`, so it can double as the queue's
    #: `dedupe_key`; the orchestrator, from the message. `None` from the CLI.
    job_id: str | None = None
    #: The lease's `attempts`, handed down by the orchestrator so the result can carry it.
    attempt: int | None = None
    bank: JobBank
    #: Scenario ids to run, in the bank's own order whatever order they are given in. `None`
    #: runs the whole bank.
    scenarios: list[str] | None = None
    options: JobOptions = Field(default_factory=JobOptions)
    #: A `pkg.mod:Name` for `load_policy`.
    policy: str
    #: Handed to the policy's constructor as `checkpoint_path=` when set. What Step 7's AV3
    #: policy loads its weights from; the two diagnostic policies take none.
    checkpoint_path: str | None = None
    #: The decision rate, as a stride in the loop. `None` decides at every step.
    decision_hz: float | None = None
    #: Write each scenario's per-decision action stream beside its result. Off by default: it is
    #: the only artifact of a run whose size grows with the episode.
    save_trajectories: bool = False


class Trajectory(BaseModel):
    """One scenario's per-decision action stream, written only with `save_trajectories`."""

    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    #: Env steps each action was held for, so a reader can place each action in time.
    stride: int
    actions: list[list[float]]


def summarize(results: list[ScenarioResult]) -> Summary:
    """The four numbers over a list of rows."""
    by_reason: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for result in results:
        by_status[result.status] = by_status.get(result.status, 0) + 1
        if result.failure_reason is not None:
            by_reason[result.failure_reason] = by_reason.get(result.failure_reason, 0) + 1
    successes = sum(1 for result in results if result.success)
    return Summary(
        n=len(results),
        success_rate=round(successes / len(results), 6) if results else 0.0,
        by_failure_reason=dict(sorted(by_reason.items())),
        by_status=dict(sorted(by_status.items())),
    )


def dump_json(model: BaseModel) -> str:
    """A model as the house JSON: indented, keys sorted, trailing newline. Byte-stable."""
    return json.dumps(model.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"


def write_json(path: Path, model: BaseModel) -> Path:
    """Write a model to `path`, creating the directory, and return the path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_json(model))
    return path


__all__ = [
    "CAPPED",
    "JOB_SCHEMA_VERSION",
    "RESULTS_SCHEMA_VERSION",
    "STOPPED",
    "SUCCESS_KEY",
    "TERMINATIONS",
    "BankInfo",
    "EnvInfo",
    "Job",
    "JobBank",
    "JobOptions",
    "Results",
    "ScenarioResult",
    "Summary",
    "Trajectory",
    "dump_json",
    "failure_reason",
    "summarize",
    "write_json",
]
