from __future__ import annotations

import numpy as np

# tables: rows index v_ego (m/s), cols index control magnitude (0..1).
# from CARLA Town10HD calibration sweep on Tesla M3 @ 20 Hz sync
# (dev/tmp/throttle_calibration.py, dev/tmp/build_accel_map.py)
V_EGO_GRID = np.array([0.0, 2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 14.0])
CONTROL_GRID = np.array([0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0])

# col 0 (zero-control coast) is from closed-loop measurements — CARLA's engine
# braking at speed is far stronger than the original sweep captured.
THROTTLE_ACCEL = np.array([
    [-0.400, -0.323, +0.167, +3.608, +3.608, +3.608, +3.608, +3.608, +3.924, +4.634, +6.349],
    [-1.790, -0.197, +0.167, +2.400, +2.400, +2.709, +3.444, +3.444, +3.924, +4.634, +6.349],
    [-1.240, -0.686, -0.119, +0.691, +1.587, +1.862, +3.347, +3.347, +3.924, +4.634, +6.349],
    [-1.060, -1.060, -0.900, -0.422, +0.496, +1.492, +1.889, +3.429, +4.119, +4.634, +6.349],
    [-1.160, -1.160, -1.100, -0.782, +0.146, +1.219, +2.366, +3.569, +4.621, +5.771, +6.601],
    [-1.582, -1.582, -1.582, -0.795, -0.037, +0.848, +2.029, +3.310, +4.709, +6.071, +7.037],
    [-1.582, -1.582, -1.582, -0.795, -0.037, +0.847, +1.801, +2.935, +4.214, +5.505, +6.747],
    [-1.582, -1.582, -1.582, -0.795, -0.037, +0.847, +1.801, +2.860, +3.831, +5.099, +6.459],
])

BRAKE_ACCEL = np.array([
    [+0.000, -1.768, -2.120, -2.405, -2.695, -2.984, -3.274, -3.568, -3.858, -4.151, -4.449],
    [+0.000, -1.768, -2.120, -2.405, -2.695, -2.984, -3.274, -3.568, -3.858, -4.151, -4.449],
    [+0.000, -1.768, -2.120, -2.420, -2.715, -3.026, -3.324, -3.623, -3.914, -4.206, -4.496],
    [+0.000, -2.389, -2.617, -2.839, -3.048, -3.413, -3.675, -3.943, -4.197, -4.451, -4.691],
    [+0.000, -2.794, -3.067, -3.333, -3.602, -3.866, -4.115, -4.372, -4.617, -4.860, -5.086],
    [+0.000, -3.486, -3.774, -4.061, -4.346, -4.631, -4.914, -5.194, -5.471, -5.744, -6.013],
    [+0.000, -4.341, -4.638, -4.934, -5.231, -5.526, -5.820, -6.112, -6.400, -6.686, -6.969],
    [+0.000, -4.907, -5.182, -5.457, -5.728, -6.004, -6.278, -6.551, -6.827, -7.098, -7.367],
])

# Decels within this margin below coast(v) map to zero control (engine braking
# ~= the request), reserving the near-step brake bite for genuinely hard decel.
# Deliberately NO deadband around a_cmd=0: zero control at speed realizes coast,
# not ~0 (cruise sawtooth); a_cmd ~ 0 inverts to the hold throttle instead.
_ENGINE_BRAKE_MARGIN = 0.30

# stiction step in Tesla physics model: throttle ≤0.2 from rest doesn't move,
# 0.3 launches at +3.6 m/s². Linear interp would under-throttle launches.
_LAUNCH_V_MS = 0.5
_LAUNCH_THROTTLE = 0.30


def _invert_row(row_accels: np.ndarray, accel_cmd: float, controls: np.ndarray, ascending: bool) -> float:
    # np.interp needs monotonically increasing x — flip when not
    if ascending:
        return float(np.interp(accel_cmd, row_accels, controls))
    return float(np.interp(accel_cmd, row_accels[::-1], controls[::-1]))


def _lookup_control(table: np.ndarray, accel_cmd: float, v_ego: float, ascending: bool) -> float:
    v_clamped = float(np.clip(v_ego, V_EGO_GRID[0], V_EGO_GRID[-1]))
    upper = int(np.searchsorted(V_EGO_GRID, v_clamped))
    upper = min(max(upper, 1), len(V_EGO_GRID) - 1)
    lower = upper - 1
    v_lo, v_hi = V_EGO_GRID[lower], V_EGO_GRID[upper]
    if v_hi == v_lo:
        alpha = 0.0
    else:
        alpha = (v_clamped - v_lo) / (v_hi - v_lo)

    c_lo = _invert_row(table[lower], accel_cmd, CONTROL_GRID, ascending)
    c_hi = _invert_row(table[upper], accel_cmd, CONTROL_GRID, ascending)
    return (1.0 - alpha) * c_lo + alpha * c_hi


def coast_accel(v_ego: float) -> float:
    """Realized accel at zero control (engine braking), from the measured col 0."""
    return float(np.interp(np.clip(v_ego, V_EGO_GRID[0], V_EGO_GRID[-1]),
                           V_EGO_GRID, THROTTLE_ACCEL[:, 0]))


def accel_to_carla(accel_cmd: float, v_ego: float) -> tuple[float, float]:
    # stop approach / standstill: never map small negative accels to throttle
    if v_ego < _LAUNCH_V_MS and accel_cmd < 0.0:
        brake = _lookup_control(BRAKE_ACCEL, accel_cmd, v_ego, ascending=False)
        return 0.0, float(np.clip(brake, 0.0, 1.0))

    coast = coast_accel(v_ego)
    if accel_cmd >= coast:
        # includes accel_cmd ~ 0: inverts to the hold throttle (no coast sawtooth)
        throttle = _lookup_control(THROTTLE_ACCEL, accel_cmd, v_ego, ascending=True)
        if v_ego < _LAUNCH_V_MS and accel_cmd > 0.0:
            throttle = max(throttle, _LAUNCH_THROTTLE)
        return float(np.clip(throttle, 0.0, 1.0)), 0.0

    if accel_cmd >= coast - _ENGINE_BRAKE_MARGIN:
        return 0.0, 0.0  # engine braking delivers ~the request

    # brake row is monotone decreasing in accel as control rises → ascending=False
    brake = _lookup_control(BRAKE_ACCEL, accel_cmd, v_ego, ascending=False)
    return 0.0, float(np.clip(brake, 0.0, 1.0))
