import unittest

from cereal import car
from common.clock import sec_since_boot
from selfdrive.car.tests.test_common import get_car_interface
from selfdrive.car.tests.test_common import get_logmessages
from selfdrive.controls.controlsd import Controls


class TestControlsSendCan(unittest.TestCase):
  def test(self):
    car_interface = get_car_interface()

    controls = Controls(CI=car_interface)
    controls.initialized = True

    CS = car.CarState.new_message()
    CC, lac_log = controls.state_control(CS)

    CC.latActive = True
    CC.actuators.accel = 2.0
    controls.CI.CS.out.steeringTorqueEps = 20.0
    controls.CI.CS.out.steeringAngleDeg = 100.0

    controls.publish_logs(CS, sec_since_boot(), CC, lac_log)

    CS = controls.data_sample()
    controls.update_events(CS)
    controls.step()

    j = get_logmessages()[0]

    self.assertAlmostEqual(j['msg']['content']['STEERING_MODULE_ADAS']['STEER_ANGLE']['vl'], 100.0)
    self.assertAlmostEqual(j['msg']['content']['ACC_CMD']['ACCEL_CMD']['vl'], 30.0)
