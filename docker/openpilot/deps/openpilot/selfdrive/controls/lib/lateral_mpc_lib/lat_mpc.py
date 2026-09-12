#!/usr/bin/env python3
import os
import numpy as np

from casadi import SX, vertcat, sin, cos
from common.realtime import sec_since_boot
# WARNING: imports outside of constants will not trigger a rebuild
from selfdrive.modeld.constants import T_IDXS
from selfdrive.modeld.constants import SHOULD_USE_OPENPILOT_TRAJECTORY
from selfdrive.wingfin_common import InferenceConfigParser
INF_CONFIG_PARSER = InferenceConfigParser()

# The acados code-gen classes are needed at RUNTIME too: the menu factory
# (get_lateral_solver) generates + compiles a per-N solver on demand when the
# requested waypoint count isn't in the prebuilt menu. So always import them.
from third_party.acados.acados_template import AcadosModel, AcadosOcp, AcadosOcpSolver
# Legacy single-N cython solver (built by the SConscript). Optional: menu-mode images
# may not build it, and the menu factory uses the ctypes AcadosOcpSolver instead.
try:
  from selfdrive.controls.lib.lateral_mpc_lib.c_generated_code.acados_ocp_solver_pyx import AcadosOcpSolverCython  # pylint: disable=no-name-in-module, import-error
except Exception:
  AcadosOcpSolverCython = None

LAT_MPC_DIR = os.path.dirname(os.path.abspath(__file__))
EXPORT_DIR = os.path.join(LAT_MPC_DIR, "c_generated_code")
JSON_FILE = os.path.join(LAT_MPC_DIR, "acados_ocp_lat.json")
X_DIM = 4
P_DIM = 2
COST_E_DIM = 3
COST_DIM = COST_E_DIM + 2
SPEED_OFFSET = 10.0
MODEL_NAME = 'lat'
ACADOS_SOLVER_TYPE = 'SQP_RTI'
# One shooting node per trajectory grid point: len(T_IDXS)-1. Equivalent to the
# old `32 if openpilot else 4` for the two legacy grids (33 / 5 points), and in the
# AV3-uniform third mode this becomes the model's waypoint count (dense lateral MPC).
N = len(T_IDXS) - 1

def gen_lat_model(model_name=MODEL_NAME):
  model = AcadosModel()
  model.name = model_name

  # set up states & controls
  x_ego = SX.sym('x_ego')
  y_ego = SX.sym('y_ego')
  psi_ego = SX.sym('psi_ego')
  psi_rate_ego = SX.sym('psi_rate_ego')
  model.x = vertcat(x_ego, y_ego, psi_ego, psi_rate_ego)

  # parameters
  v_ego = SX.sym('v_ego')
  rotation_radius = SX.sym('rotation_radius')
  model.p = vertcat(v_ego, rotation_radius)

  # controls
  psi_accel_ego = SX.sym('psi_accel_ego')
  model.u = vertcat(psi_accel_ego)

  # xdot
  x_ego_dot = SX.sym('x_ego_dot')
  y_ego_dot = SX.sym('y_ego_dot')
  psi_ego_dot = SX.sym('psi_ego_dot')
  psi_rate_ego_dot = SX.sym('psi_rate_ego_dot')

  model.xdot = vertcat(x_ego_dot, y_ego_dot, psi_ego_dot, psi_rate_ego_dot)

  # dynamics model
  f_expl = vertcat(v_ego * cos(psi_ego) - rotation_radius * sin(psi_ego) * psi_rate_ego,
                   v_ego * sin(psi_ego) + rotation_radius * cos(psi_ego) * psi_rate_ego,
                   psi_rate_ego,
                   psi_accel_ego)
  model.f_impl_expr = model.xdot - f_expl
  model.f_expl_expr = f_expl
  return model


def gen_lat_ocp(t_grid=None, export_dir=EXPORT_DIR, model_name=MODEL_NAME):
  # t_grid = the N+1 shooting-node times; defaults to the active-mode grid so the
  # legacy code path (SConscript / __main__) is unchanged.
  if t_grid is None:
    t_grid = np.array(T_IDXS)[:N + 1]
  t_grid = np.asarray(t_grid, dtype=float)
  n_nodes = len(t_grid) - 1

  ocp = AcadosOcp()
  ocp.model = gen_lat_model(model_name)

  Tf = float(t_grid[-1])

  # set dimensions
  ocp.dims.N = n_nodes

  # set cost module
  ocp.cost.cost_type = 'NONLINEAR_LS'
  ocp.cost.cost_type_e = 'NONLINEAR_LS'

  Q = np.diag(np.zeros(COST_E_DIM))
  QR = np.diag(np.zeros(COST_DIM))

  ocp.cost.W = QR
  ocp.cost.W_e = Q

  y_ego, psi_ego, psi_rate_ego = ocp.model.x[1], ocp.model.x[2], ocp.model.x[3]
  psi_rate_ego_dot = ocp.model.u[0]
  v_ego = ocp.model.p[0]

  ocp.parameter_values = np.zeros((P_DIM, ))

  ocp.cost.yref = np.zeros((COST_DIM, ))
  ocp.cost.yref_e = np.zeros((COST_E_DIM, ))
  # Add offset to smooth out low speed control
  # TODO unclear if this right solution long term
  v_ego_offset = v_ego + SPEED_OFFSET
  # TODO there are two costs on psi_rate_ego_dot, one
  # is correlated to jerk the other to steering wheel movement
  # the steering wheel movement cost is added to prevent excessive
  # wheel movements
  ocp.model.cost_y_expr = vertcat(y_ego,
                                  v_ego_offset * psi_ego,
                                  v_ego_offset * psi_rate_ego,
                                  v_ego_offset * psi_rate_ego_dot,
                                  psi_rate_ego_dot / (v_ego + 0.1))
  ocp.model.cost_y_expr_e = vertcat(y_ego,
                                   v_ego_offset * psi_ego,
                                   v_ego_offset * psi_rate_ego)

  # set constraints
  ocp.constraints.constr_type = 'BGH'
  ocp.constraints.idxbx = np.array([2,3])
  ocp.constraints.ubx = np.array([np.radians(90), np.radians(50)])
  ocp.constraints.lbx = np.array([-np.radians(90), -np.radians(50)])
  x0 = np.zeros((X_DIM,))
  ocp.constraints.x0 = x0

  ocp.solver_options.qp_solver = 'PARTIAL_CONDENSING_HPIPM'
  ocp.solver_options.hessian_approx = 'GAUSS_NEWTON'
  ocp.solver_options.integrator_type = 'ERK'
  ocp.solver_options.nlp_solver_type = ACADOS_SOLVER_TYPE
  ocp.solver_options.qp_solver_iter_max = 1
  ocp.solver_options.qp_solver_cond_N = 1

  # set prediction horizon
  ocp.solver_options.tf = Tf
  ocp.solver_options.shooting_nodes = t_grid

  ocp.code_export_directory = export_dir
  return ocp


# ---- menu of prebuilt per-N solvers, with runtime generate+compile fallback ----
_LAT_SOLVER_CACHE = {}


def uniform_grid(n_nodes, horizon):
  """Uniform shooting-node times [0, h/N, ..., h] for n_nodes intervals."""
  return [horizon * i / n_nodes for i in range(n_nodes + 1)]


def get_lateral_solver(t_grid, build_if_missing=True):
  """Return an acados lateral solver for this node grid.

  Loads the prebuilt solver (menu) if its compiled .so is present; otherwise
  generates + compiles it on demand (the runtime fallback for a waypoint count
  that wasn't prebuilt) via the ctypes AcadosOcpSolver. Solvers are keyed and
  cached by node count so a given N is only built once per process; the compiled
  code persists on disk (c_generated_code_lat_<N>/) so it is reused across runs.
  """
  t_grid = list(t_grid)
  n_nodes = len(t_grid) - 1
  if n_nodes in _LAT_SOLVER_CACHE:
    return _LAT_SOLVER_CACHE[n_nodes]

  model_name = f"lat_{n_nodes}"
  export_dir = os.path.join(LAT_MPC_DIR, f"c_generated_code_lat_{n_nodes}")
  json_file = os.path.join(LAT_MPC_DIR, f"acados_ocp_lat_{n_nodes}.json")
  so_path = os.path.join(export_dir, f"libacados_ocp_solver_{model_name}.so")
  have = os.path.exists(so_path)
  if not have and not build_if_missing:
    raise FileNotFoundError(f"no prebuilt lateral solver for N={n_nodes} at {so_path}")

  ocp = gen_lat_ocp(t_grid, export_dir, model_name)
  solver = AcadosOcpSolver(
    ocp, json_file=json_file, build=not have, generate=not have,
  )
  _LAT_SOLVER_CACHE[n_nodes] = solver
  return solver


class LateralMpc():
  def __init__(self, x0=np.zeros(X_DIM), t_grid=None):
    # t_grid given (AV3 menu mode): per-N solver from the menu factory (load or
    # build on demand). Otherwise the legacy single-N SConscript-built cython solver.
    if t_grid is not None:
      self.N = len(list(t_grid)) - 1
      self.solver = get_lateral_solver(t_grid)
    else:
      if AcadosOcpSolverCython is None:
        raise RuntimeError(
          "legacy lateral cython solver unavailable; pass t_grid to use the menu factory")
      self.N = N
      self.solver = AcadosOcpSolverCython(MODEL_NAME, ACADOS_SOLVER_TYPE, N)
    self.reset(x0)

  def reset(self, x0=np.zeros(X_DIM)):
    n = self.N
    self.x_sol = np.zeros((n+1, X_DIM))
    self.u_sol = np.zeros((n, 1))
    self.yref = np.zeros((n+1, COST_DIM))
    for i in range(n):
      self.solver.cost_set(i, "yref", self.yref[i])
    self.solver.cost_set(n, "yref", self.yref[n][:COST_E_DIM])

    # Somehow needed for stable init
    for i in range(n+1):
      self.solver.set(i, 'x', np.zeros(X_DIM))
      self.solver.set(i, 'p', np.zeros(P_DIM))
    self.solver.constraints_set(0, "lbx", x0)
    self.solver.constraints_set(0, "ubx", x0)
    self.solver.solve()
    self.solution_status = 0
    self.solve_time = 0.0
    self.cost = 0

  def set_weights(self, path_weight, heading_weight,
                  lat_accel_weight, lat_jerk_weight,
                  steering_rate_weight):
    W = np.asfortranarray(np.diag([path_weight, heading_weight,
                                   lat_accel_weight, lat_jerk_weight,
                                   steering_rate_weight]))
    for i in range(self.N):
      self.solver.cost_set(i, 'W', W)
    self.solver.cost_set(self.N, 'W', W[:COST_E_DIM,:COST_E_DIM])

  # x0: initial state constraint
  # y_pts: y-axis of the 3d trajectory
  # p: v_plan + lateral_factor (from car params & v_plan)
  # heading_pts: md.orientation.z
  # yaw_rate_pts: md.orientationRate.z
  def run(self, x0, p, y_pts, heading_pts, yaw_rate_pts):
    n = self.N
    x0_cp = np.copy(x0)
    p_cp = np.copy(p)
    mpc_mode = INF_CONFIG_PARSER.get_value('lateral_mpc_stateless_mode',0)
    if  mpc_mode == 1:
      self.solver.set(0, "x", x0_cp)
    elif mpc_mode == 2:
      EPS = 100.
      self.solver.constraints_set(0, "lbx", x0_cp - EPS)  # lower bound for x
      self.solver.constraints_set(0, "ubx", x0_cp + EPS)  # upper bound for x
    else:
      self.solver.constraints_set(0, "lbx", x0_cp)  # lower bound for x
      self.solver.constraints_set(0, "ubx", x0_cp)  # upper bound for x
    self.yref[:,0] = y_pts
    v_ego = p_cp[0, 0]
    # rotation_radius = p_cp[1]
    self.yref[:,1] = heading_pts * (v_ego + SPEED_OFFSET)
    self.yref[:,2] = yaw_rate_pts * (v_ego + SPEED_OFFSET)
    for i in range(n):
      self.solver.cost_set(i, "yref", self.yref[i])
      self.solver.set(i, "p", p_cp[i])
    self.solver.set(n, "p", p_cp[n])
    self.solver.cost_set(n, "yref", self.yref[n][:COST_E_DIM])

    t = sec_since_boot()
    self.solution_status = self.solver.solve()
    self.solve_time = sec_since_boot() - t

    for i in range(n+1):
      self.x_sol[i] = self.solver.get(i, 'x')
    for i in range(n):
      self.u_sol[i] = self.solver.get(i, 'u')
    self.cost = self.solver.get_cost()


if __name__ == "__main__":
  ocp = gen_lat_ocp()
  AcadosOcpSolver.generate(ocp, json_file=JSON_FILE)
  # AcadosOcpSolver.build(ocp.code_export_directory, with_cython=True)
