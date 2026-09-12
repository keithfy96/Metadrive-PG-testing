import unittest

from selfdrive.boardd.boardd import can_list_to_can_capnp
from selfdrive.car.byd.bydcan import create_lkas_hud
from selfdrive.car.tests.test_common import get_car_interface
from selfdrive.car.tests.test_common import get_logmessages


class TestCarStateLkasHudUpdate(unittest.TestCase):
  def test(self):
    car_interface = get_car_interface()

    self.update_can_lkas_hud(car_interface, lka_on=False, lkas_on_btn=False)
    car_interface.CS.update(car_interface.cp)

    self.update_can_lkas_hud(car_interface, lka_on=True, lkas_on_btn=True)
    car_interface.CS.update(car_interface.cp)

    j = get_logmessages(last_msg_cnt=2)

    self.assertEqual(j[0]['msg']['id'], 'lka')
    self.assertAlmostEqual(j[0]['msg']['content']['lka_on'], 0.0)
    self.assertAlmostEqual(j[0]['msg']['content']['lkas_on_btn'], 0.0)
    self.assertEqual(j[1]['msg']['id'], 'lka')
    self.assertAlmostEqual(j[1]['msg']['content']['lka_on'], 1.0)
    self.assertAlmostEqual(j[1]['msg']['content']['lkas_on_btn'], 1.0)

  @staticmethod
  def update_can_lkas_hud(car_interface, lka_on, lkas_on_btn):
    lkas_hud_can = create_lkas_hud(car_interface.CC.packer, enabled=not lka_on, raw_cnt=5)
    pcm_buttons_can = car_interface.CC.packer.make_can_msg(
      "PCM_BUTTONS", 0, {'LKAS_ON_BTN': 1.0 if lkas_on_btn else 0.0})
    bts = can_list_to_can_capnp([lkas_hud_can, pcm_buttons_can])
    for cp in car_interface.can_parsers:
      if cp is not None:
        cp.update_strings([bts])
