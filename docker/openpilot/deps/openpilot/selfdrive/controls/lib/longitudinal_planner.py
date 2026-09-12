#!/usr/bin/env python3
import math
import numpy as np
from common.numpy_fast import clip, interp
from common.params import Params
from cereal import log

import cereal.messaging as messaging
from common.conversions import Conversions as CV
from common.filter_simple import FirstOrderFilter
from common.realtime import DT_MDL
from selfdrive.modeld.constants import T_IDXS
from selfdrive.controls.lib.longcontrol import LongCtrlState
from selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc import LongitudinalMpc, MIN_ACCEL, MAX_ACCEL
from selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc import T_IDXS as T_IDXS_MPC
from selfdrive.controls.lib.drive_helpers import V_CRUISE_MAX, V_CRUISE_SAFETY_THRESHOLD, CONTROL_N, get_speed_error
from system.swaglog import cloudlog

LON_MPC_STEP = 0.2  # first step is 0.2s
A_CRUISE_MIN = -1.2
A_CRUISE_MAX_VALS = [1.6, 1.2, 0.8, 0.6]
A_CRUISE_MAX_BP = [0., 10.0, 25., 40.]

# Lookup table for turns
_A_TOTAL_MAX_V = [1.7, 3.2]
_A_TOTAL_MAX_BP = [20., 40.]


def get_max_accel(v_ego):
  return interp(v_ego, A_CRUISE_MAX_BP, A_CRUISE_MAX_VALS)


should_print_in_longitudinal_planner = True


def cprint(*args, **kwargs):
  if should_print_in_longitudinal_planner:
    print(*args, **kwargs)


def limit_accel_in_turns(v_ego, angle_steers, a_target, CP):
  """
  This function returns a limited long acceleration allowed, depending on the existing lateral acceleration
  this should avoid accelerating when losing the target in turns
  """

  # FIXME: This function to calculate lateral accel is incorrect and should use the VehicleModel
  # The lookup table for turns should also be updated if we do this
  a_total_max = interp(v_ego, _A_TOTAL_MAX_BP, _A_TOTAL_MAX_V)
  a_y = v_ego ** 2 * angle_steers * CV.DEG_TO_RAD / (CP.steerRatio * CP.wheelbase)
  a_x_allowed = math.sqrt(max(a_total_max ** 2 - a_y ** 2, 0.))

  return [a_target[0], min(a_target[1], a_x_allowed)]


class LongitudinalPlanner:
  def __init__(self, CP, init_v=0.0, init_a=0.0):
    self.CP = CP
    self.mpc = LongitudinalMpc()
    self.fcw = False

    self.a_desired = init_a
    self.v_desired_filter = FirstOrderFilter(init_v, 2.0, DT_MDL)
    self.v_model_error = 0.0

    self.v_desired_trajectory = np.zeros(CONTROL_N)
    self.a_desired_trajectory = np.zeros(CONTROL_N)
    self.j_desired_trajectory = np.zeros(CONTROL_N)
    self.solverExecutionTime = 0.0
    self.params = Params()
    self.param_read_counter = 0
    self.read_param()
    self.personality = log.LongitudinalPersonality.standard

  def read_param(self):
    try:
      self.personality = int(self.params.get('LongitudinalPersonality'))
    except (ValueError, TypeError):
      self.personality = log.LongitudinalPersonality.standard

  @staticmethod
  def parse_model(model_msg, model_error):
    cprint(f'pos_x = {model_msg.position.x}')
    cprint(f'vel_x = {model_msg.velocity.x}')
    cprint(f'acc_x = {model_msg.acceleration.x}')
    # Interpolate the model trajectory onto the long-MPC grid from the model's OWN
    # sample times (model_msg.position.t), so any number of waypoints at any spacing
    # is supported (was: assumed exactly TRAJECTORY_SIZE points on the constant T_IDXS).
    n = len(model_msg.position.x)
    if (n >= 2 and len(model_msg.velocity.x) == n and len(model_msg.acceleration.x) == n):
      model_t = np.array(model_msg.position.t)
      x = np.interp(T_IDXS_MPC, model_t, model_msg.position.x) - model_error * T_IDXS_MPC
      v = np.interp(T_IDXS_MPC, model_t, model_msg.velocity.x) - model_error
      a = np.interp(T_IDXS_MPC, model_t, model_msg.acceleration.x)
      j = np.zeros(len(T_IDXS_MPC))
    else:
      x = np.zeros(len(T_IDXS_MPC))
      v = np.zeros(len(T_IDXS_MPC))
      a = np.zeros(len(T_IDXS_MPC))
      j = np.zeros(len(T_IDXS_MPC))
    return x, v, a, j

  def update(self, sm):
    cprint('========== update ==========')
    cprint(f'frameId = {sm["modelV2"].frameId}')
    if self.param_read_counter % 50 == 0:
      self.read_param()
    self.param_read_counter += 1
    self.mpc.mode = 'blended' if sm['controlsState'].experimentalMode else 'acc'

    v_ego = sm['carState'].vEgo
    v_cruise_kph = sm['controlsState'].vCruise
    v_cruise_kph = min(v_cruise_kph, V_CRUISE_MAX)
    v_cruise = v_cruise_kph * CV.KPH_TO_MS
    cprint(f'v_cruise (m/s) = {v_cruise}')
    long_control_off = sm['controlsState'].longControlState == LongCtrlState.off
    force_slow_decel = sm['controlsState'].forceDecel

    cprint(f'long_control_off = {long_control_off}')
    cprint(f'controlsState.enabled = {sm["controlsState"].enabled}')
    cprint(f'force_slow_decel = {sm["controlsState"].forceDecel}')

    # Reset current state when not engaged, or user is controlling the speed
    reset_state = long_control_off if self.CP.openpilotLongitudinalControl else not sm['controlsState'].enabled

    cprint(f'reset_state = {reset_state}')

    # No change cost when user is controlling the speed, or when standstill
    prev_accel_constraint = not (reset_state or sm['carState'].standstill)

    if self.mpc.mode == 'acc':
      accel_limits = [A_CRUISE_MIN, get_max_accel(v_ego)]
      accel_limits_turns = limit_accel_in_turns(v_ego, sm['carState'].steeringAngleDeg, accel_limits, self.CP)
    else:
      accel_limits = [MIN_ACCEL, MAX_ACCEL]
      accel_limits_turns = [MIN_ACCEL, MAX_ACCEL]

    if reset_state:
      self.v_desired_filter.x = v_ego
      # Clip aEgo to cruise limits to prevent large accelerations when becoming active
      self.a_desired = clip(sm['carState'].aEgo, accel_limits[0], accel_limits[1])

    # Prevent divergence, smooth in current v_ego
    self.v_desired_filter.x = max(0.0, self.v_desired_filter.update(v_ego))
    # Compute model v_ego error
    # [pimpke] TODO: check if this is 0
    self.v_model_error = get_speed_error(sm['modelV2'], v_ego)

    cprint(f'a_desired = {self.a_desired}')
    cprint(f'v_desired_filter.x = {self.v_desired_filter.x}')
    cprint(f'v_model_error = {self.v_model_error}')

    # [pimpke] TODO: see a proper way to workaround this
    # if force_slow_decel:
    #   v_cruise = 0.0

    # clip limits, cannot init MPC outside of bounds
    accel_limits_turns[0] = min(accel_limits_turns[0], self.a_desired + 0.05)
    accel_limits_turns[1] = max(accel_limits_turns[1], self.a_desired - 0.05)

    self.mpc.set_weights(prev_accel_constraint, personality=self.personality)
    self.mpc.set_accel_limits(accel_limits_turns[0], accel_limits_turns[1])
    self.mpc.set_cur_state(self.v_desired_filter.x, self.a_desired)
    x, v, a, j = self.parse_model(sm['modelV2'], self.v_model_error)
    cprint(f'x = {x}')
    cprint(f'v = {v}')
    cprint(f'a = {a}')
    cprint(f'j = {j}')

    import sys
    sys.path.append('/opt/project/common')
    from inference_config import InferenceConfig
    sys.path.pop()

    if InferenceConfig.o_flag:
      if np.max(v) > (V_CRUISE_MAX + V_CRUISE_SAFETY_THRESHOLD) / 3.6 or np.average(v) > V_CRUISE_MAX / 3.6:
        cprint('safe_speed = 0')
        # x = np.array([0, v_ego * 0.5, v_ego, v_ego * 1.5, v_ego * 2])
        # v = np.array([v_ego, v_ego, v_ego, v_ego, v_ego])
        x = np.array([0, V_CRUISE_MAX / 3.6 * 0.5, V_CRUISE_MAX / 3.6, V_CRUISE_MAX / 3.6 * 1.5, V_CRUISE_MAX / 3.6 * 2])
        v = np.array([V_CRUISE_MAX / 3.6] * 5)
        a = np.array([0., 0., 0., 0., 0.])
      else:
        cprint('safe_speed = 1')

    self.mpc.update(sm['radarState'], v_cruise, x, v, a, j, personality=self.personality)

    self.v_desired_trajectory_full = np.interp(T_IDXS, T_IDXS_MPC, self.mpc.v_solution)
    self.a_desired_trajectory_full = np.interp(T_IDXS, T_IDXS_MPC, self.mpc.a_solution)
    self.v_desired_trajectory = self.v_desired_trajectory_full[:CONTROL_N]
    self.a_desired_trajectory = self.a_desired_trajectory_full[:CONTROL_N]
    self.j_desired_trajectory = np.interp(T_IDXS[:CONTROL_N], T_IDXS_MPC[:-1], self.mpc.j_solution)

    # TODO counter is only needed because radar is glitchy, remove once radar is gone
    self.fcw = self.mpc.crash_cnt > 2 and not sm['carState'].standstill
    if self.fcw:
      cloudlog.info("FCW triggered")

    # Interpolate 0.05 seconds and save as starting point for next iteration
    a_prev = self.a_desired
    self.a_desired = float(interp(DT_MDL, T_IDXS[:CONTROL_N], self.a_desired_trajectory))
    self.v_desired_filter.x = self.v_desired_filter.x + DT_MDL * (self.a_desired + a_prev) / 2.0

  def publish(self, sm, pm):
    cprint('========== publish ==========')
    plan_send = messaging.new_message('longitudinalPlan')

    plan_send.valid = sm.all_checks(service_list=['carState', 'controlsState'])

    longitudinalPlan = plan_send.longitudinalPlan
    # [pimpke] TODO: check if modelMonoTime and processingDelay are reasonable
    longitudinalPlan.modelMonoTime = sm.logMonoTime['modelV2']
    longitudinalPlan.processingDelay = (plan_send.logMonoTime / 1e9) - sm.logMonoTime['modelV2']

    longitudinalPlan.speeds = self.v_desired_trajectory.tolist()
    longitudinalPlan.accels = self.a_desired_trajectory.tolist()
    longitudinalPlan.jerks = self.j_desired_trajectory.tolist()

    cprint(f'modelMonoTime = {longitudinalPlan.modelMonoTime}')
    cprint(f'processingDelay = {longitudinalPlan.processingDelay}')
    cprint(f'speeds = {longitudinalPlan.speeds}')
    cprint(f'accels = {longitudinalPlan.accels}')
    cprint(f'jerks = {longitudinalPlan.jerks}')
    cprint(f'plan_send.valid = {plan_send.valid}')

    longitudinalPlan.hasLead = sm['radarState'].leadOne.status
    longitudinalPlan.longitudinalPlanSource = self.mpc.source
    longitudinalPlan.fcw = self.fcw

    cprint(f'hasLead = {longitudinalPlan.hasLead}')
    cprint(f'fcs = {longitudinalPlan.fcw}')

    longitudinalPlan.solverExecutionTime = self.mpc.solve_time
    longitudinalPlan.personality = self.personality

    cprint(f'personality = {longitudinalPlan.personality}')

    pm.send('longitudinalPlan', plan_send)
