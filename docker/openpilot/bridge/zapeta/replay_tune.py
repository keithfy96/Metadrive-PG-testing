"""Offline closed-loop replay tuner for the zapeta lateral stack (no CARLA).

Replays a recorded eval run's model predictions through the REAL fork lateral
planner + dense MPC + the bridge's lag compensation and plant inversion, closing
the loop with a plant surrogate identified from telemetry (CARLA's steer plant
settles within one 50 ms tick: kappa_real[t+1] ~= S*kappa_cmd[t]).

The recorded per-frame predictions are treated as a WORLD-FRAME reference
trajectory (re-anchored each tick at the recorded pose, then projected into the
simulated ego frame). That preserves lane feedback — the reference pulls the sim
back toward the recorded line — which is the right regime for tuning controller
/ MPC knobs. A divergence guard flags frames where the sim strays far enough
that re-projection stops approximating what the camera model would predict.

Run inside the openpilot-zapeta container (bridge is bind-mounted):
  docker compose -f docker-compose.yaml -f docker-compose.openpilot-zapeta.yaml \
    run --rm openpilot-zapeta python3 /opt/bridge/zapeta/replay_tune.py \
    --telemetry /opt/bridge/zapeta/_replay_telemetry.json \
    [--n-waypoints 20] [--set lateral_jerk_cost=0.02 ...] [--frames 4000]

Copy the run's telemetry.json to bridge/zapeta/_replay_telemetry.json first
(the out/ tree is not mounted into this container).
"""
from __future__ import annotations

import argparse
import json
import math
import os

import numpy as np

DT = 0.05
MID_FWD_M = 0.116           # actor origin -> axle midpoint (the model's reference point)
MID_SLIP_ARM = 0.33         # fallback rotation arm: midpoint ahead of the static slip pivot
PLANT_GAIN_SCALE = 0.977    # fallback steady realized/commanded curvature scale
DIVERGENCE_WARN_M = 0.75


def _late_imports(n_mpc_nodes: int):
    """Size the dense MPC + initialize zapeta.server's own late globals
    (CONTROL_N/_T_IDXS/...) so its lag-comp helper is usable standalone."""
    import zapeta.server as srv
    srv._late_imports(int(n_mpc_nodes or 0))
    global LateralPlanner, CONTROL_N, build_tesla_m3_car_params
    from selfdrive.controls.lib.lateral_planner import LateralPlanner
    from selfdrive.controls.lib.drive_helpers import CONTROL_N
    from zapeta.car_params import build_tesla_m3_car_params


def rebuild_derived(fr):
    """Reconstruct the from_predicted() derived dict out of telemetry fields."""
    t = np.array([0.0] + [w[2] for w in fr["waypoints"]])
    pos_y = np.array([0.0] + [w[1] for w in fr["waypoints"]])
    return {
        "position_x": np.asarray(fr["model_position_x"], dtype=np.float64),
        "position_y": pos_y,
        "position_t": t,
        "orientation_z": np.asarray(fr["model_orientation_z"], dtype=np.float64),
        "orientation_rate_z": np.asarray(fr["model_orientation_rate_z"], dtype=np.float64),
        "velocity_x": np.asarray(fr["model_velocity_x"], dtype=np.float64),
        "velocity_y": np.zeros(len(t)),
        "acceleration_x": np.asarray(fr["model_acceleration_x"], dtype=np.float64),
    }


class FakeSM:
    def __init__(self):
        self._msgs, self.logMonoTime = {}, {}

    def set(self, name, msg, t_ns):
        self._msgs[name], self.logMonoTime[name] = msg, t_ns

    def __getitem__(self, name):
        return getattr(self._msgs[name], name)

    def all_checks(self, service_list=None):
        return True


def build_msgs(sm, derived, v_ego, frame_id):
    from zapeta.server import (_build_modelv2, _build_car_state,
                               _build_controls_state, _build_car_control)
    from cereal import car
    t_ns = int(frame_id * DT * 1e9)
    sm.set("modelV2", _build_modelv2(derived, frame_id), t_ns)
    sm.set("carState", _build_car_state(v_ego, 0.0, 10.0), t_ns)
    sm.set("controlsState", _build_controls_state(
        0.0, 10.0, car.CarControl.Actuators.LongControlState.pid, True), t_ns)
    sm.set("carControl", _build_car_control(), t_ns)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--telemetry", default="/opt/bridge/zapeta/_replay_telemetry.json")
    ap.add_argument("--n-waypoints", type=int, default=20)
    ap.add_argument("--frames", type=int, default=0, help="0 = all")
    ap.add_argument("--set", action="append", default=[],
                    help="override lateral_planner cost globals, e.g. lateral_jerk_cost=0.02")
    ap.add_argument("--lead-tau", type=float, default=0.0,
                    help="feedforward steer=(k+tau*kdot)/g test term")
    args = ap.parse_args()

    _late_imports(args.n_waypoints)
    import selfdrive.controls.lib.lateral_planner as lp_mod
    from zapeta.server import _get_lag_adjusted_curvature

    # cost overrides: the planner reads module globals in every set_weights call
    ovr = {}
    for kv in args.set:
        k, v = kv.split("=")
        gname = {"path_cost": "PATH_COST", "lateral_motion_cost": "LATERAL_MOTION_COST",
                 "lateral_accel_cost": "LATERAL_ACCEL_COST",
                 "lateral_jerk_cost": "LATERAL_JERK_COST",
                 "st_rate_cost": "STEERING_RATE_COST"}[k.strip()]
        setattr(lp_mod, gname, float(v))
        ovr[k.strip()] = float(v)

    with open(args.telemetry) as f:
        d = json.load(f)
    N = len(d) if not args.frames else min(args.frames, len(d))

    CP = build_tesla_m3_car_params()
    planner = LateralPlanner(CP)
    sm = FakeSM()

    g0 = d[0].get("carla_steer_curvature_gain", 0.385) or 0.385
    ga = d[0].get("carla_steer_understeer_coef", 0.003) or 0.0

    # self-calibrate the plant surrogate against THIS run: realized/commanded
    # curvature per speed bin (steady turning frames); a flat scale mistracks
    # the low-speed turn range.
    _v = np.array([f["speed"] for f in d])
    _st = np.array([f["steer"] for f in d])
    _ac = np.array([f["actual_curvature"] for f in d])
    _g = g0 / (1 + ga * _v ** 2)
    _steady = np.zeros(len(d), bool)
    _steady[2:-2] = np.abs(_st[4:] - _st[:-4]) < 0.006
    _csgn = 1 if np.corrcoef(_st[_v > 2], _ac[_v > 2])[0, 1] > 0 else -1
    S_CTR, S_VAL = [], []
    for lo, hi in ((0, 2), (2, 4), (4, 6), (6, 9)):
        m = (_v >= lo) & (_v < hi) & _steady & (np.abs(_st) > 0.03)
        if m.sum() >= 30:
            S_CTR.append((lo + hi) / 2)
            S_VAL.append(float(np.median((_csgn * _ac[m]) / (_g[m] * _st[m]))))
    if not S_CTR:
        S_CTR, S_VAL = [0.0, 10.0], [PLANT_GAIN_SCALE, PLANT_GAIN_SCALE]
    print(f"[replay] plant scale S(v) bins: {list(zip(S_CTR, [round(x,3) for x in S_VAL]))}")

    # measure the midpoint's rotation ARM (v_y = yaw_rate * arm) from the run:
    # ego-lateral one-tick velocity of the recorded midpoint over yaw_rate.
    # (At turn speeds CARLA pivots near the rear axle, NOT the slip-advanced
    # static prior; missing this loses much of the short-horizon lateral.)
    _ry = np.radians(np.array([f["yaw"] for f in d]))
    _mx = np.array([f["x"] for f in d]) + MID_FWD_M * np.cos(_ry)
    _my = np.array([f["y"] for f in d]) + MID_FWD_M * np.sin(_ry)
    _yr = np.array([f["yaw_rate"] for f in d])
    _lat1 = (-(np.diff(_mx)) * np.sin(_ry[:-1]) + np.diff(_my) * np.cos(_ry[:-1])) / DT
    A_CTR, A_VAL = [], []
    for lo, hi in ((2, 4), (4, 6), (6, 9)):
        m = (_v[:-1] >= lo) & (_v[:-1] < hi) & (np.abs(_yr[:-1]) > 0.08)
        if m.sum() >= 30:
            A_CTR.append((lo + hi) / 2)
            A_VAL.append(float(np.median(_lat1[m] / _yr[:-1][m])))
    if not A_CTR:
        A_CTR, A_VAL = [0.0, 10.0], [MID_SLIP_ARM, MID_SLIP_ARM]
    print(f"[replay] rotation arm ARM(v) bins: {list(zip(A_CTR, [round(x,2) for x in A_VAL]))}")

    # sim state: world midpoint pose seeded from the recorded start
    X = d[0]["x"] + MID_FWD_M * math.cos(math.radians(d[0]["yaw"]))
    Y = d[0]["y"] + MID_FWD_M * math.sin(math.radians(d[0]["yaw"]))
    PSI = math.radians(d[0]["yaw"])
    kappa = 0.0

    sim = np.zeros((N, 3))          # midpoint world pose per tick
    preds = [None] * N              # (world pts x,y at wp times, |pred y| sign frame)
    v_rec = np.array([f["speed"] for f in d])
    diverged = 0

    for i in range(N):
        fr = d[i]
        der = rebuild_derived(fr)
        # world-frame reference: recorded prediction anchored at the RECORDED pose,
        # projected into the SIM pose frame
        ryaw = math.radians(fr["yaw"])
        rc, rs = math.cos(ryaw), math.sin(ryaw)
        rx = fr["x"] + MID_FWD_M * rc
        ry = fr["y"] + MID_FWD_M * rs
        wx = rx + der["position_x"] * rc - der["position_y"] * rs
        wy = ry + der["position_x"] * rs + der["position_y"] * rc
        c, s = math.cos(PSI), math.sin(PSI)
        der = dict(der)
        der["position_x"] = (wx - X) * c + (wy - Y) * s
        der["position_y"] = -(wx - X) * s + (wy - Y) * c
        # wrap: recorded CARLA yaw jumps by 2*pi at +/-180 deg while sim PSI is continuous
        dyaw = (ryaw - PSI + math.pi) % (2.0 * math.pi) - math.pi
        der["orientation_z"] = der["orientation_z"] + dyaw
        lat_off = abs(-(rx - X) * s + (ry - Y) * c)
        if lat_off > DIVERGENCE_WARN_M:
            diverged += 1

        v = max(float(v_rec[i]), 0.0)
        build_msgs(sm, der, v, i)
        planner.update(sm)

        v_plan = planner.v_ego
        psis = planner.lat_mpc.x_sol[0:CONTROL_N, 2].tolist()
        curvs = (planner.lat_mpc.x_sol[0:CONTROL_N, 3] / v_plan).tolist()
        rates = [float(u / v_plan) for u in planner.lat_mpc.u_sol[0:CONTROL_N - 1]] + [0.0]
        des, des_rate, _, _ = _get_lag_adjusted_curvature(max(v, 1.0), psis, curvs, rates, 0.0)

        g = g0 / (1.0 + ga * v * v)
        steer = float(np.clip((des + args.lead_tau * des_rate) / g, -1.0, 1.0))
        kappa = float(np.interp(v, S_CTR, S_VAL)) * g * steer   # 1-tick plant, run-fit S(v)

        if os.environ.get("REPLAY_DEBUG") and 590 <= i < 720 and i % 5 == 0:
            ry = np.asarray(fr.get("lat_plan_y_pts", []))
            my = np.asarray(planner.y_pts)
            dy = np.abs(my - ry).max() if len(ry) == len(my) else float("nan")
            print(f"dbg f{i:4d} v={v:4.1f} lat_off={lat_off:+.3f} dpsi={(PSI-ryaw):+.4f} "
                  f"des={des:+.5f} rec_des={fr['desired_curvature']:+.5f} "
                  f"|y_pts-rec|max={dy:.3f} y_end my={my[-1]:+.2f} rec={ry[-1] if len(ry) else 0:+.2f} "
                  f"yaw_end={planner.plan_yaw[-1]:+.3f} cost={planner.lat_mpc.cost:.1f}")

        v_y = kappa * v * float(np.interp(v, A_CTR, A_VAL))   # midpoint sweep, run-fit arm
        X += DT * (v * math.cos(PSI) - v_y * math.sin(PSI))
        Y += DT * (v * math.sin(PSI) + v_y * math.cos(PSI))
        PSI += kappa * v * DT
        sim[i] = (X, Y, PSI)

    # score: the analyzer's turn-signed bias with the SIM as 'actual' — recorded
    # waypoints are ego-relative (midpoint frame), compared against the sim's own
    # ego-frame future displacement, exactly like analyze_run on a real run.
    wpt = [w[2] for w in d[0]["waypoints"]]
    steer_rec = np.array([abs(f["steer"]) for f in d[:N]])
    # recorded midpoint world poses, for the apples-to-apples real-run bias
    ryawv = np.array([math.radians(f["yaw"]) for f in d])
    recm = np.column_stack([
        np.array([f["x"] for f in d]) + MID_FWD_M * np.cos(ryawv),
        np.array([f["y"] for f in d]) + MID_FWD_M * np.sin(ryawv),
        ryawv,
    ])
    out, outr = {}, {}
    for tt in (0.5, 1.0, 2.0):
        k = min(range(len(wpt)), key=lambda j: abs(wpt[j] - tt))
        h = int(round(wpt[k] / DT))
        rows, rrows = [], []
        for i in range(N - h):
            if v_rec[i] < 2 or steer_rec[i] <= 0.05:
                continue
            py = d[i]["waypoints"][k][1]
            if abs(py) < 0.03:
                continue
            for P, acc in ((sim, rows), (recm, rrows)):
                c, s = math.cos(P[i][2]), math.sin(P[i][2])
                dx, dy = P[i + h][0] - P[i][0], P[i + h][1] - P[i][1]
                acc.append(np.sign(py) * ((-dx * s + dy * c) - py))
        out[tt] = (len(rows), float(np.mean(rows)) if rows else float("nan"))
        outr[tt] = float(np.mean(rrows)) if rrows else float("nan")
        if os.environ.get("REPLAY_DEBUG") and tt == 0.5:
            for i in range(640, 700, 5):
                py = d[i]["waypoints"][k][1]
                o = []
                for P in (sim, recm):
                    c, s = math.cos(P[i][2]), math.sin(P[i][2])
                    dx, dy = P[i + h][0] - P[i][0], P[i + h][1] - P[i][1]
                    o.append(-dx * s + dy * c)
                print(f"dbg2 f{i} wp_y={py:+.3f} sim_disp={o[0]:+.3f} real_disp={o[1]:+.3f} "
                      f"sim_yaw={sim[i][2]:+.3f} rec_yaw={recm[i][2]:+.3f}")
    print("[replay] real-run bias (same metric code): "
          + "  ".join(f"@{t}s={outr[t]:+.3f}" for t in outr))
    tag = " ".join(f"{k}={v}" for k, v in ovr.items()) or "baseline"
    if args.lead_tau:
        tag += f" lead_tau={args.lead_tau}"
    print(f"[replay] {tag}: frames={N} diverged(>{DIVERGENCE_WARN_M}m)={diverged}  "
          + "  ".join(f"bias@{t}s={out[t][1]:+.3f}(n={out[t][0]})" for t in out))


if __name__ == "__main__":
    main()
