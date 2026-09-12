from cereal import car
from selfdrive.car import STD_CARGO_KG, scale_rot_inertia, scale_tire_stiffness, get_safety_config


def build_tesla_m3_car_params() -> car.CarParams:
    # fork predates upstream Tesla support; hand-build with Model 3 LR AWD dynamics
    CP = car.CarParams.new_message()

    CP.carName = "tesla_model_3"
    CP.carFingerprint = "TESLA MODEL 3"
    # we don't read carControl from CAN; safetyModel is unused
    CP.safetyConfigs = [get_safety_config(car.CarParams.SafetyModel.noOutput)]

    CP.wheelbase = 3.005  # CARLA-measured (physics dump wheel positions), not the 2.875 spec
    # 50/50 makes openpilot's CoG the axle midpoint = the point the model predicts modelv2 for
    CP.centerToFront = CP.wheelbase * 0.5
    CP.mass = 1844.0 + STD_CARGO_KG
    CP.steerRatio = 12.0
    CP.steerRatioRear = 0.0
    tire_stiffness_factor = 1.0

    CP.rotationalInertia = scale_rot_inertia(CP.mass, CP.wheelbase)
    CP.tireStiffnessFront, CP.tireStiffnessRear = scale_tire_stiffness(
        CP.mass, CP.wheelbase, CP.centerToFront, tire_stiffness_factor=tire_stiffness_factor
    )

    CP.steerControlType = car.CarParams.SteerControlType.angle
    # bridge overrides these per-eval — sync-mode CARLA has no real EPS delay
    CP.steerActuatorDelay = 0.2
    CP.steerLimitTimer = 0.4
    CP.minSteerSpeed = 0.0

    # we feed accel to CARLA directly, but plannerd clips its plan against these
    CP.openpilotLongitudinalControl = True
    CP.minEnableSpeed = -1.0
    CP.pcmCruise = False
    CP.stopAccel = -2.0
    CP.stoppingDecelRate = 0.8
    # low threshold so blended-mode's brief positive v_plan at intersections
    # doesn't trip planned_stop (from which the "creep then decline" shape
    # never satisfies starting_condition)
    CP.vEgoStopping = 0.05
    CP.vEgoStarting = 0.5
    CP.stoppingControl = True
    CP.longitudinalActuatorDelayLowerBound = 0.15
    CP.longitudinalActuatorDelayUpperBound = 0.4
    CP.longitudinalTuning.kpBP = [0.0]
    CP.longitudinalTuning.kpV = [1.0]
    CP.longitudinalTuning.kiBP = [0.0]
    CP.longitudinalTuning.kiV = [1.0]
    CP.longitudinalTuning.deadzoneBP = [0.0]
    CP.longitudinalTuning.deadzoneV = [0.0]
    CP.longitudinalTuning.kf = 1.0

    return CP
