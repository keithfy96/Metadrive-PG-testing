"""Drive one scenario of a bank end to end, and report what the drive measured.

Steps 1-5 of Phase 3 built the *reading* half of the import and not one line of it opens a
simulator: `import` copies pickles, `review` is a manifest read and some arithmetic, and a test
pins both. So until this module nothing had shown that a bank in `banks/junction-1` actually
**drives** -- only that it describes itself well. Phase 4 Step 2 widened it to a procedural bank
for the same reason: `generate` proves a road builds and a route resolves, and nothing before this
had stepped one.

This is a diagnostic and deliberately not a runner. It writes no result file and loads no policy
by path; the result record is Phase 4 Step 3's, and one invented here to satisfy a single
recording would be the frontend contract designed by accident. What it does *not* own any more is
the step loop: from Phase 3 Step 6 until Phase 4 Step 2 it was the only `env.step` in the package,
and it refused procedural banks precisely so it would not become a second runner. The loop is
`runner.run_episode` now, the env is `env.build_env`'s, and this module is one caller of both --
the same shape `run` will have.

Three things were claims with no measurement behind them, and driving `junction-1` settled all
three.

**A stored episode does not end by itself.** The recording is 3782 frames; with `horizon` left at
`BaseEnv`'s `None` default the env was still stepping at 6000, `terminated` and `truncated` both
false, quietly replaying past the last recorded frame rather than raising or stopping. `horizon`
is read by `done_function` (`scenario_env.py:162`) and simply was not being set -- `ScenarioEnv`'s
own config never sets it (`scenario_env.py:22-115`). So the config sets it to the entry's own
length, and the loop keeps a per-row cap as well. The two agree on every recording here; they are
belt-and-braces because the failure they guard against is silent.

**The observation is 31 wide, not 19.** Same `StateObservation` and same `SENSOR_CONFIG` as a PG
bank, and the whole 12-wide difference is navigation: a stored scenario gets `TrajectoryNavigation`
(22 scalars) where a PG road gets `NodeNetworkNavigation` (10). The number lives in
`config.SCENARIO_OBSERVATION_SHAPE` and this module is what measures it.

**A camera rig rides on the same drive** (Phase 4 Step 6). `--camera-rig rigs/av3.txt` puts the
spec's cameras on the env through `env.build_env(rig=...)`, reads every one of them at each
decision, and reports what the env held: its sensors by name, how many image buffers, which
camera `image_source` names, and the shape of every frame that came back. That is the check that
the cameras are alive -- `base_env.py:343-346` deletes them silently from a headless env unless
`image_observation` is on -- and it changes nothing the episode measures, because the cameras are
read off the engine and never through the observation (`agent_observation` is pinned at 19).
A spec's `tick_rate` must equal the interval it is read at, the decision stride over the step
rate; a road steps at 10 Hz, so the AV3 rig's 0.05 s is refused there unless `--ignore-rig-rate`
says the mismatch is understood -- a switch for looking at the cameras, and `run` has no such
switch.

**The decision rate is a stride in the loop, not a MetaDrive setting.** Replay advances one
recorded frame per `env.step`, which is why the entry's `step_hz` becomes
`physics_world_step_size` with `decision_repeat = 1`; a policy that decides at 20 Hz on a 100 Hz
recording therefore holds each action for five steps. On a procedural road one `env.step` is
10 Hz (`env.step_hz_for`), so a decision rate above that is refused the same way. The stride
changes how many actions are issued and never how long the episode is.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from scenariobank.bank import BankError, Manifest, RealWorldEntry
from scenariobank.env import (
    Entry,
    Row,
    build_env,
    replay_config,
    seed_for,
    step_hz_for,
)
from scenariobank.options import Kind, resolve_options
from scenariobank.results import TERMINATIONS
from scenariobank.runner import run_episode, stride_for

#: The action a drive issues when it is given none: neither steering nor throttle.
#:
#: Enough to answer every question this command asks -- how long the episode is, how wide the
#: observation is, what ends it, how the stride behaves -- none of which depends on going
#: anywhere. Policies arrive in Phase 4 Step 4.
IDLE_ACTION: tuple[float, float] = (0.0, 0.0)

#: The phrase printed for each ending, keyed by the `info` flag.
_PHRASES: dict[str, str] = {
    "crash_vehicle": "hit a vehicle",
    "crash_object": "hit an object",
    "crash_building": "hit a building",
    "crash_human": "hit a pedestrian",
    "crash_sidewalk": "hit the kerb",
    "crash": "crashed",
    "out_of_road": "left the road",
    "arrive_dest": "arrived",
    "max_step": "ran out of recording",
}

#: What ended the episode, worst first, as `info` key -> the phrase printed for it.
#:
#: Ordered rather than mapped, because more than one can be true at once: a crash on the last
#: frame of a recording sets `crash` *and* `max_step`, and the crash is the interesting half.
#: `max_step` last of the real endings, since on a clean replay under a zero action it is the
#: expected one -- the recording ran out, which is success for a replay and would be a timeout
#: for a drive. On a procedural road the same flag means the step cap, and is phrased as such.
#: The order is `results.TERMINATIONS`, the measured list, so this report and the result record
#: cannot rank an ending differently; a key phrased here and not measured there raises at import.
ENDINGS: tuple[tuple[str, str], ...] = tuple((key, _PHRASES[key]) for key in TERMINATIONS)

#: What `max_step` reads as by kind. A recording that ran out is the expected ending; a road whose
#: step cap was hit under a zero action is one too, but "ran out of recording" would be a lie there.
MAX_STEP_PHRASE: dict[str, str] = {"recorded": "ran out of recording", "pg": "ran out of steps"}

#: Neither ended nor capped: the loop was still going when it was told to stop.
STILL_DRIVING = "still driving"

#: Stopped by `--steps` rather than by anything the env said.
CAPPED = "capped short"

#: Stopped by the row's own budget, with the env still going. Only a procedural row that declares
#: a `max_steps` below its entry's can end this way: `horizon` is the entry's and the loop cap is
#: the row's, and here the loop cap came first -- which is the whole reason it is enforced.
BUDGETED = "hit its own budget"


class EnvReport(BaseModel):
    """What the env carried, read off its engine after the drive: the cameras' half of a report.

    Present on every drive, rig or not, so a report with no rig still says `image_observation`
    was off and the sensors were the three ray detectors -- which is what a headless env holds.
    """

    model_config = ConfigDict(extra="forbid")

    #: Every sensor the engine registered, by its config name, sorted. With a rig, the rig's
    #: cameras and never `rgb_camera` (see `CameraRig.image_source`).
    sensors: list[str]
    #: `config["image_observation"]`: what keeps the cameras alive on a headless env.
    image_observation: bool
    #: `vehicle_config["image_source"]`. MetaDrive's default is `rgb_camera`.
    image_source: str | None
    #: How many image buffers the engine really holds; `camera_rig.MAX_IMAGE_BUFFERS` caps it.
    image_buffers: int
    #: The spec the rig was read from. `None` on a drive without one.
    rig: str | None = None
    #: The rig's camera names, in spec order.
    rig_cameras: list[str] = Field(default_factory=list)
    #: What the spec declared it must be read at, in seconds; `None` if it declared nothing.
    rig_tick_rate_s: float | None = None
    #: What the cameras were read at: the decision stride over the step rate.
    read_interval_s: float | None = None
    #: The shape of the last frame every camera returned, `(H, W, 3)`.
    frames: dict[str, tuple[int, ...]] = Field(default_factory=dict)
    #: How many times the rig was read: once per decision, the reset's frame included.
    reads: int = 0
    #: Wall clock per read of the whole rig, in ms. A cost, not a property of the bank.
    ms_per_read: float = 0.0


class Episode(BaseModel):
    """One drive, measured. A report rather than a result -- see the module docstring.

    A pydantic model and not a dataclass so that `--json` is `model_dump_json` like every other
    report this CLI prints, and so `extra="forbid"` catches a field added here and not printed.
    """

    model_config = ConfigDict(extra="forbid")

    #: Which half of the seam this drove through. Says which of the two observation widths and
    #: which of `seed` / `scenario_index` to read.
    kind: Kind
    bank_id: str
    category: str
    scenario_id: str
    #: The seed a procedural row is built from. `None` on a recording.
    seed: int | None = None
    #: The node the route ends at, read back off the navigation after `set_route`. `None` on a
    #: recording, which carries its own route.
    destination: str | None = None
    #: The id the recording carries inside itself. Printed because a result that cannot be traced
    #: back to its pickle is a result nobody can check. `None` on a procedural row.
    stored_id: str | None = None
    scenario_index: int | None = None
    #: The rate one `env.step` advances at: the recording's own sampling rate, or 10 Hz on a road.
    step_hz: float
    #: What a policy would have decided at. `None` means every step, which is `step_hz` itself.
    decision_hz: float | None
    #: Env steps per action. 1 when `decision_hz` is unset or equals `step_hz`.
    stride: int
    #: `entry.budget_for(row)`: the loop cap for this row. `horizon` is the entry's `max_steps`
    #: and the two agree unless the row declares its own. See `runner.py` on why they are two.
    budget: int
    steps: int
    #: How many actions were issued. `ceil(steps / stride)`, and the only number the decision rate
    #: is allowed to move.
    actions: int
    terminated: bool
    truncated: bool
    #: One phrase, chosen from `ENDINGS`. Worst first -- a crash on the last frame reads as a
    #: crash rather than as the recording running out.
    ended_by: str
    #: Every `TerminationState` flag `info` carried at the end, so the phrase above can be
    #: checked against what it was chosen from.
    flags: dict[str, bool]
    #: Rising-edge collision counts by kind of thing hit, from `runner.COLLISION_FLAGS`. A flag
    #: held high for thirty steps is one collision, not thirty.
    collisions: dict[str, int] = Field(default_factory=dict)
    #: How far along the route the ego got. Under a zero action this is small and that is
    #: correct: nothing drove.
    route_completion: float | None
    #: At reset, and again after the last step. Reported twice because "it was 31 the whole way"
    #: is the claim, and one number cannot make it.
    observation_shape: tuple[int, ...] | None
    observation_shape_end: tuple[int, ...] | None
    action_shape: tuple[int, ...] | None
    #: Wall clock, which is what says whether a batch of these is affordable. Not a property of
    #: the bank -- it will differ on the rig -- so nothing refuses on it.
    seconds: float
    ms_per_step: float
    #: The env's sensors, buffers and rig, read after the drive. `None` only on an `Episode`
    #: built by hand rather than driven.
    env: EnvReport | None = None


def select(manifest: Manifest, scenario: str | None = None) -> tuple[str, Entry, Row]:
    """The category, entry and row to drive: the one named, or the first in the bank.

    Defaulting to the first is safe *here* and would not be in a runner. Every conversion in every
    workspace this project has holds exactly one scenario, and a procedural bank's first row is
    as good a smoke test as any other; a command that made you name it would be asking for a
    string it already knows.
    """
    for name, entry in manifest.categories.items():
        for row in entry.scenarios:
            if scenario is None or row.scenario_id == scenario:
                return name, entry, row
    if scenario is None:
        raise BankError(f"{manifest.bank_id} holds no scenarios to drive")
    raise BankError(f"no scenario named {scenario!r} in {manifest.bank_id}")


def _ending(flags: dict[str, bool], *, steps: int, cap: int, budget: int, kind: str) -> str:
    """The one phrase for how this episode finished."""
    for key, phrase in ENDINGS:
        if flags.get(key):
            return MAX_STEP_PHRASE[kind] if key == "max_step" else phrase
    if steps < cap:
        return STILL_DRIVING
    return CAPPED if cap < budget else BUDGETED


def drive(
    bank_dir: Path,
    manifest: Manifest,
    *,
    scenario: str | None = None,
    decision_hz: float | None = None,
    steps: int | None = None,
    action: tuple[float, float] = IDLE_ACTION,
    record_video: Path | None = None,
    camera_rig: Path | None = None,
    ignore_rig_rate: bool = False,
) -> Episode:
    """Drive one scenario and report the drive. Builds an env, so it needs the simulator.

    The options are the manifest's own pinned block, resolved by `resolve_options`; this command
    takes no option flags because it measures a bank, not a run of it. `steps` caps the run short,
    for a quick check that the round trip works without paying for the whole episode; the episode
    it reports then ends `capped short` rather than pretending the cap was the env's.
    `record_video` films the drive into that one file, top-down, at the step rate (`video.py`),
    and with a rig, every camera beside it (`<name>.<camera>.mp4`, `<name>.rig.mp4`).
    `camera_rig` mounts that spec's cameras and reads them at every decision; its `tick_rate`
    must match the read interval unless `ignore_rig_rate`, and either way the report says both.
    """
    # Every refusal first, and before the simulator is touched: a bank that pins an axis this
    # phase cannot run, an unknown scenario id and an impossible decision rate are all answerable
    # off the manifest, and answering them on a machine with no simulator is worth more than the
    # line saved by building first. `build_env` is where MetaDrive is imported.
    options = resolve_options(manifest)
    category, entry, row = select(manifest, scenario)
    kind: Kind = "recorded" if isinstance(entry, RealWorldEntry) else "pg"
    step_hz = step_hz_for(entry)
    stride = stride_for(
        step_hz, decision_hz, what="the recording" if kind == "recorded" else "the env"
    )
    budget = entry.budget_for(row)
    cap = budget if steps is None else min(budget, steps)
    read_interval_s = stride / step_hz
    rig = None
    if camera_rig is not None:
        from scenariobank.av3.camera_rig import load_rig

        rig = load_rig(camera_rig, read_interval_s=None if ignore_rig_rate else read_interval_s)

    recorder = None
    film = None
    if record_video is not None:
        from scenariobank.video import CameraFilm, Recorder, chain

        record_video = Path(record_video)
        recorder = Recorder().open(record_video, fps=step_hz)
        if rig is not None:
            # `x.mp4` -> `x.<camera>.mp4` and `x.rig.mp4` beside it, the way `run` names them.
            film = CameraFilm().open(record_video.parent, record_video.stem, rig, fps=step_hz)
    else:
        from scenariobank.video import chain
    reader = None if rig is None else _RigReader(rig, stride)
    env, prepare = build_env(bank_dir, entry, options, rig=rig)
    try:
        run = run_episode(
            env,
            seed=seed_for(row),
            prepare=lambda built: prepare(built, row),
            cap=cap,
            stride=stride,
            act=lambda _observation: action,
            observe=chain(
                None if recorder is None else recorder.add,
                None if film is None else film.add,
                reader,
            ),
        )
        env_report = _env_report(env, rig, reader, read_interval_s)
    finally:
        if recorder is not None:
            recorder.close()
        if film is not None:
            film.close()
        env.close()

    flags = {key: bool(run.info.get(key)) for key, _ in ENDINGS if key in run.info}
    completion = run.info.get("route_completion")
    recorded = isinstance(entry, RealWorldEntry)
    return Episode(
        kind=kind,
        bank_id=manifest.bank_id,
        category=category,
        scenario_id=row.scenario_id,
        seed=None if recorded else run.seed,
        destination=run.destination,
        stored_id=row.stored_id if recorded else None,
        scenario_index=row.scenario_index if recorded else None,
        step_hz=step_hz,
        decision_hz=decision_hz,
        stride=stride,
        budget=budget,
        steps=run.steps,
        actions=run.actions,
        terminated=run.terminated,
        truncated=run.truncated,
        ended_by=_ending(flags, steps=run.steps, cap=cap, budget=budget, kind=kind),
        flags=flags,
        collisions=run.collisions,
        route_completion=None if completion is None else round(float(completion), 6),
        observation_shape=run.observation_shape,
        observation_shape_end=run.observation_shape_end,
        action_shape=run.action_shape,
        seconds=round(run.seconds, 3),
        ms_per_step=round(run.seconds / run.steps * 1000, 3) if run.steps else 0.0,
        env=env_report,
    )


class _RigReader:
    """The `observe` hook that reads a rig once per decision and keeps the cost.

    The loop calls `observe` after the reset and after every step, so call `k` sees the scene
    the decision at step `k` is made on; reading every `stride`-th call is reading at the
    decision rate, which is the interval the spec's `tick_rate` is checked against.
    """

    def __init__(self, rig: Any, stride: int) -> None:
        self.rig = rig
        self.stride = stride
        self.calls = 0
        self.reads = 0
        self.seconds = 0.0
        self.frames: dict[str, tuple[int, ...]] = {}

    def __call__(self, env: Any) -> None:
        del env  # the cameras are already on the ego; the rig reads its own sensors
        if self.calls % self.stride == 0:
            started = time.perf_counter()
            frames = self.rig.read()
            self.seconds += time.perf_counter() - started
            self.reads += 1
            self.frames = {name: tuple(int(n) for n in f.shape) for name, f in frames.items()}
        self.calls += 1


def _env_report(env: Any, rig: Any, reader: _RigReader | None, read_interval_s: float) -> EnvReport:
    """What the env held, read off its engine while it is still open."""
    from scenariobank.av3.camera_rig import image_buffers

    return EnvReport(
        sensors=sorted(env.engine.sensors),
        image_observation=bool(env.config["image_observation"]),
        image_source=env.config["vehicle_config"].get("image_source"),
        image_buffers=image_buffers(env),
        rig=None if rig is None else rig.path,
        rig_cameras=[] if rig is None else list(rig.names),
        rig_tick_rate_s=None if rig is None else rig.tick_rate_s,
        read_interval_s=None if rig is None else round(read_interval_s, 6),
        frames={} if reader is None else dict(reader.frames),
        reads=0 if reader is None else reader.reads,
        ms_per_read=(
            round(reader.seconds / reader.reads * 1000, 3) if reader and reader.reads else 0.0
        ),
    )


def format_episode(episode: Episode) -> str:
    """The drive as aligned text, in the order a person reads it.

    Identity first, then what was set up, then what happened -- the same shape
    `workspace.format_report` and `review`'s recorded half already print in, so a reader moving
    between the three is not relearning a layout each time.
    """
    hz = (
        "every step"
        if episode.decision_hz is None
        else f"{episode.decision_hz:g} Hz, one action every {episode.stride} steps"
    )
    shape = episode.observation_shape
    same = shape == episode.observation_shape_end
    lines = [f"{episode.bank_id}: {episode.scenario_id}  ({episode.category})"]
    if episode.kind == "recorded":
        lines.append(f"  recording:  {episode.stored_id}  index {episode.scenario_index}")
        lines.append(
            f"  replay:     {episode.step_hz:g} Hz, {episode.budget} frames   decisions {hz}"
        )
    else:
        lines.append(f"  road:       seed {episode.seed}  to {episode.destination}")
        lines.append(
            f"  drive:      {episode.step_hz:g} Hz, {episode.budget} steps   decisions {hz}"
        )
    lines += [
        f"  drove:      {episode.steps} steps, {episode.actions} actions"
        f"   ended: {episode.ended_by}",
        f"  observed:   {shape}"
        + (
            "  unchanged across the episode"
            if same
            else f"  CHANGED to {episode.observation_shape_end}"
        )
        + f"   action {episode.action_shape}",
    ]
    hits = {name: count for name, count in episode.collisions.items() if count}
    if hits:
        lines.append(
            "  collided:   " + ", ".join(f"{name} x{count}" for name, count in hits.items())
        )
    if episode.route_completion is not None:
        lines.append(f"  route:      {episode.route_completion * 100:.1f}% completed")
    lines.append(
        f"  cost:       {episode.seconds:.1f} s wall, {episode.ms_per_step:.2f} ms/step"
    )
    if episode.env is not None and episode.env.rig is not None:
        lines += _format_rig(episode.env)
    if not same:
        lines.append(
            "  ! the observation changed width mid-episode, which no policy can be handed"
        )
    return "\n".join(lines)


def _format_rig(report: EnvReport) -> list[str]:
    """The rig's lines of the text report: what is alive, what it cost, and the rate."""
    shapes = {
        name: "x".join(str(n) for n in shape[1::-1]) for name, shape in report.frames.items()
    }
    lines = [
        f"  rig:        {report.rig}  {len(report.rig_cameras)} cameras, "
        f"{report.image_buffers} image buffers, image_source {report.image_source}",
        "  cameras:    "
        + ", ".join(f"{name} {shapes.get(name, 'unread')}" for name in report.rig_cameras),
        f"  read:       {report.reads} times, {report.ms_per_read:.1f} ms per read of the rig",
    ]
    missing = sorted(set(report.rig_cameras) - set(report.sensors))
    if missing:
        lines.append(f"  ! not on the env: {', '.join(missing)}")
    if (
        report.rig_tick_rate_s is not None
        and report.read_interval_s is not None
        and abs(report.rig_tick_rate_s - report.read_interval_s) > 1e-9
    ):
        lines.append(
            f"  ! the spec declares tick_rate {report.rig_tick_rate_s:g} s and the cameras "
            f"were read every {report.read_interval_s:g} s; nothing resampled"
        )
    return lines


__all__ = [
    "BUDGETED",
    "CAPPED",
    "ENDINGS",
    "IDLE_ACTION",
    "MAX_STEP_PHRASE",
    "STILL_DRIVING",
    "EnvReport",
    "Episode",
    "drive",
    "format_episode",
    "replay_config",
    "select",
    "stride_for",
]
