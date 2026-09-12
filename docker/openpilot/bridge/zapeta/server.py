from __future__ import annotations

import logging
import math
import os
import signal
import socket
import sys
import time
from types import SimpleNamespace
from typing import Optional

sys.path.insert(0, "/opt/openpilot")

# must be set before any cereal/openpilot import
os.environ.setdefault("SIMULATION", "1")
os.environ.setdefault("NOBOARD", "1")
os.environ.setdefault("SKIP_FW_QUERY", "1")
os.environ.setdefault("OPENPILOT_TRAJECTORY_TYPE", "0")  # 5-pt T_IDXS
os.environ.setdefault("FINGERPRINT", "TESLA MODEL 3")
os.environ.setdefault("PASSIVE", "0")

import numpy as np  # noqa: E402

import cereal.messaging as messaging  # noqa: E402
from cereal import car, log  # noqa: E402

from common.params import Params  # noqa: E402

# The fork's control-stack imports are deferred until the init message tells us the
# model's waypoint count: AV3_MPC_N must be in the env before the fork constants are
# first imported so T_IDXS / CONTROL_N / TRAJECTORY_SIZE compute for that N (one
# lateral-MPC node per waypoint). Populated by _late_imports().
lateral_planner_module = longitudinal_planner_module = None
LateralPlanner = LongitudinalPlanner = LatControlAngle = LongControl = VehicleModel = None
TRAJECTORY_SIZE = MIN_LATERAL_CONTROL_SPEED = CONTROL_N = None
LONG_MPC_T_IDXS = _T_IDXS = None
_late_imports_done = False


def _late_imports(n_mpc_nodes: int = 0) -> None:
    global lateral_planner_module, longitudinal_planner_module
    global LateralPlanner, LongitudinalPlanner, LatControlAngle, LongControl, VehicleModel
    global TRAJECTORY_SIZE, MIN_LATERAL_CONTROL_SPEED, CONTROL_N
    global LONG_MPC_T_IDXS, _T_IDXS, _late_imports_done
    if _late_imports_done:
        return
    if n_mpc_nodes and int(n_mpc_nodes) > 0:
        os.environ["AV3_MPC_N"] = str(int(n_mpc_nodes))

    # disable before planner import — else LateralPlanner.__init__ reads CURVATURE_SCALE env and crashes
    from common.dbg_config import DbgConfig
    DbgConfig.logs_update = False

    import selfdrive.controls.lib.lateral_planner as lateral_planner_module
    import selfdrive.controls.lib.longitudinal_planner as longitudinal_planner_module
    from selfdrive.controls.lib.lateral_planner import LateralPlanner, TRAJECTORY_SIZE
    from selfdrive.controls.lib.longitudinal_planner import LongitudinalPlanner
    from selfdrive.controls.lib.latcontrol import MIN_LATERAL_CONTROL_SPEED
    from selfdrive.controls.lib.latcontrol_angle import LatControlAngle
    from selfdrive.controls.lib.longcontrol import LongControl
    from selfdrive.controls.lib.drive_helpers import CONTROL_N
    from selfdrive.modeld.constants import T_IDXS
    from selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc import T_IDXS as LONG_MPC_T_IDXS
    from selfdrive.controls.lib.vehicle_model import VehicleModel

    _T_IDXS = list(T_IDXS)
    _late_imports_done = True
    logger.info(
        "planners imported: AV3_MPC_N=%s -> %d-pt grid, CONTROL_N=%d, TRAJECTORY_SIZE=%d",
        os.environ.get("AV3_MPC_N", "0"), len(_T_IDXS), CONTROL_N, TRAJECTORY_SIZE,
    )


from bridge_constants import TESLA_ACCEL_MAX as _TESLA_ACCEL_MAX  # noqa: E402
from bridge_constants import TESLA_ACCEL_MIN as _TESLA_ACCEL_MIN  # noqa: E402
from bridge_protocol import send_msg as _send_msg, recv_msg as _recv_msg  # noqa: E402
from zapeta.car_params import build_tesla_m3_car_params  # noqa: E402
from zapeta.derive_modelv2 import derive, from_predicted  # noqa: E402
from zapeta.accel_map import accel_to_carla  # noqa: E402

logger = logging.getLogger(__name__)

_MPS_TO_KPH = 3.6
_STANDSTILL_THRESHOLD = 0.01

_STEER_SAFETY_CLIP_DEG = 15.0
_STEER_SIM_MAX_DEG = 500.0
_SPEED_INTENT_OVERSPEED_EPS = 0.25
_SPEED_INTENT_DECEL_TAU_S = 1.5
_SPEED_INTENT_MIN_DECEL = -3.0
_MIN_LAG_CURVATURE_SPEED = 1.0
_MAX_LATERAL_JERK = 5.0
_DT_MDL = 0.05
# below this configured steer delay the 2*avg-current look-ahead is degenerate
# (it lands on the plan 0.1 s ahead); command the one-tick mean instead
_NO_LAG_DELAY_THRESHOLD_S = 0.02
# minimum forward accel while the eval-side creep nudge is active: with the model
# collapsed at v=0, LongControl latches 'stopping' (the creep cruise tops out at
# exactly vEgoStarting=0.5) and would hold the brake forever
_CREEP_FORCE_ACCEL = 0.6
_CREEP_FORCE_MAX_V = 1.5
_LAG_SPEED_EMA_ALPHA = 0.15
_LONGITUDINAL_MODES = {"acc", "blended", "blended_except_creep"}


class _FakeSubMaster:
    """SubMaster shim — bypasses ZMQ/timing waits, hands capnp builders straight to planners."""

    def __init__(self):
        # stores outer msg to keep capnp segments alive; __getitem__ unwraps
        self._msgs = {}
        self.logMonoTime = {}

    def set(self, name: str, msg, mono_time_ns: int) -> None:
        self._msgs[name] = msg
        self.logMonoTime[name] = mono_time_ns

    def __getitem__(self, name: str):
        return getattr(self._msgs[name], name)

    def all_checks(self, service_list=None) -> bool:
        return True


def _set_xyzt(field, x, y, z, t):
    field.x, field.y, field.z, field.t = x, y, z, t


def _build_modelv2(derived: dict, frame_id: int):
    msg = messaging.new_message("modelV2")
    msg.valid = True
    md = msg.modelV2
    md.frameId = frame_id

    t = derived["position_t"].tolist()
    zeros = [0.0] * len(t)
    _set_xyzt(md.position, derived["position_x"].tolist(), derived["position_y"].tolist(), zeros, t)
    _set_xyzt(md.velocity, derived["velocity_x"].tolist(), derived["velocity_y"].tolist(), zeros, t)
    _set_xyzt(md.acceleration, derived["acceleration_x"].tolist(), zeros, zeros, t)
    _set_xyzt(md.orientation, zeros, zeros, derived["orientation_z"].tolist(), t)
    _set_xyzt(md.orientationRate, zeros, zeros, derived["orientation_rate_z"].tolist(), t)

    return msg


def _build_car_state(v_ego: float, steering_angle_deg: float, target_speed: float):
    msg = messaging.new_message("carState")
    msg.valid = True
    cs = msg.carState
    cs.vEgo = v_ego
    cs.aEgo = 0.0
    cs.steeringAngleDeg = steering_angle_deg
    cs.steeringPressed = False
    cs.brakePressed = False
    cs.gasPressed = False
    # intent-aware standstill — reporting standstill with nonzero target_speed
    # keeps LongControl in hold instead of launching
    want_stop = target_speed < _STANDSTILL_THRESHOLD
    in_standstill = want_stop and (v_ego < _STANDSTILL_THRESHOLD)
    cs.standstill = in_standstill
    cs.cruiseState.enabled = True
    cs.cruiseState.available = True
    cs.cruiseState.standstill = in_standstill
    cs.cruiseState.speed = max(target_speed, v_ego) * _MPS_TO_KPH
    cs.gearShifter = car.CarState.GearShifter.drive
    return msg


def _build_controls_state(
    measured_curvature: float, target_speed: float, long_control_state, experimental_mode: bool
):
    msg = messaging.new_message("controlsState")
    msg.valid = True
    cst = msg.controlsState
    cst.curvature = measured_curvature
    cst.vCruise = target_speed * _MPS_TO_KPH
    # True → blended longitudinal MPC, False → ACC/cruise
    cst.experimentalMode = bool(experimental_mode)
    cst.enabled = True
    cst.active = True
    cst.longControlState = long_control_state
    cst.forceDecel = False
    return msg


def _experimental_mode_for(longitudinal_mode: str, creep_state: str) -> bool:
    if longitudinal_mode == "acc":
        return False
    if longitudinal_mode == "blended":
        return True
    # blended_except_creep: ACC only during eval-side creep
    return creep_state != "creep"


def _build_car_control():
    msg = messaging.new_message("carControl")
    msg.valid = True
    cc = msg.carControl
    cc.enabled = True
    cc.latActive = True
    cc.longActive = True
    return msg


def _build_radar_state():
    msg = messaging.new_message("radarState")
    msg.valid = True
    rs = msg.radarState
    rs.leadOne.status = False
    rs.leadTwo.status = False
    return msg


def _build_longitudinal_plan(planner: LongitudinalPlanner):
    msg = messaging.new_message("longitudinalPlan")
    msg.valid = True
    lp = msg.longitudinalPlan
    lp.speeds = planner.v_desired_trajectory.tolist()
    lp.accels = planner.a_desired_trajectory.tolist()
    lp.jerks = planner.j_desired_trajectory.tolist()
    lp.hasLead = False
    lp.fcw = bool(planner.fcw)
    return msg


def _apply_speed_intent_envelope(long_plan, v_ego: float, target_speed: float) -> float:
    """cap planner speeds when intent is slower than current — ACC mode otherwise
    accelerates against AV3 waypoints already compressing toward a stop"""
    target_speed = max(0.0, float(target_speed))
    if float(v_ego - target_speed) <= _SPEED_INTENT_OVERSPEED_EPS:
        return 0.0

    times = np.array(_T_IDXS[:len(long_plan.speeds)], dtype=np.float64)
    if len(times) < 2:
        return 0.0

    intent_accel = float(np.clip(
        (target_speed - v_ego) / _SPEED_INTENT_DECEL_TAU_S,
        max(_TESLA_ACCEL_MIN, _SPEED_INTENT_MIN_DECEL),
        -0.05,
    ))

    speed_cap = np.maximum(target_speed, v_ego + intent_accel * times)
    speed_cap[0] = min(speed_cap[0], v_ego)

    speeds = np.array(list(long_plan.speeds), dtype=np.float64)
    n = min(len(speeds), len(speed_cap))
    speeds[:n] = np.minimum(speeds[:n], speed_cap[:n])
    speeds = np.maximum(speeds, 0.0)
    long_plan.speeds = [float(x) for x in speeds]

    accels = np.array(list(long_plan.accels), dtype=np.float64)
    if len(accels):
        accel_cap = np.gradient(speed_cap, times)
        m = min(len(accels), len(accel_cap))
        accels[:m] = np.minimum(accels[:m], accel_cap[:m])
        long_plan.accels = [float(x) for x in accels]

    return intent_accel


def _get_lag_adjusted_curvature(
    v_ego: float,
    psis: list,
    curvatures: list,
    curvature_rates: list,
    delay_s: float,
) -> tuple[float, float, float, float]:
    # local copy of fork's lag compensation — fork hardcodes +0.2s, but CARLA
    # sync mode has no EPS dynamics so we need a tunable delay

    if len(psis) != CONTROL_N:
        psis = [0.0] * CONTROL_N
        curvatures = [0.0] * CONTROL_N
        curvature_rates = [0.0] * CONTROL_N

    v = max(_MIN_LAG_CURVATURE_SPEED, float(v_ego))
    current_curvature_desired = float(curvatures[0])
    if float(delay_s) < _NO_LAG_DELAY_THRESHOLD_S:
        # No EPS lag (CARLA sync mode): the control applied this tick acts over
        # [t, t+DT], so command the mean plan curvature over one actuation tick
        # (~kappa(DT/2)). The 2*avg-current extrapolation would reduce to a 0.1 s
        # lead here, which unwinds turns early (measured exit-phase understeer).
        psi = float(np.interp(_DT_MDL, _T_IDXS[:CONTROL_N], psis))
        desired_curvature_unclipped = psi / (v * _DT_MDL)
    else:
        delay = float(delay_s)
        psi = float(np.interp(delay, _T_IDXS[:CONTROL_N], psis))
        average_curvature_desired = psi / (v * delay)
        desired_curvature_unclipped = 2.0 * average_curvature_desired - current_curvature_desired

    desired_curvature_rate = float(curvature_rates[0])
    max_curvature_rate = _MAX_LATERAL_JERK / (v ** 2)
    safe_desired_curvature_rate = float(np.clip(
        desired_curvature_rate,
        -max_curvature_rate,
        max_curvature_rate,
    ))
    safe_desired_curvature = float(np.clip(
        desired_curvature_unclipped,
        current_curvature_desired - max_curvature_rate * _DT_MDL,
        current_curvature_desired + max_curvature_rate * _DT_MDL,
    ))

    return (
        safe_desired_curvature,
        safe_desired_curvature_rate,
        float(desired_curvature_unclipped),
        current_curvature_desired,
    )


def _long_control_debug(CP, state_before, CS, long_plan, t_since_plan: float) -> dict:
    # shadow of LongControl.update() / long_control_state_trans() — only the
    # values the state-transition log line reads. resync if the fork changes either
    speeds = list(long_plan.speeds)
    if len(speeds) == CONTROL_N:
        v_target_lower = float(np.interp(
            CP.longitudinalActuatorDelayLowerBound + t_since_plan, _T_IDXS[:CONTROL_N], speeds))
        v_target_upper = float(np.interp(
            CP.longitudinalActuatorDelayUpperBound + t_since_plan, _T_IDXS[:CONTROL_N], speeds))
        v_target = min(v_target_lower, v_target_upper)
        v_target_1sec = float(np.interp(
            CP.longitudinalActuatorDelayUpperBound + t_since_plan + 1.0, _T_IDXS[:CONTROL_N], speeds))
    else:
        v_target = 0.0
        v_target_1sec = 0.0

    cruise_standstill = bool(CS.cruiseState.standstill) and not CP.enableGasInterceptor
    accelerating = v_target_1sec > v_target
    planned_stop = (
        v_target < CP.vEgoStopping
        and v_target_1sec < CP.vEgoStopping
        and not accelerating
    )
    stay_stopped = (
        CS.vEgo < CP.vEgoStopping
        and (bool(CS.brakePressed) or cruise_standstill)
    )
    stopping_condition = planned_stop or stay_stopped
    starting_condition = (
        v_target_1sec > CP.vEgoStarting
        and accelerating
        and not cruise_standstill
        and not bool(CS.brakePressed)
    )

    return {
        "long_state_before": str(state_before),
        "long_v_target": float(v_target),
        "long_v_target_1sec": float(v_target_1sec),
        "long_planned_stop": bool(planned_stop),
        "long_stay_stopped": bool(stay_stopped),
        "long_stopping_condition": bool(stopping_condition),
        "long_starting_condition": bool(starting_condition),
    }


def _apply_zapeta_sim_steer_limits(target_deg: float, measured_deg: float) -> float:
    # sim-only: per-tick window around measured + absolute cap; the fork's
    # rate/per-speed limits exist to protect real EPS, not anything CARLA models
    clipped = float(np.clip(
        target_deg,
        measured_deg - _STEER_SAFETY_CLIP_DEG,
        measured_deg + _STEER_SAFETY_CLIP_DEG,
    ))
    return float(np.clip(clipped, -_STEER_SIM_MAX_DEG, _STEER_SIM_MAX_DEG))


class BridgeServer:
    def __init__(self, host: str = "0.0.0.0", port: int = 5558):
        self.host = host
        self.port = port
        self.CP: Optional[car.CarParams] = None
        self.lateral_planner: Optional[LateralPlanner] = None
        self.longitudinal_planner: Optional[LongitudinalPlanner] = None
        self.long_control: Optional[LongControl] = None
        self.lat_control: Optional[LatControlAngle] = None
        self.VM: Optional[VehicleModel] = None
        self.sm = _FakeSubMaster()
        self._frame_id = 0
        self._mono_time_ns = 0
        self._max_steer_angle: float = 70.0
        self._steer_ratio_carla: float = 12.0
        self._longitudinal_mode = "blended_except_creep"
        self._steer_actuator_delay = 0.2
        self._steer_delay_extra = 0.2
        self._effective_steer_delay = 0.4
        self._longitudinal_delay_lower = 0.15
        self._longitudinal_delay_upper = 0.4
        self._carla_steer_curvature_gain = 0.0  # g0; 0 = disabled (geometric steer output)
        self._carla_steer_understeer_coef = 0.0  # a in g(v)=g0/(1+a*v^2)
        self._telemetry_mpc_outputs = False
        self._mpc_telemetry_error_count = 0
        self._running = True

    def _reset_per_connection_state(self):
        self._curv_calc_speed_ema = 0.0
        self._steer_limited = False
        self._last_actuators = car.CarControl.Actuators.new_message()
        self._live_params = SimpleNamespace(
            stiffnessFactor=1.0,
            steerRatio=self.CP.steerRatio,
            angleOffsetDeg=0.0,
            angleOffsetAverageDeg=0.0,
            roll=0.0,
        )

    def run(self):
        signal.signal(signal.SIGTERM, lambda *_: self._shutdown())
        signal.signal(signal.SIGINT, lambda *_: self._shutdown())

        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((self.host, self.port))
        server.listen(1)
        server.settimeout(1.0)
        logger.info(f"Bridge listening on {self.host}:{self.port}")

        while self._running:
            try:
                conn, addr = server.accept()
            except socket.timeout:
                continue
            logger.info(f"Client connected from {addr}")
            try:
                self._handle_connection(conn)
            except ConnectionError as e:
                logger.info(f"Client disconnected: {e}")
            except Exception:
                logger.exception("Error handling connection")
            finally:
                conn.close()
                self.lateral_planner = None
                self.longitudinal_planner = None
                self.long_control = None
                self.lat_control = None
                self.VM = None

        server.close()
        logger.info("Bridge server stopped")

    def _handle_connection(self, conn: socket.socket):
        conn.settimeout(5.0)

        msg = _recv_msg(conn)
        if msg.get("type") != "init":
            raise ValueError(f"Expected init message, got: {msg.get('type')}")

        # The model's waypoint count selects the lateral-MPC grid: import the fork
        # planners now with AV3_MPC_N set so T_IDXS/CONTROL_N match. <=0 or absent
        # keeps the legacy 5-point grid.
        _late_imports(int(msg.get("n_waypoints", 0) or 0))

        self._steer_ratio_carla = msg.get("steer_ratio", 12.0)
        self._max_steer_angle = msg.get("max_steer_angle", 70.0)
        self._longitudinal_mode = str(
            msg.get("zapeta_longitudinal_mode", self._longitudinal_mode)
        ).lower()
        self._telemetry_mpc_outputs = bool(msg.get("telemetry_mpc_outputs", False))
        if self._longitudinal_mode not in _LONGITUDINAL_MODES:
            logger.warning(
                "Unknown zapeta_longitudinal_mode=%s; using blended_except_creep",
                self._longitudinal_mode,
            )
            self._longitudinal_mode = "blended_except_creep"

        for key, attr in (
            ("zapeta_steer_actuator_delay", "_steer_actuator_delay"),
            ("zapeta_steer_delay_extra", "_steer_delay_extra"),
            ("zapeta_longitudinal_actuator_delay_lower", "_longitudinal_delay_lower"),
            ("zapeta_longitudinal_actuator_delay_upper", "_longitudinal_delay_upper"),
        ):
            val = msg.get(key)
            if val is not None:
                setattr(self, attr, float(val))

        gain = msg.get("carla_steer_curvature_gain")
        self._carla_steer_curvature_gain = float(gain) if gain else 0.0
        coef = msg.get("carla_steer_understeer_coef")
        self._carla_steer_understeer_coef = float(coef) if coef else 0.0

        logger.info(
            f"Init: steer_ratio={self._steer_ratio_carla}, "
            f"max_steer_angle={self._max_steer_angle}, "
            f"longitudinal_mode={self._longitudinal_mode}, "
            f"telemetry_mpc_outputs={self._telemetry_mpc_outputs}, "
            f"steer_delay={self._steer_actuator_delay}+{self._steer_delay_extra}s, "
            f"long_delay=[{self._longitudinal_delay_lower}, {self._longitudinal_delay_upper}]s, "
            f"carla_steer_curvature_gain={self._carla_steer_curvature_gain or 'off (geometric)'}, "
            f"carla_steer_understeer_coef={self._carla_steer_understeer_coef}"
        )

        self._setup_planners()
        _send_msg(conn, {"type": "ready"})
        logger.info("Sent ready, entering control loop")

        # 30s for first-step startup on camera-heavy configs; steady-state ticks <100ms
        conn.settimeout(30.0)
        while self._running:
            msg = _recv_msg(conn)
            t = msg.get("type")
            if t == "shutdown":
                logger.info("Received shutdown")
                break
            elif t == "step":
                response = self._handle_step(msg)
                _send_msg(conn, response)
            else:
                logger.warning(f"Unknown message type: {t}")

    def _setup_planners(self):
        # LongitudinalPlanner reads this from Params
        params = Params()
        if params.get("LongitudinalPersonality") is None:
            params.put("LongitudinalPersonality", str(log.LongitudinalPersonality.standard))

        lateral_planner_module.should_print_in_lateral_planner = False
        longitudinal_planner_module.should_print_in_longitudinal_planner = False

        self.CP = build_tesla_m3_car_params()
        self.CP.steerActuatorDelay = self._steer_actuator_delay
        self._effective_steer_delay = max(0.0, self.CP.steerActuatorDelay + self._steer_delay_extra)
        self.CP.longitudinalActuatorDelayLowerBound = max(1e-3, self._longitudinal_delay_lower)
        self.CP.longitudinalActuatorDelayUpperBound = max(
            self.CP.longitudinalActuatorDelayLowerBound,
            self._longitudinal_delay_upper,
        )
        self.lateral_planner = LateralPlanner(self.CP)
        self.longitudinal_planner = LongitudinalPlanner(self.CP)
        self.long_control = LongControl(self.CP)
        self.lat_control = LatControlAngle(self.CP, None)
        self.VM = VehicleModel(self.CP)
        self._reset_per_connection_state()

        logger.info(
            f"Planners ready: wheelbase={self.CP.wheelbase}m, "
            f"steerRatio={self.CP.steerRatio}, mass={self.CP.mass:.1f}kg; "
            f"steerDelayEffective={self._effective_steer_delay:.3f}s; "
            f"longDelay=[{self.CP.longitudinalActuatorDelayLowerBound:.3f}, "
            f"{self.CP.longitudinalActuatorDelayUpperBound:.3f}]s"
        )

    def _build_mpc_telemetry(self) -> dict:
        lat_mpc = self.lateral_planner.lat_mpc
        long_mpc = self.longitudinal_planner.mpc

        return {
            "lateral": {
                "state_columns": ["x", "y", "psi", "psi_rate"],
                "control_columns": ["psi_accel"],
                "t": self.lateral_planner.t_idxs[:lat_mpc.x_sol.shape[0]].tolist(),
                "x_sol": lat_mpc.x_sol.tolist(),
                "u_sol": lat_mpc.u_sol.tolist(),
                "status": int(lat_mpc.solution_status),
                "cost": float(lat_mpc.cost),
                "solve_time_ms": float(lat_mpc.solve_time * 1e3),
                "solution_invalid_cnt": int(self.lateral_planner.solution_invalid_cnt),
                "v_plan": self.lateral_planner.v_plan.tolist(),
                "v_ego_mpc": float(self.lateral_planner.v_ego),
            },
            "longitudinal": {
                "state_columns": ["x", "v", "a"],
                "control_columns": ["j"],
                "t": LONG_MPC_T_IDXS[:long_mpc.x_sol.shape[0]].tolist(),
                "x_sol": long_mpc.x_sol.tolist(),
                "u_sol": long_mpc.u_sol.tolist(),
                "v_solution": long_mpc.v_solution.tolist(),
                "a_solution": long_mpc.a_solution.tolist(),
                "j_solution": long_mpc.j_solution.tolist(),
                "status": int(long_mpc.solution_status),
                "mode": str(long_mpc.mode),
                "source": str(long_mpc.source),
                "solve_time_ms": float(long_mpc.solve_time * 1e3),
                "time_qp_solution_ms": float(long_mpc.time_qp_solution * 1e3),
                "time_linearization_ms": float(long_mpc.time_linearization * 1e3),
                "time_integrator_ms": float(long_mpc.time_integrator * 1e3),
                "crash_cnt": float(long_mpc.crash_cnt),
                "fcw": bool(self.longitudinal_planner.fcw),
                "v_model_error": float(self.longitudinal_planner.v_model_error),
            },
        }

    def _carla_plant_gain(self, v_ego: float) -> float:
        """Empirical CARLA steer→curvature gain g(v) = g0/(1+a·v²). Speed-dependent
        because CARLA's steering_curve loses authority with speed; a constant g
        over-steers at low speed and under-steers at high."""
        return self._carla_steer_curvature_gain / (
            1.0 + self._carla_steer_understeer_coef * v_ego * v_ego
        )

    def _measured_angle_for_limits(self, steering_angle_deg: float, v_ego: float) -> float:
        """Measured column angle re-expressed in the target-angle (VM) scale.

        With the plant-inverted output, the measured proxy angle encodes curvature
        at a different scale than the VM-scale target; clipping across the two
        scales turns the ±15°/tick band into a turn-exit ratchet (the floor sits
        above the MPC's falling request, forcing steer up while the plan unwinds).
        Invert the output map (kappa = g(v)·carla_steer) and re-encode through the
        same VM call that produces the target, so the band is a true rate limit in
        one scale. Geometric output mode already echoes the previous command in
        the VM scale — no conversion needed there.
        """
        if self._carla_steer_curvature_gain <= 0.0:
            return steering_angle_deg
        carla_steer_prev = -steering_angle_deg / (self._max_steer_angle * self.CP.steerRatio)
        kappa_prev = carla_steer_prev * self._carla_plant_gain(v_ego)
        return math.degrees(self.VM.get_steer_from_curvature(-kappa_prev, v_ego, 0.0))

    def _handle_step(self, msg: dict) -> dict:
        wps_in = msg["waypoints"]
        ego = msg["ego"]
        target_speed = float(msg.get("target_speed", 0.0))
        creep_state = str(msg.get("creep_state", "idle"))
        v_ego = float(ego["v_ego"])
        # CARLA is right-positive, openpilot is left-positive — negate at ingress
        steering_angle_deg = -float(ego.get("steering_angle_deg", 0.0))

        if not wps_in:
            return {
                "type": "control",
                "steer": 0.0,
                "throttle": 0.0,
                "brake": 1.0,
                "desired_curvature": 0.0,
                "steer_angle_deg": 0.0,
                "accel_cmd": 0.0,
            }

        # Full-modelv2 models ship yaw/velocity/accel directly — use them as-is;
        # otherwise reconstruct them from waypoint positions. Either way the planners
        # interpolate onto their MPC grids from the model's own times, so any
        # waypoint count/spacing works.
        mv2_in = msg.get("modelv2")
        if mv2_in:
            derived = from_predicted(mv2_in, v_ego=v_ego)
        else:
            wps_xyt = [(0.0, 0.0, 0.0)] + [(float(w[0]), float(w[1]), float(w[2])) for w in wps_in]
            derived = derive(wps_xyt, v_ego=v_ego)
        n_pts = len(derived["position_x"])
        if n_pts < 2:
            logger.warning(f"Degenerate trajectory: only {n_pts} pt(s)")

        # column angle → road wheel → curvature (Ackermann bicycle)
        road_wheel_deg = steering_angle_deg / self.CP.steerRatio
        measured_curvature = math.tan(math.radians(road_wheel_deg)) / self.CP.wheelbase

        self._frame_id += 1
        self._mono_time_ns = int(time.monotonic() * 1e9)

        long_control_state = (
            self.long_control.long_control_state
            if self.long_control is not None
            else car.CarControl.Actuators.LongControlState.pid
        )
        experimental_mode = _experimental_mode_for(self._longitudinal_mode, creep_state)

        self.sm.set("modelV2", _build_modelv2(derived, self._frame_id), self._mono_time_ns)
        self.sm.set("carState", _build_car_state(v_ego, steering_angle_deg, target_speed), self._mono_time_ns)
        self.sm.set("controlsState", _build_controls_state(
            measured_curvature, target_speed, long_control_state, experimental_mode,
        ), self._mono_time_ns)
        self.sm.set("carControl", _build_car_control(), self._mono_time_ns)
        self.sm.set("radarState", _build_radar_state(), self._mono_time_ns)
        carState = self.sm["carState"]

        self.lateral_planner.update(self.sm)
        x_sol = self.lateral_planner.lat_mpc.x_sol  # (N+1, 4): [x, y, psi, curv*v]
        u_sol = self.lateral_planner.lat_mpc.u_sol  # (N, 1): curv_rate*v
        v_plan = self.lateral_planner.v_ego         # clipped to MIN_SPEED=1.0

        psis = x_sol[0:CONTROL_N, 2].tolist()
        curvatures = (x_sol[0:CONTROL_N, 3] / v_plan).tolist()
        curvature_rates = [float(u / v_plan) for u in u_sol[0:CONTROL_N - 1]] + [0.0]

        self._curv_calc_speed_ema = (
            _LAG_SPEED_EMA_ALPHA * v_ego
            + (1.0 - _LAG_SPEED_EMA_ALPHA) * self._curv_calc_speed_ema
        )
        (
            desired_curvature,
            desired_curvature_rate,
            curvature_unclipped_lag,
            curvature_pre_lag,
        ) = _get_lag_adjusted_curvature(
            self._curv_calc_speed_ema,
            psis,
            curvatures,
            curvature_rates,
            self._effective_steer_delay,
        )

        lat_active = bool(
            v_ego > max(self.CP.minSteerSpeed, MIN_LATERAL_CONTROL_SPEED)
            and not carState.standstill
        )
        if not lat_active:
            self.lat_control.reset()

        _, steer_angle_raw_deg, _lac_log = self.lat_control.update(
            lat_active,
            carState,
            self.VM,
            self._live_params,
            self._last_actuators,
            self._steer_limited,
            desired_curvature,
            desired_curvature_rate,
            None,
        )
        measured_for_limits = self._measured_angle_for_limits(steering_angle_deg, v_ego)
        steer_angle_deg = (
            _apply_zapeta_sim_steer_limits(steer_angle_raw_deg, measured_for_limits)
            if lat_active else float(steer_angle_raw_deg)
        )

        self.longitudinal_planner.update(self.sm)
        long_plan_msg = _build_longitudinal_plan(self.longitudinal_planner)
        long_plan = long_plan_msg.longitudinalPlan
        overspeed_accel_limit = _apply_speed_intent_envelope(long_plan, v_ego, target_speed)
        planner_a_target = float(np.clip(
            self.longitudinal_planner.a_desired,
            _TESLA_ACCEL_MIN,
            _TESLA_ACCEL_MAX,
        ))
        long_state_before = self.long_control.long_control_state
        long_debug = _long_control_debug(self.CP, long_state_before, carState, long_plan, 0.0)
        a_target = float(np.clip(
            self.long_control.update(
                True,
                carState,
                long_plan,
                [_TESLA_ACCEL_MIN, _TESLA_ACCEL_MAX],
                0.0,
            ),
            _TESLA_ACCEL_MIN,
            _TESLA_ACCEL_MAX,
        ))
        if creep_state == "creep" and v_ego < _CREEP_FORCE_MAX_V:
            # make the eval-side creep nudge reach the wheels (see constant docs)
            a_target = max(a_target, _CREEP_FORCE_ACCEL)
        long_debug["long_state_after"] = str(self.long_control.long_control_state)
        if long_debug["long_state_after"] != long_debug["long_state_before"]:
            logger.info(
                "LongControl %s -> %s: mode=%s exp=%s source=%s "
                "v_ego=%.2f target_speed=%.2f v_target=%.2f v_1s=%.2f "
                "planned_stop=%s stay_stopped=%s stopping=%s starting=%s wp_far=%.2f",
                long_debug["long_state_before"],
                long_debug["long_state_after"],
                self._longitudinal_mode,
                experimental_mode,
                self.longitudinal_planner.mpc.source,
                v_ego,
                target_speed,
                long_debug["long_v_target"],
                long_debug["long_v_target_1sec"],
                long_debug["long_planned_stop"],
                long_debug["long_stay_stopped"],
                long_debug["long_stopping_condition"],
                long_debug["long_starting_condition"],
                float(derived["position_x"][-1]),
            )

        creep_override_accel = 0.0
        if creep_state == "creep" and v_ego < target_speed:
            creep_override_accel = float(np.clip(target_speed - v_ego, 0.0, _TESLA_ACCEL_MAX))
            a_target = max(a_target, creep_override_accel)

        if math.isfinite(steer_angle_deg) and math.isfinite(a_target):
            self._last_actuators.steeringAngleDeg = steer_angle_deg
            self._last_actuators.accel = a_target
            self._last_actuators.curvature = desired_curvature
            self._last_actuators.longControlState = self.long_control.long_control_state
        else:
            logger.warning("non-finite plan output, using last known")

        # steer angle (openpilot, left-positive column deg) → CARLA-normalized [-1, 1].
        # negate for CARLA's right-positive sign.
        last_steer_angle_deg = self._last_actuators.steeringAngleDeg
        if self._carla_steer_curvature_gain > 0.0:
            # plant inversion: the curvature openpilot's VM says this (limited)
            # column angle yields, divided by the empirical gain g(v). The VM
            # round-trip cancels the wheelbase, so g alone sets the output scale.
            implied_curv = self.VM.calc_curvature(math.radians(last_steer_angle_deg), v_ego, 0.0)
            carla_steer = float(np.clip(-implied_curv / self._carla_plant_gain(v_ego), -1.0, 1.0))
        else:
            last_road_wheel_deg = last_steer_angle_deg / self.CP.steerRatio
            carla_steer = float(np.clip(
                -last_road_wheel_deg / self._max_steer_angle, -1.0, 1.0,
            ))
        throttle, brake = accel_to_carla(self._last_actuators.accel, v_ego)

        response = {
            "type": "control",
            "steer": carla_steer,
            "throttle": throttle,
            "brake": brake,
            "desired_curvature": float(desired_curvature),
            "steer_angle_deg": float(self._last_actuators.steeringAngleDeg),
            "accel_cmd": float(self._last_actuators.accel),
            # diagnostics — not gated on for control, only recorded as telemetry
            "mpc_mode": str(self.longitudinal_planner.mpc.mode),
            "longitudinal_mode": self._longitudinal_mode,
            "experimental_mode": bool(experimental_mode),
            "long_mpc_source": str(self.longitudinal_planner.mpc.source),
            "raw_steer_angle_deg": float(steer_angle_raw_deg),
            "steer_angle_limited_deg": float(steer_angle_deg),
            "carla_steer_curvature_gain": float(self._carla_steer_curvature_gain),
            "carla_steer_understeer_coef": float(self._carla_steer_understeer_coef),
            "measured_steer_angle_deg": float(steering_angle_deg),
            "measured_steer_equiv_deg": float(measured_for_limits),
            "curvature_pre_lag": float(curvature_pre_lag),
            "curvature_unclipped_lag": float(curvature_unclipped_lag),
            "steer_delay_s": float(self._effective_steer_delay),
            "longitudinal_delay_lower_s": float(self.CP.longitudinalActuatorDelayLowerBound),
            "longitudinal_delay_upper_s": float(self.CP.longitudinalActuatorDelayUpperBound),
            "lateral_mpc_status": int(self.lateral_planner.lat_mpc.solution_status),
            "lateral_mpc_cost": float(self.lateral_planner.lat_mpc.cost),
            "lateral_mpc_solve_time_ms": float(self.lateral_planner.lat_mpc.solve_time * 1e3),
            "lat_plan_y_pts": self.lateral_planner.y_pts.tolist(),
            "lat_plan_yaw": self.lateral_planner.plan_yaw.tolist(),
            "lat_plan_yaw_rate": self.lateral_planner.plan_yaw_rate.tolist(),
            "lat_plan_curvatures": curvatures,
            "overspeed_accel_limit": float(overspeed_accel_limit),
            "creep_override_accel": float(creep_override_accel),
            "v_cruise_kph": float(target_speed * _MPS_TO_KPH),
            "model_position_x": derived["position_x"].tolist(),
            "model_velocity_x": derived["velocity_x"].tolist(),
            "model_acceleration_x": derived["acceleration_x"].tolist(),
            "model_orientation_z": derived["orientation_z"].tolist(),
            "model_orientation_rate_z": derived["orientation_rate_z"].tolist(),
            "a_desired_trajectory": self.longitudinal_planner.a_desired_trajectory.tolist(),
            "long_plan_speed_trajectory": list(long_plan.speeds),
            "long_plan_accel_trajectory": list(long_plan.accels),
            "planner_accel_cmd": float(planner_a_target),
            "long_control_state": str(self.long_control.long_control_state),
            **long_debug,
        }
        if self._telemetry_mpc_outputs:
            try:
                response["mpc"] = self._build_mpc_telemetry()
            except Exception:
                self._mpc_telemetry_error_count += 1
                n = self._mpc_telemetry_error_count
                if n <= 3 or n % 100 == 0:
                    logger.exception(
                        "failed to serialize MPC telemetry; omitting nested mpc payload "
                        "for this frame (count=%d)",
                        n,
                    )
        return response

    def _shutdown(self):
        self._running = False


def main():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [bridge-zapeta] %(levelname)s %(message)s"
    )
    host = os.environ.get("BRIDGE_HOST", "0.0.0.0")
    port = int(os.environ.get("BRIDGE_PORT", "5558"))
    server = BridgeServer(host=host, port=port)
    server.run()


if __name__ == "__main__":
    main()
