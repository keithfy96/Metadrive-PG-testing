#!/usr/bin/env bash
#
# The AV3 sign-convention probe: measure MetaDrive's vehicle frame on a real road, mount the
# rig and read every camera, then run the model's own conversions beside a drive. Phase 4
# Steps 6 and 7.
#
#   bash scripts/av3-probe.sh                                  # rigs/av3.txt on banks/t-junction
#   RIG=rigs/av3.txt BANK=banks/curve bash scripts/av3-probe.sh
#   MODEL_CHECKPOINT=../models/step_440000_trt_direct_full.ep \
#   docker run --rm --gpus all --network host -v $PWD:/work -v $PWD/../models:/models:ro \
#       -e MODEL_CONFIG=/models/model_dev.yml -e MODEL_CHECKPOINT=/models/step_440000_trt_direct_full.ep \
#       scenariobank-sim:latest bash /work/scripts/av3-probe.sh
#
# Six conversions stand between the AV3 model and the car and not one of them raises when it
# is wrong. Stage one measures the ones the RIG rests on -- which way is forward, right, left
# and up on `DefaultVehicle`, so a spec's yaw and pitch land where its names say -- and stage
# two shows the six cameras alive: their names on the env, the buffer count under the ceiling,
# a frame of the right shape from each. Stage three is the model's: `scenariobank av3` drives
# the road with the bundled expert while the model observes beside it and reports the camera
# map, the ego state, the route block, and -- with a checkpoint -- the predicted waypoints
# against where the car went and the model's answer to a synthetic bend either way.
#
# The rig declares tick_rate 0.05 s and a road steps at 10 Hz, so stages two and three step the
# road at 100 Hz and decide at 20 (`--step-hz 100 --decision-hz 20`, the AV3 stack's clock),
# which is the rate a scored AV3 run uses. Nothing is asked to ignore the rate.
#
# Stage three needs the model config: `MODEL_CONFIG` in the environment, or the submitted copy
# beside this checkout at ../models/model_dev.yml. With `MODEL_CHECKPOINT` set and torch present
# (the sim image) the checkpoint runs; without, `--no-model` checks the three conversions that
# need no forward pass.
#
# In the sim container the interpreter is `python -m scenariobank` (the console script is not
# installed there); on the host it is `uv run scenariobank`. Same commands either way.
set -euo pipefail

cd "$(dirname "$0")/.."

RIG="${RIG:-rigs/av3.txt}"
BANK="${BANK:-banks/t-junction}"
STEPS="${STEPS:-100}"
STEP_HZ="${STEP_HZ:-100}"
DECISION_HZ="${DECISION_HZ:-20}"
MODEL_CONFIG="${MODEL_CONFIG:-../models/model_dev.yml}"
DECISIONS="${DECISIONS:-40}"

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

printf '\n== the cameras alive: %s steps of %s at %s Hz, read at %s Hz ==\n' "$STEPS" "$BANK" "$STEP_HZ" "$DECISION_HZ"
"${CLI[@]}" replay --bank "$BANK" --camera-rig "$RIG" --steps "$STEPS" \
    --step-hz "$STEP_HZ" --decision-hz "$DECISION_HZ"

[[ -f "$MODEL_CONFIG" ]] || {
    printf '\n  no model config at %s -- set MODEL_CONFIG to the submission'"'"'s model_dev.yml\n\n' "$MODEL_CONFIG" >&2
    exit 1
}
MODEL_ARGS=(--no-model)
if [[ -n "${MODEL_CHECKPOINT:-}" ]]; then
    MODEL_ARGS=(--checkpoint "$MODEL_CHECKPOINT" --decisions "$DECISIONS")
fi
printf '\n== the model'"'"'s conversions: %s ==\n' "${MODEL_ARGS[*]}"
"${CLI[@]}" av3 --bank "$BANK" --camera-rig "$RIG" --model-config "$MODEL_CONFIG" \
    --step-hz "$STEP_HZ" --decision-hz "$DECISION_HZ" "${MODEL_ARGS[@]}"
