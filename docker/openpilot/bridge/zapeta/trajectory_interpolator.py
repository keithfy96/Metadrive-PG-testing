import numpy as np


class TrajectoryInterpolator:
    """Origin-constrained cubic fit over the AV3 horizon.

    p(tau) = b1*tau + b2*tau^2 + b3*tau^3, tau in seconds from the current frame
    (p(0) = (0,0), no intercept term). alpha / alpha_y add optional ridge to the
    x / y fits; both default to 0 (plain OLS). The normal matrix depends only on
    the fixed time grid, so the solve is well-posed even at alpha=0.
    """

    MIN_STD = 1e-4  # 0.1 mm floor — avoids div-by-zero at full stop

    def __init__(self, points, tstamps, alpha=0.0, alpha_y=0.0):
        tstamps = np.asarray(tstamps, dtype=np.float64)
        self.ref_time = tstamps[0]
        self.alpha = alpha
        self.alpha_y = alpha_y
        self._fit(np.asarray(points, dtype=np.float64), tstamps)

    def _fit(self, points, tstamps):
        tau = (tstamps - self.ref_time) / 1e9
        x, y = points[:, 0], points[:, 1]

        # scale-only normalization — origin stays at 0 (no mean shift)
        self.sx = max(float(np.std(x)), self.MIN_STD)
        self.sy = max(float(np.std(y)), self.MIN_STD)

        # design matrix drops the tau^0 column -> origin constraint
        X = np.column_stack([tau, tau ** 2, tau ** 3])
        XtX = X.T @ X
        self.bx = np.linalg.solve(XtX + self.alpha * np.eye(3), X.T @ (x / self.sx))
        self.by = np.linalg.solve(XtX + self.alpha_y * np.eye(3), X.T @ (y / self.sy))

    def get_point(self, tstamp):
        tau = (tstamp - self.ref_time) / 1e9
        tv = np.array([tau, tau ** 2, tau ** 3])
        return np.array([np.dot(self.bx, tv) * self.sx,
                         np.dot(self.by, tv) * self.sy])

    def get_velocity(self, tstamp):
        # vx clamped >= 0 — car only moves forward
        tau = (tstamp - self.ref_time) / 1e9
        tv = np.array([1.0, 2 * tau, 3 * tau ** 2])
        return np.array([max(0.0, float(np.dot(self.bx, tv) * self.sx)),
                         float(np.dot(self.by, tv) * self.sy)])

    def get_acceleration(self, tstamp):
        tau = (tstamp - self.ref_time) / 1e9
        tv = np.array([0.0, 2.0, 6 * tau])
        return np.array([np.dot(self.bx, tv) * self.sx,
                         np.dot(self.by, tv) * self.sy])

    def get_yaw(self, tstamp):
        # 0 below 0.3 m/s — no heading at standstill
        vx, vy = self.get_velocity(tstamp)
        if vx ** 2 + vy ** 2 < 0.09:
            return 0.0
        return float(np.arctan2(vy, vx))

    def get_yaw_rate(self, tstamp):
        # quotient rule for d/dt atan2(vy, vx)
        vx, vy = self.get_velocity(tstamp)
        ax, ay = self.get_acceleration(tstamp)
        denom = vx ** 2 + vy ** 2
        if denom < 0.09:
            return 0.0
        return float((vx * ay - vy * ax) / denom)
