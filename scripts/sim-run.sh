#!/usr/bin/env bash
#
# One scenariobank command in the sim container, and exit with its code. THE line a rig executes
# for a job: Phase 7's agent issues exactly this, once per lease, and the laptop and the Phase 5
# Step 4 gate run the same line so the three never drift. compose.yaml's `run` service is the
# laptop alias of it, not the rig's launcher.
#
#   bash scripts/sim-run.sh doctor
#   NO_GPU=1 bash scripts/sim-run.sh run --bank /work/banks/t-junction \
#       --policy scenariobank.policies:ExpertPolicy --out /out/gate
#   GPU=1 BRIDGE_PORT=5559 bash scripts/sim-run.sh run --bank /work/banks/t-junction \
#       --policy scenariobank.av3:AV3Policy --camera-rig /work/rigs/av3.txt \
#       --model-config /models/model_dev.yml --checkpoint /models/step_440000_trt_direct_full.ep \
#       --out /out/<job_id>
#
# And the form the agent will issue, once per lease (Phase 7 Step 1): the whole job in a file
# the agent wrote into this rig's own /out, and stdout a stream of JSON objects rather than
# prose. Everything a supervisor reads is then under --out -- batch.json, starts/, results/,
# events.jsonl, and exit_code written last whatever happened.
#
#   NO_GPU=1 bash scripts/sim-run.sh run --job /out/<job_id>/job.json --out /out/<job_id> --events
#
# What the container sees, and why -- every path below is the container's, so the arguments
# after the command name are written in the container's terms (/work, /out, /models):
#   /work      this repo, READ-ONLY. Read-only is the test: a runner that can rewrite the bank
#              it is scoring makes "the same numbers everywhere" uncheckable. The image's
#              editable install is the bare path line `/work/src`, so whatever is mounted here
#              has its src/ on sys.path with no PYTHONPATH, and /work is the image's WORKDIR.
#   /out       the one writable path; OUT_DIR on the host, ./out when unset.
#   /models    the model checkpoint and config, read-only; MODELS_DIR on the host, ../models
#              when unset (the models directory sits beside the repo on the laptop and the rig).
#   root       the container runs as root, deliberately (Phase 5 Step 1, closing Phase 4 Step 7
#              note 7): as the host uid the scored AV3 row hung for four hours, as root it drove
#              on the laptop and on the rig. Results under /out come out root-owned; on a rig
#              the agent reads them and owns the copy to the share, so that costs nothing. The
#              studio, which writes banks into the repo, is the service that runs as the host
#              uid -- see compose.yaml.
#   host net   --network host: the openpilot bridge listens on 127.0.0.1 and a bridge network
#              in front of a round trip per control tick is a cost the timing rows measure.
#   clock      /etc/localtime mounted, or the image has no zoneinfo, glibc falls back to UTC and
#              started_utc comes out hours adrift of the same run made on the host.
#
# Read from the environment, all optional:
#   SIM_IMAGE     the image; metadrive-wingfin-sim:latest when unset (the converter's, the
#                 laptop's default). SIM_IMAGE=scenariobank-sim:latest selects this repo's
#                 fallback, which is what a rig has, built there by `sim-image.sh build`.
#   GPU           the card, as `docker --gpus device=$GPU` takes it: an index or a UUID. Unset
#                 means every card. This is one of the two per-card inputs.
#   NO_GPU=1      no --gpus at all: generate, doctor and ExpertPolicy need none, and a machine
#                 with no NVIDIA runtime can still run those.
#   BRIDGE_PORT   the bridge this simulation talks to, 127.0.0.1:$BRIDGE_PORT; the client's
#                 own default (5558) when unset. The second per-card input: a rig runs one
#                 bridge per running simulation on 5558 + card index. Exported into the
#                 container as AV3_BRIDGE, which the AV3 and Bridge policies read.
#   OUT_DIR       host directory mounted at /out; ./out when unset
#   MODELS_DIR    host directory mounted at /models, read-only; ../models when unset
#   NAME          the container's name, for `docker ps`, `docker logs` and the agent's adopt-
#                 on-restart; unnamed when unset
#
# The label guard runs FIRST. `sim-image.sh status` compares the image's `wingfin.groups` label
# with the recipe's own `uv sync` line, so an image that is missing, or was built before a
# group was added, is named here -- with the build command -- and no container is created. The
# alternative, docker's own "unable to find image" after the run has started, or a missing
# torch four minutes into a drive, is what a rig lost a morning to.
set -euo pipefail

cd "$(dirname "$0")/.."
REPO="$(pwd)"

IMAGE="${SIM_IMAGE:-metadrive-wingfin-sim:latest}"
OUT_DIR="${OUT_DIR:-$REPO/out}"
MODELS_DIR="${MODELS_DIR:-$REPO/../models}"

die() { printf '\n  %s\n\n' "$*" >&2; exit 1; }

usage() { awk 'NR>1 && /^set -euo/ {exit} NR>1' "$0" | sed 's/^#\s\?//'; }
case "${1:-}" in
    "") usage >&2; exit 1 ;;
    -h|--help|help) usage; exit 0 ;;
esac

command -v docker >/dev/null 2>&1 || die "no docker on PATH. The runner is a container."

# --- the guard, before anything is created ---------------------------------------------------
# Its own output only when it fails: on the rig this line runs once per job, and a green status
# report per job is noise in the agent's log.
if ! guard="$(SIM_IMAGE="$IMAGE" bash scripts/sim-image.sh status 2>&1)"; then
    printf '%s\n' "$guard" >&2
    die "sim image $IMAGE is not ready -- see above; bash scripts/sim-image.sh build"
fi

# --- the mounts ------------------------------------------------------------------------------
# /out is created here so the bind mount does not make docker create it as root on the host,
# and /models is optional: generate, doctor and ExpertPolicy never read it, and a machine with
# no models directory must still run them.
mkdir -p "$OUT_DIR"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"
mounts=(
    -v "$REPO:/work:ro"
    -v "$OUT_DIR:/out"
    -v /etc/localtime:/etc/localtime:ro
)
if [[ -d "$MODELS_DIR" ]]; then
    MODELS_DIR="$(cd "$MODELS_DIR" && pwd)"
    mounts+=(-v "$MODELS_DIR:/models:ro")
fi

# --- the card ----------------------------------------------------------------------------------
gpus=()
if [[ -z "${NO_GPU:-}" ]]; then
    if [[ -n "${GPU:-}" ]]; then
        gpus=(--gpus "device=$GPU")
    else
        gpus=(--gpus all)
    fi
fi

# --- the environment ------------------------------------------------------------------------
# HOME and MPLCONFIGDIR are already the image's; repeated so that nothing outside this file can
# silently take them away (panda3d caches shaders under HOME, matplotlib writes a font list).
env=(
    -e HOME=/tmp
    -e MPLCONFIGDIR=/tmp/matplotlib
)
[[ -n "${BRIDGE_PORT:-}" ]] && env+=(-e "AV3_BRIDGE=127.0.0.1:$BRIDGE_PORT")
[[ -n "${AV3_TARGET_SPEED_MPS:-}" ]] && env+=(-e "AV3_TARGET_SPEED_MPS=$AV3_TARGET_SPEED_MPS")
[[ -n "${AV3_LONGITUDINAL:-}" ]] && env+=(-e "AV3_LONGITUDINAL=$AV3_LONGITUDINAL")

name=()
[[ -n "${NAME:-}" ]] && name=(--name "$NAME")

# --- the line ----------------------------------------------------------------------------------
# No `user:` -- root, see the header. No -t: this is a log, not a terminal, on the rig. `-i`
# only when stdin is one, so a heartbeat can be watched from a shell and the agent's pipe is
# still a pipe. `--rm`: the results are on /out, the container has nothing else to keep.
tty=()
[[ -t 0 ]] && tty=(-i)

exec docker run --rm "${tty[@]}" "${name[@]}" \
    "${gpus[@]}" \
    --network host \
    --workdir /work \
    "${mounts[@]}" \
    "${env[@]}" \
    --entrypoint python \
    "$IMAGE" -m scenariobank "$@"
