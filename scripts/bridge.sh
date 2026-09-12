#!/usr/bin/env bash
#
# The openpilot bridge container: openpilot's real planner and controller behind TCP 5558, the
# thing `scenariobank.av3:AV3Policy` and `:BridgePolicy` send a trajectory to and get pedals
# back from. Phase 4 Step 7 / Phase 5 Step 3.
#
#   bash scripts/bridge.sh build         # build the image from docker/openpilot/, ~30 min the first time
#   bash scripts/bridge.sh status        # is it up, and is anything listening
#   bash scripts/bridge.sh start         # run it on port 5558, host networking
#   bash scripts/bridge.sh stop          # remove the container; the image stays
#   bash scripts/bridge.sh logs          # what it has said, following
#   bash scripts/bridge.sh save FILE     # write the image to a .tar.gz for a machine that cannot build
#
# **Built here since 2026-09-12, from docker/openpilot/.** That directory is the converter's,
# carried whole: the Dockerfile, the zapeta server (`bridge/`), and the openpilot fork itself
# **vendored** at a pinned commit under `deps/openpilot` (309 MB of tracked files), so a fresh
# clone builds the bridge with no SSH access to the private zapetaai org. It is a second
# container and not a group in the sim image because the fork is Python 3.8 and torch has no
# 3.8 wheel. docker/openpilot/README.md has the whole story, including the two ways the vendored
# tree can be damaged in transit and how `build` checks for them.
#
# Read from the environment, all optional:
#   BRIDGE_PORT   the TCP port the bridge listens on; 5558 when unset, which is what
#                 `AV3_BRIDGE` defaults to on the client side
#   BRIDGE_IMAGE  the image tag; metadrive-wingfin-openpilot:prod when unset
#   BRIDGE_NAME   the container name; metadrive-wingfin-openpilot-bridge when unset
#
# `--network host` on purpose: the runner reaches the bridge on 127.0.0.1 whether it is itself
# in the sim container (also host networking) or on the host, and a bridge network in front of
# a round trip per control tick would be a cost the timing rows exist to measure. The
# environment is the fork's simulation contract -- no panda, no firmware query, and the
# five-point T_IDXS that matches the AV3 model's 2 s horizon -- copied from the converter's
# script, which is the one place it was ever measured to work.
set -euo pipefail

cd "$(dirname "$0")/.."

BRIDGE_PORT="${BRIDGE_PORT:-5558}"
BRIDGE_IMAGE="${BRIDGE_IMAGE:-metadrive-wingfin-openpilot:prod}"
BRIDGE_NAME="${BRIDGE_NAME:-metadrive-wingfin-openpilot-bridge}"
CONTEXT=docker/openpilot

note() { printf '  %s\n' "$*"; }
die() { printf '\n  %s\n\n' "$*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "no docker on PATH. The bridge is a container."

# The container's own status word, or empty for "no container of that name at all". Docker
# prints nothing and exits 0 for a name that does not exist, which is why this is a filter
# rather than an inspect -- inspect exits 1 and would trip `set -e`.
container_status() {
    docker ps -a --filter "name=^/${BRIDGE_NAME}$" --format '{{.Status}}'
}

image_exists() {
    [[ -n "$(docker images -q "$BRIDGE_IMAGE" 2>/dev/null)" ]]
}

listening() {
    python3 - "$BRIDGE_PORT" <<'EOF'
import socket, sys
s = socket.socket(); s.settimeout(2)
sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
EOF
}

# The ten mode-120000 paths in the vendored fork. scons dies on `Missing SConscript
# 'rednose/SConscript'` if a transport flattened them, which reads like a broken Dockerfile and
# is not. git checks them out as symlinks; rsync and zip may not.
VENDORED_SYMLINKS="rednose laika tinygrad selfdrive/hardware
third_party/libyuv/x64/include third_party/snpe/x86_64 third_party/snpe/larch64
third_party/acados/x86_64/lib/libqpOASES_e.so third_party/acados/larch64/lib/libqpOASES_e.so
third_party/acados/Darwin/lib/libqpOASES_e.dylib"

cmd_build() {
    [[ -f "$CONTEXT/Dockerfile" ]] || die "no $CONTEXT/Dockerfile -- this checkout is missing the bridge's recipe."
    local fork="$CONTEXT/deps/openpilot"
    echo "== fork =="
    if [[ -f "$fork/SConstruct" && -n "$(ls -A "$fork/cereal" 2>/dev/null)" ]]; then
        note "vendored   $(du -sh "$fork" | cut -f1) at $(grep -oE '[0-9a-f]{40}' "$fork/VENDORED.md" 2>/dev/null | head -1 | cut -c1-9)"
        local lost=0 p
        for p in $VENDORED_SYMLINKS; do
            [[ -L "$fork/$p" ]] || { note "MISSING SYMLINK  $p"; lost=1; }
        done
        [[ $lost -eq 0 ]] || die "the vendored tree lost symlinks in transit -- git checks them out
  as mode 120000, so this means a copy that flattened them. Re-clone this repo rather than rsync."
        note "symlinks   all 10 present"
    else
        note "not vendored -- fetching the fork instead (needs SSH access to zapetaai)"
        "$CONTEXT/pull.sh"
    fi

    printf '\n== image ==\n'
    note "context    $CONTEXT"
    note "tag        $BRIDGE_IMAGE"
    note "budget     half an hour if nothing is cached; the apt + pyenv base dominates"
    printf '\n'
    docker build -t "$BRIDGE_IMAGE" -f "$CONTEXT/Dockerfile" "$CONTEXT"
    printf '\n'
    note "built. bash scripts/bridge.sh start"
}

cmd_status() {
    local status
    status="$(container_status)"
    note "image      $BRIDGE_IMAGE"
    if image_exists; then
        note "           present, $(docker images --format '{{.Size}}' "$BRIDGE_IMAGE" | head -1)"
    else
        note "           NOT PRESENT -- bash scripts/bridge.sh build"
        note "           or point BRIDGE_IMAGE at a tag that is here:"
        docker images --format '             {{.Repository}}:{{.Tag}}' | grep -i openpilot || true
    fi
    note "container  $BRIDGE_NAME"
    case "$status" in
        Up*)
            note "           $status"
            ;;
        "")
            note "           none -- bash scripts/bridge.sh start"
            ;;
        *)
            note "           $status -- it started once and stopped; 'logs' says why, then 'stop' and 'start'"
            ;;
    esac
    if listening; then
        note "port       something is listening on 127.0.0.1:$BRIDGE_PORT"
    else
        note "port       nothing is listening on 127.0.0.1:$BRIDGE_PORT"
        [[ "$status" == Up* ]] && note "           (the container is up; give it a few seconds, or read the logs)"
    fi
}

cmd_start() {
    local status
    status="$(container_status)"
    case "$status" in
        Up*) die "$BRIDGE_NAME is already up ($status). Nothing to do." ;;
        "") ;;
        *) die "a stopped $BRIDGE_NAME is holding the name ($status).
  bash scripts/bridge.sh logs    # why it stopped
  bash scripts/bridge.sh stop    # then start again" ;;
    esac
    image_exists || die "no image $BRIDGE_IMAGE.
  bash scripts/bridge.sh build                       # ~30 min
  gunzip < bridge-image.tar.gz | docker load         # or a copy from a machine that has it"

    docker run -d --name "$BRIDGE_NAME" --network host \
        -e SIMULATION=1 -e NOBOARD=1 -e SKIP_FW_QUERY=1 -e "FINGERPRINT=TESLA MODEL 3" \
        -e OPENPILOT_TRAJECTORY_TYPE=0 -e BRIDGE_PORT="$BRIDGE_PORT" \
        -e PYTHONPATH=/opt/bridge:/opt/openpilot:/opt/project/common \
        -w /opt/project "$BRIDGE_IMAGE" python3 -m zapeta.server >/dev/null

    note "started $BRIDGE_NAME on 127.0.0.1:$BRIDGE_PORT"
    note "bash scripts/bridge.sh status    # confirm it is listening"
}

cmd_stop() {
    [[ -n "$(container_status)" ]] || die "no container named $BRIDGE_NAME. Nothing to stop."
    docker rm -f "$BRIDGE_NAME" >/dev/null
    note "removed $BRIDGE_NAME. The image is untouched."
}

cmd_logs() {
    [[ -n "$(container_status)" ]] || die "no container named $BRIDGE_NAME. Nothing to show."
    # A socket.timeout traceback at the end is not a crash: it is the last drive disconnecting.
    # The server catches it and goes back to listening, which is why it is still up.
    docker logs -f "$BRIDGE_NAME"
}

cmd_save() {
    local out="${1:-}"
    [[ -n "$out" ]] || die "save needs a file: bash scripts/bridge.sh save bridge-image.tar.gz"
    image_exists || die "no image $BRIDGE_IMAGE to save."
    note "writing $out -- about 6 GB compressed, so this is minutes"
    docker save "$BRIDGE_IMAGE" | gzip > "$out"
    note "done. On the other machine:  gunzip < $(basename "$out") | docker load"
}

case "${1:-status}" in
    -h|--help|help) sed -n '2,36p' "$0" | sed 's/^#\s\?//'; exit 0 ;;
    build) cmd_build ;;
    status) cmd_status ;;
    start) cmd_start ;;
    stop) cmd_stop ;;
    logs) cmd_logs ;;
    save) shift; cmd_save "${1:-}" ;;
    *) die "unknown command: $1
  bash scripts/bridge.sh [build|status|start|stop|logs|save FILE]" ;;
esac
