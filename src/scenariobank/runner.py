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

**A film is read off the scene between two steps, and the gate says it changed nothing.**
`run_episode` takes an `observe` hook, handed the env after the reset and after every step;
`run_bank(record_video=True)` points it at `video.Recorder`, one mp4 per row at the step rate,
under `<out>/videos/`. It is a switch on the run rather than a field of the job, so a queue job
cannot ask for it, and `test_reproducibility.py` holds the row filmed to the row unfilmed,
`actions_digest` included. What it is for is looking: cones, barriers, people and traffic on
the road the numbers describe.

**A camera rig rides on a run the same way, and a film from it is the same hook** (Step 6b).
`run_bank(camera_rig=...)` loads the spec before any env is built -- the same rate refusal
`replay` makes, unless `ignore_rig_rate` -- hands it to `build_env`, which mounts it after every
reset, and with `record_video` points a second hook at `video.CameraFilm`: one mp4 per camera
and a mosaic of all of them beside the top-down film. The cameras are read off the engine and
never through the observation, so a row with a rig scores as the row without one;
`test_camera_rig.py` holds that, `actions_digest` included.

**The decision rate is a stride in this loop**, never a MetaDrive key: the same action is handed
to `env.step` until the next decision is due, so a slower rate changes how many actions are
issued and never how long the episode is. **The step rate is the env's** (`env.step_hz_for`):
a job's `step_hz` re-rates a procedural road (Step 7, `--step-hz 100 --decision-hz 20` for the
AV3 stack), and every step budget is scaled with it by `env.budget_at`, so a cap sized in
seconds at 10 Hz is the same seconds at 100.

**A policy is told about the run once, and about each env as it is built.** `load_policy` makes
`Name(checkpoint_path=)`; `setup_policy` then hands a policy with a `setup(run)` hook a
`RunSetup` -- the step rate, the stride, the rig, whether its rate check was waived, the model
config -- before any env exists, which is where a policy that reads the rig refuses a run it
cannot drive; `bind_policy` hands each env over before its rows; `close_policy` ends the batch.
All three are optional halves of the protocol and the loop reads none of them.

**The batch is `run_bank(job, out)`, and it never aborts.** One env per row, built and closed
around it, every row of the job through the loop above, each row's result written to
`<out>/results/<scenario_id>.json` the moment it ends and `<out>/results.json` assembled from
those last. One env per *row* and not per entry because an episode run after another one in the
same env is not the episode run alone -- measured on `banks/curve` at `hard`: the same row ended
at 339 steps by itself, 218 after one other row and 300 after three, with the actor layout
identical every time and the expert's actions parting at step two. The object pool is not the
carrier (`force_destroy=True` moves the numbers and keeps the dependence), and what is was not
found; a fresh env costs a quarter of a second per row and makes a job that names a subset of
the rows score them exactly as the whole bank does, which Phase 7's queue relies on. A row
that raises -- in the policy, in `reset`, anywhere -- is a `status: "error"` row with a
traceback, and the next row runs. A batch told to stop (SIGTERM, SIGINT) ends the row it is in
as `stopped`, writes what it has, and closes the env on the normal path: the handlers are ours
and set a flag, so nothing is ever raised into `env.close()` -- the panda3d/bullet teardown
wedge that once needed a reboot.
The per-row file is the progress signal Phase 7's orchestrator extends a lease off, and the
reason a run killed at 30 of 35 is a scored partial run rather than a lost one.

**And the batch says what it is doing, in JSON, as it does it** (Phase 7 Step 1). `on_event=` is
handed an `events.Event` at each of four moments -- the batch validated (`batch.started`), each
scenario built (`scenario.started`), each scenario ended (`scenario.finished`) and each
heartbeat -- and the first two are written to `<out>/batch.json` and `<out>/starts/<id>.json`
as well, so a supervisor reads a bar off the directory without opening a log. The job's id and
attempt are stamped on every event here rather than by the caller, because they are the job's
and this is the only place that holds it. A caller that hands no `on_event=` (the studio's own
job engine, `calibrate`) still gets the files; nothing about a run changes either way.
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

from scenariobank import events
from scenariobank.bank import Manifest, RealWorldEntry, read_manifest
from scenariobank.env import Entry, Row, budget_at, build_env, seed_for, step_hz_for
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
from scenariobank.video import chain

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
    observe: Callable[[Any], None] | None = None,
) -> Drive:
    """Reset onto `seed`, `prepare`, then step until the env ends the episode or `cap` is hit.

    `prepare` is `env.py`'s per-row step with the row already bound; it is called after the reset
    and its return is the drive's `destination`. `act` is asked once per `stride` steps and its
    answer held between. `stop`, when given, is asked once per step before the step is taken; a
    true answer ends the episode there, `stopped`. `observe`, when given, is handed the env once
    after the reset and prepare -- the placed scene, before anything moves -- and once after
    every step, the ending step included, so a film has `steps + 1` frames; it may read the env
    and must not write it. The caller owns the env, including `close()`.
    """
    observation, _ = env.reset(seed=seed)
    destination = prepare(env)
    placed = placed_counts(env)
    layout = actor_layout_digest(env)
    if observe is not None:
        observe(env)
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
        if observe is not None:
            observe(env)
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




class Heartbeat:
    """The `observe` hook that says, every `every_s` of wall time, whether the car is moving.

    A scored row with a model on the car takes a second per decision and prints nothing until it
    ends, which reads the same as a hang. This prints one line per interval off the env: the
    step and decision counts, the car's speed, how far it moved since the last line, the route
    completed, and the action being held. It reads the env and writes nothing; a row on the
    host that ends inside the first interval prints nothing at all. `stride` is the decision
    stride, so the decision count is arithmetic and not a second counter on the loop.

    **The reading is made once and said twice** (Phase 7 Step 1): as the prose line a person
    watching a log reads, through `say`, and as the `events.Heartbeat` a supervisor reads,
    through `emit`. One measurement, two renderings -- so the two can never disagree about how
    fast the car was going, which is the whole complaint against scraping the prose.
    """

    def __init__(
        self,
        say: Callable[[str], None],
        *,
        every_s: float,
        stride: int,
        emit: Callable[[events.Heartbeat], Any] | None = None,
        scenario_id: str = "",
    ) -> None:
        if every_s <= 0:
            raise ValueError(f"a heartbeat interval must be positive, not {every_s}")
        self.say = say
        self.every_s = float(every_s)
        self.stride = max(1, int(stride))
        self.emit = emit
        self.scenario_id = scenario_id
        self.calls = 0
        self.lines = 0
        self.started: float | None = None
        self.last_said: float | None = None
        self.last_position: tuple[float, float] | None = None

    def __call__(self, env: Any) -> None:
        now = time.perf_counter()
        if self.started is None:
            # The reset call: the placed scene, before anything moves.
            self.started = self.last_said = now
            self.last_position = _position(env)
            return
        self.calls += 1
        if now - self.last_said < self.every_s:
            return
        beat = self.reading(env, now)
        self.say(self.line(beat))
        if self.emit is not None:
            self.emit(beat)
        self.last_said = now
        self.last_position = _position(env)
        self.lines += 1

    def reading(self, env: Any, now: float) -> events.Heartbeat:
        """What the env says at this instant. Reads it; changes nothing about it."""
        agent = env.agent
        steps = self.calls
        position = _position(env)
        moved = (
            0.0
            if position is None or self.last_position is None
            else float(
                ((position[0] - self.last_position[0]) ** 2
                 + (position[1] - self.last_position[1]) ** 2) ** 0.5
            )
        )
        try:
            speed = float(agent.speed)
        except Exception:  # noqa: BLE001 -- a fake env without a body
            speed = None
        completion = getattr(getattr(agent, "navigation", None), "route_completion", None)
        action = getattr(agent, "last_current_action", None)
        held: list[float] | None = None
        if action:
            try:
                held = [float(value) for value in action[-1]]
            except (TypeError, ValueError, IndexError):
                held = None
        return events.Heartbeat(
            scenario_id=self.scenario_id,
            elapsed_s=round(now - (self.started or now), 1),
            step=steps,
            decision=(steps + self.stride - 1) // self.stride,
            speed_mps=speed,
            moved_m=round(moved, 3),
            route_completion=None if completion is None else round(float(completion), 6),
            action=held,
        )

    def line(self, beat: events.Heartbeat) -> str:
        """The reading as the prose a person reads. Unchanged since Phase 4 Step 7."""
        speed = float("nan") if beat.speed_mps is None else beat.speed_mps
        route = (
            ""
            if beat.route_completion is None
            else f"  route {beat.route_completion * 100:5.1f}%"
        )
        held = "" if not beat.action else "  action " + ",".join(
            f"{value:+.2f}" for value in beat.action
        )
        return (
            f"  t+{beat.elapsed_s:5.0f}s  step {beat.step}  decision {beat.decision}  "
            f"speed {speed:4.1f} m/s  moved {beat.moved_m:5.1f} m{route}{held}"
        )


def _position(env: Any) -> tuple[float, float] | None:
    try:
        x, y = env.agent.position[:2]
        return float(x), float(y)
    except Exception:  # noqa: BLE001 -- a fake env without a body
        return None


class RunError(RuntimeError):
    """A job that cannot be run as written. Always says what, and against which bank."""


@dataclass(frozen=True)
class RunSetup:
    """What a policy may ask the batch about, once, before any env is built.

    Handed to a policy's `setup(run)` hook by `setup_policy`. `stride / step_hz` is the interval
    between two of its decisions; `rig` is the `CameraRig` the run mounts on every ego, or
    `None`; `ignore_rig_rate` says the rig's declared rate was not checked against that
    interval, which a policy reading the rig refuses; `model_config` is the job's.
    """

    step_hz: float
    stride: int
    rig: Any | None = None
    ignore_rig_rate: bool = False
    model_config: str | None = None
    checkpoint_path: str | None = None

    @property
    def decision_interval_s(self) -> float:
        return self.stride / self.step_hz


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
    observe: Callable[[Any], None] | None = None,
    step_hz: float | None = None,
) -> tuple[ScenarioResult, Drive | None]:
    """One row through the loop, as a result. Raises nothing: an exception is an error row."""
    recorded = isinstance(entry, RealWorldEntry)
    cap = budget_at(entry.budget_for(row), entry, step_hz)
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
            observe=observe,
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
    record_video: bool = False,
    camera_rig: Path | None = None,
    ignore_rig_rate: bool = False,
    heartbeat_s: float | None = 10.0,
    on_event: Callable[[events.Event], Any] | None = None,
) -> Results:
    """Run every scenario a job names and write the results under `out`. The one entry point.

    Every refusal comes first and needs no simulator: the bank must be the one the job names,
    the options must resolve, the policy must load, every scenario id must exist and one
    decision rate must fit the bank's step rate. Then one env per row, built and closed around
    `run_episode`, each result written as it ends. `stop` is the batch's own SIGTERM/SIGINT flag
    unless the caller hands one in, which is how a test stops a batch without a signal.
    `record_video` films every row into `<out>/videos/<scenario_id>.mp4` at the step rate; it is
    a switch on the run and not a field of the job, so nothing a queue job says can turn it on.
    `camera_rig` mounts that spec's cameras on the ego for every row, and with `record_video`
    films each of them too (`<out>/videos/<scenario_id>.<camera>.mp4` and `<scenario_id>.rig.mp4`,
    the mosaic); its `tick_rate` must equal the read interval unless `ignore_rig_rate`, the
    switch for filming, which a policy that reads the rig refuses. `heartbeat_s` prints, through
    `progress`, one line every that many seconds of wall time while a row runs -- step, decision,
    speed, distance moved, route completed, the action held -- so a slow row with a model on the
    car can be told from a hung one; `None` or 0 turns it off. It changes nothing a row records.
    `on_event` is handed the same four moments as `events.Event` objects, for a supervisor that
    parses no prose; `<out>/batch.json` and `<out>/starts/<id>.json` are written whether or not
    one is given, because they are the record and not the stream.

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
    rates = sorted({step_hz_for(entry, job.step_hz) for _, entry, _ in chosen})
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
    rig = None
    if camera_rig is not None:
        from scenariobank.av3.camera_rig import load_rig

        rig = load_rig(camera_rig, read_interval_s=None if ignore_rig_rate else stride / step_hz)
    say = progress or (lambda _line: None)
    # The policy's own refusals, still before any env: a rig it needs and was not given, a rate
    # it will not read at, a config that does not load.
    for note in setup_policy(
        act,
        RunSetup(
            step_hz=step_hz,
            stride=stride,
            rig=rig,
            ignore_rig_rate=ignore_rig_rate,
            model_config=job.model_config_path,
            checkpoint_path=job.checkpoint_path,
        ),
    ):
        say(f"note: {note}")
    out = Path(out)
    (out / "results").mkdir(parents=True, exist_ok=True)
    (out / events.STARTS_DIR).mkdir(parents=True, exist_ok=True)
    if record_video:
        (out / "videos").mkdir(parents=True, exist_ok=True)

    sink = on_event or (lambda _event: None)

    def emit(event: events.Event) -> events.Event:
        """Stamp the job on an event, hand it on, and give it back so a file can be written."""
        event = event.model_copy(update={"job_id": job.job_id, "attempt": job.attempt})
        sink(event)
        return event

    def finished(result: ScenarioResult, index: int) -> None:
        """A row has ended: its record on disk first, then the line that says so."""
        write_json(out / "results" / f"{result.scenario_id}.json", result)
        emit(
            events.ScenarioFinished(
                scenario_id=result.scenario_id,
                index=index,
                n=len(chosen),
                status=result.status,
                success=result.success,
                failure_reason=result.failure_reason,
                steps=result.steps,
                wall_time_s=result.wall_time_s,
            )
        )

    started_utc = _utc_now()
    write_json(
        out / events.BATCH_FILE,
        emit(
            events.BatchStarted(
                bank_id=manifest.bank_id,
                source=manifest.source,
                policy=job.policy,
                n=len(chosen),
                scenarios=[row.scenario_id for _, _, row in chosen],
                step_hz=step_hz,
                decision_hz=job.decision_hz,
                stride=stride,
            )
        ),
    )
    results: list[ScenarioResult] = []
    shape_before: tuple[int, ...] | None = None
    shape_after: tuple[int, ...] | None = None
    try:
        with nullcontext(stop) if stop is not None else stop_on_signals() as flag:
            # One env per row, closed before the next is built: a row scores the same alone, in
            # any company and in any order. See the module docstring for the measurement.
            for index, (name, entry, row) in enumerate(chosen, start=1):
                if flag():
                    break
                write_json(
                    out / events.STARTS_DIR / f"{row.scenario_id}.json",
                    emit(
                        events.ScenarioStarted(
                            scenario_id=row.scenario_id,
                            category=name,
                            index=index,
                            n=len(chosen),
                            max_steps=budget_at(entry.budget_for(row), entry, job.step_hz),
                        )
                    ),
                )
                env = None
                recorder = None
                film = None
                try:
                    built = time.perf_counter()
                    try:
                        env, prepare = build_env(
                            bank_dir, entry, options, rig=rig, step_hz=job.step_hz
                        )
                        bind_policy(act, env)
                    except Exception:  # noqa: BLE001 -- the batch's promise: this row is an error row
                        result = _error_row(name, entry, row, seconds=time.perf_counter() - built)
                        results.append(result)
                        finished(result, index)
                        say(f"{row.scenario_id}: error building the env")
                        continue
                    if record_video:
                        from scenariobank.video import CameraFilm, Recorder

                        recorder = Recorder().open(
                            out / "videos" / f"{row.scenario_id}.mp4", fps=step_hz
                        )
                        if rig is not None:
                            film = CameraFilm().open(
                                out / "videos", row.scenario_id, rig, fps=step_hz
                            )
                    result, drive = _score(
                        env, prepare, name=name, entry=entry, row=row, act=act,
                        stride=stride, stop=flag, step_hz=job.step_hz,
                        observe=chain(
                            None if recorder is None else recorder.add,
                            None if film is None else film.add,
                            None if not heartbeat_s else Heartbeat(
                                say,
                                every_s=heartbeat_s,
                                stride=stride,
                                emit=emit,
                                scenario_id=row.scenario_id,
                            ),
                        ),
                    )
                    results.append(result)
                    finished(result, index)
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
                    if recorder is not None:
                        recorder.close()
                    if film is not None:
                        film.close()
                    if env is not None:
                        env.close()
            stopped = bool(flag())
    finally:
        close_policy(act)

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
            camera_rig=None if rig is None else rig.path,
            rig_tick_rate_s=None if rig is None else rig.tick_rate_s,
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


def setup_policy(act: Actor, run: RunSetup) -> list[str]:
    """Hand the run to a policy with a `setup`, before any env is built; its notes come back.

    A policy raises here to refuse the run -- the AV3 policy with no rig, or with the rig's
    rate check waived -- and a `PolicyError` from it is the same refusal `load_policy` makes.
    Nothing for a policy without the hook.
    """
    setup = getattr(act, "setup", None)
    if not callable(setup):
        return []
    notes = setup(run)
    return [str(note) for note in (notes or [])]


def close_policy(act: Actor) -> None:
    """End the batch for a policy with a `close`: a bridge connection dropped, an engine freed."""
    close = getattr(act, "close", None)
    if callable(close):
        close()


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
    "Heartbeat",
    "RunError",
    "RunSetup",
    "StopFlag",
    "actor_layout_digest",
    "bind_policy",
    "close_policy",
    "count_rising_edges",
    "placed_counts",
    "run_bank",
    "run_episode",
    "select_rows",
    "setup_policy",
    "shape_of",
    "stop_on_signals",
    "stride_for",
]
