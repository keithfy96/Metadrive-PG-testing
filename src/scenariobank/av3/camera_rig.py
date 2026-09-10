"""A multi-camera rig, read from a CARLA-shaped sensor spec and mounted on the ego.

    from scenariobank.av3.camera_rig import load_rig

    rig = load_rig("rigs/av3.txt", read_interval_s=stride / step_hz)
    env, prepare = build_env(bank_dir, entry, options, rig=rig)     # `env.py` wires the config
    env.reset(seed=seed_for(row)); prepare(env, row)                 # `prepare` mounts the rig
    frames = rig.read()                                              # {name: (H, W, 3) uint8}

Ported from `wingfin-osm-scenarionet-converter/tools/camera_rig.py` (Phase 4 Step 6, 2026-09-10)
with its measurements intact, because every one of them was taken against the MetaDrive commit
this repo pins. What changed in the port: `mount` refuses an env whose cameras MetaDrive has
already deleted (gotcha 1 below), the buffer ceiling is refused at parse time rather than in a
driver, `check_frame` takes an env this repo built, and the report models at the bottom are what
`scenariobank rig` prints. What did not change is any number.

**Mount the cameras, do not borrow one.** MetaDrive's own six-view example
(`tests/scripts/multiview_generation_with_image_on_cuda.py`) re-aims a single camera per view
through `perceive(..., new_parent_node, position, hpr)`, which calls `taskMgr.step()` twice each
time -- six serialised render passes. Six cameras parented to the ego instead are all filled by
the *same* pass. Measured on the converter's `junction-1` at 320x180: **20.4 ms/step mounted
against 77.3 ms/step borrowed**, of which mounted spends only 2.2 ms in the read. That example
also shares one `ImageObservation` across its six views, so its six dict entries are the same
array object; nothing here goes through `ImageObservation` at all.

**The cameras must be kept alive on purpose.** `base_env.py:343-346` filters every `BaseCamera`
out of `config["sensors"]` when `use_render` and `image_observation` are both false, to save
render passes in headless mode, and says nothing. `env.build_env(rig=...)` therefore sets
`image_observation=True` -- not for the observation, which `agent_observation` pins at 19 either
way (`base_env.py:674-678`) -- and points `vehicle_config["image_source"]` at a rig camera, so
MetaDrive's default `rgb_camera` is never registered as a seventh buffer nothing reads. `mount`
checks the switch before it asks the engine for a camera, so a rig on an env built any other way
is refused naming that line rather than dying in `get_sensor`.

**The spec is CARLA's and MetaDrive's frame is not.** Measured by parenting a `NodePath` to
`env.agent.origin` and reading its world pose back (`check_frame` re-measures it on demand):

    local +y 1 m  ->  +1.000 m ahead, -0.000 m right     MetaDrive: +y forward,
    local +x 1 m  ->  +0.000 m ahead, +1.000 m right                +x right, +z up
    H = +55       ->  +55.00 deg from the car's heading  H positive turns LEFT
    H = -55       ->  -55.00 deg from the car's heading
    P = +10       ->  +10.00 deg from the car's attitude  P positive is nose UP
    P = -10       ->  -10.00 deg from the car's attitude

CARLA is x forward, y right, z up, with **yaw positive to the right**. So the conversion is an
**x/y swap** and a **sign flip on yaw**, neither of which is a rename:

    position = (carla_y, carla_x, carla_z)
    hpr      = (-carla_yaw, carla_pitch, carla_roll)

**Pitch passes through, and that was measured rather than reasoned** -- the two rows above.
CARLA quotes pitch nose-UP positive and panda3d's P agrees. The measurement has to be taken
against the **car's own attitude**, exactly as the heading rows are: a car under throttle sits
nose-up on its suspension, and read against the world the same probe returns 9.89 rather than
10.00. That 0.11 deg is the vehicle, not the frame. `rigs/av3.txt` needs it: four of its six
cameras are pitched 5-10 deg toward the road. **Roll is still refused.** Its sign is the
counter-intuitive one -- wing-sim measured that CARLA's roll does *not* flip against ISO where y
and yaw both do -- and no spec here carries a non-zero one, so guessing it would rotate a picture
by twice the roll with nothing to notice.

**The mirror does not reach the cameras.** `handedness.install` reflects lane geometry about the
x-axis; a camera is parented to the vehicle's own node and its offsets are in the vehicle's
frame, which the mirror never touches. `check_frame` on a mirrored road returns the same six rows.

**Not a YAML parser, deliberately.** The converter had no PyYAML on its MetaDrive interpreter and
wrote `_parse` for the one shape the spec has -- a list of flat mappings under `sensors:` -- and
raises on anything else. That is kept, and not because of a dependency: a camera silently dropped
from a rig is a hole in the model's input that looks like a blind spot in the map.
"""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

#: What a camera's `type:` may say. Only RGB is wired: a depth or semantic view is a different
#: MetaDrive class with a different channel count, and guessing which the caller meant would
#: put the wrong array under a name their model reads.
SUPPORTED_TYPES: tuple[str, ...] = ("rgb_camera",)

#: Keys `_parse` understands under `transform:`. Anything else in the file is an error rather
#: than a shrug -- a `tick_rate` of 0.05 would mean the caller expects 20 Hz and would silently
#: get 10.
TRANSFORM_KEYS: tuple[str, ...] = ("x", "y", "z", "pitch", "yaw", "roll")

#: How many image buffers one env may hold before panda3d stops being reliable.
#:
#: Past this, `env.reset` fails *intermittently* inside `graphicsEngine.renderFrame()` with
#: `AssertionError: _formats_by_animation.empty() at line 350 of panda/src/gobj/geomMunger.cxx`
#: or `MutexPosixImpl::~MutexPosixImpl(): Assertion 'result == 0' failed`, and the process then
#: aborts or segfaults. Measured by the converter over 5 runs at each size on `junction-1`,
#: counting the buffers the engine really holds (`env.engine.sensors` less the ray detectors):
#:
#:     7 RGB  5/5      10 RGB  3/5
#:     8 RGB  5/5      11 RGB  1/5
#:     9 RGB  5/5      12 RGB  1/5
#:
#: Mixing camera types costs more than the count suggests: one non-RGB camera beside seven RGB
#: is free, two give 1/5 at nine where nine RGB give 5/5. So this ceiling is honest for an
#: all-RGB rig, which is what `SUPPORTED_TYPES` allows. Not the GPU (1/5 on the RTX 4050, 2/5 on
#: the iGPU), not `multi_thread_render`, not panda3d's threading model, not `stm-max-views`.
#: The number does not appear anywhere in the pinned simulator; it was measured, and it ports
#: with the rig. The intermittency is why this is a refusal rather than a warning: a rig one
#: camera over the line looks like it works, and then fails on a run somebody is relying on.
MAX_IMAGE_BUFFERS = 9

#: The line in MetaDrive that deletes the cameras of a headless env, named in the refusal.
CAMERA_FILTER = "base_env.py:343"


class RigError(ValueError):
    """The spec could not be turned into a rig, or the rig cannot go on this env. Always names
    the camera and the reason."""


class Camera:
    """One camera, already converted into MetaDrive's frame."""

    def __init__(
        self,
        name: str,
        position: tuple[float, float, float],
        hpr: tuple[float, float, float],
        width: int,
        height: int,
        fov: float,
        carla_yaw: float,
    ) -> None:
        self.name = name
        self.position = position  # (x right, y forward, z up), metres, ego frame
        self.hpr = hpr  # (heading, pitch, roll), degrees, + heading is LEFT
        self.width = width
        self.height = height
        self.fov = fov  # horizontal; the vertical angle follows the aspect ratio
        self.carla_yaw = carla_yaw

    @property
    def aim(self) -> str:
        """Where this camera actually points, in words, under the CARLA reading.

        The converter's older spec disagreed with itself about the sign of `yaw` -- its front
        pair read `+` as right and its back pair read `+` as left -- so exactly two of its four
        side cameras were named backwards whichever convention was chosen. Printing the
        resolved aim on every run is what keeps that visible instead of baked in; `rigs/av3.txt`
        was generated so that its names and this column agree.
        """
        turn = ((self.carla_yaw + 180.0) % 360.0) - 180.0
        if abs(turn) < 1.0:
            return "straight ahead"
        if abs(abs(turn) - 180.0) < 1.0:
            return "straight behind"
        side = "right" if turn > 0 else "left"
        quarter = "rear-" if abs(turn) > 90.0 else ("front-" if abs(turn) < 90.0 else "")
        return f"{abs(turn):.0f} deg to the {side}, i.e. {quarter}{side}"

    @property
    def megabytes(self) -> float:
        return self.width * self.height * 3 / 1e6

    def __repr__(self) -> str:
        return f"Camera({self.name}, {self.width}x{self.height}, fov {self.fov})"


class CameraRig:
    """The cameras of one spec, registerable on an env and readable each step."""

    def __init__(
        self, cameras: list[Camera], path: str | None = None, tick_rate_s: float | None = None
    ) -> None:
        if not cameras:
            raise RigError("the spec defines no cameras")
        self.cameras = cameras
        self.path = path
        #: What the spec itself declared, or `None` if it declared nothing. Kept so a caller that
        #: loaded the rig with the rate check deferred can still say what the spec asked for.
        self.tick_rate_s = tick_rate_s
        self._mounted: list[tuple[str, Any]] = []

    def __len__(self) -> int:
        return len(self.cameras)

    @property
    def names(self) -> list[str]:
        return [camera.name for camera in self.cameras]

    @property
    def megabytes(self) -> float:
        """MB of uint8 image produced per read, across the whole rig."""
        return sum(camera.megabytes for camera in self.cameras)

    def sensors(self) -> dict[str, tuple[Any, int, int]]:
        """The entries to add to the env's `sensors` config.

        The FOV is *not* set here: `sensors` carries only the constructor arguments, and
        `camera_fov` (`base_env.py:102`) is one global number for every camera. Per-camera FOV
        is applied in `mount`, through the lens.
        """
        from metadrive.component.sensors.rgb_camera import RGBCamera

        return {
            camera.name: (RGBCamera, camera.width, camera.height) for camera in self.cameras
        }

    def image_source(self) -> str:
        """The rig camera to point `vehicle_config["image_source"]` at.

        With `image_observation` on, `ImageObservation.observation_space` reads
        `config["sensors"][image_source]` (`image_obs.py:68`), and the key defaults to
        `rgb_camera` (`base_env.py:133`). Left alone, that registers a 320x240 camera nothing in
        the rig reads -- an extra render pass every step and one of the nine buffers. Naming a
        rig camera instead drops it.
        """
        return self.cameras[0].name

    def mount(self, env: Any) -> CameraRig:
        """Parent every camera to the ego and set its lens. Call after each `reset()`.

        `env.agent.origin` is measured to be the *same* NodePath across a reset, so a mount does
        survive one -- but re-mounting costs nothing and does not rest on that staying true for
        a scenario whose ego is a different vehicle class. `env.build_env` folds this into the
        per-row `prepare` step so no caller has to remember.
        """
        if not env.config["image_observation"]:
            raise RigError(
                f"the env was built with image_observation off, and MetaDrive ({CAMERA_FILTER}) "
                "deletes every camera from a headless env's sensors when it is; the rig's "
                "cameras do not exist on this env. Build it with `env.build_env(rig=...)`."
            )
        held = image_buffers(env)
        if held > MAX_IMAGE_BUFFERS:
            raise RigError(
                f"this env holds {held} image buffers and panda3d is reliable to "
                f"{MAX_IMAGE_BUFFERS}; a stray camera is registered beside the rig"
            )
        self._mounted = []
        for camera in self.cameras:
            sensor = env.engine.get_sensor(camera.name)
            sensor.lens.setFov(camera.fov)
            sensor.track(env.agent.origin, camera.position, camera.hpr)
            self._mounted.append((camera.name, sensor))
        return self

    def read(self, to_float: bool = False) -> dict[str, Any]:
        """One frame from every camera: `{name: (H, W, 3)}`, uint8 unless `to_float`.

        `perceive` is called with no parent node, so it reads the buffer the frame pass has
        already filled rather than re-aiming and re-rendering -- which is the whole reason the
        cameras are mounted. `numpy.asarray` would refuse a CuPy array under `image_on_cuda`;
        nothing here sets that key, and Step 7 is where a frame that stays on the GPU is read.
        """
        import numpy

        if not self._mounted:
            raise RigError("read() before mount(env): the cameras are on nothing")
        return {
            name: numpy.asarray(sensor.perceive(to_float)) for name, sensor in self._mounted
        }

    def describe(self) -> list[str]:
        """The resolved rig, in lines, for a report or a log."""
        lines = [
            f"{len(self.cameras)} camera(s) from {self.path or '<spec>'}",
            "  CARLA spec (x fwd, y right, z up, +yaw right, +pitch nose-up) -> MetaDrive "
            "(x right, y fwd, z up, +heading LEFT)",
        ]
        for camera in self.cameras:
            x, y, z = camera.position
            size = f"{camera.width}x{camera.height}"
            lines.append(
                f"  {camera.name:<16} {size:>9}  fov {camera.fov:>3.0f}  "
                f"mount x{x:+.2f} y{y:+.2f} z{z:+.2f}  "
                f"H{camera.hpr[0]:+7.1f} P{camera.hpr[1]:+5.1f}  aims {camera.aim}"
            )
        if self.tick_rate_s:
            lines.append(
                f"  declares tick_rate {self.tick_rate_s:g} s "
                f"({1.0 / self.tick_rate_s:g} Hz) - the interval it must be READ at, "
                "which is the decision stride over the step rate"
            )
        lines.append(f"  {self.megabytes:.2f} MB of uint8 image per read")
        return lines


def image_buffers(env: Any) -> int:
    """How many image buffers the env's engine really holds. What `MAX_IMAGE_BUFFERS` caps."""
    from metadrive.engine.core.image_buffer import ImageBuffer

    return sum(1 for sensor in env.engine.sensors.values() if isinstance(sensor, ImageBuffer))


def _scalar(text: str, camera: str, key: str) -> float:
    """A YAML scalar. Numbers only -- every value this spec carries is one."""
    text = text.strip()
    try:
        return float(text)
    except ValueError:
        raise RigError(f"{camera}: {key} is {text!r}, which is not a number") from None


def _parse(
    text: str, path: str | None = None, read_interval_s: float | None = None
) -> CameraRig:
    """A list of flat mappings under `sensors:`, and nothing else.

    `read_interval_s` is how long passes between two reads of these cameras -- the decision
    stride over the step rate -- and a declared `tick_rate` that disagrees with it is refused:
    nothing here resamples, and a silently wrong frame rate is the fault this check exists for.
    `None` skips the check, for a caller that only describes the file or that says so.
    """
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    section = None
    seen_root = False
    tick_rate_s = None

    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()

        if indent == 0:
            if stripped != "sensors:":
                raise RigError(
                    f"line {number}: expected `sensors:` at the top level, found {stripped!r}"
                )
            seen_root = True
            continue
        if not seen_root:
            raise RigError(f"line {number}: content before `sensors:`")

        if stripped.startswith("- "):
            current = {"transform": {}}
            entries.append(current)
            section = None
            stripped = stripped[2:].strip()
            indent += 2
        if current is None:
            raise RigError(f"line {number}: {stripped!r} is not inside a `- ` list item")

        key, separator, value = stripped.partition(":")
        if not separator:
            raise RigError(f"line {number}: {stripped!r} is not `key: value`")
        key = key.strip()
        value = value.strip()

        if key == "transform":
            if value:
                raise RigError(f"line {number}: inline `transform:` is not supported")
            section = "transform"
            continue
        # A transform's keys are indented under it; anything at the item's own indent ends it.
        if section == "transform" and key in TRANSFORM_KEYS:
            current["transform"][key] = value
        else:
            section = None
            current[key] = value

    if not seen_root:
        raise RigError("no `sensors:` key - this does not look like a camera spec")

    cameras: list[Camera] = []
    for index, entry in enumerate(entries):
        name = entry.get("name") or f"camera {index}"
        kind = entry.get("type")
        if kind not in SUPPORTED_TYPES:
            raise RigError(
                f"{name}: type is {kind!r}; only {' / '.join(SUPPORTED_TYPES)} is wired. A depth "
                "or semantic view is a different MetaDrive class with a different channel count."
            )
        for required in ("width", "height", "fov"):
            if required not in entry:
                raise RigError(f"{name}: no {required}")
        transform = entry["transform"]
        missing = [key for key in TRANSFORM_KEYS if key not in transform]
        if missing:
            raise RigError(f"{name}: transform has no {', '.join(missing)}")

        rate = entry.get("tick_rate")
        if rate is not None:
            declared = _scalar(rate, name, "tick_rate")
            if tick_rate_s is None:
                tick_rate_s = declared
            elif abs(declared - tick_rate_s) > 1e-9:
                raise RigError(
                    f"{name}: tick_rate {declared} s, but another camera in this spec "
                    f"declares {tick_rate_s} s. The rate has one source - the run - so a "
                    "spec cannot ask for two."
                )
            if read_interval_s is not None and abs(declared - read_interval_s) > 1e-9:
                raise RigError(
                    f"{name}: tick_rate {declared} s ({1.0 / declared:g} Hz), but these "
                    f"cameras are read every {read_interval_s:g} s "
                    f"({1.0 / read_interval_s:g} Hz). Nothing here resamples: the read "
                    "interval is the decision stride over the env's step rate, and the two "
                    "have to agree."
                )

        pitch = _scalar(transform["pitch"], name, "pitch")
        roll = _scalar(transform["roll"], name, "roll")
        if roll:
            raise RigError(
                f"{name}: roll {roll}. Only yaw and pitch are converted. Roll is the sign that "
                "cannot be reasoned out - wing-sim measured that it does NOT flip between ISO "
                "and CARLA where y and yaw both do - and no spec here carries a non-zero one, "
                "so it has never been checked against MetaDrive. A guessed sign rotates the "
                "picture by twice the roll and nothing raises."
            )

        forward = _scalar(transform["x"], name, "x")
        right = _scalar(transform["y"], name, "y")
        up = _scalar(transform["z"], name, "z")
        yaw = _scalar(transform["yaw"], name, "yaw")

        cameras.append(
            Camera(
                name=name,
                # The swap: CARLA's forward is MetaDrive's y, CARLA's right is its x.
                position=(right, forward, up),
                # The flip: CARLA's +yaw is right, MetaDrive's +heading is left.
                # `+ 0.0` so a yaw of 0.0 does not come back as -0.0.
                hpr=(-yaw + 0.0, pitch, roll),
                width=int(_scalar(entry["width"], name, "width")),
                height=int(_scalar(entry["height"], name, "height")),
                fov=_scalar(entry["fov"], name, "fov"),
                carla_yaw=yaw,
            )
        )

    names = [camera.name for camera in cameras]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise RigError(f"duplicate camera name(s): {', '.join(duplicates)}")
    if len(cameras) > MAX_IMAGE_BUFFERS:
        raise RigError(
            f"{len(cameras)} cameras, and panda3d is reliable to {MAX_IMAGE_BUFFERS} image "
            "buffers per env; past that `reset` fails intermittently and the process aborts"
        )
    return CameraRig(cameras, path=path, tick_rate_s=tick_rate_s)


def load_rig(path: str | os.PathLike[str], read_interval_s: float | None = None) -> CameraRig:
    """Read a rig spec from disk.

    `read_interval_s` is the interval the cameras will really be read at, and a spec declaring
    another is refused; `None` defers the check to the caller (see `_parse`).
    """
    path = Path(path)
    if not path.exists():
        raise RigError(f"no rig spec at {path}")
    return _parse(path.read_text(), path=str(path), read_interval_s=read_interval_s)


# --- the probe --------------------------------------------------------------------------------


class FrameCheck(BaseModel):
    """One row of `check_frame`: a fact the conversion rests on, and whether it still holds."""

    model_config = ConfigDict(extra="forbid")

    label: str
    ok: bool
    detail: str


def check_frame(env: Any) -> list[FrameCheck]:
    """Re-measure MetaDrive's vehicle frame, which the conversion above is built on.

    The facts the module rests on -- local +y is forward and local +x is right, a positive
    heading turns *left*, a positive pitch tilts *up* -- are properties of MetaDrive rather than
    of this repo, so they are checked here, where an engine exists. `env` is built and reset;
    the probe steps it once under throttle so the car sits the way a driven car does, parents a
    `NodePath` to the ego, gives it a local offset or angle, and reads it back in world
    coordinates against the car's own heading and attitude.
    """
    from panda3d.core import NodePath

    env.step([0.0, 0.3])
    agent = env.agent
    heading = agent.heading_theta
    render = env.engine.render

    def probe(position, hpr):
        node = NodePath("probe")
        node.reparentTo(agent.origin)
        node.setPos(*position)
        node.setHpr(*hpr)
        where = node.getPos(render)
        forward = node.getQuat(render).getForward()
        node.removeNode()
        return (
            where,
            math.degrees(math.atan2(forward[1], forward[0])),
            math.degrees(math.asin(max(-1.0, min(1.0, forward[2])))),
        )

    base, _, base_elevation = probe((0, 0, 0), (0, 0, 0))

    def offset(position):
        where, _, _ = probe(position, (0, 0, 0))
        dx, dy = where[0] - base[0], where[1] - base[1]
        return (
            dx * math.cos(heading) + dy * math.sin(heading),
            dx * math.sin(heading) - dy * math.cos(heading),
        )

    results: list[FrameCheck] = []
    ahead, right = offset((0, 1, 0))
    results.append(
        FrameCheck(
            label="local +y is 1 m forward",
            ok=abs(ahead - 1.0) < 1e-3 and abs(right) < 1e-3,
            detail=f"ahead {ahead:+.3f} m, right {right:+.3f} m",
        )
    )
    ahead, right = offset((1, 0, 0))
    results.append(
        FrameCheck(
            label="local +x is 1 m right",
            ok=abs(right - 1.0) < 1e-3 and abs(ahead) < 1e-3,
            detail=f"ahead {ahead:+.3f} m, right {right:+.3f} m",
        )
    )
    for degrees, label in ((55.0, "left"), (-55.0, "right")):
        _, facing, _ = probe((0, 0, 0), (degrees, 0, 0))
        relative = ((facing - math.degrees(heading) + 180.0) % 360.0) - 180.0
        results.append(
            FrameCheck(
                label=f"H={degrees:+.0f} turns {label}",
                ok=abs(relative - degrees) < 1e-2,
                detail=f"{relative:+.2f} deg from the car's heading",
            )
        )
    # The one `rigs/av3.txt` rests on: CARLA quotes pitch nose-UP positive and `_parse` passes
    # it through untouched, so panda3d's P has to mean the same thing or four of that rig's six
    # cameras look at the sky. Against the car's own attitude, not the world: a car under
    # throttle sits nose-up on its suspension.
    for degrees, label in ((10.0, "up"), (-10.0, "down")):
        _, _, elevation = probe((0, 0, 0), (0, degrees, 0))
        relative = elevation - base_elevation
        results.append(
            FrameCheck(
                label=f"P={degrees:+.0f} tilts {label}",
                ok=abs(relative - degrees) < 1e-2,
                detail=f"{relative:+.2f} deg from the car's own attitude "
                f"(which is {base_elevation:+.2f} deg)",
            )
        )
    return results


# --- the report `scenariobank rig` prints -----------------------------------------------------


class CameraReport(BaseModel):
    """One camera of a rig, resolved into MetaDrive's frame."""

    model_config = ConfigDict(extra="forbid")

    name: str
    width: int
    height: int
    fov: float
    #: Metres in the ego frame: x right, y forward, z up.
    position: tuple[float, float, float]
    #: Degrees: heading (positive is left), pitch (positive is up), roll (always 0).
    hpr: tuple[float, float, float]
    #: The spec's own yaw, CARLA's sign, so the flip above can be checked against it.
    carla_yaw: float
    aim: str


class RigReport(BaseModel):
    """A rig spec, read and converted, with the frame probe's rows when one was run."""

    model_config = ConfigDict(extra="forbid")

    path: str
    cameras: list[CameraReport]
    tick_rate_s: float | None
    megabytes: float
    #: `check_frame`'s rows, or `None` when the probe was not asked for.
    frame: list[FrameCheck] | None = None


def report_for(rig: CameraRig, frame: list[FrameCheck] | None = None) -> RigReport:
    return RigReport(
        path=rig.path or "<spec>",
        cameras=[
            CameraReport(
                name=camera.name,
                width=camera.width,
                height=camera.height,
                fov=camera.fov,
                position=camera.position,
                hpr=camera.hpr,
                carla_yaw=camera.carla_yaw,
                aim=camera.aim,
            )
            for camera in rig.cameras
        ],
        tick_rate_s=rig.tick_rate_s,
        megabytes=round(rig.megabytes, 3),
        frame=frame,
    )


def format_frame(rows: list[FrameCheck]) -> list[str]:
    """The probe's rows as text, one per line, `ok` or `FAIL` first."""
    lines = [f"  {'ok  ' if row.ok else 'FAIL'}  {row.label:<26} {row.detail}" for row in rows]
    if not all(row.ok for row in rows):
        lines.append(
            "  ! MetaDrive's vehicle frame is not what the conversion assumes. Every rig mount "
            "and aim is wrong until this passes."
        )
    return lines


__all__ = [
    "CAMERA_FILTER",
    "MAX_IMAGE_BUFFERS",
    "SUPPORTED_TYPES",
    "TRANSFORM_KEYS",
    "Camera",
    "CameraReport",
    "CameraRig",
    "FrameCheck",
    "RigError",
    "RigReport",
    "check_frame",
    "format_frame",
    "image_buffers",
    "load_rig",
    "report_for",
]
