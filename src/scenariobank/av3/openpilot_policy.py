"""Drive MetaDrive's ego from the openpilot bridge: the wire, the frame and both negations.

    bash scripts/bridge.sh start                          # the zapeta bridge, on TCP 5558
    driver = OpenpilotDriver("127.0.0.1", 5558, target_speed_mps=10.0)
    driver.episode(max_steering_deg=40.0, wheelbase_m=2.47, n_waypoints=20)
    steering, throttle_brake = driver.act(speed, yaw_rate, waypoints=..., modelv2=...)

Ported from the converter's `tools/openpilot_policy.py` (Phase 4 Step 7). That file sat
behind an HTTP policy server because MetaDrive and the converter ran different interpreters;
here it is a plain module the policies in `av3/policy.py` call directly, and the only process
boundary left is the bridge itself, which stays Python 3.8 in its own container.

**What the bridge is.** A controller, not a driver. `bridge/zapeta/server.py` fronts
openpilot's real `plannerd` + `controlsd`; per tick it takes a *predicted path* and three ego
scalars and returns pedals. It never sees an image. So it needs a trajectory handed to it:
the AV3 model's twenty waypoints (`AV3Policy`), or -- with no model at all -- the bank's own
route resampled at the car's current speed (`BridgePolicy`), which is wing-sim's
`route_gt.py` and a **controller** test by construction.

**Three things that make the two ends fit, each read off the bridge rather than assumed:**

* **`carla_steer_curvature_gain: 0.0` selects a geometric branch whose output is already
  MetaDrive's.** `server.py:788` computes `-road_wheel_deg / max_steer_angle`, and
  `action[0] x max_steering` *is* the road-wheel angle in degrees (`base_vehicle.py:478`).
  Pass MetaDrive's own `max_steering` -- 40 for the default vehicle -- as `max_steer_angle`
  and no conversion is left over. The default path instead inverts an empirical CARLA
  curvature gain measured on Town10HD, which would mean nothing here.
* **Both ends negate, because MetaDrive is left-positive and CARLA is right-positive.** The
  bridge negates the column angle at ingress (`server.py:616`) and emits a right-positive
  steer, so the action is `-steer` and the waypoints' `y` is `-left`.
* **The waypoints need no model.** wing-sim ships `route_gt.py` for exactly this -- route in,
  four points at t = 0.5/1.0/1.5/2.0 s using the car's *current* speed -- and this rebuilds it
  against the route line `av3/policy.py` builds from the bank.

**Four things that bite, all measured or read rather than guessed:**

* **`target_speed` defaults to 0**, which is a stop. `server.py:614` is
  `float(msg.get("target_speed", 0.0))`, so an omitted target is not "no opinion", it is
  "stand still". It has to be sent every tick.
* **`steer_ratio` in `init` is stored and never used.** The bridge divides by
  `self.CP.steerRatio` -- the fork's own car params -- on both ingress (`:646`) and egress
  (`:788`). The two cancel when ours matches, so a mismatch does not change the output scale;
  it mis-reports the current wheel angle to the rate limiter and the lag compensation.
  `DEFAULT_STEER_RATIO` is 12.0 because that is what wing-sim's own config sends.
* **The bridge is written for 20 Hz.** `_DT_MDL = 0.05` sets its lag compensation, its
  curvature-rate limit and its per-tick steer window, and its tick is the interval between
  two `act()` calls -- so **`--step-hz 100 --decision-hz 20` is the answer**: the same 0.05 s
  control interval with ten times the physics under it. A drive at any other decision rate
  mis-scales the three limits above, and `rate_note` says so.
* **`accel_map.py` is CARLA pedal calibration**, not physics: two 8x11 tables from a
  "Town10HD calibration sweep on Tesla M3 @ 20 Hz sync", whose zero crossing is the CARLA
  Tesla's own -1.582 m/s^2 of drag. MetaDrive's car coasts at -0.364, so **every request to
  slow down more gently than that comes back as throttle** -- 137 of 201 on the converter's
  `junction-1`, and the car ran away and left the road. `LONGITUDINAL_MODES` below; the
  converter's third mode, a pedal map measured on the car by its `pedal_sweep`, is not
  ported because no such map has been measured on this bank's car. Steering is unaffected,
  because that path is geometric.

`StubBridge` speaks the same protocol with a pure-pursuit law and needs no fork, no Docker
and no GPU. It is what proves the frame, the signs and the round trip before the real bridge
is available to blame, and what the tests and `BridgePolicy`'s live test drive against.

Nothing here imports MetaDrive: an agent reaches this module as three numbers.
"""

from __future__ import annotations

import contextlib
import json
import math
import socket
import struct
import threading
from typing import Any

# ---------------------------------------------------------------------------------------
# Framing. Copied from `openpilot/bridge/bridge_protocol.py` rather than imported, for the
# reason wing-sim's own docs give for duplicating it on their two sides: the ends run
# different interpreters, and there is no import that would catch the drift. 4-byte
# big-endian length, then that many bytes of UTF-8 JSON. Strictly synchronous.
# `tests/unit/test_openpilot_policy.py` round-trips it so drift shows up as a test failure.
# ---------------------------------------------------------------------------------------

HEADER_FMT = "!I"
HEADER_SIZE = struct.calcsize(HEADER_FMT)

#: Where `scripts/bridge.sh start` puts the bridge, and what `AV3_BRIDGE` overrides.
DEFAULT_BRIDGE_HOST = "127.0.0.1"
DEFAULT_BRIDGE_PORT = 5558

#: What one control tick is to the bridge: its `_DT_MDL`, and the rate its per-tick limits are
#: counted at. Reported by `OpenpilotDriver.rate_note` when the drive decides at anything else.
BRIDGE_DT_S = 0.05

#: `route_gt.py`'s own grid and floor. 4 is in the bridge's prebuilt acados menu
#: (`AV3_MPC_MENU="4 16 20 32"`), so the lateral MPC for it starts instantly.
WAYPOINT_OFFSETS_S = (0.5, 1.0, 1.5, 2.0)
MIN_WAYPOINT_SPEED_MPS = 3.0

#: See the module docstring: the bridge divides by its own `CP.steerRatio` whatever this says.
DEFAULT_STEER_RATIO = 12.0

#: The Tesla envelope the bridge itself plans within (`bridge_constants.py`), used by the
#: `accel` longitudinal mode below to turn an acceleration into MetaDrive's one signed number.
TESLA_ACCEL_MAX_MPS2 = 2.0
TESLA_ACCEL_MIN_MPS2 = -3.48

#: `pedal` is what the bridge emits and what a CARLA consumer gets, so it stays reproducible;
#: `accel` ignores the pedals and normalises `accel_cmd` by the envelope above -- sign-correct
#: and unit-consistent on this simulator, which the pedal map is not. `accel` is the default.
LONGITUDINAL_MODES = ("accel", "pedal")
DEFAULT_LONGITUDINAL = "accel"

#: The converter's default target: 36 km/h, under a 50 posted limit. The model's waypoints
#: carry the speed intent; this is the bridge's cruise ceiling and must be sent every tick.
DEFAULT_TARGET_SPEED_MPS = 10.0


class BridgeError(RuntimeError):
    """The bridge could not be reached, or answered with something undrivable."""


def send_msg(sock: socket.socket, data: dict[str, Any]) -> None:
    payload = json.dumps(data).encode("utf-8")
    sock.sendall(struct.pack(HEADER_FMT, len(payload)) + payload)


def recv_msg(sock: socket.socket) -> dict[str, Any]:
    header = b""
    while len(header) < HEADER_SIZE:
        chunk = sock.recv(HEADER_SIZE - len(header))
        if not chunk:
            raise ConnectionError("connection closed")
        header += chunk
    length = struct.unpack(HEADER_FMT, header)[0]
    payload = b""
    while len(payload) < length:
        chunk = sock.recv(length - len(payload))
        if not chunk:
            raise ConnectionError("connection closed")
        payload += chunk
    return json.loads(payload)


# ---------------------------------------------------------------------------------------
# The route, in the car's own frame
# ---------------------------------------------------------------------------------------

#: The converter's `route` sensor: this many points, this far apart, index 0 at the car's own
#: projection onto the route. What `waypoints_from_route` consumes and what the probe compares
#: the model's navigation block against.
ROUTE_POINTS = 25
ROUTE_SPACING_M = 2.0


def route_points(
    agent: Any, trajectory: Any, points: int = ROUTE_POINTS, spacing_m: float = ROUTE_SPACING_M
) -> dict[str, Any]:
    """The route ahead in MetaDrive's ego frame: `(ahead, left)` in metres, unnormalised.

    The converter's `policy_client.SensorPack` `route` sensor, off the same kind of line the
    model reads (`av3_model.navigation`): index 0 is the car's own projection onto the route,
    then `spacing_m` steps of arc length, clamped at the end of the route. Sent in metres and
    unclipped, because a controller cannot undo a normalised, 30 m window. The frame is
    MetaDrive's own -- x ahead, y to the **left** -- and the dict says so.
    """
    if trajectory is None:
        raise BridgeError("the bridge needs a route to follow, and this drive has none")
    longitudinal, lateral = trajectory.local_coordinates(agent.position)
    heading = float(agent.heading_theta)
    cos_heading, sin_heading = math.cos(heading), math.sin(heading)
    here = agent.position
    length = float(trajectory.length)
    ahead_left = []
    for index in range(points):
        along = min(float(longitudinal) + index * spacing_m, length)
        world = trajectory.position(along, 0.0)
        east = float(world[0]) - float(here[0])
        north = float(world[1]) - float(here[1])
        ahead_left.append(
            [east * cos_heading + north * sin_heading, -east * sin_heading + north * cos_heading]
        )
    return {
        "frame": "ego_x_ahead_y_left",
        "points_m": ahead_left,
        "spacing_m": float(spacing_m),
        "longitudinal_m": float(longitudinal),
        "lateral_m": float(lateral),
        "remaining_m": length - float(longitudinal),
    }


def sample_route(points: list[list[float]], spacing_m: float, distance_m: float) -> list[float]:
    """The route point `distance_m` along the path, interpolated between the samples.

    `points` is `route_points()["points_m"]`: (ahead, left) in metres at a fixed arc spacing.
    Interpolated rather than rounded to the nearest index as `route_gt.py` does, which costs
    nothing and is exact at the offsets rather than within a metre of them.
    """
    if not points:
        raise BridgeError("the route has no points")
    index = max(0.0, float(distance_m) / float(spacing_m))
    low = int(index)
    if low >= len(points) - 1:
        # Past the end of the route. The last point is the destination, and holding there is
        # right: the car is meant to stop, not to be steered at something beyond the map.
        return list(points[-1])
    fraction = index - low
    ahead = points[low][0] + fraction * (points[low + 1][0] - points[low][0])
    left = points[low][1] + fraction * (points[low + 1][1] - points[low][1])
    return [ahead, left]


def waypoints_from_route(
    route: dict[str, Any], speed_mps: float, offsets: tuple[float, ...] = WAYPOINT_OFFSETS_S
) -> list[list[float]]:
    """`[[x_forward, y_right, t], ...]` in the bridge's frame, from `route_points()`.

    Constant-speed, the reasoning `route_gt.py` writes down: placing the points at the car's
    *own* projected positions keeps them reachable. Placing them where a plan says the car
    will be sends the MPC at a target it cannot make whenever the two speeds differ.

    The `y` flip is the whole of the frame conversion: MetaDrive's ego frame is x ahead and
    y to the **left**, the bridge's is CARLA's x ahead and y to the **right**.
    """
    points = route["points_m"]
    spacing = float(route.get("spacing_m", ROUTE_SPACING_M))
    speed = max(MIN_WAYPOINT_SPEED_MPS, float(speed_mps))
    result = []
    for offset in offsets:
        ahead, left = sample_route(points, spacing, speed * offset)
        result.append([float(ahead), -float(left), float(offset)])
    return result


# ---------------------------------------------------------------------------------------
# The ego state and the reply
# ---------------------------------------------------------------------------------------


def read_ego(agent: Any) -> tuple[float, float]:
    """`(speed m/s, yaw rate rad/s)` off a MetaDrive vehicle: the converter's `imu` sensor.

    The yaw rate is the body's own angular velocity about z, CCW-positive -- openpilot's
    convention as well as MetaDrive's, so it crosses unnegated.
    """
    return float(agent.speed), float(agent.body.getAngularVelocity()[2])


def bridge_ego(
    speed_mps: float,
    yaw_rate_radps: float,
    steering: float,
    max_steering_deg: float,
    steer_ratio: float = DEFAULT_STEER_RATIO,
) -> dict[str, float]:
    """The three scalars the bridge's `step` message wants.

    `steering` is the **last action this driver returned**, not a measurement: in a
    synchronous simulator a commanded steer is applied within the same tick, so the command
    is the wheel state and there are no actuator dynamics to model. wing-sim says the same
    thing about CARLA, and reads `get_control().steer` for it.

    Negated on the way out because the bridge takes a right-positive column angle and negates
    it back at ingress (`server.py:616`).
    """
    column_deg = float(steering) * float(max_steering_deg) * float(steer_ratio)
    return {
        "v_ego": float(speed_mps),
        "yaw_rate": float(yaw_rate_radps),
        "steering_angle_deg": -column_deg,
    }


def to_metadrive_action(
    reply: Any, longitudinal: str = DEFAULT_LONGITUDINAL
) -> list[float]:
    """`{"steer", "throttle", "brake", "accel_cmd"}` -> `[steering, throttle_brake]` in [-1, 1].

    The steer is negated because the bridge emits CARLA's right-positive normalised value,
    and it needs nothing else: `carla_steer_curvature_gain: 0.0` selects a geometric branch
    whose output is already `road_wheel_deg / max_steer_angle`. Measured by the converter
    against the real bridge: a 124.95 deg column angle came back as steer 0.2603, which is
    124.95 / 12 / 40 exactly.

    **The longitudinal half is not a calibration either way, and `longitudinal` is which of
    the two to take.** MetaDrive wants one signed number, braking below zero
    (`base_vehicle.py:494`), which is why an action in [0, 1] cannot brake at all.

    * `pedal` is what the bridge emits: `throttle - brake`, from CARLA's pedal map, whose
      zero crossing is not at zero -- `accel_cmd` -1.55 gives throttle 0.204, so every gentle
      slow-down comes back as a fifth of full throttle, and the converter's car ran away.
    * `accel` ignores the two pedals and normalises `accel_cmd`, which is in m/s^2 and owes
      nothing to any vehicle, by the Tesla envelope the bridge itself plans within.
      MetaDrive's `action[1]` is engine and brake force, not acceleration, so the magnitude
      is only roughly right; what it is is *sign*-correct and unit-consistent. The default.

    Refused here rather than in the policy, so a bad number names the bridge that produced it.
    """
    if longitudinal not in LONGITUDINAL_MODES:
        raise BridgeError(
            f"unknown longitudinal mode {longitudinal!r}; one of {', '.join(LONGITUDINAL_MODES)}"
        )
    if not isinstance(reply, dict):
        raise BridgeError(f"the bridge replied with {type(reply).__name__}, not an object")
    if reply.get("type") == "error" or "steer" not in reply:
        raise BridgeError(f"the bridge did not reply with a control: {str(reply)[:200]}")
    steer = -float(reply["steer"])
    if longitudinal == "accel":
        if "accel_cmd" not in reply:
            raise BridgeError(
                "longitudinal mode `accel` needs `accel_cmd`, which this reply does not carry; "
                "use `pedal` with a bridge that answers in pedals only"
            )
        accel = float(reply["accel_cmd"])
        # Before the clip, not after: `min(1.0, nan)` is 1.0 in Python, so clipping first
        # would turn a NaN into full throttle rather than into the refusal below.
        if not math.isfinite(accel):
            raise BridgeError(f"the bridge returned accel_cmd = {accel}")
        envelope = TESLA_ACCEL_MAX_MPS2 if accel >= 0.0 else -TESLA_ACCEL_MIN_MPS2
        throttle_brake = max(-1.0, min(1.0, accel / envelope))
    else:
        throttle_brake = float(reply.get("throttle", 0.0)) - float(reply.get("brake", 0.0))
    for name, value in (("steering", steer), ("throttle_brake", throttle_brake)):
        if not math.isfinite(value):
            raise BridgeError(
                f"the bridge returned {name} = {value}, which MetaDrive does not clip -- it "
                "reaches setSteeringValue as it stands"
            )
        if not -1.0 <= value <= 1.0:
            raise BridgeError(
                f"the bridge returned {name} = {value:.4f}, outside [-1, 1]. Its `steer` is "
                "meant to be normalised already; check `max_steer_angle` in the init message."
            )
    return [steer, throttle_brake]


# ---------------------------------------------------------------------------------------
# The connection
# ---------------------------------------------------------------------------------------


class BridgeConnection:
    """One TCP connection to the bridge: `init` once, then a `step` per tick.

    A connection is what the bridge scopes its per-episode state to
    (`_reset_per_connection_state`), so a new scenario gets a new connection rather than a
    reset message -- there is no reset message.
    """

    def __init__(
        self,
        host: str = DEFAULT_BRIDGE_HOST,
        port: int = DEFAULT_BRIDGE_PORT,
        connect_timeout: float = 60.0,
        step_timeout: float = 2.0,
    ) -> None:
        self.host = host
        self.port = int(port)
        self._connect_timeout = float(connect_timeout)
        self._step_timeout = float(step_timeout)
        self._sock: socket.socket | None = None
        self.steps = 0

    def connect(self, **init_fields: Any) -> dict[str, Any]:
        """Connect and send `init`, returning the message once the bridge answers `ready`."""
        self.close()
        try:
            sock = socket.create_connection((self.host, self.port), self._connect_timeout)
        except OSError as error:
            raise BridgeError(
                f"cannot reach the bridge at {self.host}:{self.port} -- "
                f"{type(error).__name__}: {error}. `bash scripts/bridge.sh status` says "
                "whether it is up."
            ) from error
        # A round trip per control tick: no Nagle.
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(self._connect_timeout)

        message: dict[str, Any] = {
            "type": "init",
            "steer_ratio": DEFAULT_STEER_RATIO,
            "max_steer_angle": 40.0,
            # The bridge builds its lateral MPC from this, once, at connect -- so it cannot
            # follow the first `step`. `OpenpilotDriver.episode` overrides it with the model's
            # real count; `route_gt.py`'s four is the default.
            "n_waypoints": len(WAYPOINT_OFFSETS_S),
            "zapeta_longitudinal_mode": "blended_except_creep",
            "telemetry_mpc_outputs": False,
            # 0 is what selects the geometric steer branch. The whole fit turns on it.
            "carla_steer_curvature_gain": 0.0,
            "carla_steer_understeer_coef": 0.0,
        }
        message.update(init_fields)
        try:
            send_msg(sock, message)
            reply = recv_msg(sock)
        except (OSError, ConnectionError) as error:
            sock.close()
            raise BridgeError(
                f"the bridge at {self.host}:{self.port} closed during init -- "
                f"{type(error).__name__}: {error}"
            ) from error
        if reply.get("type") != "ready":
            sock.close()
            raise BridgeError(f"the bridge did not become ready: {str(reply)[:200]}")
        sock.settimeout(self._step_timeout)
        self._sock = sock
        self.steps = 0
        return message

    def step(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._sock is None:
            raise BridgeError("step() before connect()")
        try:
            send_msg(self._sock, payload)
            reply = recv_msg(self._sock)
        except (OSError, ConnectionError) as error:
            raise BridgeError(
                f"the bridge stopped answering at step {self.steps} -- "
                f"{type(error).__name__}: {error}"
            ) from error
        self.steps += 1
        return reply

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def close(self) -> None:
        if self._sock is None:
            return
        # Both guarded: a bridge that has already died must not turn teardown into an error.
        with contextlib.suppress(OSError):
            send_msg(self._sock, {"type": "shutdown"})
        with contextlib.suppress(OSError):
            self._sock.close()
        self._sock = None


# ---------------------------------------------------------------------------------------
# A stand-in bridge, so the path can be proven without the fork
# ---------------------------------------------------------------------------------------


class StubBridge:
    """A real socket speaking the real protocol, with a pure-pursuit law behind it.

    Not a mock: it binds, frames, inits and replies exactly as the bridge does, so it
    exercises the wire, the frame and both sign conventions. What it does **not** do is
    anything openpilot does -- no MPC, no lag compensation, no longitudinal state machine.
    It is here to answer "is the plumbing right" before there is a fork to blame, and a
    drive that stays on the road under it is that answer. It answers in pedals *and* in
    `accel_cmd`, so both longitudinal modes can be driven against it.
    """

    def __init__(self, host: str = DEFAULT_BRIDGE_HOST, port: int = 0) -> None:
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((host, port))
        self._server.listen(4)
        self.host, self.port = self._server.getsockname()
        self.inits: list[dict[str, Any]] = []
        self.steps = 0
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._running = True
        self._thread.start()

    def _serve(self) -> None:
        while self._running:
            try:
                connection, _ = self._server.accept()
            except OSError:
                return
            threading.Thread(target=self._session, args=(connection,), daemon=True).start()

    def _session(self, connection: socket.socket) -> None:
        connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            message = recv_msg(connection)
            if message.get("type") != "init":
                send_msg(connection, {"type": "error", "reason": "expected init"})
                return
            self.inits.append(dict(message))
            settings = {
                "max_steer_angle": float(message.get("max_steer_angle", 40.0)),
                "wheelbase_m": float(message.get("wheelbase_m", 2.5)),
            }
            send_msg(connection, {"type": "ready"})
            while self._running:
                message = recv_msg(connection)
                kind = message.get("type")
                if kind == "shutdown":
                    return
                if kind != "step":
                    send_msg(connection, {"type": "error", "reason": f"unknown type {kind}"})
                    continue
                self.steps += 1
                send_msg(connection, self.control(message, settings))
        except (OSError, ConnectionError, ValueError):
            return
        finally:
            with contextlib.suppress(OSError):
                connection.close()

    @staticmethod
    def control(message: dict[str, Any], settings: dict[str, float]) -> dict[str, Any]:
        """Pure pursuit on the waypoints, answered in the bridge's own conventions."""
        waypoints = message.get("waypoints") or []
        ego = message.get("ego") or {}
        speed = float(ego.get("v_ego", 0.0))
        target = float(message.get("target_speed", 0.0))
        if not waypoints:
            # What the bridge itself does with an empty list: a hard stop, built directly.
            return {
                "type": "control", "steer": 0.0, "throttle": 0.0, "brake": 1.0,
                "accel_cmd": TESLA_ACCEL_MIN_MPS2,
            }

        # A lookahead that grows with speed, floored so a stopped car still has one.
        lookahead = max(6.0, 0.6 * speed)
        chosen = waypoints[-1]
        for waypoint in waypoints:
            if math.hypot(waypoint[0], waypoint[1]) >= lookahead:
                chosen = waypoint
                break
        ahead, right = float(chosen[0]), float(chosen[1])
        chord = ahead * ahead + right * right
        # Left-positive curvature, which is openpilot's sign and MetaDrive's.
        curvature = 0.0 if chord < 1e-6 else -2.0 * right / chord
        road_wheel_deg = math.degrees(math.atan(settings["wheelbase_m"] * curvature))
        # And out in CARLA's right-positive normalised form, exactly as `server.py:788`.
        steer = max(-1.0, min(1.0, -road_wheel_deg / settings["max_steer_angle"]))

        accel = max(TESLA_ACCEL_MIN_MPS2, min(TESLA_ACCEL_MAX_MPS2, 0.6 * (target - speed)))
        throttle = max(0.0, min(1.0, accel / TESLA_ACCEL_MAX_MPS2))
        brake = max(0.0, min(1.0, accel / TESLA_ACCEL_MIN_MPS2))
        return {
            "type": "control",
            "steer": steer,
            "throttle": throttle,
            "brake": brake,
            "accel_cmd": accel,
            "stub": True,
            "lookahead_m": lookahead,
            "curvature": curvature,
        }

    def close(self) -> None:
        self._running = False
        with contextlib.suppress(OSError):
            self._server.close()


# ---------------------------------------------------------------------------------------
# The driver the policies call
# ---------------------------------------------------------------------------------------


def rate_note(step_seconds: float) -> str | None:
    """What to say when the interval between two `act()` calls is not the bridge's tick.

    Not a refusal: a drive at 10 Hz decisions is a drive. But the bridge's lag compensation,
    curvature-rate limit and per-tick steer window are counted per tick, so they are scaled,
    and that must be said rather than discovered.
    """
    if abs(float(step_seconds) - BRIDGE_DT_S) <= 1e-9:
        return None
    ratio = float(step_seconds) / BRIDGE_DT_S
    return (
        f"the drive decides every {float(step_seconds):.3f} s and the bridge is written for "
        f"{BRIDGE_DT_S:.2f} s (_DT_MDL). Its lag compensation and curvature-rate limit are "
        f"counted per tick, so they are scaled by {ratio:.1f}x here. --step-hz 100 "
        "--decision-hz 20 is what matches it."
    )


class OpenpilotDriver:
    """`episode` once a scenario, `act` once a decision, `close` at the end.

    Holds the two things neither end sends every tick: the car's steering geometry, which
    arrives once per episode, and the last action, which is what the bridge is told the
    wheel is at.
    """

    def __init__(
        self,
        host: str = DEFAULT_BRIDGE_HOST,
        port: int = DEFAULT_BRIDGE_PORT,
        target_speed_mps: float = DEFAULT_TARGET_SPEED_MPS,
        steer_ratio: float = DEFAULT_STEER_RATIO,
        offsets: tuple[float, ...] = WAYPOINT_OFFSETS_S,
        longitudinal: str = DEFAULT_LONGITUDINAL,
    ) -> None:
        self.bridge = BridgeConnection(host, port)
        self.target_speed_mps = float(target_speed_mps)
        self.steer_ratio = float(steer_ratio)
        self.offsets = tuple(offsets)
        if longitudinal not in LONGITUDINAL_MODES:
            raise BridgeError(
                f"unknown longitudinal mode {longitudinal!r}; one of "
                f"{', '.join(LONGITUDINAL_MODES)}"
            )
        self.longitudinal = longitudinal
        # How many waypoints the bridge is told to expect. `route_gt.py`'s four until a model
        # says otherwise: the AV3 checkpoint emits 20, which is in the prebuilt AV3_MPC_MENU
        # ("4 16 20 32"), so the lateral MPC for it starts instantly rather than generating
        # code on connect.
        self.n_waypoints = len(self.offsets)
        self.max_steering_deg = 40.0
        self.wheelbase_m = 2.5
        self.last_action = [0.0, 0.0]
        self.last_reply: dict[str, Any] = {}
        self.last_v_ego = 0.0
        self.calls = 0

    def episode(
        self, max_steering_deg: float, wheelbase_m: float, n_waypoints: int | None = None
    ) -> dict[str, Any]:
        """A new scenario: the car's geometry, then a fresh connection and a fresh init.

        `init` is sent once a connection and the bridge builds its lateral MPC from
        `n_waypoints`, so a model's count has to be known here and cannot wait for the
        first `act`.
        """
        self.max_steering_deg = float(max_steering_deg)
        self.wheelbase_m = float(wheelbase_m)
        self.n_waypoints = int(n_waypoints or len(self.offsets))
        self.last_action = [0.0, 0.0]
        self.last_reply = {}
        self.calls = 0
        return self.bridge.connect(
            n_waypoints=self.n_waypoints,
            max_steer_angle=self.max_steering_deg,
            steer_ratio=self.steer_ratio,
            # Ours, not the protocol's. The real bridge ignores what it does not know; the
            # stub needs a wheelbase to turn a curvature into a wheel angle.
            wheelbase_m=self.wheelbase_m,
        )

    def act(
        self,
        speed_mps: float,
        yaw_rate_radps: float,
        *,
        route: dict[str, Any] | None = None,
        waypoints: list[list[float]] | None = None,
        modelv2: list[list[float]] | None = None,
    ) -> list[float]:
        """One control tick. `waypoints` / `modelv2` are a model's; `route` is the fallback.

        **The fallback is not a nicety.** Without a model this is `route_gt.py` -- the route
        resampled at the car's own current speed -- which is a controller test by construction
        and carries no speed intent for the longitudinal planner to read. The converter
        measured what that costs: median `accel_cmd` -0.30 m/s^2 with 159 of 1559 calls
        positive. A model's rows are what fix that.

        **`waypoints` is sent even when `modelv2` is.** `server.py:_handle_step` reads it FIRST
        and returns a hard stop on an empty list, before it looks at `modelv2` at all.
        """
        if not self.bridge.connected:
            raise BridgeError("act() before episode(): no connection to the bridge")
        if not waypoints and route is None:
            raise BridgeError("the bridge needs a model's waypoints or a route, and got neither")
        state = bridge_ego(
            speed_mps, yaw_rate_radps, self.last_action[0], self.max_steering_deg,
            self.steer_ratio,
        )
        payload: dict[str, Any] = {
            "type": "step",
            "waypoints": (
                waypoints if waypoints else waypoints_from_route(route, speed_mps, self.offsets)
            ),
            "ego": state,
            # Never omitted: `server.py:614` reads a missing target as 0.0, which is a stop.
            "target_speed": self.target_speed_mps,
            "creep_state": "idle",
        }
        if modelv2:
            # `from_predicted` rather than `derive`: the model already emits yaw, yaw rate,
            # velocity and acceleration per waypoint, and reconstructing them from positions
            # by a cubic fit throws that away. Its rows are used as-is, at their own times.
            payload["modelv2"] = modelv2
        self.last_v_ego = float(speed_mps)
        self.last_reply = self.bridge.step(payload)
        self.last_action = to_metadrive_action(self.last_reply, self.longitudinal)
        self.calls += 1
        return list(self.last_action)

    def close(self) -> None:
        self.bridge.close()


__all__ = [
    "BRIDGE_DT_S",
    "DEFAULT_BRIDGE_HOST",
    "DEFAULT_BRIDGE_PORT",
    "DEFAULT_LONGITUDINAL",
    "DEFAULT_STEER_RATIO",
    "DEFAULT_TARGET_SPEED_MPS",
    "HEADER_FMT",
    "HEADER_SIZE",
    "LONGITUDINAL_MODES",
    "MIN_WAYPOINT_SPEED_MPS",
    "ROUTE_POINTS",
    "ROUTE_SPACING_M",
    "TESLA_ACCEL_MAX_MPS2",
    "TESLA_ACCEL_MIN_MPS2",
    "WAYPOINT_OFFSETS_S",
    "BridgeConnection",
    "BridgeError",
    "OpenpilotDriver",
    "StubBridge",
    "bridge_ego",
    "rate_note",
    "read_ego",
    "recv_msg",
    "route_points",
    "sample_route",
    "send_msg",
    "to_metadrive_action",
    "waypoints_from_route",
]
