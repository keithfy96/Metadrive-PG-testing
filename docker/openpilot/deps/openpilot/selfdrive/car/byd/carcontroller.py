from selfdrive.car.byd.bydcan import create_wheelspeed_spoof_command, create_can_steer_command, create_accel_command, send_buttons, create_lkas_hud
from selfdrive.car.byd.values import CAR, DBC
from opendbc.can.packer import CANPacker
from common.numpy_fast import clip
from selfdrive.car.byd.carcontroller_helpers import *
from collections import deque
import os

#ECU_FAULT_ANGLE = 260 # degress #NOT USED ANY MORE

class CarController:
  def __init__(self, dbc_name, CP, VM):
    self.packer = CANPacker(DBC[CP.carFingerprint]['pt'])
    self.steer_rate_limited = False
    self.frame = 0
    self.CP = CP
    self.lka_active = False
    run_dir = os.environ.get('RUN_DIR', '/tmp')
    self.log_file = open(os.path.join(run_dir, 'carcontroller.txt'), 'w')
    self.current_angle = 0
    self.turn_max_reached = False
    self.speeed_reached = False
    self.command_modificator = CarCommandValueModificator()

  def update(self, CC, CS, now_nanos):
    can_sends = []
    actuators = CC.actuators
    hud_control = CC.hudControl
    enabled = CC.latActive

    # lkas user activation, cannot tie to lka_on state because it may deactivate itself
    if CS.lka_on:
      self.lka_active = True
    if not CS.lka_on and CS.lkas_rdy_btn:
      self.lka_active = False

    lat_active = enabled and self.lka_active and not CS.out.standstill
      #and not CS.out.steeringPressed and abs(CS.out.steeringAngleDeg) < ECU_FAULT_ANGLE

    system_requested_angle = 0
    self.log_file.write(f'{self.frame}\n')
    if (self.frame % 2) == 0:
      self.command_modificator.update_car_state(CS)
      if lat_active:
        self.log_file.write('steer command \n')
        self.command_modificator.modify_steering_angle(actuators.steeringAngleDeg)
        # assumption why eps fault:
        # 1. steer rate too high
        # 2. met with resistance while steering
        # 3. applied steer too far away from current steeringAngleDeg
      else:
        self.log_file.write('NA NA NA\n')

      if INF_CONFIG_PARSER.get_value('speed_spoofing_enabled', False):
        can_sends.append(create_wheelspeed_spoof_command(self.packer, (self.frame/2) % 16))

      can_sends.append(create_can_steer_command(self.packer, self.command_modificator.output_angle, lat_active, CS.out.standstill, CS.lkas_healthy, CS.lkas_rdy_btn, (self.frame/2) % 16))
      can_sends.append(create_lkas_hud(self.packer, enabled, CS.lss_state, CS.lss_alert, CS.tsr, \
        CS.abh, CS.passthrough, CS.HMA, CS.pt2, CS.pt3, CS.pt4, CS.pt5, self.lka_active, self.frame % 16))

      if self.CP.openpilotLongitudinalControl:
        long_active = (enabled and not CS.out.gasPressed) or INF_CONFIG_PARSER.get_value('set_fixed_speed', False)
        brake_hold = CS.out.standstill and actuators.accel < 0
        self.command_modificator.modify_acc_value(actuators.accel)
        can_sends.append(create_accel_command(self.packer, self.command_modificator.output_acceleration, long_active, brake_hold, (self.frame/2) % 16))

    new_actuators = actuators.copy()
    new_actuators.steeringAngleDeg = self.command_modificator.output_angle

    self.log_file.write('\n\n')
    self.frame += 1
    return new_actuators, can_sends
