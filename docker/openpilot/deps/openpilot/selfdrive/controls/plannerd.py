#!/usr/bin/env python3
import numpy as np
import socket
import pickle
import json
import os
from cereal import car
from common.params import Params
from common.realtime import Priority, config_realtime_process
from common.dbg_config import DbgConfig
from system.swaglog import cloudlog
from selfdrive.modeld.constants import T_IDXS
from selfdrive.controls.lib.longitudinal_planner import LongitudinalPlanner
from selfdrive.controls.lib.lateral_planner import LateralPlanner
import cereal.messaging as messaging


def cumtrapz(x, t):
  return np.concatenate([[0], np.cumsum(((x[0:-1] + x[1:]) / 2) * np.diff(t))])

def publish_ui_plan(sm, pm, lateral_planner, longitudinal_planner):
  plan_odo = cumtrapz(longitudinal_planner.v_desired_trajectory_full, T_IDXS)
  model_odo = cumtrapz(lateral_planner.v_plan, T_IDXS)

  ui_send = messaging.new_message('uiPlan')
  # [pimpke] TODO: check if ui plan is valid
  ui_send.valid = sm.all_checks(service_list=['carState', 'controlsState', 'modelV2'])
  uiPlan = ui_send.uiPlan
  # [pimpke] TODO: check if frameId is correctly increasing here
  uiPlan.frameId = sm['modelV2'].frameId
  uiPlan.position.x = np.interp(plan_odo, model_odo, lateral_planner.lat_mpc.x_sol[:, 0]).tolist()
  uiPlan.position.y = np.interp(plan_odo, model_odo, lateral_planner.lat_mpc.x_sol[:, 1]).tolist()
  uiPlan.position.z = np.interp(plan_odo, model_odo, lateral_planner.path_xyz[:, 2]).tolist()
  uiPlan.accel = longitudinal_planner.a_desired_trajectory_full.tolist()
  pm.send('uiPlan', ui_send)

def plannerd_thread(sm=None, pm=None):
  config_realtime_process(5, Priority.CTRL_LOW)

  if DbgConfig.logs_update:
    address = (os.environ.get("HOST"), int(os.environ.get("OPENPILOT_LOGS_PORT")))

    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.bind(address)
    server_socket.listen(1)
    print(f"Listening for connections on {address[0]}:{address[1]}")
    client_socket, client_address = server_socket.accept()
    print(f"Accepted connection from {client_address}")
    openpilot_logs_fp = client_socket.recv(1024).decode()
    print(f"Received openpilot_logs_fp: {openpilot_logs_fp}")
    client_socket.close()
    openpilot_logs_file = open(openpilot_logs_fp, 'w+b')
    os.chmod(openpilot_logs_fp, 0o777)

    socket_receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    address = (os.environ.get("HOST"), int(os.environ.get("OPENPILOT_FRAME_COUNT_PORT")))
    socket_receiver.bind(address)
    socket_receiver.setblocking(False)
  else:
    frame_counter = 0

  cloudlog.info("plannerd is waiting for CarParams")
  params = Params()

  if DbgConfig.logs_update:
    if int(os.environ.get("FORCE_ROSBAG_REPLAY", "0")) == 1:
      class Object(object):
        pass

      STD_CARGO_KG = 136.
      CP = Object()
      CP.openpilotLongitudinalControl = True
      CP.steerRatio = 13.0
      CP.wheelbase = 2.72
      CP.centerToFront = CP.wheelbase * 0.44
      CP.mass = 2090. + STD_CARGO_KG
      # constants taken from scale_tire_stiffness
      CP.tireStiffnessRear = ((202500 * 0.9871) * CP.mass / (1326. + STD_CARGO_KG) *
                              (CP.centerToFront / CP.wheelbase) / (2.70 * 0.4 / 2.70))
      CP.carName = 'mock'
    else:
      CP = car.CarParams.from_bytes(params.get("CarParams", block=True))
  else:
    CP = car.CarParams.from_bytes(params.get("CarParams", block=True))

  cloudlog.info("plannerd got CarParams: %s", CP.carName)

  longitudinal_planner = LongitudinalPlanner(CP)
  lateral_planner = LateralPlanner(CP)

  if sm is None:
    # [pimpke] TODO: check what the poll list does
    sm = messaging.SubMaster(['carControl', 'carState', 'controlsState', 'radarState', 'modelV2'],
                             poll=['radarState', 'modelV2'], ignore_avg_freq=['radarState'])

  if pm is None:
    pm = messaging.PubMaster(['longitudinalPlan', 'lateralPlan', 'uiPlan'])

  while True:
    sm.update()

    if sm.updated['modelV2']:
      modelV2_frameId = sm["modelV2"].frameId
      lateral_planner.update(sm)
      lateral_planner.publish(sm, pm)
      longitudinal_planner.update(sm)
      longitudinal_planner.publish(sm, pm)
      publish_ui_plan(sm, pm, lateral_planner, longitudinal_planner)

      if DbgConfig.logs_update:
        try:
          data, _ = socket_receiver.recvfrom(1024)
          deserialized_data = json.loads(data.decode('utf-8'))

          # noinspection PyUnresolvedReferences
          openpilot_log_dict = {
            'frame_counter': deserialized_data['frame_counter'],
            'timestamp': deserialized_data['timestamp'],
            'modelV2_frameId': modelV2_frameId,
            'vEgo': sm['carState'].vEgo,
            'gasPressed': sm['carState'].gasPressed,
            'brakePressed': sm['carState'].brakePressed,
            'clutchPressed': sm['carState'].clutchPressed,
            'cruiseState': sm['carState'].cruiseState.enabled,
            'actuators_steer': sm['carControl'].actuators.steer,
            'actuatorsOutput_steer': sm['carControl'].actuatorsOutput.steer,
            'actuatorsOutput_steerOutputCan': sm['carControl'].actuatorsOutput.steerOutputCan,
            'actuatorsOutput_steeroutputAngleDeg': sm['carControl'].actuatorsOutput.steeringAngleDeg,
            'logMonoTime': sm.logMonoTime['modelV2'],
            'actuators_steeringAngleDeg': sm['carControl'].actuators.steeringAngleDeg,
            'measured_curvature': sm['controlsState'].curvature,
            'measured_desired_curvature': sm['controlsState'].desiredCurvature,
            'measured_desired_curvature_rate': sm['controlsState'].desiredCurvatureRate,
            'activate': sm['controlsState'].active,
            'right_blinker': sm['carState'].rightBlinker,
            'leftBlinker': sm['carState'].leftBlinker,
            'steeringPressed': sm['carState'].steeringPressed,
            'steeringRateDeg': sm['carState'].steeringRateDeg,
            'steeringTorqueEps': sm['carState'].steeringTorqueEps,
            'steeringTorque': sm['carState'].steeringTorque,
            'leftBlindspot': sm['carState'].leftBlindspot,
            'rightBlindspot': sm['carState'].rightBlindspot,
            'lateral_planner_path_xyz': lateral_planner.path_xyz,
            'lateral_planner_plan_yaw': lateral_planner.plan_yaw,
            'lateral_planner_plan_yaw_rate': lateral_planner.plan_yaw_rate,
            'lateral_planner_v_ego': lateral_planner.v_ego,
            'lateral_planner_v_plan': lateral_planner.v_plan,
            'lateral_planner_y_pts': lateral_planner.y_pts,
            'lateral_planner_velocity_xyz': lateral_planner.velocity_xyz,
            'lateral_planner_x0': lateral_planner.x0,
            'lateral_planner_lat_mpc_u_sol': lateral_planner.lat_mpc.u_sol,
            'lateral_planner_lat_mpc_x_sol': lateral_planner.lat_mpc.x_sol,
            'lateral_planner_lat_mpc_yref': lateral_planner.lat_mpc.yref,
          }

          pickle.dump(openpilot_log_dict, openpilot_logs_file, protocol=pickle.HIGHEST_PROTOCOL)
          openpilot_logs_file.flush()

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


def main(sm=None, pm=None):
  try:
    import pydevd_pycharm
    pydevd_pycharm.settrace('localhost', port=1239, stdoutToServer=True, stderrToServer=True, suspend=False)
  except ConnectionRefusedError:
    print('[Warning] No debugger to attach to. Continuing...')

  plannerd_thread(sm, pm)


if __name__ == "__main__":
  main()
