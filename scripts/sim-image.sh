#!/usr/bin/env bash
# Is this machine able to run scenariobank in a container? Run it before the first
# `docker compose run`.
#
#   ./scripts/sim-image.sh            # what is present, what is missing, what to do
#   ./scripts/sim-image.sh --doctor   # also start the container and compare it to the host
#
# The failure this exists for: `compose.yaml` names an image built by a *different* repo and
# published to no registry, so a machine with this checkout and not that image gets compose's own
# error -- "pull access denied for metadrive-wingfin-sim" -- which points at a registry that was
# never involved and sends people looking for credentials they do not need.
#
# It also reads the image's `wingfin.groups` label, the way that repo's own `sim.sh` does. An
# image built before its `--group gpu --group model` lines were added carries neither torch nor
# TensorRT, and `docker compose run` reuses whatever holds the tag rather than rebuilding -- so a
# Phase 4 camera-rig or AV3 run fails partway in, with a missing-module error that names the
# module and not the cause. A rig lost a morning to exactly that.
#
# An image with NO such label is not thereby stale: the label was added after the groups were, so
# an image built between the two carries all of them and has nothing to say so. Silence is
# reported as silence.
set -euo pipefail

cd "$(dirname "$0")/.."

IMAGE="${SIM_IMAGE:-metadrive-wingfin-sim:latest}"
STUDIO="${STUDIO_IMAGE:-scenariobank-studio:85e5dad}"
CONVERTER="${CONVERTER_REPO:-../wingfin-osm-scenarionet-converter}"
# What this repo actually reaches for. `sim` is `generate`, `run` and every figure; `gpu` and
# `model` are Phase 4 Steps 6 and 7 and nothing before them.
WANT="sim gpu model"

note() { printf '  %s\n' "$*"; }
fail=0

command -v docker >/dev/null 2>&1 || {
    echo "no docker on PATH. Everything below needs it."
    exit 1
}

echo "== base image =="
note "tag        $IMAGE"
if [[ -z "$(docker images -q "$IMAGE" 2>/dev/null)" ]]; then
    note "           NOT PRESENT"
    printf '\n'
    note "This repo does not build it. The converter repo does:"
    note "    cd $CONVERTER && docker compose build sim"
    note "Or load a copy from a machine that has it:"
    note "    docker save $IMAGE | gzip > sim.tar.gz     # on that machine"
    note "    gunzip < sim.tar.gz | docker load          # here"
    fail=1
else
    note "           present, $(docker images --format '{{.Size}}' "$IMAGE" | head -1)"
    groups="$(docker image inspect "$IMAGE" --format '{{index .Config.Labels "wingfin.groups"}}' 2>/dev/null || true)"
    if [[ -z "$groups" || "$groups" == "<no value>" ]]; then
        note "groups     unlabelled -- says nothing either way; see the header"
    else
        note "groups     $groups"
        for want in $WANT; do
            case " $groups " in
                *" $want "*) ;;
                *) note "           MISSING '$want' -- rebuild it in $CONVERTER"; fail=1 ;;
            esac
        done
    fi
fi

printf '\n== studio image ==\n'
note "tag        $STUDIO"
if [[ -z "$(docker images -q "$STUDIO" 2>/dev/null)" ]]; then
    note "           not built yet -- docker compose build studio"
else
    note "           present, $(docker images --format '{{.Size}}' "$STUDIO" | head -1)"
fi

if [[ "${1:-}" == "--doctor" ]]; then
    printf '\n== doctor, host against container ==\n'
    [[ $fail -eq 0 ]] || { echo "  skipped: fix the base image first."; exit 1; }
    # The one comparison worth making by hand: both must name the same MetaDrive commit. A bank
    # generated against one commit and scored against another is two different roads with one name.
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
