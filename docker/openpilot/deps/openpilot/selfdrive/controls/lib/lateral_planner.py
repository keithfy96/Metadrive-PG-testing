import numpy as np
import os
from common.realtime import sec_since_boot, DT_MDL
from common.numpy_fast import interp
from common.dbg_config import DbgConfig
from system.swaglog import cloudlog
from selfdrive.controls.lib.lateral_mpc_lib.lat_mpc import LateralMpc
from selfdrive.controls.lib.lateral_mpc_lib.lat_mpc import N as LAT_MPC_N
from selfdrive.controls.lib.drive_helpers import CONTROL_N, MIN_SPEED, get_speed_error
from selfdrive.controls.lib.desire_helper import DesireHelper
from selfdrive.modeld.constants import SHOULD_USE_OPENPILOT_TRAJECTORY, T_IDXS, AV3_UNIFORM_TRAJECTORY
from selfdrive.wingfin_common import InferenceConfigParser
import cereal.messaging as messaging
from cereal import log

# number of trajectory grid points: 33 stock / 5 legacy-AV3 / N+1 in the AV3-uniform
# third mode. Equivalent to `33 if openpilot else 5` for the two legacy grids.
TRAJECTORY_SIZE = len(T_IDXS)
CAMERA_OFFSET = 0.04
# Extreme steering rate is unpleasant, even
# when it does not cause bad jerk.
# TODO this cost should be lowered when low
# speed lateral control is stable on all cars
INF_CONFIG_PARSER = InferenceConfigParser()
STEERING_RATE_COST = INF_CONFIG_PARSER.get_value('st_rate_cost', 350.0)
PATH_COST = INF_CONFIG_PARSER.get_value('path_cost',1.0)
LATERAL_MOTION_COST = INF_CONFIG_PARSER.get_value('lateral_motion_cost', 0.11)
LATERAL_ACCEL_COST = INF_CONFIG_PARSER.get_value('lateral_accel_cost',0.0)
LATERAL_JERK_COST =  INF_CONFIG_PARSER.get_value('lateral_jerk_cost', 0.04)

should_print_in_lateral_planner = True


def cprint(*args, **kwargs):
  if should_print_in_lateral_planner:
    print(*args, **kwargs)


class LateralPlanner:
  def __init__(self, CP):
    self.DH = DesireHelper()

    # Vehicle model parameters used to calculate lateral movement of car
    self.factor1 = CP.wheelbase - CP.centerToFront
    self.factor2 = (CP.centerToFront * CP.mass) / (CP.wheelbase * CP.tireStiffnessRear)
    self.last_cloudlog_t = 0
    self.solution_invalid_cnt = 0

    self.path_xyz = np.zeros((TRAJECTORY_SIZE, 3))
    self.velocity_xyz = np.zeros((TRAJECTORY_SIZE, 3))
    self.plan_yaw = np.zeros((TRAJECTORY_SIZE,))
    self.plan_yaw_rate = np.zeros((TRAJECTORY_SIZE,))
    self.t_idxs = np.arange(TRAJECTORY_SIZE)
    self.y_pts = np.zeros((TRAJECTORY_SIZE,))
    self.v_plan = np.zeros((TRAJECTORY_SIZE,))
    self.v_ego = 0.0
    self.l_lane_change_prob = 0.0
    self.r_lane_change_prob = 0.0

    # AV3-uniform mode: one lateral-MPC node per waypoint via the menu factory
    # (loads the prebuilt solver for this N, or generates+compiles it on demand).
    # Otherwise the legacy single-N SConscript-built solver.
    if AV3_UNIFORM_TRAJECTORY:
      self.lat_mpc = LateralMpc(t_grid=list(T_IDXS))
    else:
      self.lat_mpc = LateralMpc()
    self.reset_mpc(np.zeros(4))
    self.max_curvature = 0.
    self.max_yaw = 0.

    if DbgConfig.logs_update:
      self.curvature_scale = int(os.environ.get("CURVATURE_SCALE"))

  def reset_mpc(self, x0=np.zeros(4)):
    self.x0 = x0
    self.lat_mpc.reset(x0=self.x0)

  def update(self, sm):
    cprint('========== LateralPlanner.update ==========')
    # clip speed , lateral planning is not possible at 0 speed
    measured_curvature = sm['controlsState'].curvature
    # measured_curvature *= self.curvature_scale
    v_ego_car = sm['carState'].vEgo

    cprint(f'measured_curvatue = {measured_curvature}')
    cprint(f'v_ego_car = {v_ego_car}')

    # Parse model predictions
    md = sm['modelV2']
    cprint(f'modelMonoTime = {sm.logMonoTime["modelV2"]}')
    cprint(f'sec_since_boot = {sec_since_boot()}')

    # The model may emit any number of waypoints at any spacing over the horizon;
    # parse them all (was: required exactly TRAJECTORY_SIZE points).
    cprint(f'len(md.position.x) = {len(md.position.x)}')
    if len(md.position.x) >= 2 and len(md.orientation.x) >= 2:
      self.path_xyz = np.column_stack([md.position.x, md.position.y, md.position.z])
      self.t_idxs = np.array(md.position.t)
      self.plan_yaw = np.array(md.orientation.z)
      self.plan_yaw_rate = np.array(md.orientationRate.z)
      # [pimpke] TODO: populate velocity or have a separate speed field? (see all uses of modelV2.velocity)
      self.velocity_xyz = np.column_stack([md.velocity.x, md.velocity.y, md.velocity.z])
      speed_error = get_speed_error(md, v_ego_car)
      if INF_CONFIG_PARSER.get_value('use_new_vego_calc', False):
        car_speed = np.abs(md.velocity.x) + 1e-3
      else:
        car_speed = np.linalg.norm(self.velocity_xyz, axis=1) - speed_error
      self.v_plan = np.clip(car_speed, MIN_SPEED, np.inf)
      self.v_ego = self.v_plan[0]

      # CARLA time-vs-space fix: the MPC integrates position at v_plan, but the
      # model's targets are parameterized at the model's assumed traversal speed —
      # when v_ego << v_model the MPC over-curves by ~v_model/v_ego. Integrate at
      # the actual longitudinal speed (uniform v_ego) and resample the geometric
      # path to the arc-length the car really covers (s = v_ego * t); the MPC
      # output is then geometric curvature at any speed. v_x semantics throughout
      # (kappa = yaw_rate / v_x, not / |v|).
      if INF_CONFIG_PARSER.get_value('carla_uniform_vego_arclen', False):
        v_lat = max(float(v_ego_car), MIN_SPEED)
        seg = np.linalg.norm(np.diff(self.path_xyz[:, :2], axis=0), axis=1)
        s_model = np.concatenate([[0.0], np.cumsum(seg)])      # model path arc-length
        s_car = v_lat * self.t_idxs                            # arc-length car reaches per node
        vx_model = np.maximum(self.velocity_xyz[:, 0], 0.1)    # longitudinal speed, per node
        kappa = self.plan_yaw_rate / vx_model                  # steering curvature omega/vx
        self.path_xyz[:, 1] = np.interp(s_car, s_model, self.path_xyz[:, 1])
        self.plan_yaw = np.interp(s_car, s_model, self.plan_yaw)
        self.plan_yaw_rate = np.interp(s_car, s_model, kappa) * v_lat
        self.v_plan = np.full(len(self.t_idxs), v_lat)
        self.v_ego = v_lat

      # Interpolate the (variable-length) model trajectory from its own sample
      # times onto the MPC's fixed node times — supports any waypoint count /
      # spacing (identity when the model already emits the grid points).
      mpc_t = np.array(T_IDXS)[:LAT_MPC_N + 1]
      model_t = self.t_idxs
      self.path_xyz = np.column_stack([
        np.interp(mpc_t, model_t, self.path_xyz[:, 0]),
        np.interp(mpc_t, model_t, self.path_xyz[:, 1]),
        np.interp(mpc_t, model_t, self.path_xyz[:, 2]),
      ])
      self.velocity_xyz = np.column_stack([
        np.interp(mpc_t, model_t, self.velocity_xyz[:, 0]),
        np.interp(mpc_t, model_t, self.velocity_xyz[:, 1]),
        np.interp(mpc_t, model_t, self.velocity_xyz[:, 2]),
      ])
      self.plan_yaw = np.interp(mpc_t, model_t, self.plan_yaw)
      self.plan_yaw_rate = np.interp(mpc_t, model_t, self.plan_yaw_rate)
      self.v_plan = np.interp(mpc_t, model_t, self.v_plan)
      self.t_idxs = mpc_t
      self.v_ego = self.v_plan[0]

    cprint(f'position.x = {list(md.position.x)}')
    cprint(f'position.y = {list(md.position.y)}')
    cprint(f'position.t = {list(md.position.t)}')
    cprint(f'plan_yaw = {list(md.orientation.z)}')
    cprint(f'plan_yaw_rate = {list(md.orientationRate.z)}')
    cprint(f'velocity.x = {list(md.velocity.x)}')
    cprint(f'velocity.y = {list(md.velocity.y)}')
    cprint(f'speed_error = {speed_error}')
    cprint(f'car_speed = {list(car_speed)}')
    cprint(f'v_plan = {list(car_speed)}')
    cprint(f'v_ego = {self.v_ego}')

    # Lane change logic
    desire_state = md.meta.desireState
    cprint(f'desire_state = {desire_state}')
    # [pimpke] TODO: check if desire_state is of len 0
    if len(desire_state):
      self.l_lane_change_prob = desire_state[log.LateralPlan.Desire.laneChangeLeft]
      self.r_lane_change_prob = desire_state[log.LateralPlan.Desire.laneChangeRight]
    lane_change_prob = self.l_lane_change_prob + self.r_lane_change_prob
    cprint(f'lane_change_prob = {lane_change_prob}')
    cprint(f'latActive = {sm["carControl"].latActive}')
    self.DH.update(sm['carState'], sm['carControl'].latActive, lane_change_prob)

    self.lat_mpc.set_weights(PATH_COST, LATERAL_MOTION_COST,
                             LATERAL_ACCEL_COST, LATERAL_JERK_COST,
                             STEERING_RATE_COST)

    y_pts = self.path_xyz[:LAT_MPC_N+1, 1]
    heading_pts = self.plan_yaw[:LAT_MPC_N+1]
    yaw_rate_pts = self.plan_yaw_rate[:LAT_MPC_N+1]
    self.y_pts = y_pts

    assert len(y_pts) == LAT_MPC_N + 1
    assert len(heading_pts) == LAT_MPC_N + 1
    assert len(yaw_rate_pts) == LAT_MPC_N + 1
    lateral_factor = np.clip(self.factor1 - (self.factor2 * self.v_plan**2), 0.0, np.inf)
    p = np.column_stack([self.v_plan, lateral_factor])

    cprint(f'x0 = {list(self.x0)}')
    cprint(f'lateral_factor = {lateral_factor}')
    cprint(f'p = {list(p)}')
    cprint(f'y_pts = {list(y_pts)}')
    cprint(f'heading_pts = {list(heading_pts)}')
    cprint(f'yaw_rate_pts = {list(yaw_rate_pts)}')

    self.lat_mpc.run(self.x0,
                     p,
                     y_pts,
                     heading_pts,
                     yaw_rate_pts)
    # init state for next iteration
    # mpc.u_sol is the desired second derivative of psi given x0 curv state.
    # with x0[3] = measured_yaw_rate, this would be the actual desired yaw rate.
    # instead, interpolate x_sol so that x0[3] is the desired yaw rate for lat_control.
    self.x0[3] = interp(DT_MDL, self.t_idxs[:LAT_MPC_N + 1], self.lat_mpc.x_sol[:, 3])
    cprint(f'self.x0[3] = {self.x0[3]}')

    #  Check for infeasible MPC solution
    mpc_nans = np.isnan(self.lat_mpc.x_sol[:, 3]).any()
    cprint(f'mpc_nans = {mpc_nans}')
    cprint(f'solution_status = {self.lat_mpc.solution_status}')

    t = sec_since_boot()
    if mpc_nans or self.lat_mpc.solution_status != 0:
      self.reset_mpc()
      self.x0[3] = measured_curvature * self.v_ego
      if t > self.last_cloudlog_t + 5.0:
        self.last_cloudlog_t = t
        cloudlog.warning("Lateral mpc - nan: True")

    if self.lat_mpc.cost > 1e6 or mpc_nans:
      self.solution_invalid_cnt += 1
    else:
      self.solution_invalid_cnt = 0

    cprint(f'solution_invalid_cnt = {self.solution_invalid_cnt}')

    curr_max_yaw = np.abs(self.plan_yaw).max()
    if curr_max_yaw > self.max_yaw:
      self.max_yaw = curr_max_yaw
      cprint(f'max_yaw = {self.max_yaw}')

    curvatures = (self.lat_mpc.x_sol[0:CONTROL_N, 3] / self.v_ego).tolist()
    for i in range(len(curvatures)):
      curvatures[i] = abs(curvatures[i])

      if curvatures[i] > self.max_curvature:
        self.max_curvature = curvatures[i]
        cprint(f'max_curvature = {self.max_curvature}')

      if curvatures[i] > 0.2:
        x = 5

  def publish(self, sm, pm):
    cprint('========== LateralPlanner.publish ==========')

    plan_solution_valid = self.solution_invalid_cnt < 2
    cprint(f'plan_solution_valid = {plan_solution_valid}')

    plan_send = messaging.new_message('lateralPlan')
    # [pimpke] TODO: check if the plan is valid
    plan_send.valid = sm.all_checks(service_list=['carState', 'controlsState', 'modelV2'])

    lateralPlan = plan_send.lateralPlan
    # [pimpke] TODO: check if logMonoTime is reasonable
    lateralPlan.modelMonoTime = sm.logMonoTime['modelV2']
    lateralPlan.dPathPoints = self.y_pts.tolist()
    lateralPlan.psis = self.lat_mpc.x_sol[0:CONTROL_N, 2].tolist()

    cprint(f'dPathPoints = {lateralPlan.dPathPoints}')
    cprint(f'psis = {lateralPlan.psis}')

    lateralPlan.curvatures = (self.lat_mpc.x_sol[0:CONTROL_N, 3]/self.v_ego).tolist()

    u_sol = self.lat_mpc.u_sol[0:CONTROL_N - 1]
    lateralPlan.curvatureRates = [float(x/self.v_ego) for x in u_sol] + [0.0]

    cprint(f'u_sol = {list(u_sol)}')
    cprint(f'curvatures = {lateralPlan.curvatures}')
    cprint(f'curvatureRates = {lateralPlan.curvatureRates}')

    lateralPlan.mpcSolutionValid = bool(plan_solution_valid)
    lateralPlan.solverExecutionTime = self.lat_mpc.solve_time

    cprint(f'mpcSolutionValid = {lateralPlan.mpcSolutionValid}')

    lateralPlan.desire = self.DH.desire
    lateralPlan.useLaneLines = False
    lateralPlan.laneChangeState = self.DH.lane_change_state
    lateralPlan.laneChangeDirection = self.DH.lane_change_direction

    cprint(f'desire = {lateralPlan.desire}')
    cprint(f'laneChangeState = {lateralPlan.laneChangeState}')
    cprint(f'laneChangeDirection = {lateralPlan.laneChangeDirection}')

    pm.send('lateralPlan', plan_send)
