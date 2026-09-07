"""Drive one imported recording end to end, and report what the drive measured.

Steps 1-5 of Phase 3 built the *reading* half of the import and not one line of it opens a
simulator: `import` copies pickles, `review` is a manifest read and some arithmetic, and a test
pins both. So until this module nothing had shown that a bank in `banks/junction-1` actually
**drives** -- only that it describes itself well.

This is a diagnostic and deliberately not a runner. It writes no result file, loads no policy by
path and does not touch `options.py`; the result record is Phase 4 Step 3's, and one invented here
to satisfy a single recording would be the frontend contract designed by accident. What it does
own is the step loop, which had never existed anywhere in this package -- every other MetaDrive
call in `src/` is `reset`-only, and `doctor.probe_simulator` (`doctor.py:110-134`) is the closest
model: build, reset, read, `close` in a `finally`.

Three things were claims with no measurement behind them, and driving `junction-1` settled all
three.

**A stored episode does not end by itself.** The recording is 3782 frames; with `horizon` left at
`BaseEnv`'s `None` default the env was still stepping at 6000, `terminated` and `truncated` both
false, quietly replaying past the last recorded frame rather than raising or stopping. `horizon`
is read by `done_function` (`scenario_env.py:162`) and simply was not being set -- `ScenarioEnv`'s
own config never sets it (`scenario_env.py:22-115`). So `replay_config` sets it to the row's own
`budget_for`, and `drive` keeps a loop cap as well. The two agree on every recording here; they
are belt-and-braces because the failure they guard against is silent.

**The observation is 31 wide, not 19.** Same `StateObservation` and same `SENSOR_CONFIG` as a PG
bank, and the whole 12-wide difference is navigation: a stored scenario gets `TrajectoryNavigation`
(22 scalars) where a PG road gets `NodeNetworkNavigation` (10). The number lives in
`config.SCENARIO_OBSERVATION_SHAPE` and this module is what measures it.

**The decision rate is a stride in this loop, not a MetaDrive setting.** Replay advances one
recorded frame per `env.step`, which is why the entry's `step_hz` becomes
`physics_world_step_size` with `decision_repeat = 1`; a policy that decides at 20 Hz on a 100 Hz
recording therefore holds each action for five steps. The stride changes how many actions are
issued and never how long the episode is.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from scenariobank.bank import BankError, Manifest, RealWorldEntry, RealWorldRow
from scenariobank.config import SENSOR_CONFIG

#: The action a drive issues when it is given none: neither steering nor throttle.
#:
#: Enough to answer every question this command asks -- how long the episode is, how wide the
#: observation is, what ends it, how the stride behaves -- none of which depends on going
#: anywhere. Policies arrive in Phase 4 Step 4.
IDLE_ACTION: tuple[float, float] = (0.0, 0.0)

#: What ended the episode, worst first, as `info` key -> the phrase printed for it.
#:
#: Ordered rather than mapped, because more than one can be true at once: a crash on the last
#: frame of a recording sets `crash` *and* `max_step`, and the crash is the interesting half.
#: `max_step` last of the real endings, since on a clean replay under a zero action it is the
#: expected one -- the recording ran out, which is success for a replay and would be a timeout
#: for a drive.
ENDINGS: tuple[tuple[str, str], ...] = (
    ("crash_vehicle", "hit a vehicle"),
    ("crash_object", "hit an object"),
    ("crash_building", "hit a building"),
    ("crash_human", "hit a pedestrian"),
    ("crash_sidewalk", "hit the kerb"),
    ("crash", "crashed"),
    ("out_of_road", "left the road"),
    ("arrive_dest", "arrived"),
    ("max_step", "ran out of recording"),
)

#: Neither ended nor capped: the loop was still going when it was told to stop.
STILL_DRIVING = "still driving"

#: Stopped by `--steps` rather than by anything the env said.
CAPPED = "capped short"


class Episode(BaseModel):
    """One drive, measured. A report rather than a result -- see the module docstring.

    A pydantic model and not a dataclass so that `--json` is `model_dump_json` like every other
    report this CLI prints, and so `extra="forbid"` catches a field added here and not printed.
    """

    model_config = ConfigDict(extra="forbid")

    bank_id: str
    category: str
    scenario_id: str
    #: The id the recording carries inside itself. Printed because a result that cannot be traced
    #: back to its pickle is a result nobody can check.
    stored_id: str
    scenario_index: int
    #: The rate the recording was sampled at, and so the rate the physics ran at.
    step_hz: float
    #: What a policy would have decided at. `None` means every step, which is `step_hz` itself.
    decision_hz: float | None
    #: Env steps per action. 1 when `decision_hz` is unset or equals `step_hz`.
    stride: int
    #: `entry.budget_for(row)`: the recording's own length, which is both the `horizon` and the
    #: loop cap. See the module docstring on why it is two things.
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
    #: How far along the recorded route the ego got. Under a zero action this is small and that is
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


def recorded(manifest: Manifest) -> None:
    """Refuse a bank this command cannot drive.

    The mirror of `review._procedural`, and refused by name for the same reason: a PG bank needs
    `MetaDriveEnv` plus `set_route` per row, which is Phase 4 Step 2's work, and a `replay` that
    quietly did half of it would be the second runner this project cannot afford. One runner, one
    step loop -- that is the whole bet Phase 4 is written on.
    """
    if manifest.source == "pg":
        raise BankError(
            f"{manifest.bank_id} is a procedural bank, and `replay` drives a stored recording: "
            "it opens a dataset directory at the rate its tracks were sampled at. A PG bank has "
            "no recording to replay -- its scenarios are built from a seed and a block sequence "
            "at run time. Driving one is the runner's job, which Phase 4 builds; "
            "`scenariobank review` describes what this bank holds today."
        )


def select(
    manifest: Manifest, scenario: str | None = None
) -> tuple[str, RealWorldEntry, RealWorldRow]:
    """The category, entry and row to drive: the one named, or the first in the bank.

    Defaulting to the first is safe *here* and would not be in a runner. Every conversion in every
    workspace this project has holds exactly one scenario, so "the first row" and "the only row"
    coincide; a command that made you name it would be asking for a string it already knows.
    """
    for name, entry in manifest.categories.items():
        if not isinstance(entry, RealWorldEntry):
            continue
        for row in entry.scenarios:
            if scenario is None or row.scenario_id == scenario:
                return name, entry, row
    if scenario is None:
        raise BankError(f"{manifest.bank_id} holds no recordings to replay")
    raise BankError(f"no scenario named {scenario!r} in {manifest.bank_id}")


def stride_for(step_hz: float, decision_hz: float | None) -> int:
    """How many env steps one action is held for.

    Not a MetaDrive setting. `decision_repeat` is pinned at 1 because replay advances exactly one
    recorded frame per `env.step`, so the only place a slower decision rate can live is this
    loop's own counter.
    """
    if decision_hz is None:
        return 1
    if decision_hz <= 0:
        raise ValueError(f"--decision-hz must be positive, not {decision_hz}")
    if decision_hz > step_hz:
        raise ValueError(
            f"--decision-hz {decision_hz:g} is faster than the recording's {step_hz:g} Hz. "
            "A replay cannot decide more often than there are frames to decide on."
        )
    return max(1, round(step_hz / decision_hz))


def replay_config(bank_dir: Path, entry: RealWorldEntry, row: RealWorldRow) -> dict[str, Any]:
    """The `ScenarioEnv` config for one recorded row.

    **Deliberately not `config.base_config()`.** That one pins `start_seed`, `num_scenarios: 1`,
    `horizon: 1000` and `random_spawn_lane_index` for a *generated* road, and it calls
    `handedness.install()`, which mirrors MetaDrive's PG lane geometry -- a stored map has no
    generated geometry to mirror, and it already drives on whichever side it was recorded on.
    What the two do share is the observation, so `agent_observation` and `SENSOR_CONFIG` come
    from `config.py` rather than being restated here: a recording and a PG scenario must be
    perceived through the same rig even though they end up different widths.

    `data_directory` is made absolute. It is stored relative to the bank root because a bank is
    copied and mounted, and MetaDrive resolves it against the process's working directory.
    """
    from metadrive.obs.state_obs import StateObservation

    return {
        "use_render": False,
        "agent_observation": StateObservation,
        "vehicle_config": dict(SENSOR_CONFIG),
        "data_directory": str((Path(bank_dir) / entry.dataset_dir).resolve()),
        # The dataset holds one recording per import, but the index is still the recording's own
        # position rather than 0 -- see `RealWorldRow.scenario_index` on why the two are separate.
        "start_scenario_index": row.scenario_index,
        "num_scenarios": 1,
        # One recorded frame per step. MetaDrive's default 0.02 x 5 is 10 Hz, and opening a 100 Hz
        # recording at it replays every actor at a tenth of its speed while nothing raises.
        "physics_world_step_size": 1 / entry.step_hz,
        "decision_repeat": 1,
        # The recording's own length. Without it the env replays past the last frame forever --
        # measured, see the module docstring.
        "horizon": entry.budget_for(row),
        # The recording's contents, replayed as recorded. These are what Step 5's review already
        # reports about the bank, and turning any of them off would make the drive disagree with
        # the description.
        "no_traffic": False,
        "no_light": False,
        "reactive_traffic": False,
        "log_level": logging.WARNING,
    }


def _ending(flags: dict[str, bool], *, capped: bool) -> str:
    """The one phrase for how this episode finished."""
    for key, phrase in ENDINGS:
        if flags.get(key):
            return phrase
    return CAPPED if capped else STILL_DRIVING


def _shape(value: Any) -> tuple[int, ...] | None:
    """The shape of an observation or a space, or `None` if it has none."""
    shape = getattr(value, "shape", None)
    return tuple(int(n) for n in shape) if shape else None


def drive(
    bank_dir: Path,
    manifest: Manifest,
    *,
    scenario: str | None = None,
    decision_hz: float | None = None,
    steps: int | None = None,
    action: tuple[float, float] = IDLE_ACTION,
) -> Episode:
    """Drive one recording and report the drive. Builds an env, so it needs the simulator.

    `steps` caps the run short, for a quick check that the round trip works without paying for
    the whole recording; the episode it reports then ends `capped short` rather than pretending
    the recording ran out.
    """
    # Every refusal first, and before MetaDrive is imported: a wrong bank, an unknown scenario
    # id and an impossible decision rate are all answerable off the manifest, and answering them
    # on a machine with no simulator is worth more than the line saved by importing at the top.
    recorded(manifest)
    category, entry, row = select(manifest, scenario)
    stride = stride_for(entry.step_hz, decision_hz)
    budget = entry.budget_for(row)
    cap = budget if steps is None else min(budget, steps)

    from metadrive.envs.scenario_env import ScenarioEnv

    env = ScenarioEnv(replay_config(bank_dir, entry, row))
    try:
        observation, _ = env.reset(seed=row.scenario_index)
        at_reset = _shape(observation)
        action_shape = _shape(env.action_space)
        taken = 0
        issued = 0
        info: dict[str, Any] = {}
        terminated = truncated = False
        started = time.perf_counter()
        while taken < cap:
            # The stride, and the whole of the decision rate: the same action is handed to
            # `env.step` until the next decision is due.
            if taken % stride == 0:
                issued += 1
            observation, _reward, terminated, truncated, info = env.step(list(action))
            taken += 1
            if terminated or truncated:
                break
        seconds = time.perf_counter() - started
    finally:
        env.close()

    flags = {key: bool(info.get(key)) for key, _ in ENDINGS if key in info}
    completion = info.get("route_completion")
    return Episode(
        bank_id=manifest.bank_id,
        category=category,
        scenario_id=row.scenario_id,
        stored_id=row.stored_id,
        scenario_index=row.scenario_index,
        step_hz=entry.step_hz,
        decision_hz=decision_hz,
        stride=stride,
        budget=budget,
        steps=taken,
        actions=issued,
        terminated=terminated,
        truncated=truncated,
        ended_by=_ending(flags, capped=taken >= cap and cap < budget),
        flags=flags,
        route_completion=None if completion is None else round(float(completion), 6),
        observation_shape=at_reset,
        observation_shape_end=_shape(observation),
        action_shape=action_shape,
        seconds=round(seconds, 3),
        ms_per_step=round(seconds / taken * 1000, 3) if taken else 0.0,
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
    lines = [
        f"{episode.bank_id}: {episode.scenario_id}  ({episode.category})",
        f"  recording:  {episode.stored_id}  index {episode.scenario_index}",
        f"  replay:     {episode.step_hz:g} Hz, {episode.budget} frames"
        f"   decisions {hz}",
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
    if episode.route_completion is not None:
        lines.append(f"  route:      {episode.route_completion * 100:.1f}% completed")
    lines.append(
        f"  cost:       {episode.seconds:.1f} s wall, {episode.ms_per_step:.2f} ms/step"
    )
    if not same:
        lines.append(
            "  ! the observation changed width mid-episode, which no policy can be handed"
        )
    return "\n".join(lines)


__all__ = [
    "CAPPED",
    "ENDINGS",
    "IDLE_ACTION",
    "STILL_DRIVING",
    "Episode",
    "drive",
    "format_episode",
    "recorded",
    "replay_config",
    "select",
    "stride_for",
]
