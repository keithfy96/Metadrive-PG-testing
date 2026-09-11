"""The AV3 checkpoint, loaded here and fed from MetaDrive rather than from CARLA.

    from scenariobank.av3.av3_model import AV3Model, load_config

    model = AV3Model(load_config(path), checkpoint, decision_interval_s=0.05).load()
    model.observe(rig.read(), env.agent)               # once per decision, every decision
    prediction = model.predict(env.agent, route)       # (20, 8)
    rows = model.modelv2_rows(prediction)              # what the bridge's `from_predicted` eats

Ported from the converter's `tools/av3_model.py` (Phase 4 Step 7), which is itself
`wing-sim/evaluation/src/inference_models/av3_trt.py` + `av3_base.py` minus everything CARLA.
It is **reimplemented rather than imported** because the halves that differ -- the sensor
source, the route source, the frame -- are exactly the halves this file exists to write. One
more half differs here than in the converter: **the route.** A recorded scenario carries the
ego's own track as a `PointLane` (`navigation.reference_trajectory`); a procedural road has
no tape, so `scenariobank.av3.policy.route_for` builds the same kind of line from the route
the bank pinned, and `navigation()` takes the trajectory as an argument rather than reaching
for one attribute. Everything downstream of that line is the converter's, measurement for
measurement.

**Every conversion below fails silently.** Nothing raises when a sign is wrong; the car
simply drives somewhere else. So each one is named, its source cited, and each is checked
by `scenariobank av3` (`av3/probe.py`) against a real drive before anything steers.

    1  pixels        rig camera (H, W, 3) uint8 BGR  ->  (3, 288, 512) float32 RGB in [0, 1]
    2  camera order  by NAME, against `rigs/av3.txt`, whose names and aims agree
    3  history       a ring of `t_frames` frames `frame_stride_s` apart
    4  ego speed     [v_fwd / 8.09, v_lat_RIGHT / 0.27]
    5  route         (20, 7), MIRRORED out of MetaDrive's frame
    6  waypoints out (20, 8) straight through - already x-forward, y-RIGHT

**4 and 5 are one fact seen twice.** MetaDrive is right-handed with **y left** and yaw
CCW-positive; the frame the model was trained in is CARLA's, **y right** and yaw
CW-positive. That is a mirror, so `y`, `sin(theta)`, `yaw`, `yaw_rate`, `v_y` and curvature
all negate **together**, and `x`, `cos(theta)`, `v_x`, `a_x` do not. Getting half of it right
steers smoothly into the oncoming carriageway with nothing raising -- the same failure
`openpilot_policy`'s docstring records for the bridge's own two negations. This bank's roads
are mirrored to the left (`handedness.py`), and that changes nothing here: the mirror is on
lane geometry, and every quantity above is read off the vehicle's own frame.

**6 does NOT negate, and that is the one asymmetry.** `waypoints_from_route` flips `y`
because it *starts* from MetaDrive's left-positive route. The model's output starts in its
own training frame, which is already the bridge's; wing-sim passes it through unflipped too
(`controllers/openpilot/controller.py:140, :189`), and the converter's probe measured it
(`--nav-sweep`: a right-hand arc moves the predicted lateral +1.109 m toward the right).

**MetaDrive's camera really is BGR**, which is what makes conversion 1 the fork's modifier
verbatim rather than an adaptation of it: `perceive()` ends in panda3d's RAM image, which is
BGRA sliced to three (`image_buffer.py:104-110`), exactly what CARLA's `raw_data` is.

**The forward pass is about a second** -- 947-1002 ms measured by the converter on a card
capped at 35 W. `env.step` is the tick, so a slow policy makes a slow drive and never a wrong
one; a 60 s route at 20 Hz decisions is 1200 forward passes, so twenty minutes of wall clock.
That is arithmetic, not a fault, and Step 8 records it.

**The config is the submission's, and nothing in it is defaulted.** Both this repo's
neighbours ship a `model_dev.yml` with different schemas; `load_config` reads the `model:`
block, requires every key in `REQUIRED_KEYS` and refuses a missing one by name, because a
silently-defaulted `frame_stride_s` is the exact failure that file's own comment warns
about: the model runs, on history spaced differently to how it was trained, and the run
still scores. The path is `run --model-config`, or `MODEL_CONFIG` in the environment (the
converter's convention, kept so a container can carry it), and nothing else.

Nothing here imports torch or MetaDrive at module scope: the config, the ring and every
conversion are testable on a machine with neither, and `AV3Model.load` is where the 1.2 GB
engine and the GPU are first touched.
"""

from __future__ import annotations

import collections
import contextlib
import math
import os
from pathlib import Path
from typing import Any

import numpy as np

#: The fixed horizon the waypoint times are spread over. `av3_base.MODEL_HORIZON_S`, and NOT a
#: `model_dev.yml` key: it is `Av3ModelSettings.waypoint_horizon_s`'s default
#: (`model_dev_config.py:19`) and the shipped config does not carry it. With the 20 waypoints
#: this checkpoint emits that is 0.1 s spacing.
MODEL_HORIZON_S = 2.0

#: `av3_base.MODELV2_OUTPUT_WIDTH`: [x, y, yaw, yaw_rate, v_x, v_y, a_x, a_y]. The narrower
#: layout (2, waypoints only) exists in wing-sim and this checkpoint does not use it -- the
#: converter read (1, 20, 8) straight out of the archive -- so a width of 2 is refused here
#: rather than silently taking the bridge's `derive` path.
MODELV2_OUTPUT_WIDTH = 8

#: `routes/route.py:ROUTE_FEATURE_DIM`: `[fwd/H, right/H, cos t, sin t, curv*H, s_norm, valid]`.
ROUTE_FEATURE_DIM = 7

#: The environment variable that names the config when no path is given: the converter's own
#: convention, paired with `MODEL_CHECKPOINT` for the weights. There is no path default at all.
MODEL_CONFIG_VARIABLE = "MODEL_CONFIG"
MODEL_CHECKPOINT_VARIABLE = "MODEL_CHECKPOINT"

#: Read out of `model_dev.yml`'s `model:` block and required to be present. Nothing here is
#: defaulted.
REQUIRED_KEYS: tuple[str, ...] = (
    "camera_order",
    "t_frames",
    "frame_stride_s",
    "ego_velocity_scale",
    "n_route",
    "route_spacing_m",
    "route_max_offset_m",
    "expected_camera_image_width",
    "expected_camera_image_height",
)


class ModelError(RuntimeError):
    """Something about the model, its config or its inputs. Always says which."""


class Config:
    """`model_dev.yml`'s `model:` block, with every field this needs present."""

    def __init__(self, values: dict[str, Any], path: str | None = None) -> None:
        self.path = path
        missing = [key for key in REQUIRED_KEYS if values.get(key) is None]
        if missing:
            raise ModelError(
                f"{path or 'the model config'} has no {', '.join(missing)} under `model:`. "
                "Nothing here is defaulted: the shipped config's own comment says a wrong "
                "frame_stride_s never stops a run, it just feeds the model history it was not "
                "trained on."
            )
        self.camera_order = [str(name) for name in values["camera_order"]]
        self.t_frames = int(values["t_frames"])
        self.frame_stride_s = float(values["frame_stride_s"])
        scale = values["ego_velocity_scale"]
        if not isinstance(scale, (list, tuple)) or len(scale) != 2:
            raise ModelError(
                f"ego_velocity_scale is {scale!r}; this checkpoint is AV31 and takes the "
                "two-component [s_lon, s_lat] form"
            )
        self.ego_velocity_scale = (float(scale[0]), float(scale[1]))
        self.n_route = int(values["n_route"])
        self.route_spacing_m = float(values["route_spacing_m"])
        self.route_max_offset_m = float(values["route_max_offset_m"])
        self.image_width = int(values["expected_camera_image_width"])
        self.image_height = int(values["expected_camera_image_height"])
        # Read and NOT applied, exactly as in wing-sim, where `Evaluation.cleanup` hardcodes
        # `reference_offset_m=0.0` and its own `docs/reference/known-gaps.md` records that
        # nothing reads this field. Carried so the probe can say so beside a cross-track number
        # that the anchor would bias in corners.
        self.waypoint_reference = str(values.get("waypoint_reference", "origin"))
        #: The checkpoint file the yml names, relative to itself; a hint for `--checkpoint`,
        #: never a default -- the weights are a separate submission artifact.
        named = values.get("checkpoint")
        self.checkpoint_name = None if named is None else str(named)
        if len(self.camera_order) != len(set(self.camera_order)):
            raise ModelError(f"camera_order repeats a name: {self.camera_order}")
        if self.t_frames < 1:
            raise ModelError(f"t_frames is {self.t_frames}")
        if self.frame_stride_s <= 0:
            raise ModelError(f"frame_stride_s is {self.frame_stride_s}")
        if self.n_route < 2:
            raise ModelError(f"n_route is {self.n_route}; the route window needs two points")
        if self.route_spacing_m <= 0:
            raise ModelError(f"route_spacing_m is {self.route_spacing_m}")
        if self.image_width < 1 or self.image_height < 1:
            raise ModelError(
                f"expected_camera_image_width x height is {self.image_width}x{self.image_height}"
            )


def config_path(given: str | os.PathLike[str] | None) -> str:
    """The config to read: the path given, else `MODEL_CONFIG`, else a refusal naming both."""
    if given is not None:
        return str(given)
    from_env = os.environ.get(MODEL_CONFIG_VARIABLE)
    if from_env:
        return from_env
    raise ModelError(
        "no model config: pass --model-config <submission>/model_dev.yml, or set "
        f"{MODEL_CONFIG_VARIABLE}. There is no default -- the file is a contract with one "
        "set of weights, and the wrong one runs and scores."
    )


def checkpoint_path(given: str | os.PathLike[str] | None) -> str:
    """The weights to load: the path given, else `MODEL_CHECKPOINT`, else a refusal."""
    if given is not None:
        return str(given)
    from_env = os.environ.get(MODEL_CHECKPOINT_VARIABLE)
    if from_env:
        return from_env
    raise ModelError(
        "no checkpoint: pass --checkpoint <submission>/<name>.ep, or set "
        f"{MODEL_CHECKPOINT_VARIABLE}. The model config names the file it was trained with "
        "under `checkpoint:`, beside itself."
    )


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """Read a `model_dev.yml`: the `model:` block, every required key present."""
    import yaml

    resolved = Path(config_path(path)).resolve()
    if not resolved.exists():
        raise ModelError(f"no model config at {resolved}")
    try:
        document = yaml.safe_load(resolved.read_text()) or {}
    except yaml.YAMLError as error:
        raise ModelError(f"{resolved} is not YAML: {error}") from error
    section = document.get("model") if isinstance(document, dict) else None
    if not isinstance(section, dict):
        raise ModelError(f"{resolved} has no `model:` block")
    return Config(section, path=str(resolved))


# ---------------------------------------------------------------------------------------
# 1. pixels
# ---------------------------------------------------------------------------------------


def preprocess(bgr: np.ndarray, width: int, height: int) -> np.ndarray:
    """(H, W, 3) uint8 BGR -> (3, height, width) **uint8** RGB, channel-first.

    The fork's `modifiers.py:camera_preprocessing` without its final `/ 255.0`, which
    `FrameHistory.sampled` applies instead so the ring can hold a quarter of the bytes. The
    converter established that the divide is the thing that *creates* the float and that
    `(uint8 / 255 * 255).round()` returns all 256 values exactly, so nothing is lost by
    holding the picture the way it was rendered; `test_av3_model.py` holds the two pixel for
    pixel against the fork's own file where it is present.

    The resize is **not** a no-op and must not be made one. The rig renders 4:3 and the model
    eats 16:9: this is a straight vertical squash by 1.33x, and the model was trained on the
    squashed picture. Rendering 512x288 natively instead would give a vertical field of view
    a third narrower than the model has ever seen, with nothing raising. See `rigs/av3.txt`.
    """
    import cv2

    if bgr.ndim != 3 or bgr.shape[2] != 3:
        raise ModelError(f"a camera frame must be (H, W, 3); got {bgr.shape}")
    if bgr.dtype != np.uint8:
        raise ModelError(
            f"a camera frame must be uint8; got {bgr.dtype}. Read the rig with "
            "`to_float=False` -- a float frame here is an 8x inflation for nothing."
        )
    if bgr.shape[:2] != (height, width):
        bgr = cv2.resize(bgr, (width, height), interpolation=cv2.INTER_AREA)
    # After the resize, not before: the same pixels either way, and the fork measures 37 ms
    # of difference at 1440x1080.
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    return np.ascontiguousarray(np.transpose(rgb, (2, 0, 1)))


# ---------------------------------------------------------------------------------------
# 3. the temporal ring
# ---------------------------------------------------------------------------------------


class FrameHistory:
    """`t_frames` frames `frame_stride_s` apart, out of a ring filled once per decision.

    `av3_base.__init__`'s arithmetic, with one difference stated rather than hidden: there,
    `tick_rate` is the CAMERA rate and `observe` is called on every camera tick including the
    ones the model does not predict on. Here the camera read and the decision are the same
    event -- the policy reads the rig when the loop asks it to act, at `--decision-hz` -- so
    the two rates are one number and the ring is filled exactly once per prediction.

    **The ego state is buffered beside the pictures and sampled with them**, because the
    engine takes `(1, T, 2)` and not `(1, 2)` -- `av3_base` keeps `_ego_buf` alongside
    `_image_buf` for exactly this. Tiling the current speed T times would feed the model a car
    that has been going at its present speed for the last two seconds, which is a different
    claim from the one the history makes and one nothing would raise about.

    Pictures are held as **uint8**: at 20 Hz decisions the stride is 10 and the depth 41, so
    41 x 6 x 3 x 288 x 512 is **108.8 MB** of host RAM. The same ring in preprocessed float32
    would be 435 MB for a picture that is 8-bit at the buffer.
    """

    def __init__(self, t_frames: int, frame_stride_s: float, read_interval_s: float) -> None:
        if read_interval_s <= 0:
            raise ModelError(f"the read interval is {read_interval_s} s")
        self.read_interval_s = float(read_interval_s)
        self.stride = max(1, round(frame_stride_s / read_interval_s))
        self.depth = (t_frames - 1) * self.stride + 1
        self.sample_index = [k * self.stride for k in range(t_frames)]
        self.actual_stride_s = self.stride * self.read_interval_s
        self.requested_stride_s = float(frame_stride_s)
        self._frames: collections.deque[np.ndarray] = collections.deque(maxlen=self.depth)
        self._ego: collections.deque[np.ndarray] = collections.deque(maxlen=self.depth)

    @property
    def spacing_note(self) -> str | None:
        """What to say when the read interval cannot divide the training stride.

        `av3_base` warns and carries on, and this does the same rather than refusing: the
        stride is a property of the *drive's* rate, and a run at 10 Hz decisions is still a
        run. But it is history the model was not trained on, so it must be said out loud.
        """
        if abs(self.actual_stride_s - self.requested_stride_s) <= 1e-9:
            return None
        return (
            f"frame_stride_s is {self.requested_stride_s:g} s and the cameras are read every "
            f"{self.read_interval_s:g} s, which does not divide it -- so the model will see "
            f"frames {self.actual_stride_s:g} s apart instead. --decision-hz "
            f"{1.0 / self.requested_stride_s:g} (or any rate that divides it) is what matches."
        )

    def observe(self, stack: np.ndarray, ego: np.ndarray) -> None:
        """One decision's worth: `(cameras, 3, H, W)` uint8 and a `(2,)` ego state.

        The ring is FILLED on the first call rather than left short, so a prediction can run
        immediately -- `av3_base.observe`'s own behaviour. The alternative is `depth` decisions
        at the start of every episode with nothing to steer by.
        """
        if not self._frames:
            for _ in range(self.depth):
                self._frames.append(stack)
                self._ego.append(ego)
        else:
            self._frames.append(stack)
            self._ego.append(ego)

    def reset(self) -> None:
        self._frames.clear()
        self._ego.clear()

    def __len__(self) -> int:
        return len(self._frames)

    def sampled(self) -> tuple[np.ndarray, np.ndarray]:
        """`((1, T, cameras, 3, H, W) float32 in [0, 1], (1, T, 2) float32)`.

        `av3_base._sampled_images` / `_sampled_ego` index a newest-*last* deque at
        `[0, stride, 2*stride, ...]`, so index 0 is the OLDEST frame in a full ring. The order
        is reproduced exactly rather than reasoned about: a reversed history is another thing
        that runs and is wrong.
        """
        if not self._frames:
            raise ModelError("sampled() before observe(): the ring is empty")
        images = np.stack([self._frames[i] for i in self.sample_index], axis=0)
        ego = np.stack([self._ego[i] for i in self.sample_index], axis=0)
        return (images.astype(np.float32) / 255.0)[None, ...], ego[None, ...]


# ---------------------------------------------------------------------------------------
# 4 and 5. the mirror
# ---------------------------------------------------------------------------------------


def ego_state(agent: Any, velocity_scale: tuple[float, float]) -> np.ndarray:
    """`[v_fwd / s_lon, v_lat_right / s_lat]` float32, from MetaDrive's own velocity.

    `av3_base._build_ego_state` reads CARLA's `velocity_x` / `velocity_y` and its `rotation_yaw`,
    where `-vx sin + vy cos` is the component to the **right**. MetaDrive's world frame is
    right-handed with y north and `heading_theta` CCW from +x, so the identical expression is
    the component to the **left** -- hence the negation, and hence conversion 4.

    Read off `agent.velocity` rather than off `agent.speed`, which is a magnitude and cannot
    tell a sideways slide from forward motion.
    """
    velocity = agent.velocity
    heading = float(agent.heading_theta)
    cos_heading, sin_heading = math.cos(heading), math.sin(heading)
    east, north = float(velocity[0]), float(velocity[1])
    forward = east * cos_heading + north * sin_heading
    left = -east * sin_heading + north * cos_heading
    s_lon, s_lat = velocity_scale
    return np.array([forward / s_lon, -left / s_lat], dtype=np.float32)


def navigation(
    agent: Any, trajectory: Any, n_route: int, spacing_m: float, max_offset_m: float
) -> np.ndarray:
    """`(n_route, 7)` float32: `[fwd/H, right/H, cos t, sin t, curv*H, s_norm, valid]`.

    `routes/route.py:RouteNavigator.get_navigation`, rebuilt against a live line instead of a
    route parquet. `trajectory` is anything with `length`, `local_coordinates(position)`,
    `position(along, 0)` and `heading_theta_at(along)` -- MetaDrive's `PointLane`, which is
    what a recording's `reference_trajectory` is and what `policy.route_for` builds on a road.
    Two differences from wing-sim, both stated rather than papered over:

    * **the nearest point.** wing-sim runs a heading-gated argmin over every route vertex
      (`ROUTE_HEADING_COS_GATE` 0.5), because a CARLA route may double back within the gate's
      reach. Here `local_coordinates` returns the arc-length and the perpendicular offset
      directly -- MetaDrive's own projection, the one `TrajectoryNavigation` steers by. On a
      route that does not cross itself the two agree; on one that does, `local_coordinates`
      is the better answer, not the worse one.
    * **the off-route guard** is that perpendicular offset rather than the distance to the
      nearest vertex. The same number wherever the route is smooth at the scale of
      `route_max_offset_m`.

    THE MIRROR. `right = -left`, `sin(theta)` negates with it, and curvature -- which is
    `d(theta)/ds` -- negates because theta does. `fwd`, `cos(theta)`, `s_norm` and `valid` do
    not. Half of this is a car that steers confidently into the oncoming carriageway.
    """
    if trajectory is None:
        raise ModelError("the model needs a route to follow, and this drive has none")
    horizon = max(1e-6, n_route * spacing_m)
    longitudinal, lateral = trajectory.local_coordinates(agent.position)
    if abs(float(lateral)) > max_offset_m:
        # What wing-sim does off-route: a block of zeros, which is what the model was trained
        # to read as "no route". Not an error -- a car pushed wide of its route is an ordinary
        # thing for a drive to contain.
        return np.zeros((n_route, ROUTE_FEATURE_DIM), dtype=np.float32)

    heading = float(agent.heading_theta)
    cos_heading, sin_heading = math.cos(heading), math.sin(heading)
    here = agent.position
    length = float(trajectory.length)

    forward = np.zeros(n_route)
    right = np.zeros(n_route)
    theta = np.zeros(n_route)
    valid = np.zeros(n_route)
    along = np.zeros(n_route)
    for index in range(n_route):
        wanted = float(longitudinal) + index * spacing_m
        valid[index] = 1.0 if wanted <= length else 0.0
        clamped = min(wanted, length)
        along[index] = clamped
        world = trajectory.position(clamped, 0.0)
        east = float(world[0]) - float(here[0])
        north = float(world[1]) - float(here[1])
        forward[index] = east * cos_heading + north * sin_heading
        left = -east * sin_heading + north * cos_heading
        right[index] = -left
        # `heading_theta_at` clamps to the final segment past the end, which is what the
        # arc-length clamp above already assumes.
        route_heading = float(trajectory.heading_theta_at(clamped))
        # CCW-positive in MetaDrive, so negated into the model's CW-positive frame. Wrapped
        # before the negation so the wrap is done once, on the quantity that has a branch cut.
        theta[index] = -(((route_heading - heading) + math.pi) % (2 * math.pi) - math.pi)

    step = np.gradient(along)
    with np.errstate(divide="ignore", invalid="ignore"):
        curvature = np.where(np.abs(step) > 1e-6, np.gradient(np.unwrap(theta)) / step, 0.0)
    s_norm = np.arange(n_route, dtype=np.float64) / max(1, n_route - 1)
    return np.stack(
        [
            forward / horizon,
            right / horizon,
            np.cos(theta),
            np.sin(theta),
            curvature * horizon,
            s_norm,
            valid,
        ],
        axis=1,
    ).astype(np.float32)


def synthetic_route(n_route: int, spacing_m: float, radius_m: float) -> np.ndarray:
    """A navigation block for an arc of `radius_m`, in the model's own (fwd, RIGHT) frame.

    Positive radius bends RIGHT. Built with the same normalisation `navigation` applies, so the
    only thing that differs from a real block is the shape. The probe feeds the model one of
    each sign with every other input held fixed, which is the one test that separates "the
    route is mirrored" from "the model ignores the route" from "the road really is straight".
    """
    horizon = max(1e-6, n_route * spacing_m)
    curvature = 1.0 / float(radius_m)
    rows = []
    for index in range(n_route):
        along = index * spacing_m
        theta = curvature * along
        # Arc from the origin, tangent to +x at the start; +theta swings toward +y (right).
        rows.append(
            [
                math.sin(theta) / curvature / horizon,
                (1.0 - math.cos(theta)) / curvature / horizon,
                math.cos(theta),
                math.sin(theta),
                curvature * horizon,
                index / max(1, n_route - 1),
                1.0,
            ]
        )
    return np.asarray(rows, dtype=np.float32)


# ---------------------------------------------------------------------------------------
# 6. what the bridge is sent
# ---------------------------------------------------------------------------------------


def waypoint_times(n_waypoints: int, horizon_s: float = MODEL_HORIZON_S) -> list[float]:
    """`av3_base.uniform_waypoint_times`: t_i = horizon * (i + 1) / N, t=0 excluded."""
    if n_waypoints < 1:
        raise ModelError(f"n_waypoints must be >= 1, got {n_waypoints}")
    return [horizon_s * (index + 1) / n_waypoints for index in range(n_waypoints)]


def modelv2_rows(prediction: Any, horizon_s: float = MODEL_HORIZON_S) -> list[list[float]]:
    """`(N, 8)` -> N rows of `[x, y, t, yaw, yaw_rate, v_x, v_y, a_x, a_y]`.

    `from_predicted`'s shape exactly (`derive_modelv2.py:79`), which is where the time column
    lands third rather than last. It prepends its own t=0 anchor at the ego origin, so these
    are the predicted points only.

    **Nothing is negated here.** The model's frame is already the bridge's -- see the module
    docstring's conversion 6 -- and wing-sim's own controller passes the same numbers through
    unflipped (`controllers/openpilot/controller.py:189`).
    """
    prediction = np.asarray(prediction, dtype=np.float64)
    if prediction.ndim != 2 or prediction.shape[1] != MODELV2_OUTPUT_WIDTH:
        raise ModelError(
            f"modelv2 rows need an (N, {MODELV2_OUTPUT_WIDTH}) prediction; got "
            f"{prediction.shape}"
        )
    times = waypoint_times(prediction.shape[0], horizon_s)
    rows = []
    for index, row in enumerate(prediction):
        x, y, yaw, yaw_rate, v_x, v_y, a_x, a_y = (float(value) for value in row)
        rows.append([x, y, times[index], yaw, yaw_rate, v_x, v_y, a_x, a_y])
    return rows


def waypoints(prediction: Any, horizon_s: float = MODEL_HORIZON_S) -> list[list[float]]:
    """`[[x, y, t], ...]`, the 3-wide list.

    Sent **as well as** `modelv2`, not instead of it: `server.py:_handle_step` reads
    `msg["waypoints"]` first and returns a hard stop when it is empty, *before* it looks at
    `modelv2` at all. An empty `waypoints` beside a full `modelv2` is a car that never moves.
    """
    prediction = np.asarray(prediction, dtype=np.float64)
    if prediction.ndim != 2 or prediction.shape[1] < 2:
        raise ModelError(f"waypoints need an (N, >=2) prediction; got {prediction.shape}")
    times = waypoint_times(prediction.shape[0], horizon_s)
    return [[float(row[0]), float(row[1]), times[index]] for index, row in enumerate(prediction)]


# ---------------------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------------------


class AV3Model:
    """The compiled checkpoint, its ring, and the five conversions into it."""

    def __init__(
        self, config: Config, checkpoint: str, decision_interval_s: float, device: str = "cuda"
    ) -> None:
        self.config = config
        self.checkpoint = str(Path(checkpoint).resolve())
        self.device = device
        self.history = FrameHistory(config.t_frames, config.frame_stride_s, decision_interval_s)
        self._module: Any = None
        self._navigation: Any = None
        self._torch: Any = None
        self.n_waypoints: int | None = None
        self.output_width: int | None = None
        self.load_seconds: float | None = None

    # -- setup ---------------------------------------------------------------------------
    def load(self) -> AV3Model:
        """Deserialise the engine, run one pass of zeros, and read the output shape back.

        The warm-up is not a timing convenience: `av3_trt._warmup_inference` is where the
        waypoint count and the output width are **discovered**, and `av3_base.N_WAYPOINTS = 4`
        is a fallback until it runs rather than this model's count. This checkpoint emits 20.
        """
        import time

        if not Path(self.checkpoint).exists():
            raise ModelError(f"no checkpoint at {self.checkpoint}")
        try:
            import torch
            import torch_tensorrt
        except ImportError as error:
            raise ModelError(
                f"the AV3 checkpoint needs torch and torch_tensorrt, which this interpreter "
                f"cannot import ({error}). The sim image carries both; the host does not."
            ) from error
        self._torch = torch
        started = time.perf_counter()
        # Logs two failures before succeeding -- the `.pt2` package loader, then
        # `torch.jit.load` -- and neither is an error.
        self._module = torch_tensorrt.load(self.checkpoint).module()

        cameras = len(self.config.camera_order)
        # bfloat16 is what the archive declares for all three inputs (the converter read it out
        # of the serialized graph), and it is what `av3_trt` uses whenever `route_path` is set,
        # which for a live route it always effectively is.
        self._navigation = torch.zeros(
            (1, self.config.n_route, ROUTE_FEATURE_DIM), dtype=torch.bfloat16, device=self.device
        )
        images = torch.zeros(
            (
                1, self.config.t_frames, cameras, 3,
                self.config.image_height, self.config.image_width,
            ),
            dtype=torch.bfloat16,
            device=self.device,
        )
        ego = torch.zeros((1, self.config.t_frames, 2), dtype=torch.bfloat16, device=self.device)
        with torch.no_grad():
            output = self._module(images, self._navigation, ego)
        self.n_waypoints = int(output.shape[-2])
        self.output_width = int(output.shape[-1])
        self.load_seconds = time.perf_counter() - started
        if self.output_width != MODELV2_OUTPUT_WIDTH:
            raise ModelError(
                f"this checkpoint emits {self.output_width} columns per waypoint, and only "
                f"{MODELV2_OUTPUT_WIDTH} (full modelv2: x, y, yaw, yaw_rate, v_x, v_y, a_x, "
                "a_y) is wired here. A 2-wide waypoints-only model would have to go through "
                "the bridge's `derive` path instead."
            )
        return self

    def close(self) -> None:
        self._module = None
        self._navigation = None
        self.history.reset()
        if self._torch is not None and str(self.device).startswith("cuda"):
            with contextlib.suppress(RuntimeError):
                self._torch.cuda.empty_cache()

    # -- one decision --------------------------------------------------------------------
    def image_stack(self, frames: dict[str, np.ndarray]) -> np.ndarray:
        """`{name: (H, W, 3) uint8 BGR}` -> `(cameras, 3, H, W)` uint8 RGB, in model order.

        **By name**, which is conversion 2 and is only safe because `rigs/av3.txt` is built
        from wing-sim's own spec: its camera names and its resolved aims agree. `camera_order`
        is a contract with the weights -- `model_dev.yml` says so -- so a missing name is
        refused by name rather than filled with anything.
        """
        stacked = []
        for name in self.config.camera_order:
            frame = frames.get(name)
            if frame is None:
                raise ModelError(
                    f"the rig has no camera called {name!r}. The model reads "
                    f"{', '.join(self.config.camera_order)}; this rig offers "
                    f"{', '.join(sorted(frames)) or 'nothing'}. `rigs/av3.txt` is the spec "
                    "built for it."
                )
            stacked.append(preprocess(frame, self.config.image_width, self.config.image_height))
        return np.stack(stacked, axis=0)

    def observe(self, frames: dict[str, np.ndarray], agent: Any) -> None:
        """One decision's frames and ego state into the ring.

        Call once per decision, before `predict`, exactly as `av3_base.predict` observes
        first -- so the frame being predicted on is the newest in the ring rather than one
        decision stale.
        """
        self.history.observe(
            self.image_stack(frames), ego_state(agent, self.config.ego_velocity_scale)
        )

    def start_episode(self) -> None:
        self.history.reset()

    def predict(self, agent: Any, trajectory: Any) -> np.ndarray:
        """`(n_waypoints, 8)` float32, in the model's own frame.

        `observe` must have been called at least once -- the ring is what carries the history,
        and a prediction on an empty one is refused rather than run on zeros.
        """
        if self._module is None:
            raise ModelError("predict() before load()")
        route = navigation(
            agent,
            trajectory,
            self.config.n_route,
            self.config.route_spacing_m,
            self.config.route_max_offset_m,
        )
        return self._forward(route)

    def predict_with_navigation(self, route: Any) -> np.ndarray:
        """`predict`, with the route replaced by a block the caller built.

        Only the probe uses it, and only to ask the model a question a drive cannot: feed the
        same pictures and the same ego state with a synthetic arc bending one way and then the
        other, and see whether the output moves. A model that answers both with the same
        number is not reading the route at all, which no comparison against a real drive can
        distinguish from a route that is merely straight.
        """
        if self._module is None:
            raise ModelError("predict_with_navigation() before load()")
        route = np.asarray(route, dtype=np.float32)
        if route.shape != (self.config.n_route, ROUTE_FEATURE_DIM):
            raise ModelError(
                f"a navigation block must be ({self.config.n_route}, {ROUTE_FEATURE_DIM}); "
                f"got {route.shape}"
            )
        return self._forward(route)

    def _forward(self, route: np.ndarray) -> np.ndarray:
        torch = self._torch
        self._navigation.copy_(
            torch.from_numpy(route[None]).to(dtype=torch.bfloat16, device=self.device)
        )
        sampled_images, sampled_ego = self.history.sampled()
        images = torch.from_numpy(np.ascontiguousarray(sampled_images)).to(
            dtype=torch.bfloat16, device=self.device
        )
        ego = torch.from_numpy(np.ascontiguousarray(sampled_ego)).to(
            dtype=torch.bfloat16, device=self.device
        )
        with torch.no_grad():
            output = self._module(images, self._navigation, ego)
        return output[0].detach().to("cpu", torch.float32).numpy()

    def modelv2_rows(self, prediction: Any) -> list[list[float]]:
        return modelv2_rows(prediction)

    def waypoints(self, prediction: Any) -> list[list[float]]:
        return waypoints(prediction)


__all__ = [
    "MODEL_CHECKPOINT_VARIABLE",
    "MODEL_CONFIG_VARIABLE",
    "MODEL_HORIZON_S",
    "MODELV2_OUTPUT_WIDTH",
    "REQUIRED_KEYS",
    "ROUTE_FEATURE_DIM",
    "AV3Model",
    "Config",
    "FrameHistory",
    "ModelError",
    "checkpoint_path",
    "config_path",
    "ego_state",
    "load_config",
    "modelv2_rows",
    "navigation",
    "preprocess",
    "synthetic_route",
    "waypoint_times",
    "waypoints",
]
