#!/usr/bin/env bash
# The sim image: is it here, is it current, and build it if not.
#
#   bash scripts/sim-image.sh            # what is present, what is missing, what to do
#   bash scripts/sim-image.sh build      # docker build -t scenariobank-sim:latest -f docker/Dockerfile .
#   bash scripts/sim-image.sh --doctor   # also start the container and compare it to the host
#
# Built here since 2026-09-12, from docker/Dockerfile and this repo's own lock. Before that the
# runner reused an image built by the converter repo and published to no registry, and this
# script existed to explain compose's "pull access denied" -- an error that names a registry
# that was never involved.
#
# **The label check is the part that earns its keep.** The Dockerfile's `uv sync` line names the
# dependency groups the image carries, and `LABEL wingfin.groups` repeats them where `docker
# inspect` can read them without a container. `docker compose run` reuses whatever holds the tag
# and never rebuilds, so an image built before a group was added carries neither torch nor
# TensorRT, and the first thing to notice is a missing-module error four minutes into a rig
# build. A rig lost a morning to exactly that. This compares the two and says so first.
#
# Read from the environment, all optional:
#   SIM_IMAGE      the tag; scenariobank-sim:latest when unset (compose.yaml names the same)
#   STUDIO_IMAGE   the studio tag, for the status line only
set -euo pipefail

cd "$(dirname "$0")/.."

IMAGE="${SIM_IMAGE:-scenariobank-sim:latest}"
STUDIO="${STUDIO_IMAGE:-scenariobank-studio:85e5dad}"
DOCKERFILE=docker/Dockerfile

note() { printf '  %s\n' "$*"; }
die() { printf '\n  %s\n\n' "$*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "no docker on PATH. Everything below needs it."
[[ -f "$DOCKERFILE" ]] || die "no $DOCKERFILE -- this checkout is missing the sim image's recipe."

# The groups the Dockerfile's uv sync line names. Comment lines are dropped first, since the
# comments discuss --group themselves.
wanted_groups() {
    awk '/^[^#]*uv sync/' "$DOCKERFILE" | grep -o -- '--group [a-z]*' | awk '{print $2}' | sort -u | tr '\n' ' '
}

image_groups() {
    docker image inspect "$IMAGE" --format '{{index .Config.Labels "wingfin.groups"}}' 2>/dev/null || true
}

cmd_build() {
    note "tag        $IMAGE"
    note "recipe     $DOCKERFILE, groups: $(wanted_groups)"
    note "budget     10-15 minutes and ~4 GB of wheels the first time; seconds when only src/ changed"
    printf '\n'
    docker build -t "$IMAGE" -f "$DOCKERFILE" .
    printf '\n'
    note "built. bash scripts/sim-image.sh    # confirm the label matches"
}

cmd_status() {
    local fail=0
    echo "== sim image =="
    note "tag        $IMAGE"
    if [[ -z "$(docker images -q "$IMAGE" 2>/dev/null)" ]]; then
        note "           NOT PRESENT -- bash scripts/sim-image.sh build"
        note "           (or, from a machine that has it: docker save $IMAGE | gzip > sim.tar.gz,"
        note "            then gunzip < sim.tar.gz | docker load here)"
        fail=1
    else
        note "           present, $(docker images --format '{{.Size}}' "$IMAGE" | head -1)"
        local groups want
        groups="$(image_groups)"
        want="$(wanted_groups)"
        if [[ -z "$groups" || "$groups" == "<no value>" ]]; then
            note "groups     unlabelled -- built before the label existed; says nothing either way"
        else
            note "groups     $groups"
            for w in $want; do
                case " $groups " in
                    *" $w "*) ;;
                    *) note "           MISSING '$w' -- the Dockerfile has it, the image does not:"
                       note "           bash scripts/sim-image.sh build"; fail=1 ;;
                esac
            done
        fi
    fi

    printf '\n== studio image ==\n'
    note "tag        $STUDIO"
    if [[ -z "$(docker images -q "$STUDIO" 2>/dev/null)" ]]; then
        note "           not built yet -- docker compose build studio (needs the sim image first)"
    else
        note "           present, $(docker images --format '{{.Size}}' "$STUDIO" | head -1)"
    fi

    if [[ "${1:-}" == "--doctor" ]]; then
        printf '\n== doctor, host against container ==\n'
        [[ $fail -eq 0 ]] || { echo "  skipped: fix the sim image first."; exit 1; }
        # The one comparison worth making by hand: both must name the same MetaDrive commit. A
        # bank generated against one commit and scored against another is two different roads
        # with one name.
        local host_commit cont_commit
        host_commit="$(uv run scenariobank doctor 2>/dev/null | awk '/^commit:/ {print $2}')"
        cont_commit="$(docker compose run --rm run doctor 2>/dev/null | awk '/^commit:/ {print $2}')"
        note "host       ${host_commit:-<no simulator: uv sync --group sim>}"
        note "container  ${cont_commit:-<failed>}"
        if [[ -n "$host_commit" && "$host_commit" == "$cont_commit" ]]; then
            note "           same commit"
        else
            note "           DIFFERENT -- results from the two are not comparable"
            fail=1
        fi
    fi

    printf '\n'
    [[ $fail -eq 0 ]] && echo "ready." || { echo "not ready -- see above."; exit 1; }
}

case "${1:-status}" in
    -h|--help|help) sed -n '2,22p' "$0" | sed 's/^#\s\?//'; exit 0 ;;
    build) cmd_build ;;
    status|--doctor) cmd_status "${1:-}" ;;
    *) die "unknown command: $1
  bash scripts/sim-image.sh [status|build|--doctor]" ;;
esac
