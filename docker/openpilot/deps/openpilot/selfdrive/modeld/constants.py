import os
IDX_N = 33


def index_function(idx, max_val=192, max_idx=32):
  return (max_val) * ((idx/max_idx)**2)


def str_to_bool(s):
  return s.lower() in ('1', 'true')


openpilot_var = os.getenv('OPENPILOT_TRAJECTORY_TYPE', '0')
SHOULD_USE_OPENPILOT_TRAJECTORY = str_to_bool(openpilot_var)

# Third trajectory mode ("AV3 uniform"): a uniform MPC grid of AV3_MPC_N intervals
# over AV3_MPC_HORIZON_S seconds, i.e. T_IDXS = [0, h/N, 2h/N, ..., h]. Set
# AV3_MPC_N to the model's waypoint count so the lateral MPC has one shooting node
# per predicted waypoint. AV3_MPC_N=0 (default) keeps the two legacy modes
# untouched. The zapeta bridge sets this env at runtime (before first import) from
# the model's waypoint count; the matching lateral solver comes from the prebuilt
# menu or is generated on demand (lat_mpc.get_lateral_solver) — no image rebuild.
AV3_MPC_N = int(os.getenv('AV3_MPC_N', '0'))
AV3_MPC_HORIZON_S = float(os.getenv('AV3_MPC_HORIZON_S', '2.0'))
AV3_UNIFORM_TRAJECTORY = AV3_MPC_N > 0

if AV3_UNIFORM_TRAJECTORY:
  T_IDXS = [AV3_MPC_HORIZON_S * i / AV3_MPC_N for i in range(AV3_MPC_N + 1)]
elif SHOULD_USE_OPENPILOT_TRAJECTORY:
  T_IDXS = [index_function(idx, max_val=10.0) for idx in range(IDX_N)]
else:
  T_IDXS = [0, 0.5, 1.0, 1.5, 2.0]
