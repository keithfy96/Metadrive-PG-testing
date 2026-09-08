"""The step loop. One of them, for both bank kinds, and every caller goes through it.

`replay.drive` owned this loop from Phase 3 Step 6, and it refused procedural banks precisely so
it would not become a second runner. Step 2 moves the loop here and turns `replay` into a caller
of it, so `grep -n "env.step(" src/` returns one site. Everything kind-specific -- which env, which
seed, what `prepare` does -- is settled in `env.py` before this module is reached; what arrives
here is an env that has been built, a seed, a prepare step, a cap, something that acts and,
optionally, a stop.

**The stop is read once per step, before the step.** A batch that is told to stop (Step 3's
`run_bank`, on SIGTERM) sets a flag; the loop reads it through `stop()` and ends the episode
`stopped` rather than letting the signal unwind the process with nothing written and no
`env.close()`. The flag is the loop's only contact with the outside: it never installs a handler
itself, so `replay` and the tests hand it nothing and the loop behaves as before.

**The cap is enforced here, on top of `horizon`.** `horizon` is an env-level guard and one env
carries one of them, but a row's budget is per row (`entry.budget_for(row)`: its own `max_steps`
if it declares one, else its entry's). So the loop counts, and stops at the cap whether or not
the env has said so -- which is also what turns a `horizon` that failed to take into a bounded
run rather than a silent 1000-step one. Belt and braces, on both kinds, identically.

**Collisions are counted on the rising edge.** `crash_vehicle` and its siblings are per-step
booleans (`base_vehicle.py:43-45`, set at `:788-794`), and `contact_results` is a set of *type
names* (`:61`, `:798`), so "once per vehicle per episode" needs an identity MetaDrive does not
hand over. Counting each flag's low-to-high transitions is the cheap correct thing, and it is what
the converter's complaint -- a per-step count "reports one collision as thirty, and the number
describes the frame rate" -- is actually about. Per-vehicle identity via the `contactTest` nodes
is a Step 3 check to attempt, not a promise made here.

**What was placed is read off the scene, not off the levels.** After the reset the loop counts
the engine's objects by class -- cones, barriers, people, traffic, the ego -- into `placed`,
and asks the actor manager for its layout digest when one is registered. A manager that is
registered and places nothing (the obstacle axes on an `X`, `T` or `O` road) then shows as
nothing placed, in the row, rather than as a success rate that did not move.

**The decision rate is a stride in this loop**, never a MetaDrive key: the same action is handed
to `env.step` until the next decision is due, so a slower rate changes how many actions are
issued and never how long the episode is.

**The batch is `run_bank(job, out)`, and it never aborts.** One env per entry, every row of the
job through the loop above, each row's result written to `<out>/results/<scenario_id>.json` the
moment it ends and `<out>/results.json` assembled from those last. A row that raises -- in the
policy, in `reset`, anywhere -- is a `status: "error"` row with a traceback, and the next row
runs. A batch told to stop (SIGTERM, SIGINT) ends the row it is in as `stopped`, writes what it
has, and closes the env on the normal path: the handlers are ours and set a flag, so nothing is
ever raised into `env.close()` -- the panda3d/bullet teardown wedge that once needed a reboot.
The per-row file is the progress signal Phase 7's orchestrator extends a lease off, and the
reason a run killed at 30 of 35 is a scored partial run rather than a lost one.
"""

from __future__ import annotations

import signal
import threading
import time
import traceback
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scenariobank.bank import Manifest, RealWorldEntry, read_manifest
from scenariobank.env import Entry, Row, build_env, seed_for, step_hz_for
from scenariobank.fingerprint import sha256_hex
from scenariobank.options import resolve_options
from scenariobank.results import (
    RESULTS_SCHEMA_VERSION,
    BankInfo,
    EnvInfo,
    Job,
    Results,
    ScenarioResult,
    Trajectory,
    failure_reason,
    summarize,
    write_json,
)

#: The `info` flags a collision count is read off, as `info` key -> the name the count is kept
#: under. In the order `metadrive_env.py:138-142` writes them.
COLLISION_FLAGS: tuple[tuple[str, str], ...] = (
    ("crash_vehicle", "vehicle"),
    ("crash_object", "object"),
    ("crash_building", "building"),
    ("crash_human", "human"),
    ("crash_sidewalk", "sidewalk"),
)

#: What acts: the observation in, an action out. A policy from `policies.load_policy`, or, in
#: `replay` and the tests, a closure over a constant action. One optional half, read by
#: `run_bank` and not by the loop: a `bind(env)` method is handed each env the batch builds.
Actor = Callable[[Any], Sequence[float]]


@dataclass
class Drive:
    """One episode, measured. What the loop knows and nothing it does not.

    A dataclass rather than a model because it is not a record anyone reads back -- `replay`
    turns it into an `Episode`, and Step 3 will turn it into a result. `info` is the env's last
    `info` dict whole, so the caller chooses which keys matter.
    """

    seed: int
    #: Where the route ends, read back off the env by `prepare`. `None` on a recorded row.
    destination: str | None
    steps: int
    actions: int
    terminated: bool
    truncated: bool
    info: dict[str, Any]
    #: Rising-edge counts, by `COLLISION_FLAGS` name.
    collisions: dict[str, int] = field(default_factory=dict)
    observation_shape: tuple[int, ...] | None = None
    observation_shape_end: tuple[int, ...] | None = None
    action_shape: tuple[int, ...] | None = None
    seconds: float = 0.0
    #: The loop was told to stop before the env ended the episode or the cap was reached. A
    #: stopped episode is neither capped nor budgeted, and the result record says so.
    stopped: bool = False
    #: The env's rewards, summed over the episode. Not a score -- MetaDrive's shaping reward is
    #: whatever the config's coefficients say -- but a number Phase 5 can diff between runs.
    reward: float = 0.0
    #: The env's `cost`, summed. `metadrive_env.py:209-215` writes one per step for an
    #: out-of-road or a crash; a recorded env writes none, and this reads 0 there.
    cost: float = 0.0
    #: Every action issued, one per decision. What `actions_digest` is computed over and what
    #: `--save-trajectories` writes; a 1320-step episode at every step is 1320 pairs, cheap.
    issued_actions: list[list[float]] = field(default_factory=list)
    #: `placed_counts(env)` after the reset. Empty on an env without an engine.
    placed: dict[str, int] = field(default_factory=dict)
    #: `actor_layout_digest(env)` after the reset. `None` without an actor manager.
    actor_layout_digest: str | None = None


def shape_of(value: Any) -> tuple[int, ...] | None:
    """The shape of an observation or a space, or `None` if it has none."""
    shape = getattr(value, "shape", None)
    return tuple(int(n) for n in shape) if shape else None


def placed_counts(env: Any) -> dict[str, int]:
    """Every object in the scene, by class name, sorted. Empty on an env with no engine.

    Read off `engine.get_objects()`, which holds what the managers spawned -- the ego, the
    traffic, the obstacles, the actors -- and not the map. The class name is the object's own
    (`TrafficCone`, `Pedestrian`, `DefaultVehicle`), so the record says what MetaDrive calls it.
    """
    engine = getattr(env, "engine", None)
    if engine is None:
        return {}
    counts: dict[str, int] = {}
    for placed in engine.get_objects().values():
        name = type(placed).__name__
        counts[name] = counts.get(name, 0) + 1
    return dict(sorted(counts.items()))


def actor_layout_digest(env: Any) -> str | None:
    """`VRUManager.layout_digest()` off the env's engine, or `None` when none is registered."""
    manager = getattr(getattr(env, "engine", None), "vru_manager", None)
    return None if manager is None else manager.layout_digest()


def count_rising_edges(counts: dict[str, int], previous: dict[str, bool], info: dict) -> None:
    """Advance the collision counts by one step's `info`. A flag held high counts once."""
    for key, name in COLLISION_FLAGS:
        now = bool(info.get(key))
        if now and not previous.get(name, False):
            counts[name] = counts.get(name, 0) + 1
        previous[name] = now


def run_episode(
    env: Any,
    *,
    seed: int,
    prepare: Callable[[Any], str | None],
    cap: int,
    stride: int,
    act: Actor,
    stop: Callable[[], bool] | None = None,
) -> Drive:
    """Reset onto `seed`, `prepare`, then step until the env ends the episode or `cap` is hit.

    `prepare` is `env.py`'s per-row step with the row already bound; it is called after the reset
    and its return is the drive's `destination`. `act` is asked once per `stride` steps and its
    answer held between. `stop`, when given, is asked once per step before the step is taken; a
    true answer ends the episode there, `stopped`. The caller owns the env, including `close()`.
    """
    observation, _ = env.reset(seed=seed)
    destination = prepare(env)
    placed = placed_counts(env)
    layout = actor_layout_digest(env)
    at_reset = shape_of(observation)
    action_shape = shape_of(env.action_space)
    counts: dict[str, int] = {name: 0 for _, name in COLLISION_FLAGS}
    previous: dict[str, bool] = {}
    taken = 0
    issued = 0
    info: dict[str, Any] = {}
    terminated = truncated = False
    stopped = False
    action: list[float] = []
    issued_actions: list[list[float]] = []
    reward = cost = 0.0
    started = time.perf_counter()
    while taken < cap:
        # Before the step, so a stop lands between two steps and never inside one.
        if stop is not None and stop():
            stopped = True
            break
        # The stride, and the whole of the decision rate: the same action is handed to
        # `env.step` until the next decision is due.
        if taken % stride == 0:
            action = [float(value) for value in act(observation)]
            issued += 1
            issued_actions.append(list(action))
        observation, step_reward, terminated, truncated, info = env.step(action)
        taken += 1
        reward += float(step_reward)
        cost += float(info.get("cost") or 0.0)
        count_rising_edges(counts, previous, info)
        if terminated or truncated:
            break
    seconds = time.perf_counter() - started
    return Drive(
        seed=seed,
        destination=destination,
        steps=taken,
        actions=issued,
        terminated=bool(terminated),
        truncated=bool(truncated),
        info=dict(info),
        collisions=counts,
        observation_shape=at_reset,
        observation_shape_end=shape_of(observation),
        action_shape=action_shape,
        seconds=seconds,
        stopped=stopped,
        reward=reward,
        cost=cost,
        issued_actions=issued_actions,
        placed=placed,
        actor_layout_digest=layout,
    )




class RunError(RuntimeError):
    """A job that cannot be run as written. Always says what, and against which bank."""


def stride_for(step_hz: float, decision_hz: float | None, *, what: str = "the recording") -> int:
    """How many env steps one action is held for.

    Not a MetaDrive setting. `decision_repeat` is pinned at 1 on a recording because replay
    advances exactly one recorded frame per `env.step`, and left at MetaDrive's 5 on a road, so
    the only place a slower decision rate can live is the loop's own counter. `what` names the
    thing whose rate is the ceiling, for the refusal.
    """
    if decision_hz is None:
        return 1
    if decision_hz <= 0:
        raise ValueError(f"--decision-hz must be positive, not {decision_hz}")
    if decision_hz > step_hz:
        raise ValueError(
            f"--decision-hz {decision_hz:g} is faster than {what}'s {step_hz:g} Hz. "
            "A drive cannot decide more often than the env steps."
        )
    return max(1, round(step_hz / decision_hz))


class StopFlag:
    """A flag a signal raises and the loop reads. Callable, so it is a `stop` as-is."""

    def __init__(self) -> None:
        self.raised = False
        self.signal: int | None = None

    def __call__(self) -> bool:
        return self.raised


@contextmanager
def stop_on_signals(
    signals: Sequence[int] = (signal.SIGTERM, signal.SIGINT),
) -> Iterator[StopFlag]:
    """Turn `signals` into a flag for the length of the block, then put the old handlers back.

    Installed only on the main thread, which is the only one Python lets install them; anywhere
    else the flag is returned unarmed and the caller's own `stop` is the way to end a batch. The
    handlers stay armed until the block exits -- which is after `env.close()` has returned -- so
    a second signal during teardown is absorbed rather than raised into it.
    """
    flag = StopFlag()
    if threading.current_thread() is not threading.main_thread():
        yield flag
        return

    def raise_flag(signum: int, frame: Any) -> None:
        del frame
        flag.raised = True
        flag.signal = signum

    previous = {number: signal.signal(number, raise_flag) for number in signals}
    try:
        yield flag
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def select_rows(
    manifest: Manifest, scenarios: Sequence[str] | None
) -> list[tuple[str, Entry, Row]]:
    """The rows a job names, in the bank's own order; every row when it names none.

    An id the bank does not hold is refused by name rather than skipped: a job that asked for
    thirty-five scenarios and got thirty-four results would be a partial run nobody asked for.
    """
    wanted = None if scenarios is None else set(scenarios)
    chosen = [
        (name, entry, row)
        for name, entry in manifest.categories.items()
        for row in entry.scenarios
        if wanted is None or row.scenario_id in wanted
    ]
    if wanted is not None:
        missing = sorted(wanted - {row.scenario_id for _, _, row in chosen})
        if missing:
            raise RunError(f"{manifest.bank_id} has no scenario named {', '.join(missing)}")
    if not chosen:
        raise RunError(f"{manifest.bank_id} holds no scenarios to run")
    return chosen


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _metadrive_commit() -> str | None:
    """The simulator's commit, or `None` on a machine that cannot say. Never raises."""
    from scenariobank.doctor import resolved_source

    try:
        commit, _requested = resolved_source()
    except Exception:  # noqa: BLE001 -- information only; a run is not refused over it
        return None
    return commit


def _error_row(name: str, entry: Entry, row: Row, *, seconds: float) -> ScenarioResult:
    """A row that raised. Nothing measured, the traceback whole."""
    recorded = isinstance(entry, RealWorldEntry)
    return ScenarioResult(
        scenario_id=row.scenario_id,
        category=name,
        seed=None if recorded else seed_for(row),
        scenario_index=seed_for(row) if recorded else None,
        status="error",
        success=False,
        failure_reason=None,
        steps=0,
        actions=0,
        reward=0.0,
        cost=0.0,
        wall_time_s=round(seconds, 3),
        collisions={flag: 0 for _, flag in COLLISION_FLAGS},
        traceback=traceback.format_exc(),
    )


def _score(
    env: Any,
    prepare: Callable[[Any, Row], str | None],
    *,
    name: str,
    entry: Entry,
    row: Row,
    act: Actor,
    stride: int,
    stop: Callable[[], bool],
) -> tuple[ScenarioResult, Drive | None]:
    """One row through the loop, as a result. Raises nothing: an exception is an error row."""
    recorded = isinstance(entry, RealWorldEntry)
    cap = entry.budget_for(row)
    started = time.perf_counter()
    try:
        drive = run_episode(
            env,
            seed=seed_for(row),
            prepare=lambda built: prepare(built, row),
            cap=cap,
            stride=stride,
            act=act,
            stop=stop,
        )
    except Exception:  # noqa: BLE001 -- the batch's promise: one row's failure is that row's
        return _error_row(name, entry, row, seconds=time.perf_counter() - started), None
    completion = drive.info.get("route_completion")
    stream = "\n".join(
        ",".join(f"{value:.6f}" for value in action) for action in drive.issued_actions
    )
    result = ScenarioResult(
        scenario_id=row.scenario_id,
        category=name,
        seed=None if recorded else drive.seed,
        scenario_index=drive.seed if recorded else None,
        destination=drive.destination,
        status="ok",
        success=bool(drive.info.get("arrive_dest")),
        failure_reason=failure_reason(
            drive.info, stopped=drive.stopped, capped=drive.steps >= cap
        ),
        steps=drive.steps,
        actions=drive.actions,
        reward=round(drive.reward, 6),
        cost=round(drive.cost, 6),
        wall_time_s=round(drive.seconds, 3),
        collisions=dict(drive.collisions),
        route_completion=None if completion is None else round(float(completion), 6),
        actions_digest=sha256_hex(stream),
        placed=dict(drive.placed),
        actor_layout_digest=drive.actor_layout_digest,
    )
    return result, drive


def run_bank(
    job: Job,
    out: Path,
    *,
    stop: Callable[[], bool] | None = None,
    progress: Callable[[str], None] | None = None,
) -> Results:
    """Run every scenario a job names and write the results under `out`. The one entry point.

    Every refusal comes first and needs no simulator: the bank must be the one the job names,
    the options must resolve, the policy must load, every scenario id must exist and one
    decision rate must fit the bank's step rate. Then one env per entry, each row through
    `run_episode`, each result written as it ends. `stop` is the batch's own SIGTERM/SIGINT flag
    unless the caller hands one in, which is how a test stops a batch without a signal.

    Returns the `Results` it wrote to `<out>/results.json`. A stopped batch returns normally --
    a cancelled run that still writes its results is a scored partial run. A batch whose
    observation shape moved between its first reset and its last step is failed *after* the
    record is written, with the two shapes: something in the loop -- a policy that swaps the
    vehicle's sensor config in and out, which the bundled expert does -- leaked into the env,
    and every row after the leak was scored against a different observation.
    """
    bank_dir = Path(job.bank.path)
    manifest = read_manifest(bank_dir)
    if job.bank.id is not None and job.bank.id != manifest.bank_id:
        raise RunError(
            f"the bank at {bank_dir} is {manifest.bank_id!r}, and the job names {job.bank.id!r}"
        )
    options = resolve_options(
        manifest, tier=job.options.tier, levels=job.options.levels, raw=job.options.raw
    )
    from scenariobank.policies import load_policy

    act = load_policy(job.policy, checkpoint_path=job.checkpoint_path)
    chosen = select_rows(manifest, job.scenarios)
    rates = sorted({step_hz_for(entry) for _, entry, _ in chosen})
    if len(rates) > 1:
        raise RunError(
            f"{manifest.bank_id} steps at more than one rate ({', '.join(f'{r:g}' for r in rates)}"
            " Hz) and one run holds one decision rate; run the entries separately"
        )
    step_hz = rates[0]
    recorded = manifest.source != "pg"
    stride = stride_for(
        step_hz, job.decision_hz, what="the recording" if recorded else "the env"
    )
    out = Path(out)
    (out / "results").mkdir(parents=True, exist_ok=True)
    say = progress or (lambda _line: None)

    started_utc = _utc_now()
    results: list[ScenarioResult] = []
    shape_before: tuple[int, ...] | None = None
    shape_after: tuple[int, ...] | None = None
    with nullcontext(stop) if stop is not None else stop_on_signals() as flag:
        # One env per entry: rows are contiguous by entry in the manifest, and `select_rows`
        # keeps that order, so a change of entry is a change of env.
        index = 0
        while index < len(chosen) and not flag():
            name, entry, _ = chosen[index]
            rows = [row for group, _, row in chosen[index:] if group == name]
            index += len(rows)
            env = None
            try:
                built = time.perf_counter()
                try:
                    env, prepare = build_env(bank_dir, entry, options)
                    bind_policy(act, env)
                except Exception:  # noqa: BLE001 -- every row of this entry is an error row
                    for row in rows:
                        result = _error_row(name, entry, row, seconds=time.perf_counter() - built)
                        results.append(result)
                        write_json(out / "results" / f"{row.scenario_id}.json", result)
                        say(f"{row.scenario_id}: error building the env")
                    continue
                for row in rows:
                    if flag():
                        break
                    result, drive = _score(
                        env, prepare, name=name, entry=entry, row=row, act=act,
                        stride=stride, stop=flag,
                    )
                    results.append(result)
                    write_json(out / "results" / f"{row.scenario_id}.json", result)
                    if drive is not None:
                        shape_before = shape_before or drive.observation_shape
                        shape_after = drive.observation_shape_end
                        if job.save_trajectories:
                            write_json(
                                out / "trajectories" / f"{row.scenario_id}.json",
                                Trajectory(
                                    scenario_id=row.scenario_id,
                                    stride=stride,
                                    actions=drive.issued_actions,
                                ),
                            )
                    say(_progress_line(result))
            finally:
                if env is not None:
                    env.close()
        stopped = bool(flag())

    first_recorded = next(
        (entry for _, entry, _ in chosen if isinstance(entry, RealWorldEntry)), None
    )
    report = Results(
        schema_version=RESULTS_SCHEMA_VERSION,
        started_utc=started_utc,
        finished_utc=_utc_now(),
        job_id=job.job_id,
        attempt=job.attempt,
        stopped=stopped,
        bank=BankInfo(
            path=str(job.bank.path),
            id=manifest.bank_id,
            source=manifest.source,
            schema_version=manifest.schema_version,
            provenance=None if first_recorded is None else first_recorded.provenance,
            attribution=None if first_recorded is None else first_recorded.attribution,
        ),
        policy=job.policy,
        options=options,
        env=EnvInfo(
            observation_shape_before=shape_before,
            observation_shape_after=shape_after,
            step_hz=step_hz,
            decision_hz=job.decision_hz,
            stride=stride,
            metadrive_commit=_metadrive_commit(),
        ),
        results=results,
        summary=summarize(results),
    )
    write_json(out / "results.json", report)
    if shape_before is not None and shape_after is not None and shape_before != shape_after:
        raise RunError(
            f"the observation shape moved during the run, from {list(shape_before)} at the "
            f"first reset to {list(shape_after)} after the last step, so the policy or the env "
            f"leaked into the env config; the record is at {out / 'results.json'}"
        )
    return report


def bind_policy(act: Actor, env: Any) -> None:
    """Hand `env` to a policy with a `bind`, before its rows run. Nothing for one without."""
    bind = getattr(act, "bind", None)
    if callable(bind):
        bind(env)


def _progress_line(result: ScenarioResult) -> str:
    """One row as one line: id, what happened, how long."""
    if result.status == "error":
        return f"{result.scenario_id}: error (see traceback)"
    what = "arrived" if result.success else (result.failure_reason or "ended")
    return f"{result.scenario_id}: {what}  {result.steps} steps  {result.wall_time_s:.1f} s"


__all__ = [
    "COLLISION_FLAGS",
    "Actor",
    "Drive",
    "RunError",
    "StopFlag",
    "actor_layout_digest",
    "bind_policy",
    "count_rising_edges",
    "placed_counts",
    "run_bank",
    "run_episode",
    "select_rows",
    "shape_of",
    "stop_on_signals",
    "stride_for",
]
