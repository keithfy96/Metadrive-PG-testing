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
# And the form the agent issues, once per lease (Phase 7 Steps 1 and 3): the whole job in a file
# the agent wrote into this rig's own /out, and stdout a stream of JSON objects rather than
# prose. Everything a supervisor reads is then under --out -- batch.json, starts/, results/,
# events.jsonl, and exit_code written last whatever happened.
#
#   NO_GPU=1 bash scripts/sim-run.sh run --job /out/<job_id>/job.json --out /out/<job_id> --events
#
# The agent's own call adds four more (Phase 7 Step 3): DETACH=1 so the container outlives the
# supervisor, REPO_DIR and BANK_DIR so the mounts are the HOST's paths and not the agent
# container's own, and JOB_ID/ATTEMPT so `docker ps` and a restarted agent can find the run by
# label rather than by guessing at a name.
#
#   DETACH=1 REPO_DIR=/home/metadrive/dev/Metadrive-PG-testing BANK_DIR=/mnt/share/banks/b \
#   OUT_DIR=/home/metadrive/scenariobank/out GPU=0 BRIDGE_PORT=5600 JOB_ID=j7 ATTEMPT=1 \
#   NAME=scenariobank-gpu0-j7-1 bash scripts/sim-run.sh run --job /out/j7/job.json --out /out/j7 --events
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
#   /bank      ONE bank, read-only, and only when BANK_DIR is set: the share's
#              banks/<bank_id> as the HOST names it. A job the agent resolved then says
#              `"path": "/bank"` and the bank travels no further than a bind mount. Unset
#              leaves the container with no /bank at all, which is the laptop's case -- there
#              the bank is already under /work.
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
#                 bridge per running simulation on **5600 + card index** (Phase 7 Step 3), so
#                 gpu0 is 5600 and gpu1 is 5601 -- deliberately not 5558, which is wing-sim's
#                 on the same rig with the same host networking, so a collision with theirs is
#                 an error and not our simulator driving against their planner. Exported into
#                 the container as AV3_BRIDGE, which the AV3 and Bridge policies read.
#   OUT_DIR       host directory mounted at /out; ./out when unset
#   MODELS_DIR    host directory mounted at /models, read-only; ../models when unset
#   BANK_DIR      host directory mounted at /bank, read-only; no /bank at all when unset
#   REPO_DIR      host directory mounted at /work, read-only; this checkout when unset. Set by
#                 the agent, which runs this script from INSIDE a container where `pwd` is its
#                 own mount point and not a path the daemon can resolve.
#   NAME          the container's name, for `docker ps`, `docker logs` and the agent's adopt-
#                 on-restart; unnamed when unset
#   JOB_ID        labelled onto the container as scenariobank.job-id, with ATTEMPT and GPU
#   ATTEMPT       beside it. A name is a guess; `docker ps --filter label=` is a query, and
#                 that query is how an agent restarted mid-run finds the run it launched.
#   DETACH=1      `docker run --detach`, printing the container id, and NO `--rm`: the run is
#                 then owned by the daemon and survives its supervisor being restarted, and the
#                 exited container stays until the launcher has read its logs and removed it.
#                 Without this the script runs the container in the foreground and exits with
#                 its code, which is what a person at a terminal wants.
#
# The label guard runs FIRST. `sim-image.sh status` compares the image's `wingfin.groups` label
# with the recipe's own `uv sync` line, so an image that is missing, or was built before a
# group was added, is named here -- with the build command -- and no container is created. The
# alternative, docker's own "unable to find image" after the run has started, or a missing
# torch four minutes into a drive, is what a rig lost a morning to.
set -euo pipefail

cd "$(dirname "$0")/.."
# Two different paths, and confusing them is the trap this variable exists for. CHECKOUT is
# where this script and the recipes are, as THIS process sees them; REPO is the same tree as the
# DAEMON must be told to bind-mount. They are equal on the laptop and on a rig shell, and they
# differ inside the agent container, where the checkout is at /work and the daemon has never
# heard of /work. `docker run -v` is resolved on the host, so the mount below must be REPO.
CHECKOUT="$(pwd)"
REPO="${REPO_DIR:-$CHECKOUT}"

IMAGE="${SIM_IMAGE:-metadrive-wingfin-sim:latest}"
OUT_DIR="${OUT_DIR:-$CHECKOUT/out}"
MODELS_DIR="${MODELS_DIR:-$CHECKOUT/../models}"

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
# The bank, when the caller named one. NOT resolved with `cd`: on a rig the agent hands over a
# host path it can see at the same place, but a path it cannot enter is still a path the daemon
# can mount, and refusing it here would be this script deciding a question that is the daemon's.
[[ -n "${BANK_DIR:-}" ]] && mounts+=(-v "$BANK_DIR:/bank:ro")

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

# --- the labels ------------------------------------------------------------------------------
# Every container this script starts carries the first one, hand-run ones included, because a
# sweep looking for strays wants to find those too. The other three are a query: `docker ps
# --filter label=scenariobank.gpu=0` is how an agent restarted mid-run finds the run it
# launched, where a name prefix would be a guess and a record it wrote could be stale.
labels=(--label "scenariobank.managed-by=scenariobank")
[[ -n "${JOB_ID:-}" ]] && labels+=(--label "scenariobank.job-id=$JOB_ID")
[[ -n "${ATTEMPT:-}" ]] && labels+=(--label "scenariobank.attempt=$ATTEMPT")
[[ -n "${GPU:-}" ]] && labels+=(--label "scenariobank.gpu=$GPU")

# --- the line ----------------------------------------------------------------------------------
# No `user:` -- root, see the header. No -t: this is a log, not a terminal, on the rig. `-i`
# only when stdin is one, so a heartbeat can be watched from a shell and the agent's pipe is
# still a pipe.
argv=(
    "${name[@]}"
    "${labels[@]}"
    "${gpus[@]}"
    --network host
    --workdir /work
    "${mounts[@]}"
    "${env[@]}"
    --entrypoint python
    "$IMAGE" -m scenariobank "$@"
)

# Detached: the daemon owns the run, so restarting the agent cannot kill it, and the container
# is NOT removed on exit -- an exited container is how an agent that was down when the run
# ended still finds out that it ended, and where its logs still are. Whoever launched it
# removes it once it has been harvested.
if [[ -n "${DETACH:-}" ]]; then
    exec docker run --detach "${argv[@]}"
fi

# Attached: a person at a terminal. `--rm` because the results are on /out and the container has
# nothing else worth keeping.
tty=()
[[ -t 0 ]] && tty=(-i)

exec docker run --rm "${tty[@]}" "${argv[@]}"
