import numpy as np
from typing import Tuple

from bridge_constants import TESLA_ACCEL_MAX as _TESLA_ACCEL_MAX
from bridge_constants import TESLA_ACCEL_MIN as _TESLA_ACCEL_MIN

from .trajectory_interpolator import TrajectoryInterpolator

_YAW_SEGMENT_MIN_DX_M = 0.05
_YAW_SEGMENT_MIN_DIST_M = 0.10
_YAW_MAX_GEOMETRY_ERROR_RAD = 1.0


def _wrap_angle(angle: float) -> float:
    return float((angle + np.pi) % (2.0 * np.pi) - np.pi)


def _geometry_yaw_reference(wps: np.ndarray) -> np.ndarray:
    # robust per-point heading — the fit flattens to 0 at near-stop/plateau points;
    # those inherit the last forward heading

    n = len(wps)
    if n <= 1:
        return np.zeros(n)

    deltas = np.diff(wps, axis=0)
    dists = np.linalg.norm(deltas, axis=1)
    valid = (deltas[:, 0] > _YAW_SEGMENT_MIN_DX_M) & (dists > _YAW_SEGMENT_MIN_DIST_M)
    headings = np.arctan2(deltas[:, 1], deltas[:, 0])

    valid_idxs = np.flatnonzero(valid)
    if len(valid_idxs) == 0:
        return np.zeros(n)

    valid_headings = np.unwrap(headings[valid_idxs])
    heading_by_seg = dict(zip(valid_idxs.tolist(), valid_headings.tolist()))

    refs = np.zeros(n)
    for i in range(n):
        adjacent = []
        if i - 1 in heading_by_seg:
            adjacent.append(heading_by_seg[i - 1])
        if i in heading_by_seg:
            adjacent.append(heading_by_seg[i])

        if adjacent:
            refs[i] = float(np.mean(adjacent))
            continue

        nearest = int(valid_idxs[np.argmin(np.abs(valid_idxs - min(i, n - 2)))])
        refs[i] = heading_by_seg[nearest]

    return np.unwrap(refs)


def _sanitize_orientation(wps: np.ndarray, t_secs: np.ndarray, yaws: np.ndarray,
                          yaw_rates: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    refs = _geometry_yaw_reference(wps)
    sanitized = np.array(yaws, dtype=np.float64, copy=True)
    changed = False

    for i, yaw in enumerate(sanitized):
        if (not np.isfinite(yaw) or
                abs(_wrap_angle(float(yaw - refs[i]))) > _YAW_MAX_GEOMETRY_ERROR_RAD):
            sanitized[i] = refs[i]
            changed = True

    sanitized = np.unwrap(sanitized)
    if changed:
        yaw_rates = np.gradient(sanitized, t_secs)

    return sanitized, yaw_rates


def from_predicted(modelv2_rows, v_ego: float = 0.0):
    """Assemble the ego-frame derived dict directly from a model that predicts
    the full modelV2 message, bypassing the waypoint interpolators.

    Each row is [x, y, t, yaw, yaw_rate, v_x, v_y, a_x, a_y], used as-is; any
    count/spacing passes through at its own times (the planners interpolate onto
    their MPC grids), with a t=0 ego-origin anchor prepended. The longitudinal
    planner consumes a 1-D along-path reference, so velocity_x = |v| (frame-
    invariant, doesn't dip in turns; node 0 anchored to measured v_ego,
    velocity_y = 0) and acceleration_x = d|v|/dt so the feed-forward accel
    matches the speed profile (the model's raw a_x need not equal dv/dt).
    """
    rows = np.asarray(modelv2_rows, dtype=np.float64)
    if rows.ndim != 2 or rows.shape[1] < 9:
        raise ValueError(
            f"from_predicted expects rows of "
            f"[x, y, t, yaw, yaw_rate, v_x, v_y, a_x, a_y]; got shape {rows.shape}"
        )

    def _anchor(col: int, head: float) -> np.ndarray:
        return np.concatenate(([head], rows[:, col]))

    t = _anchor(2, 0.0)
    v_tangential = np.sqrt(rows[:, 5] ** 2 + rows[:, 6] ** 2)
    velocity_x = np.concatenate(([v_ego], v_tangential))
    acceleration_x = np.gradient(velocity_x, t)

    return {
        "position_x": _anchor(0, 0.0),
        "position_y": _anchor(1, 0.0),
        "position_t": t,
        "orientation_z": _anchor(3, 0.0),
        "orientation_rate_z": _anchor(4, 0.0),
        "velocity_x": velocity_x,
        "velocity_y": np.zeros_like(velocity_x),
        "acceleration_x": acceleration_x,
    }


def derive(waypoints_xyt, v_ego: float = 0.0):
    # v_ego anchors velocity_x[0]; otherwise the polynomial slope reflects the
    # model's *implicit assumed* current speed and the MPC sees a discontinuous
    # state→trajectory, producing near-zero accel in blended mode.
    # Output stays in ego frame (x forward, y left) — what LongitudinalPlanner
    # and LateralPlanner consume. Any number of waypoints at any spacing is kept;
    # the planners interpolate onto their MPC grids.

    wps = np.array([(x, y) for x, y, _ in waypoints_xyt], dtype=np.float64)
    t_secs = np.array([t for _, _, t in waypoints_xyt], dtype=np.float64)
    t_ns = (t_secs * 1e9).astype(np.float64)

    # cubic fit — supplies orientation (yaw/yaw_rate) via analytic derivatives
    ti = TrajectoryInterpolator(wps, t_ns)

    n = len(waypoints_xyt)
    yaws = np.zeros(n)
    yaw_rates = np.zeros(n)
    velocity_x = np.zeros(n)
    velocity_y = np.zeros(n)
    acceleration_x = np.zeros(n)

    # analytic tangent — defined at every point, incl. the last (no ahead/back flag)
    for i in range(n):
        yaws[i] = ti.get_yaw(t_ns[i])
        yaw_rates[i] = ti.get_yaw_rate(t_ns[i])

    yaws, yaw_rates = _sanitize_orientation(wps, t_secs, yaws, yaw_rates)

    # inter-waypoint diff instead of CPP polynomial — fit collapses on plateaued
    # positions (intersections) and wipes the model's forward intent
    if n > 1:
        dt_intervals = np.diff(t_secs)
        velocity_x[1:] = np.diff(wps[:, 0]) / dt_intervals
        velocity_y[1:] = np.diff(wps[:, 1]) / dt_intervals
    velocity_x[0] = v_ego
    velocity_y[0] = 0.0

    # clamp to actuator envelope — without this, blended MPC sees AV3's
    # aspirational waypoints (~+9 m/s² at low v_ego) as infeasible and
    # compromises with low a_des. Accel derived from clamped v for consistency.
    if n > 1:
        dt_to_each = t_secs - t_secs[0]
        v_upper = v_ego + _TESLA_ACCEL_MAX * dt_to_each
        v_lower = np.maximum(0.0, v_ego + _TESLA_ACCEL_MIN * dt_to_each)
        velocity_x = np.clip(velocity_x, v_lower, v_upper)
        velocity_x[0] = v_ego
        acceleration_x[:-1] = np.diff(velocity_x) / dt_intervals
        acceleration_x[-1] = acceleration_x[-2]

    return {
        "position_x": wps[:, 0],
        "position_y": wps[:, 1],
        "position_t": t_secs,
        "orientation_z": yaws,
        "orientation_rate_z": yaw_rates,
        "velocity_x": velocity_x,
        "velocity_y": velocity_y,
        "acceleration_x": acceleration_x,
    }
