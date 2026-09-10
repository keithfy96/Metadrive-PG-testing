#!/usr/bin/env bash
#
# The camera rig's sign-convention probe: measure MetaDrive's vehicle frame on a real road,
# then mount the rig and read every camera. Phase 4 Step 6.
#
#   bash scripts/av3-probe.sh                                  # rigs/av3.txt on banks/curve
#   RIG=rigs/av3.txt BANK=banks/t-junction bash scripts/av3-probe.sh
#   docker run --rm --gpus all -v $PWD:/work metadrive-wingfin-sim:latest bash /work/scripts/av3-probe.sh
#
# Six conversions stand between the AV3 model and the car and not one of them raises when it
# is wrong. This script measures the ones the RIG rests on -- which way is forward, right, left
# and up on `DefaultVehicle`, so a spec's yaw and pitch land where its names say -- and shows
# the six cameras alive: their names on the env, the buffer count under the ceiling, a frame of
# the right shape from each. The model's own conversions (camera order, ego state, route,
# waypoint sign) need a checkpoint and arrive with Step 7's port of the converter's
# `av3_probe.py`, which will run beside this.
#
# The rig declares tick_rate 0.05 s and a road steps at 10 Hz, so the replay is asked to
# `--ignore-rig-rate`: this is a look at the cameras, not a scored run, and the report still
# prints both rates. `run` has no such switch.
#
# In the sim container the interpreter is `python -m scenariobank` (the console script is not
# installed there); on the host it is `uv run scenariobank`. Same commands either way.
set -euo pipefail

cd "$(dirname "$0")/.."

RIG="${RIG:-rigs/av3.txt}"
BANK="${BANK:-banks/curve}"
STEPS="${STEPS:-20}"

if [[ -f /.dockerenv ]]; then
    CLI=(python -m scenariobank)
else
    CLI=(uv run scenariobank)
fi

[[ -f "$RIG" ]] || { printf '\n  no rig spec at %s\n\n' "$RIG" >&2; exit 1; }
[[ -f "$BANK/manifest.json" ]] || {
    printf '\n  no bank at %s -- generate one first:\n    %s generate --category curve --count 1 --out %s\n\n' \
        "$BANK" "${CLI[*]}" "$BANK" >&2
    exit 1
}

printf '\n== the rig, converted, and the vehicle frame it rests on ==\n'
"${CLI[@]}" rig --camera-rig "$RIG" --check-frame --bank "$BANK"

printf '\n== the cameras alive: %s steps of %s ==\n' "$STEPS" "$BANK"
"${CLI[@]}" replay --bank "$BANK" --camera-rig "$RIG" --steps "$STEPS" --ignore-rig-rate
