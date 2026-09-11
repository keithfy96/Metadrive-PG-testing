"""The two policies that drive through the openpilot bridge: with the AV3 model, and without.

    scenariobank run --bank banks/t-junction --policy scenariobank.av3:AV3Policy \\
        --camera-rig rigs/av3.txt --step-hz 100 --decision-hz 20 \\
        --model-config ../models/model_dev.yml --checkpoint ../models/step_440000_trt_direct_full.ep
    scenariobank run --bank banks/t-junction --policy scenariobank.av3:BridgePolicy \\
        --step-hz 100 --decision-hz 20

`AV3Policy` is the thing under evaluation: six cameras off the rig into the checkpoint, its
twenty waypoints into the bridge, the bridge's pedals onto the car. `BridgePolicy` is the same
path with the model taken out -- the bank's own route resampled at the car's current speed,
wing-sim's `route_gt.py` -- so the bridge, the frame and both negations can be driven and
scored on a machine with no GPU, and a model run that goes wrong has a controller run to be
compared against. Neither is a reference policy in `policies.py`'s sense: the floor and the
ceiling stay `ConstantPolicy` and `ExpertPolicy`.

**What a policy is told, and when.** `policies.load_policy` instantiates `Name(checkpoint_path=)`
and nothing else, because the thing under evaluation is not ours to make inherit from anything.
Two optional hooks carry the rest, both read by `runner.run_bank` and neither by the loop:
`setup(run)` once, before any env is built, with a `runner.RunSetup` -- the step rate, the
stride, the rig the run mounted, whether its rate check was waived, the model config -- and
`bind(env)` per env, before its rows run. `setup` is where every refusal lives, so a run that
cannot work is refused before a 13 s offscreen window is opened; it returns the notes worth
printing. `close()` at the end of the batch drops the bridge connection and the engine.

**The route on a procedural road is built, not recorded.** A recording carries the ego's own
track as a `PointLane` (`navigation.reference_trajectory`), and the converter's model read
that. A bank's road has no tape: `route_for` walks the route `env.py` pinned -- the navigation's
checkpoints, one lane per road at the ego's spawn lane position -- samples each lane's centre
line and joins them into the same kind of `PointLane`, so `av3_model.navigation` and
`openpilot_policy.route_points` project the same object on both bank kinds. It is built once
per episode, at the first decision, because it exists only after `reset`.

**Three refusals, all before any env.** `AV3Policy` refuses a run with no `--camera-rig`, since
the model reads nothing else; a run with `--ignore-rig-rate`, since that switch exists for a
film and a model reading a 20 Hz rig at 10 Hz is the silently wrong frame rate the AV3 stack's
gotcha 5 is about; and a rig missing any camera `camera_order` names. Both policies say, rather
than refuse, when the decision interval is not the bridge's 0.05 s tick.

Configuration that a `Job` does not carry -- where the bridge is, the cruise target, the
longitudinal mode -- is read from the environment, the converter's own convention:
`AV3_BRIDGE` (`host:port`, default `127.0.0.1:5558`), `AV3_TARGET_SPEED_MPS` (10),
`AV3_LONGITUDINAL` (`accel`), and `MODEL_CONFIG` / `MODEL_CHECKPOINT` when the flags are not
given. Nothing here imports MetaDrive or torch at module scope.
"""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from scenariobank.av3.av3_model import (
    AV3Model,
    ModelError,
    checkpoint_path,
    load_config,
)
from scenariobank.av3.openpilot_policy import (
    DEFAULT_BRIDGE_HOST,
    DEFAULT_BRIDGE_PORT,
    DEFAULT_LONGITUDINAL,
    DEFAULT_TARGET_SPEED_MPS,
    BridgeError,
    OpenpilotDriver,
    rate_note,
    read_ego,
    route_points,
)
from scenariobank.policies import PolicyError

if TYPE_CHECKING:
    from scenariobank.runner import RunSetup

#: The environment variables the two policies read for what a `Job` does not carry.
BRIDGE_VARIABLE = "AV3_BRIDGE"
TARGET_SPEED_VARIABLE = "AV3_TARGET_SPEED_MPS"
LONGITUDINAL_VARIABLE = "AV3_LONGITUDINAL"

#: How finely a lane's centre line is sampled into the route: a point every metre, which on a
#: 30 m-radius arc is 0.004 m of chord error and on a straight is free.
ROUTE_SAMPLE_M = 1.0


def bridge_address(given: str | None = None) -> tuple[str, int]:
    """`host:port` from the argument, else `AV3_BRIDGE`, else the bridge's own default."""
    text = given if given is not None else os.environ.get(BRIDGE_VARIABLE)
    if not text:
        return DEFAULT_BRIDGE_HOST, DEFAULT_BRIDGE_PORT
    host, colon, port = text.rpartition(":")
    if not colon or not port.isdigit():
        raise PolicyError(f"{BRIDGE_VARIABLE} must be host:port, not {text!r}")
    return host or DEFAULT_BRIDGE_HOST, int(port)


def route_for(env: Any) -> Any:
    """The line the ego is meant to follow, as a `PointLane`, on either kind of env.

    A recording's navigation carries its own (`reference_trajectory`, the ego's recorded
    track). A procedural navigation carries `checkpoints` -- the node sequence `env.py`'s
    `prepare` pinned with `set_route` -- and one lane per road is chosen at the ego's spawn
    lane position, clamped where a road has fewer lanes. Each lane's centre line is sampled
    every `ROUTE_SAMPLE_M` and the samples joined, so the result projects with MetaDrive's own
    `local_coordinates` exactly as a recorded track does. Needs the simulator, and an env that
    has been reset.
    """
    navigation = env.agent.navigation
    recorded = getattr(navigation, "reference_trajectory", None)
    if recorded is not None:
        return recorded
    checkpoints = list(getattr(navigation, "checkpoints", None) or [])
    if len(checkpoints) < 2:
        raise PolicyError("the ego's navigation has no route yet; build the route after reset")
    from metadrive.component.lane.point_lane import PointLane

    network = env.engine.current_map.road_network
    position = int(env.agent.lane_index[2])
    points: list[list[float]] = []
    width = None
    for start, end in zip(checkpoints[:-1], checkpoints[1:], strict=True):
        lanes = network.graph[start][end]
        lane = lanes[min(position, len(lanes) - 1)]
        width = float(lane.width) if width is None else width
        samples = max(2, math.ceil(float(lane.length) / ROUTE_SAMPLE_M))
        for step in range(samples + 1):
            world = lane.position(float(lane.length) * step / samples, 0.0)
            point = [float(world[0]), float(world[1])]
            if points and math.hypot(point[0] - points[-1][0], point[1] - points[-1][1]) < 1e-3:
                continue
            points.append(point)
    return PointLane(points, width, need_lane_localization=False, auto_generate_polygon=False)


def vehicle_geometry(agent: Any) -> tuple[float, float]:
    """`(max_steering_deg, wheelbase_m)` off the ego: what the bridge's `init` is told."""
    max_steering = float(agent.max_steering)
    wheelbase = float(getattr(agent, "FRONT_WHEELBASE", 1.05234)) + float(
        getattr(agent, "REAR_WHEELBASE", 1.4166)
    )
    return max_steering, wheelbase


class BridgePolicy:
    """The bank's route through the openpilot bridge, no model: a controller test.

    What the AV3 path is with the checkpoint taken out, and the drive every AV3 drive is
    compared against when the two disagree. Needs the bridge (`scripts/bridge.sh start`) or
    the stub, and nothing else: no rig, no GPU.
    """

    def __init__(
        self,
        checkpoint_path: str | None = None,
        *,
        bridge: str | None = None,
        target_speed_mps: float | None = None,
        longitudinal: str | None = None,
    ) -> None:
        del checkpoint_path  # a controller has no weights
        host, port = bridge_address(bridge)
        target = (
            float(os.environ.get(TARGET_SPEED_VARIABLE, DEFAULT_TARGET_SPEED_MPS))
            if target_speed_mps is None
            else float(target_speed_mps)
        )
        mode = longitudinal or os.environ.get(LONGITUDINAL_VARIABLE, DEFAULT_LONGITUDINAL)
        try:
            self.driver = OpenpilotDriver(host, port, target_speed_mps=target, longitudinal=mode)
        except BridgeError as error:
            raise PolicyError(str(error)) from error
        self.env: Any = None
        self.route: Any = None
        self.decision_interval_s: float | None = None
        self.notes: list[str] = []

    def setup(self, run: RunSetup) -> list[str]:
        """Before any env: the decision interval, and the note when it is not the bridge's."""
        self.decision_interval_s = run.stride / run.step_hz
        self.notes = [note for note in (rate_note(self.decision_interval_s),) if note]
        return list(self.notes)

    def bind(self, env: Any) -> None:
        """A new env, before its reset: the previous episode's connection is dropped."""
        self.env = env
        self.route = None
        self.driver.close()

    def start_episode(self, env: Any) -> None:
        """After the reset: the route, the car's geometry, and a fresh connection."""
        self.route = route_for(env)
        max_steering, wheelbase = vehicle_geometry(env.agent)
        self.driver.episode(max_steering, wheelbase)

    def __call__(self, observation: Any) -> list[float]:
        del observation  # the bridge reads the route and the ego, never the RL vector
        if self.env is None:
            raise PolicyError("BridgePolicy is not bound to an env; the batch binds it first")
        agent = self.env.agent
        if self.route is None:
            self.start_episode(self.env)
        speed, yaw_rate = read_ego(agent)
        return self.driver.act(speed, yaw_rate, route=route_points(agent, self.route))

    def close(self) -> None:
        self.driver.close()


class AV3Policy(BridgePolicy):
    """The AV3 checkpoint on the six-camera rig, through the openpilot bridge. The submission.

    Per decision: read the rig, feed the model's ring and the ego state, run the forward pass
    against the route's navigation block, send the twenty waypoints and their modelv2 rows
    to the bridge, and return its pedals negated into MetaDrive's action. Every conversion
    on that path is `av3_model.py`'s, and `scenariobank av3` measures each before a run.
    """

    def __init__(
        self,
        checkpoint_path: str | None = None,
        *,
        model_config: str | None = None,
        bridge: str | None = None,
        target_speed_mps: float | None = None,
        longitudinal: str | None = None,
    ) -> None:
        super().__init__(
            bridge=bridge, target_speed_mps=target_speed_mps, longitudinal=longitudinal
        )
        self.checkpoint_given = checkpoint_path
        self.model_config_given = model_config
        self.config: Any = None
        self.checkpoint: str | None = None
        self.rig: Any = None
        self.model: AV3Model | None = None

    def setup(self, run: RunSetup) -> list[str]:
        """Every refusal, before any env is built; then the notes worth printing."""
        notes = super().setup(run)
        if run.rig is None:
            raise PolicyError(
                "AV3Policy reads the camera rig and this run mounts none; pass "
                "--camera-rig rigs/av3.txt"
            )
        if run.ignore_rig_rate:
            raise PolicyError(
                "AV3Policy refuses --ignore-rig-rate: that switch exists for a film, which reads "
                "at the step rate whatever the spec says. A model reading a 20 Hz rig at 10 Hz "
                "is the silently wrong frame rate; step at the rig's own rate instead "
                "(--step-hz 100 --decision-hz 20 for rigs/av3.txt)."
            )
        try:
            self.config = load_config(
                run.model_config if run.model_config is not None else self.model_config_given
            )
            self.checkpoint = checkpoint_path(self.checkpoint_given)
        except ModelError as error:
            raise PolicyError(str(error)) from error
        if not Path(self.checkpoint).exists():
            raise PolicyError(f"no checkpoint at {self.checkpoint}")
        missing = [name for name in self.config.camera_order if name not in run.rig.names]
        if missing:
            raise PolicyError(
                f"the rig at {run.rig.path} has no {', '.join(missing)}; the model reads "
                f"{', '.join(self.config.camera_order)} and `camera_order` is a contract with "
                "the weights"
            )
        self.rig = run.rig
        assert self.decision_interval_s is not None
        self.model = AV3Model(self.config, self.checkpoint, self.decision_interval_s)
        if self.model.history.spacing_note:
            notes.append(self.model.history.spacing_note)
        self.notes = notes
        return list(notes)

    def start_episode(self, env: Any) -> None:
        """After the reset: the engine on first use, then the route and the connection.

        The 1.2 GB TensorRT engine is deserialised here and not in `setup`, after the env's
        own window and terrain exist, so the two peaks are not on the card at once -- the
        converter's ordering. `n_waypoints` is discovered by the load and the bridge's
        lateral MPC is built for it at connect, so the load has to come first.
        """
        if self.model is None:
            raise PolicyError("AV3Policy was not set up; the batch calls setup() first")
        if self.model.n_waypoints is None:
            try:
                self.model.load()
            except ModelError as error:
                raise PolicyError(str(error)) from error
        self.model.start_episode()
        self.route = route_for(env)
        max_steering, wheelbase = vehicle_geometry(env.agent)
        self.driver.episode(max_steering, wheelbase, n_waypoints=self.model.n_waypoints)

    def __call__(self, observation: Any) -> list[float]:
        del observation
        if self.env is None:
            raise PolicyError("AV3Policy is not bound to an env; the batch binds it first")
        if self.route is None:
            self.start_episode(self.env)
        assert self.model is not None
        agent = self.env.agent
        # Observe then predict, in that order, so the frame being predicted on is the newest
        # in the ring rather than one decision stale: `av3_base`'s own ordering.
        self.model.observe(self.rig.read(), agent)
        prediction = self.model.predict(agent, self.route)
        speed, yaw_rate = read_ego(agent)
        return self.driver.act(
            speed,
            yaw_rate,
            waypoints=self.model.waypoints(prediction),
            modelv2=self.model.modelv2_rows(prediction),
        )

    def close(self) -> None:
        super().close()
        if self.model is not None:
            self.model.close()


__all__ = [
    "BRIDGE_VARIABLE",
    "LONGITUDINAL_VARIABLE",
    "ROUTE_SAMPLE_M",
    "TARGET_SPEED_VARIABLE",
    "AV3Policy",
    "BridgePolicy",
    "bridge_address",
    "route_for",
    "vehicle_geometry",
]
