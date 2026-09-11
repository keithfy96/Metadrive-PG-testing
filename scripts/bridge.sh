#!/usr/bin/env bash
#
# The openpilot bridge container: openpilot's real planner and controller behind TCP 5558, the
# thing `scenariobank.av3:AV3Policy` and `:BridgePolicy` send a trajectory to and get pedals
# back from. Phase 4 Step 7 / Phase 5 Step 3.
#
#   bash scripts/bridge.sh status        # is it up, and is anything listening
#   bash scripts/bridge.sh start         # run it on port 5558, host networking
#   bash scripts/bridge.sh stop          # remove the container; the image stays
#   bash scripts/bridge.sh logs          # what it has said, following
#
# **The image is the converter's, reused unchanged, and this script does not build it.** The
# fork is Python 3.8 and torch has no 3.8 wheel, which is why the bridge is a second container
# and not a group in the sim image. The converter builds and saves it
# (`../wingfin-osm-scenarionet-converter/scripts/bridge.sh build|save`); its vendored fork at
# `../openpilot/` is byte-identical to the build context (`diff -rq`, empty), which is what makes
# "our own bridge, not wing-sim's" a checked fact.
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

BRIDGE_PORT="${BRIDGE_PORT:-5558}"
BRIDGE_IMAGE="${BRIDGE_IMAGE:-metadrive-wingfin-openpilot:prod}"
BRIDGE_NAME="${BRIDGE_NAME:-metadrive-wingfin-openpilot-bridge}"

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

cmd_status() {
    local status
    status="$(container_status)"
    note "image      $BRIDGE_IMAGE"
    if image_exists; then
        note "           present, $(docker images --format '{{.Size}}' "$BRIDGE_IMAGE" | head -1)"
    else
        note "           NOT PRESENT. Build or load it with the converter's scripts/bridge.sh,"
        note "           or point BRIDGE_IMAGE at the tag that is here:"
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
    image_exists || die "no image $BRIDGE_IMAGE. Build or load it with the converter's
  ../wingfin-osm-scenarionet-converter/scripts/bridge.sh build   (or: gunzip < bridge-image.tar.gz | docker load)"

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

case "${1:-status}" in
    -h|--help|help) sed -n '2,32p' "$0" | sed 's/^#\s\?//'; exit 0 ;;
    status) cmd_status ;;
    start) cmd_start ;;
    stop) cmd_stop ;;
    logs) cmd_logs ;;
    *) die "unknown command: $1
  bash scripts/bridge.sh [status|start|stop|logs]" ;;
esac
