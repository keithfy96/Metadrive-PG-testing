"""Run the AV3 model beside a drive and check every conversion into it, while nothing steers.

    scenariobank av3 --bank banks/t-junction --camera-rig rigs/av3.txt --step-hz 100 \\
        --decision-hz 20 --model-config ../models/model_dev.yml --checkpoint ../models/x.ep
    scenariobank av3 --bank banks/t-junction --camera-rig rigs/av3.txt --no-model   # host

The converter's `tools/av3_probe.py`, its model half, ported for Phase 4 Step 7 (the rig half
is `scenariobank rig --check-frame`). `av3_model.py` writes five conversions into the model --
pixels, camera order, frame history, ego speed, route -- and **not one of them raises when it
is wrong**. A mirrored route or a swapped camera pair produces a model that runs, returns
twenty plausible waypoints, and drives into the oncoming carriageway. So they are checked here
first, on a car driven by something else, where the answer is known.

**Nothing here steers from the model.** The converter replayed a tape; a bank's road has no
tape, so the reference driver is the bundled expert (`ExpertPolicy`, the ceiling), which turns
where the route turns -- the conversions this exists to check only say anything through a
corner. The model observes and predicts beside it and moves nothing.

It reports four things, in the order of what they actually prove:

1. **the camera map** -- which rig camera fills which of the model's six slots, each one's
   resolved aim in words, and any rig camera nothing reads. Conversion 2.
2. **the ego state** beside the raw speed it was built from. Conversion 4.
3. **the navigation block** beside `openpilot_policy.route_points`, the bridge's own route
   input. Both project the same `PointLane` onto the same car by different code, and the
   model's block is mirrored where the route points are not -- so agreement after
   un-mirroring is a direct test of conversion 5, and a metre of disagreement is a sign error
   rather than a rounding one.
4. **the predicted waypoints against where the car actually went.** The ego's pose is kept at
   every step, so each prediction is scored against the position the car really reached at
   that horizon: along-track and cross-track, per horizon step, and the off-path distance
   under both sign conventions -- conversion 6 is a property of the weights and cannot be
   read off a source file, so it is measured, and the **nav response** (the same pictures with
   the route replaced by a synthetic arc bending right, then left) is what separates "the
   route is mirrored" from "the model ignores the route" from "the road really is straight".

**A pass costs about a second**, so `--decisions` bounds how many are run and defaults to a
number that finishes in under a minute. `--no-model` skips the checkpoint entirely and still
checks conversions 2, 4 and 5 -- the three that need no forward pass -- in seconds, on a
machine with no torch. Exit 0 when every checked conversion agrees, 1 when one fails, 2 when
the probe could not be set up.
"""

from __future__ import annotations

import math
import statistics
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from scenariobank.av3 import av3_model
from scenariobank.av3.av3_model import ModelError
from scenariobank.av3.camera_rig import RigError, load_rig
from scenariobank.av3.openpilot_policy import ROUTE_SPACING_M, route_points
from scenariobank.av3.policy import route_for
from scenariobank.bank import BankError, Manifest
from scenariobank.env import budget_at, build_env, seed_for, step_hz_for
from scenariobank.options import resolve_options
from scenariobank.policies import PolicyError, load_policy
from scenariobank.replay import select
from scenariobank.runner import bind_policy, run_episode, stride_for

DEFAULT_DECISIONS = 40
DEFAULT_NAV_SWEEP_M = 30.0
DEFAULT_DRIVER = "scenariobank.policies:ExpertPolicy"

Say = Callable[[str], None]


class ProbeError(RuntimeError):
    """The probe could not be set up. Exit 2, and it says why."""


def _median(values: list[float]) -> float:
    return statistics.median(values) if values else float("nan")


def ego_frame(here: Any, heading: float, world: Any) -> tuple[float, float]:
    """A world point in the car's own frame: `(ahead, left)`, metres."""
    east = float(world[0]) - float(here[0])
    north = float(world[1]) - float(here[1])
    cos_heading, sin_heading = math.cos(heading), math.sin(heading)
    return (
        east * cos_heading + north * sin_heading,
        -east * sin_heading + north * cos_heading,
    )


def distance_to_path(point: tuple[float, float], path: list[tuple[float, float]]) -> float:
    """Shortest distance from a point to a polyline, both in the same plane.

    **Not** the distance to the path point at the same time. The two are different questions
    and only this one is about the path's SHAPE: a model that predicts the right line at the
    wrong speed is metres away from where the car will be in two seconds and zero metres away
    from the line it will drive. Conversion 6 is a question about shape, so it is asked this
    way; the speed intent is reported separately, in the along-track columns.
    """
    px, py = point
    best = float("inf")
    for index in range(len(path) - 1):
        ax, ay = path[index]
        bx, by = path[index + 1]
        dx, dy = bx - ax, by - ay
        span = dx * dx + dy * dy
        if span < 1e-12:
            along = 0.0
        else:
            along = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / span))
        best = min(best, math.hypot(px - (ax + along * dx), py - (ay + along * dy)))
    return best


class _Sampler:
    """The actor the loop calls: the reference driver, with the model observing beside it.

    Every decision feeds the ring -- the ring is the model's history, and filling it only where
    a prediction is wanted would hand the model five frames from five parts of the drive -- and
    every `every`-th decision is sampled: ego state, navigation block, route points, and a
    forward pass when there is a model.
    """

    def __init__(
        self,
        driver: Any,
        rig: Any,
        config: av3_model.Config,
        model: av3_model.AV3Model | None,
        *,
        stride: int,
        every: int,
        say: Say,
    ) -> None:
        self.driver = driver
        self.rig = rig
        self.config = config
        self.model = model
        self.stride = stride
        self.every = max(1, every)
        self.say = say
        self.env: Any = None
        self.route: Any = None
        self.calls = 0
        self.samples: list[dict[str, Any]] = []
        self.pass_seconds: list[float] = []
        self.levels: dict[str, tuple[float, float]] | None = None

    def bind(self, env: Any) -> None:
        self.env = env
        self.route = None
        bind_policy(self.driver, env)

    def __call__(self, observation: Any) -> Any:
        env = self.env
        agent = env.agent
        step = self.calls * self.stride
        if self.route is None:
            self.route = route_for(env)
            if self.model is not None and self.model.n_waypoints is None:
                # After the env's own window and terrain exist, the converter's ordering.
                self.model.load()
                self.say(
                    f"loaded       {self.model.n_waypoints} waypoints x {self.model.output_width}"
                    f" in {self.model.load_seconds:.1f} s; horizon {av3_model.MODEL_HORIZON_S:g} "
                    f"s, so {av3_model.MODEL_HORIZON_S / self.model.n_waypoints:g} s spacing"
                )
                self.model.start_episode()
        frames = self.rig.read()
        # Not the first decision: at step 0 the buffers hold whatever `reset` left, and all six
        # read alike. A few decisions in they are six views. One reading, so "the model is fed
        # black" is visible rather than inferred from a bad prediction.
        if self.levels is None and self.calls >= 4:
            self.levels = {
                name: (float(frame.mean()), float(frame.std())) for name, frame in frames.items()
            }
        if self.model is not None:
            self.model.observe(frames, agent)
        if self.calls % self.every == 0:
            sample: dict[str, Any] = {
                "step": step,
                "speed": float(agent.speed),
                "ego_state": av3_model.ego_state(agent, self.config.ego_velocity_scale),
                "navigation": av3_model.navigation(
                    agent,
                    self.route,
                    self.config.n_route,
                    self.config.route_spacing_m,
                    self.config.route_max_offset_m,
                ),
                "route": route_points(
                    agent, self.route, self.config.n_route, self.config.route_spacing_m
                ),
                "prediction": None,
            }
            if self.model is not None:
                started = time.perf_counter()
                sample["prediction"] = self.model.predict(agent, self.route)
                self.pass_seconds.append(time.perf_counter() - started)
            self.samples.append(sample)
        self.calls += 1
        return self.driver(observation)


class _Track:
    """The `observe` hook: the ego's pose after the reset and after every step."""

    def __init__(self) -> None:
        self.poses: list[tuple[tuple[float, float], float]] = []

    def __call__(self, env: Any) -> None:
        agent = env.agent
        self.poses.append(
            ((float(agent.position[0]), float(agent.position[1])), float(agent.heading_theta))
        )


def probe(
    bank_dir: Path,
    manifest: Manifest,
    *,
    camera_rig: Path,
    scenario: str | None = None,
    model_config: str | None = None,
    checkpoint: str | None = None,
    no_model: bool = False,
    step_hz: float | None = None,
    decision_hz: float | None = None,
    ignore_rig_rate: bool = False,
    decisions: int = DEFAULT_DECISIONS,
    nav_sweep_m: float = DEFAULT_NAV_SWEEP_M,
    driver: str = DEFAULT_DRIVER,
    say: Say = print,
) -> int:
    """Drive one scenario with the reference driver, the model beside it, and report.

    Returns the exit code: 0 when every checked conversion agrees, 1 when one fails. Raises
    `ProbeError` when the probe cannot be set up -- no config, no checkpoint, a rig the model
    cannot read -- which the command turns into exit 2.
    """
    try:
        config = av3_model.load_config(model_config)
    except ModelError as error:
        raise ProbeError(str(error)) from error
    checkpoint_file = None
    if not no_model:
        try:
            checkpoint_file = av3_model.checkpoint_path(checkpoint)
        except ModelError as error:
            raise ProbeError(str(error)) from error
        if not Path(checkpoint_file).exists():
            raise ProbeError(f"no checkpoint at {checkpoint_file}")
        try:
            import torch  # noqa: F401
        except ImportError as error:
            raise ProbeError(
                "torch is not installed in this interpreter; the sim image carries it. "
                "--no-model checks conversions 2, 4 and 5 without it."
            ) from error

    try:
        category, entry, row = select(manifest, scenario)
        options = resolve_options(manifest)
        rate = step_hz_for(entry, step_hz)
        stride = stride_for(rate, decision_hz, what="the env")
    except (BankError, ValueError) as error:
        raise ProbeError(str(error)) from error
    decision_interval = stride / rate
    try:
        rig = load_rig(camera_rig, read_interval_s=None if ignore_rig_rate else decision_interval)
    except RigError as error:
        raise ProbeError(f"rig spec rejected: {error}") from error
    try:
        reference = load_policy(driver)
    except PolicyError as error:
        raise ProbeError(str(error)) from error

    say(f"bank         {manifest.bank_id}  {category} / {row.scenario_id}")
    say(f"model cfg    {config.path}")
    for index, line in enumerate(rig.describe()):
        say(("rig          " if index == 0 else "             ") + line)
    say(
        f"decision     {1.0 / decision_interval:g} Hz - every {stride} env.step at {rate:g} Hz, "
        f"{decision_interval:g} s apart; the {driver.rpartition(':')[2]} drives"
    )

    # ---- conversion 2, before an env even exists ------------------------------------
    known = set(rig.names)
    missing = [name for name in config.camera_order if name not in known]
    unread = [name for name in rig.names if name not in config.camera_order]
    say("")
    say("camera map   model slot        rig camera        aims")
    aims = {camera.name: camera.aim for camera in rig.cameras}
    for index, name in enumerate(config.camera_order):
        say(
            "             [{}] {:<14}{:<18}{}".format(
                index, name, name if name in known else "-- MISSING --", aims.get(name, "")
            )
        )
    if unread:
        say(f"             note: {', '.join(unread)} in the rig and read by nothing")
    if missing:
        say(
            f"result       FAILED: the rig has no {', '.join(missing)}. `camera_order` in "
            "the model config is a contract with the weights."
        )
        return 1

    history = av3_model.FrameHistory(config.t_frames, config.frame_stride_s, decision_interval)
    say(
        f"history      {config.t_frames} frames {history.actual_stride_s:g} s apart "
        f"(stride {history.stride}, ring {history.depth} deep, "
        f"{history.depth * len(rig) * 3 * config.image_height * config.image_width / 1e6:.1f} "
        "MB of uint8)"
    )
    if history.spacing_note:
        say("             note: " + history.spacing_note)

    model = None
    if checkpoint_file is not None:
        say("")
        say(f"checkpoint   {checkpoint_file}")
        model = av3_model.AV3Model(config, checkpoint_file, decision_interval)

    budget = budget_at(entry.budget_for(row), entry, step_hz)
    wanted = decisions if decisions > 0 else budget
    every = max(1, math.ceil(budget / stride / max(1, wanted)))
    sampler = _Sampler(reference, rig, config, model, stride=stride, every=every, say=say)
    track = _Track()
    env, prepare = build_env(bank_dir, entry, options, rig=rig, step_hz=step_hz)
    sweep: dict[str, np.ndarray] = {}
    try:
        sampler.bind(env)
        drive = run_episode(
            env,
            seed=seed_for(row),
            prepare=lambda built: prepare(built, row),
            cap=budget,
            stride=stride,
            act=sampler,
            observe=track,
        )
        # **Does the route reach the output at all, and with which sign?** Everything below
        # infers that from a drive, which cannot separate "the model ignores the route" from
        # "the route is mirrored" from "the road ahead really is straight". This asks the model
        # directly: same pictures, same ego state, and a navigation block replaced by a
        # synthetic arc of known curvature -- once bending right, once left.
        if model is not None and nav_sweep_m > 0 and model.n_waypoints is not None:
            for label, sign in (("right", +1.0), ("left", -1.0)):
                block = av3_model.synthetic_route(
                    config.n_route, config.route_spacing_m, sign * nav_sweep_m
                )
                sweep[label] = model.predict_with_navigation(block)
    finally:
        if model is not None:
            model.close()
        env.close()

    samples = sampler.samples
    say("")
    ended = "arrived" if drive.info.get("arrive_dest") else "ended"
    say(
        f"drove        {drive.steps} of {budget} steps ({ended}), {len(samples)} decision(s) "
        f"sampled of {sampler.calls}"
    )
    if sampler.levels:
        levels = ", ".join(
            f"{name} {mean:.1f}/{sd:.1f}" for name, (mean, sd) in sampler.levels.items()
        )
        say(f"frames       mean/sd pixel level, a few decisions in: {levels}")
        flat = [name for name, (_, sd) in sampler.levels.items() if sd < 1.0]
        if flat:
            say(
                f"             FAIL  {', '.join(flat)} render a flat frame. The model is being "
                "shown a blank picture and will still return twenty waypoints."
            )
    if sampler.pass_seconds:
        per_pass = _median(sampler.pass_seconds)
        say(
            f"forward pass median {per_pass * 1000:.0f} ms over {len(sampler.pass_seconds)} - "
            f"{per_pass * sampler.calls:.0f} s for this whole drive at every decision"
        )
    if not samples:
        say("result       FAILED: no decision was sampled")
        return 1

    ok_ego = _report_ego(samples, config, say)
    ok_route = _report_navigation(samples, config, say)
    ok_waypoints = _report_waypoints(samples, track.poses, config, rate, sweep, nav_sweep_m, say)

    failed = not (ok_ego and ok_route and ok_waypoints)
    say("")
    say("result       " + ("FAILED" if failed else "every checked conversion agrees"))
    return 1 if failed else 0


def _report_ego(samples: list[dict[str, Any]], config: av3_model.Config, say: Say) -> bool:
    """Conversion 4: the pair the model is fed, beside the speed it was built from."""
    say("")
    say("ego state    the pair the model is fed, beside the speed it was built from")
    say("             step   speed m/s   v_fwd norm   v_lat norm   v_fwd m/s   v_lat m/s")
    s_lon, s_lat = config.ego_velocity_scale
    for sample in samples[:6]:
        forward = float(sample["ego_state"][0]) * s_lon
        lateral = float(sample["ego_state"][1]) * s_lat
        say(
            "             {:<6} {:>9.3f}   {:>10.4f}   {:>10.4f}   {:>9.3f}   {:>9.3f}".format(
                sample["step"], sample["speed"], sample["ego_state"][0],
                sample["ego_state"][1], forward, lateral,
            )
        )
    worst = max(
        abs(math.hypot(s["ego_state"][0] * s_lon, s["ego_state"][1] * s_lat) - s["speed"])
        for s in samples
    )
    ok = worst < 0.05
    say(
        f"             {'ok  ' if ok else 'FAIL'}  |[v_fwd, v_lat]| against agent.speed differs "
        f"by at most {worst:.4f} m/s"
    )
    say(
        "             v_lat is RIGHT-positive here and MetaDrive's own is LEFT-positive; a car "
        "on a left-hand road should read negative through a left turn"
    )
    return ok


def _report_navigation(
    samples: list[dict[str, Any]], config: av3_model.Config, say: Say
) -> bool:
    """Conversion 5: the model's block against the bridge's own route points."""
    say("")
    say("navigation   the model's block against openpilot_policy.route_points")
    say("             the points are (ahead, LEFT) m; the model's is (fwd, RIGHT)/H, mirrored")
    horizon = config.n_route * config.route_spacing_m
    compared = 0
    ahead_gap = 0.0
    left_gap = 0.0
    worst_at: tuple[int, int] | None = None
    # Index 0 is the car's own projection onto the route, so it is (0, 0) on a car that is on
    # its route: it agrees under every sign convention and proves nothing. The whole window is
    # compared, and the row printed is the far end of it, where a mirrored route is metres out.
    far = config.n_route - 1
    for sample in samples:
        if not sample["navigation"].any():
            continue
        points = sample["route"]["points_m"]
        for index in range(min(config.n_route, len(points))):
            sensor_ahead, sensor_left = points[index]
            model_ahead = float(sample["navigation"][index, 0]) * horizon
            model_left = -float(sample["navigation"][index, 1]) * horizon
            compared += 1
            ahead_gap = max(ahead_gap, abs(sensor_ahead - model_ahead))
            if abs(sensor_left - model_left) > left_gap:
                left_gap = abs(sensor_left - model_left)
                worst_at = (sample["step"], index)
    say("             step   point   ahead route / model      left route / -model")
    for sample in samples[:8]:
        if not sample["navigation"].any():
            say(f"             {sample['step']:<6} off-route (all zeros)")
            continue
        sensor_ahead, sensor_left = sample["route"]["points_m"][far]
        say(
            "             {:<6} {:<7} {:>8.3f} / {:>8.3f}     {:>8.3f} / {:>8.3f}".format(
                sample["step"], far, sensor_ahead,
                float(sample["navigation"][far, 0]) * horizon, sensor_left,
                -float(sample["navigation"][far, 1]) * horizon,
            )
        )
    ok = compared > 0 and ahead_gap < 0.05 and left_gap < 0.05
    if compared:
        say(
            "             {}  over {} point(s): at most {:.4f} m ahead and {:.4f} m across "
            "(worst at step {}, point {}). Both project the same PointLane by different code "
            "and the model's is mirrored, so a metre here is a sign error, not rounding."
            "".format(
                "ok  " if ok else "FAIL", compared, ahead_gap, left_gap,
                worst_at[0] if worst_at else "-", worst_at[1] if worst_at else "-",
            )
        )
    else:
        say("             FAIL  every sample was off-route; nothing to compare")
    turning = [
        sample for sample in samples
        if sample["navigation"].any() and abs(float(sample["navigation"][far, 1])) > 0.02
    ]
    say(
        f"             {len(turning)} of {len(samples)} sampled decisions have the far end of "
        f"the route more than {0.02 * horizon:.1f} m to one side - the mirror says nothing on "
        "a straight road"
    )
    say(
        f"             the spacing matches too: the route points step {ROUTE_SPACING_M:g} m "
        f"by default and the model {config.route_spacing_m:g} m; both used the model's here"
    )
    return ok


def _report_waypoints(
    samples: list[dict[str, Any]],
    track: list[tuple[tuple[float, float], float]],
    config: av3_model.Config,
    step_hz: float,
    sweep: dict[str, np.ndarray],
    nav_sweep_m: float,
    say: Say,
) -> bool:
    """Conversion 6: the prediction against the drive, under both signs, then the nav sweep."""
    if not samples or samples[0]["prediction"] is None:
        return True
    sim_dt = 1.0 / step_hz
    times = av3_model.waypoint_times(len(samples[0]["prediction"]))
    horizon_steps = round(times[-1] / sim_dt)
    say("")
    say("waypoints    predicted, against where the car actually went")
    say("             ahead     how far the model thinks it gets - its SPEED intent")
    say("             across    its own lateral, y as given, beside the car's RIGHT-")
    say("                       positive displacement. Conversion 6 says these agree")
    say("                       in sign with nothing flipped.")
    say("             off-path  how far the predicted point lies from the line the car")
    say("                       really drove - a question about SHAPE, with the speed")
    say("                       deficit above taken out of it")
    say("             horizon   ahead pred / actual   across pred / actual   off-path y / -y")
    # Scored only where the car really goes somewhere sideways: on a straight road the
    # predicted path and its mirror are the same line. And a mirror is only VISIBLE where the
    # model itself predicts a lateral; below `resolve_m` the honest verdict is inconclusive.
    turn_m = 1.0
    resolve_m = 0.25
    ahead_rows: dict[int, list[tuple[float, float]]] = {}
    across_rows: dict[int, list[tuple[float, float]]] = {}
    off_rows: dict[int, list[tuple[float, float]]] = {}
    agreeing = 0
    resolvable = 0
    for sample in samples:
        prediction = sample["prediction"]
        if sample["step"] >= len(track):
            continue
        here, heading = track[sample["step"]]
        future = [
            ego_frame(here, heading, track[step][0])
            for step in range(sample["step"], min(len(track), sample["step"] + horizon_steps + 1))
        ]
        if len(future) < 2:
            continue
        bends = max(abs(left) for _, left in future) >= turn_m
        for index, seconds in enumerate(times):
            step = sample["step"] + round(seconds / sim_dt)
            if step >= len(track):
                continue
            actual_ahead, actual_left = ego_frame(here, heading, track[step][0])
            x = float(prediction[index][0])
            y = float(prediction[index][1])
            ahead_rows.setdefault(index, []).append((x, actual_ahead))
            if not bends:
                continue
            across_rows.setdefault(index, []).append((y, -actual_left))
            off_rows.setdefault(index, []).append(
                # `y` read as RIGHT-positive enters the LEFT-positive ego frame negated; then
                # read as LEFT-positive, unchanged.
                (distance_to_path((x, -y), future), distance_to_path((x, y), future))
            )
            if abs(y) >= resolve_m and abs(actual_left) >= turn_m:
                resolvable += 1
                agreeing += (y > 0) == (-actual_left > 0)
    for index, seconds in enumerate(times):
        ahead = ahead_rows.get(index)
        if not ahead:
            continue
        across = across_rows.get(index, [])
        off = off_rows.get(index, [])
        if off:
            tail = f"{_median([e[0] for e in off]):>8.3f} / {_median([e[1] for e in off]):<8.3f}"
        else:
            tail = "   - (nothing turning)"
        ahead_pred = _median([e[0] for e in ahead])
        ahead_real = _median([e[1] for e in ahead])
        across_pred = _median([e[0] for e in across]) if across else float("nan")
        across_real = _median([e[1] for e in across]) if across else float("nan")
        say(
            f"             {seconds:>5.1f} s  {ahead_pred:>7.2f} / {ahead_real:<9.2f}  "
            f"{across_pred:>8.2f} / {across_real:<10.2f}  {tail}"
        )
    as_given = [value for errors in off_rows.values() for value, _ in errors]
    as_flipped = [value for errors in off_rows.values() for _, value in errors]
    given, flipped = _median(as_given), _median(as_flipped)
    predicted_across = _median(
        [abs(value) for values in across_rows.values() for value, _ in values]
    )
    ok = True
    if not as_given:
        ok = False
        say(
            f"             FAIL  no sampled decision had the car more than {turn_m:.1f} m "
            "sideways over the horizon, so the sign of y is untested. Sample more decisions "
            "(--decisions 0), or a road with a junction in it."
        )
    elif resolvable < 8:
        say(
            f"             INCONCLUSIVE  the model predicts a median |y| of "
            f"{predicted_across:.3f} m, and only {resolvable} turning point(s) had it past "
            f"{resolve_m:.2f} m - so the two columns ({given:.3f} vs {flipped:.3f} m) differ "
            "by less than this can resolve. The model is predicting a near-straight line "
            "here; the sign of y is untested until it predicts a real curve."
        )
    else:
        share = agreeing / float(resolvable)
        leans = "as given" if share >= 0.5 and given <= flipped else "negated"
        say(
            f"             over {resolvable} point(s) where the model predicted more than "
            f"{resolve_m:.2f} m of lateral on a bend, its sign agreed with the car's own on "
            f"{share:.0%}; off-path {given:.3f} m as given against {flipped:.3f} m negated, "
            f"so this leans {leans}."
        )
        say(
            "             this is CONTEXT, not the verdict on conversion 6: a model with a "
            "constant lateral bias reads exactly like a mirrored one here, and the nav "
            "response below is the test that separates them"
        )
    if sweep:
        say("")
        say(
            "nav response the same pictures and ego state, with the route replaced by a "
            f"{nav_sweep_m:g} m arc"
        )
        say("             bend    predicted lateral at 2.0 s (y, model frame)")
        for label, prediction in sweep.items():
            say(f"             {label:<7} {float(prediction[-1][1]):+8.3f} m")
        right_y = float(sweep["right"][-1][1])
        left_y = float(sweep["left"][-1][1])
        spread = abs(right_y - left_y)
        say(
            f"             the two straddle {(right_y + left_y) / 2.0:+.3f} m, which is the "
            "model's standing lateral bias on this road with the route's own bend taken out "
            "- a domain-gap reading, and the reason the drive statistic above cannot settle "
            "the sign on its own"
        )
        if spread < 0.5:
            ok = False
            say(
                f"             FAIL  the two differ by {spread:.3f} m. The model answers a hard "
                "right-hand bend and a hard left-hand one with the same number, so the "
                "navigation input is not reaching the output. Every route sign conclusion "
                "above is untestable until this moves."
            )
        elif right_y > left_y:
            ok = True
            say(
                f"             ok    right-hand bend gives the larger y, by {spread:.3f} m - so "
                "the model's +y is RIGHT, the bridge's own convention, and conversion 6 flips "
                "nothing. Every other input is held fixed across the two, so this is the sign "
                "and nothing else."
            )
        else:
            ok = False
            say(
                f"             FAIL  LEFT-hand bend gives the larger y, by {spread:.3f} m - so "
                "the model's +y is LEFT and av3_model.modelv2_rows must negate it."
            )
    say(
        f"             the model config asks for waypoint_reference "
        f"{config.waypoint_reference!r}, which nothing applies - wing-sim hardcodes "
        "reference_offset_m 0.0 too, so a small systematic off-path bias in corners is the "
        "anchor rather than the model"
    )
    return ok


__all__ = [
    "DEFAULT_DECISIONS",
    "DEFAULT_DRIVER",
    "DEFAULT_NAV_SWEEP_M",
    "ProbeError",
    "distance_to_path",
    "ego_frame",
    "probe",
]
