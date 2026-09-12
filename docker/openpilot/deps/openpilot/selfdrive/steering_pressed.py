def steering_pressed(cp, dbc_block, block_field, threshold):
    """Return boolean value of whether the driver took over control of the vehicle by comparing absolute value of
    steering to a given threshold. Steering direction (either 1 or -1) is not included here as we only care about
    absolute value of steering applied and not about the direction in which it is applied."""
    return bool(abs(cp.vl[dbc_block][block_field]) > threshold)


def get_proton_steering_pressed(cp):
    return steering_pressed(cp, "STEERING_MODULE", "STEER_RATE", 5)


def get_byd_steering_pressed(cp):
    return steering_pressed(cp, "STEER_MODULE_2", "DRIVER_EPS_TORQUE", 6)

