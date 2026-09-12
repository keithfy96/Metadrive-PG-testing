import json

import zmq

from selfdrive.car import gen_empty_fingerprint
from selfdrive.car.car_helpers import interfaces
from selfdrive.car.fingerprints import _FINGERPRINTS as FINGERPRINTS


def get_logmessages(last_msg_cnt=1):
  ctx = zmq.Context.instance()
  sock = ctx.socket(zmq.PULL)

  try:
    sock.bind('ipc:///tmp/logmessage')

    ret = []

    for _ in range(last_msg_cnt):
      dat = b''.join(sock.recv_multipart())
      record = dat[1:].decode()
      j = json.loads(record)
      ret.append(j)

    return ret
  finally:
    sock.close()
    ctx.term()


def get_car_interface():
  car_name = 'BYD ATTO 3'

  fingerprints = gen_empty_fingerprint()
  fingerprints.update({k: FINGERPRINTS[car_name][0] for k in fingerprints.keys()})

  CarInterface, CarController, CarState = interfaces[car_name]
  car_params = CarInterface.get_params(car_name, fingerprints, car_fw=[], experimental_long=False, docs=False)

  return CarInterface(car_params, CarController, CarState)
