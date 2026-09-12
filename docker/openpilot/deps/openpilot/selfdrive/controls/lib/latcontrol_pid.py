import math

from cereal import log
import pickle
import socket
import os
import json
from selfdrive.controls.lib.latcontrol import LatControl
from selfdrive.controls.lib.pid import PIDController

from common.dbg_config import DbgConfig


class LatControlPID(LatControl):
  def __init__(self, CP, CI):
    super().__init__(CP, CI)
    self.pid = PIDController((CP.lateralTuning.pid.kpBP, CP.lateralTuning.pid.kpV),
                             (CP.lateralTuning.pid.kiBP, CP.lateralTuning.pid.kiV),
                             k_f=CP.lateralTuning.pid.kf, pos_limit=self.steer_max, neg_limit=-self.steer_max)
    self.get_steer_feedforward = CI.get_steer_feedforward_function()

    if DbgConfig.logs_update:
      server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
      address = (os.environ.get("HOST"), int(os.environ.get("OPENPILOT_PID_LOGS_PORT")))
      server_socket.bind(address)
      server_socket.listen(1)
      print(f"Listening for connections on {address}")
      client_socket, client_address = server_socket.accept()
      print(f"Accepted connection from {client_address}")
      latcontrol_torque_logs_fp = client_socket.recv(1024).decode()
      print(f"Received latcontrol_torque_logs_fp: {latcontrol_torque_logs_fp}")
      client_socket.close()
      self.latcontrol_torque_logs_file = open(latcontrol_torque_logs_fp, 'w+b')
      os.chmod(latcontrol_torque_logs_fp, 0o777)

      self.socket_receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
      address = (os.environ.get("HOST"), int(os.environ.get("OPENPILOT_FRAME_COUNT_PID_PORT")))
      self.socket_receiver.bind(address)
      self.socket_receiver.setblocking(False)

  def reset(self):
    super().reset()
    self.pid.reset()

  def update(self, active, CS, VM, params, last_actuators, steer_limited, desired_curvature, desired_curvature_rate, llk):
    pid_log = log.ControlsState.LateralPIDState.new_message()
    pid_log.steeringAngleDeg = float(CS.steeringAngleDeg)
    pid_log.steeringRateDeg = float(CS.steeringRateDeg)

    angle_steers_des_no_offset = math.degrees(VM.get_steer_from_curvature(-desired_curvature, CS.vEgo, params.roll))
    print(f'angle_steers_des_no_offset = {angle_steers_des_no_offset}')
    angle_steers_des = angle_steers_des_no_offset + params.angleOffsetDeg
    print(f'angle_steers_des = {angle_steers_des}')
    error = angle_steers_des - CS.steeringAngleDeg
    print(f'error = {error}')

    pid_log.steeringAngleDesiredDeg = angle_steers_des
    pid_log.angleError = error

    latcontrol_torque_log_dict = dict()
    if not active:
      print('not active')
      output_steer = 0.0
      pid_log.active = False
      self.pid.reset()
      if DbgConfig.logs_update:
        latcontrol_torque_log_dict = {
          'pid_log_active': False,
          'pid_log_p': 0,
          'pid_log_i': 0,
          'pid_log_f': 0,
          'pid_log_output': 0,
          'pid_log_steeringAngleDeg': 0,
          'pid_log_steeringAngleDesiredDeg': 0,
          'pid_log_steeringRateDeg': 0,
          'pid_log_angleError': 0,
          'pid_log_saturated': False,
          'desired_curvature': 0,
          'steeringAngleDeg': 0,
          'angle_steers_des_no_offset': 0
        }
    else:
      # offset does not contribute to resistive torque
      steer_feedforward = self.get_steer_feedforward(angle_steers_des_no_offset, CS.vEgo)

      output_steer = self.pid.update(error, override=CS.steeringPressed,
                                     feedforward=steer_feedforward, speed=CS.vEgo)
      pid_log.active = True
      pid_log.p = self.pid.p
      pid_log.i = self.pid.i
      pid_log.f = self.pid.f
      pid_log.output = output_steer
      pid_log.saturated = self._check_saturation(self.steer_max - abs(output_steer) < 1e-3, CS, steer_limited)

      if DbgConfig.logs_update:
        latcontrol_torque_log_dict = {
          'pid_log_active': pid_log.active,
          'pid_log_p': pid_log.p,
          'pid_log_i': pid_log.i,
          'pid_log_f': pid_log.f,
          'pid_log_output': pid_log.output,
          'pid_log_steeringAngleDeg': pid_log.steeringAngleDeg,
          'pid_log_steeringAngleDesiredDeg': pid_log.steeringAngleDesiredDeg,
          'pid_log_steeringRateDeg': pid_log.steeringRateDeg,
          'pid_log_angleError': pid_log.angleError,
          'pid_log_saturated': pid_log.saturated,
          'desired_curvature': desired_curvature,
          'steeringAngleDeg': CS.steeringAngleDeg,
          'angle_steers_des_no_offset': angle_steers_des_no_offset
        }

      try:
        data, _ = self.socket_receiver.recvfrom(1024)
        deserialized_data = json.loads(data.decode('utf-8'))
        latcontrol_torque_log_dict['frame_counter'] = deserialized_data['frame_counter']
        latcontrol_torque_log_dict['timestamp'] = deserialized_data['timestamp']
        pickle.dump(latcontrol_torque_log_dict, self.latcontrol_torque_logs_file, protocol=pickle.HIGHEST_PROTOCOL)
        self.latcontrol_torque_logs_file.flush()

      except BlockingIOError:
        pass  # No data to read, ignore and continue
      except json.JSONDecodeError:
        print("Received data isn't valid JSON, ignoring")
      except UnicodeDecodeError:
        print("Received data can't be decoded as UTF-8, ignoring")
      except KeyError as e:
        print(f"Expected key {e} not found in the data, ignoring")
      except Exception as e:
        print(f"An unexpected error occurred: {e}")

    print(f'pid_log = {pid_log.to_dict()}')
    print(f'output_steer = {output_steer}')

    return output_steer, angle_steers_des, pid_log
