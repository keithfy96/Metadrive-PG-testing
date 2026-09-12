#!/usr/bin/env python3
from cereal import car
from common.conversions import Conversions as CV
from selfdrive.car import STD_CARGO_KG, scale_rot_inertia, scale_tire_stiffness, gen_empty_fingerprint, get_safety_config
from selfdrive.car.interfaces import CarInterfaceBase
from selfdrive.car.byd.values import CAR, HUD_MULTIPLIER
from selfdrive.wingfin_common import get_inference_config, InferenceConfigParser
from selfdrive.controls.lib.desire_helper import LANE_CHANGE_SPEED_MIN

EventName = car.CarEvent.EventName

INF_CONFIG_PARSER = InferenceConfigParser()

class CarInterface(CarInterfaceBase):
  @staticmethod
  def _get_params(ret, candidate, fingerprint, car_fw, experimental_long, docs):
    ret.carName = "byd"
    ret.safetyConfigs = [get_safety_config(car.CarParams.SafetyModel.byd)]
    ret.safetyConfigs[0].safetyParam = 1
    ret.transmissionType = car.CarParams.TransmissionType.automatic
    ret.enableDsu = False                  # driving support unit

    ret.steerLimitTimer = 0.1              # time before steerLimitAlert is issued
    ret.steerControlType = car.CarParams.SteerControlType.angle
    ret.steerActuatorDelay = 0.01          # Steering wheel actuator delay in seconds

    ret.openpilotLongitudinalControl = True

    if candidate == CAR.ATTO3:
      ret.wheelbase = 2.72
      ret.steerRatio = INF_CONFIG_PARSER.get_value('OP_steerRatio', 16.36)
      ret.centerToFront = ret.wheelbase * 0.5 # was 0.44 but for EV car (tesla) is set to 0.5 battery vs pertlos engine
      tire_stiffness_factor = 0.9871
      ret.mass = 1700. + STD_CARGO_KG # in all calc is used weight of empty car with all fluids
      ret.wheelSpeedFactor = HUD_MULTIPLIER           # the HUD odo is exactly 1 to 1 with gps speed

      # currently not in use, byd is using stock long
      ret.longitudinalTuning.kpBP = [0., 5., 20.]
      # ret.longitudinalTuning.kpV = [1.5, 1.2, 1.0] # IMPORTANT
      ret.longitudinalTuning.kpV = [2.2, 2.0, 1.8]
      ret.longitudinalActuatorDelayLowerBound = 0.3 # IMPORTANT but don't spend time on it
      ret.longitudinalActuatorDelayUpperBound = 0.4 # IMPORTANT but don't spend time on it

    else:
      ret.dashcamOnly = True
      ret.safetyModel = car.CarParams.SafetyModel.noOutput

    # currently not in use, byd is using stock long
    ret.longitudinalTuning.deadzoneBP = [0., 8.05, 20]
    ret.longitudinalTuning.deadzoneV = [0., 0., 0.]
    ret.longitudinalTuning.kiBP = [0., 5., 20.]
    #ret.longitudinalTuning.kiV = [0.38, 0.3, 0.12] # IMPORTANT
    ret.longitudinalTuning.kiV = [0.45, 0.4, 0.32] # IMPORTANT

    ret.minEnableSpeed = -1
    ret.enableBsm = True
    ret.stoppingDecelRate = 0.02 # reach stopping target smoothly

    ret.rotationalInertia = scale_rot_inertia(ret.mass, ret.wheelbase)
    ret.tireStiffnessFront, ret.tireStiffnessRear = scale_tire_stiffness(ret.mass, ret.wheelbase, ret.centerToFront, tire_stiffness_factor=tire_stiffness_factor)

    return ret

  # returns a car.CarState
  def _update(self, c):
    ret = self.CS.update(self.cp)

    # events
    events = self.create_common_events(ret)
    ret.events = events.to_msg()

    return ret

  # pass in a car.CarControl to be called at 100hz
  def apply(self, c, now_nanos):
    return self.CC.update(c, self.CS, now_nanos)
