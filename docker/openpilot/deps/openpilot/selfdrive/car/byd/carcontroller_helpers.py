from collections import deque
import time
import os
from common.numpy_fast import clip
import numpy as np

from selfdrive.car import apply_std_steer_angle_limits
from selfdrive.wingfin_common import InferenceConfigParser
from selfdrive.car import AngleRateLimit

INF_CONFIG_PARSER = InferenceConfigParser()

def calculate_x(v_ego):
  """
  Calculates the clipping limit x based on v_ego.
  """
  if v_ego <= 1.0:
    return np.interp(v_ego, [0.0, 1.0], [340.0, 300.0])
  elif v_ego <= 8.0:
    return np.interp(v_ego, [1.0, 8.0], [300.0, 160.0])
  else:
    return 200.0

class CarControllerParams:
  single_angle_rate_limit_mode = INF_CONFIG_PARSER.get_value('single_angle_rate_limit_mode', False)
  if single_angle_rate_limit_mode:
    ANGLE_RATE_LIMIT = INF_CONFIG_PARSER.get_value('single_angle_rate_limit', 10.0)
    ANGLE_RATE_LIMIT_UP = ANGLE_RATE_LIMIT
    ANGLE_RATE_LIMIT_DOWN = ANGLE_RATE_LIMIT
  else:
    speeds_up = INF_CONFIG_PARSER.get_value('speeds_up', [0., 5., 15.])
    speeds_down = INF_CONFIG_PARSER.get_value('speeds_down', [0., 5., 15.])
    angle_rates_up = INF_CONFIG_PARSER.get_value('angle_rates_up', [8., 6., 4.])
    angle_rates_down = INF_CONFIG_PARSER.get_value('angle_rates_down', [8., 6., 4.])
    ANGLE_RATE_LIMIT_UP = AngleRateLimit(speed_bp=speeds_up, angle_v=angle_rates_up)
    ANGLE_RATE_LIMIT_DOWN = AngleRateLimit(speed_bp=speeds_down, angle_v=angle_rates_down)

  def __init__(self, CP):
    pass


class SpeedPID:
  # Speed PID controler, roughly fintuned for tests with fix speed
  def __init__(self, Kp = 0.5, Ki = 0.005, Kd = 0.25, setpoint = INF_CONFIG_PARSER.get_value('fixed_speed_value_kmh', 0.0)/3.6):
    self.Kp = Kp
    self.Ki = Ki
    self.Kd = Kd
    self.setpoint = setpoint
    self.last_error = 0.0
    self.integral = 0.0
    self.last_time = time.time()

  def update(self, current_speed):
    current_time = time.time()
    dt = current_time - self.last_time

    if dt < 0:
      return 0.0
    error = self.setpoint - current_speed
    p_term = self.Kp * error
    self.integral += error * dt
    i_term = self.Ki * self.integral
    derivative = (error - self.last_error)/dt
    d_term = self.Kd * derivative
    self.last_time = current_time
    self.last_error = error

    pid_output = p_term + i_term +d_term

    return pid_output

  def set_target_speed(self, new_speed):
    self.integral = 0.0
    self.setpoint = new_speed

def map_pid_to_acc(pid_output):
    MAX_ACC = 0.5
    acc_value = 0.0
    if pid_output > 0:
      acc_value = min(pid_output, MAX_ACC)
    elif pid_output < 0:
      acc_value = max(pid_output, -MAX_ACC)

    return acc_value


class CarCommandValueModificator:
  def __init__(self):
    self.pid_speed = SpeedPID()
    self.CS = None
    self.car_control_params = CarControllerParams
    self.input_requested_angle = 0.0
    self.output_angle = 0.0
    self.input_requested_acceleration = 0.0
    self.output_acceleration = 0.0
    run_dir = os.environ.get('RUN_DIR', '/tmp')
    self.log_file = open(os.path.join(run_dir, 'carcontroller_command_modification.txt'), 'w')

    self.safty_clipping_limit = 15.0
    self.turn_buffer = deque(maxlen=10)
    self.buffer_wrights = [0.75, 0.15, 0.10]
    self.index = 0

    #low speed
    self.max_low_speed = 0.5 #m/s
    self.angle_max_low_speed = 50.0 #deg
    self.angle_min_low_speed = 10.0 #deg
    


  def update_car_state(self, CS):
    self.CS = CS
    self.log_file.write(f'------- {self.index} -----------------\n')
    self.index+=1

  def modify_steering_angle(self, system_requested_angle):
    self.input_requested_angle = system_requested_angle
    self.output_angle = system_requested_angle
    if INF_CONFIG_PARSER.get_value('set_fix_st_angle', False):
      self.output_angle = INF_CONFIG_PARSER.get_value('fixed_angle_value',0.0)
      self.log_file.write(f'fixed angle output {self.output_angle}\n')
      return

    # angle rate limitsfec
    self._apply_angle_rate_limits()

    # apply safty clipping
    self._apply_saft_clipping()

    #clip per speed max angle
    self._clip_steer_angle()

    #smooth output angle if selected
    self._do_output_angle_buffer_smoothing()

    #apply max angle clipping to prevent big steering angles on low speeds
    self._apply_low_speed_angle_clipping()


  def _apply_low_speed_angle_clipping(self):
    if INF_CONFIG_PARSER.get_value('clip_max_angle_at_low_speed', True):
      old_value = self.output_angle
      current_speed= self.CS.out.vEgo
      if current_speed < self.max_low_speed:
        max_output_angle = np.interp(current_speed, [0.0, self.max_low_speed], [self.angle_min_low_speed, self.angle_max_low_speed])
        self.output_angle =  float(np.clip(self.output_angle, -max_output_angle, max_output_angle))
        if abs(old_value - self.output_angle) > 0.1:
          self.log_file.write(f'low speed clipping applied from {old_value} to {self.output_angle}\n')


  def _apply_angle_rate_limits(self):
    old_value = self.output_angle
    self.output_angle = apply_std_steer_angle_limits(
      self.output_angle,
      self.CS.out.steeringAngleDeg,
      self.CS.out.vEgo,
      self.car_control_params,
      self.log_file
    )
    if abs(old_value - self.output_angle) > 0.1:
      self.log_file.write(f'angle rate limit applied from {old_value} to {self.output_angle}\n')

  def _do_output_angle_buffer_smoothing(self):
    if INF_CONFIG_PARSER.get_value('smooth_buffer_st_angle_output', False):
      old_value = self.output_angle
      self.turn_buffer.append(old_value)
      if len(self.turn_buffer) < len(self.buffer_wrights):
        self.output_angle = old_value
      else:
        total = 0
        for index, w in enumerate(self.buffer_wrights):
          total += w*self.turn_buffer[-index-1]
        self.output_angle = total
      if abs(old_value - self.output_angle) > 0.1:
        self.log_file.write(f'buffering applied from {old_value} to {self.output_angle}\n')

  def _clip_steer_angle(self):
    if INF_CONFIG_PARSER.get_value('clip_max_angle_per_speed', True):
      old_value = self.output_angle
      x = calculate_x(self.CS.out.vEgo)
      self.output_angle =  float(np.clip(self.output_angle, -x, x))
      if abs(old_value - self.output_angle) > 0.1:
        self.log_file.write(f'clipping applied to prevent EC fault from {old_value} to {self.output_angle}\n')

  def _apply_saft_clipping(self):
    if not self.car_control_params.single_angle_rate_limit_mode:
      old_value = self.output_angle
      self.output_angle = clip(self.output_angle,
                               self.CS.out.steeringAngleDeg - self.safty_clipping_limit ,
                               self.CS.out.steeringAngleDeg + self.safty_clipping_limit )
      if abs(self.output_angle - old_value) > 0.1:
        self.log_file.write(f'safty clipping applied from {old_value} to {self.output_angle}\n')



  def modify_acc_value(self, system_requested_acceleration):
    self.input_requested_acceleration = system_requested_acceleration
    self.output_acceleration = system_requested_acceleration
    if INF_CONFIG_PARSER.get_value('set_fixed_speed', False) and self.CS.out.vEgo > 1.0:
      pid_out = self.pid_speed.update(self.CS.out.vEgo)
      self.output_acceleration = map_pid_to_acc(pid_out)
      self.log_file.write(f'Accleration changed from {system_requested_acceleration} to {self.output_acceleration}\n')


