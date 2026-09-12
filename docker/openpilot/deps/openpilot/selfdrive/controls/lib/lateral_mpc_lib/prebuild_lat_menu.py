#!/usr/bin/env python3
"""Prebuild the acados lateral-MPC "menu" — one solver per waypoint count listed
in AV3_MPC_MENU, over the AV3_MPC_HORIZON_S horizon.

Run once at image-build time (see the Dockerfile) so the common waypoint counts
load instantly at runtime. A count that is NOT in the menu is generated+compiled
on first use by get_lateral_solver() (the runtime fallback), so the bridge still
works for any waypoint count — it just pays a one-time compile for a novel one.
"""
import os
import sys

# lat_mpc lives under selfdrive/...; ensure the openpilot root is importable
sys.path.insert(0, "/opt/openpilot")

from selfdrive.controls.lib.lateral_mpc_lib.lat_mpc import (  # noqa: E402
    get_lateral_solver, uniform_grid,
)


def main() -> int:
    menu = os.getenv("AV3_MPC_MENU", "").split()
    horizon = float(os.getenv("AV3_MPC_HORIZON_S", "2.0"))
    counts = sorted({int(x) for x in menu if x.strip() and int(x) > 0})
    if not counts:
        print("[prebuild_lat_menu] AV3_MPC_MENU empty — nothing to prebuild "
              "(all waypoint counts will build at runtime on first use)")
        return 0
    print(f"[prebuild_lat_menu] horizon={horizon}s  menu={counts}")
    for n in counts:
        print(f"[prebuild_lat_menu] generating + compiling lateral solver N={n} ...", flush=True)
        get_lateral_solver(uniform_grid(n, horizon))
        print(f"[prebuild_lat_menu]   done N={n}", flush=True)
    print("[prebuild_lat_menu] menu complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
