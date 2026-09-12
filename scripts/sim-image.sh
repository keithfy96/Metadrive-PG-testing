#!/usr/bin/env bash
# The sim image: is it here, is it current, and build it if not.
#
#   bash scripts/sim-image.sh            # what is present, what is missing, what to do
#   bash scripts/sim-image.sh build      # metadrive-wingfin-sim:latest from ../wingfin-osm-scenarionet-converter, else the fallback
#   bash scripts/sim-image.sh --doctor   # also start the container and compare it to the host
#
# **The image is the converter's: `metadrive-wingfin-sim:latest`, one sim container for both
# projects.** It is a superset of what this repo needs, so it runs `python -m scenariobank`
# unmodified, and it is published to no registry -- which is why compose's own error for a
# missing image, "pull access denied", names a registry that was never involved, and why this
# script exists. `build` makes that image **from the converter checkout beside this repo**
# (CONVERTER_DIR), the same `docker build` its own `docker compose build` runs, so one command
# here gives the same image on every machine that has both checkouts. With no converter
# checkout, `build` makes this repo's FALLBACK instead: `docker/Dockerfile` under its own tag,
# `scenariobank-sim:latest`, never the converter's, because it lacks that repo's libraries and
# the `ros` group and so cannot run the converter. SIM_IMAGE=scenariobank-sim:latest selects it.
#
# **The label check is the part that earns its keep.** A Dockerfile's `uv sync` line names the
# dependency groups it installs, and the image's `LABEL wingfin.groups` names the groups it
# carries (both recipes label themselves the same way). `docker compose run` reuses whatever
# holds the tag and never rebuilds, so an image built before a group was added carries neither
# torch nor TensorRT, and the first thing to notice is a missing-module error four minutes into
# a rig build. A rig lost a morning to exactly that. This compares the two and says so first --
# and it is what makes sharing the tag safe.
#
# Read from the environment, all optional:
#   SIM_IMAGE      the tag; metadrive-wingfin-sim:latest when unset (compose.yaml reads the same
#                  variable). SIM_IMAGE=scenariobank-sim:latest selects the fallback.
#   CONVERTER_DIR  the converter checkout `build` delegates to; ../wingfin-osm-scenarionet-converter
#                  when unset (the rig has the same sibling layout)
#   STUDIO_IMAGE   the studio tag, for the status line only
set -euo pipefail

cd "$(dirname "$0")/.."

IMAGE="${SIM_IMAGE:-metadrive-wingfin-sim:latest}"
STUDIO="${STUDIO_IMAGE:-scenariobank-studio:85e5dad}"
CONVERTER_DIR="${CONVERTER_DIR:-../wingfin-osm-scenarionet-converter}"
# The two recipes and the tag each one is allowed to write. Fixed on purpose: the converter's
# tag comes only from the converter's Dockerfile, the fallback's only from ours.
CONVERTER_IMAGE=metadrive-wingfin-sim:latest
CONVERTER_DOCKERFILE="$CONVERTER_DIR/docker/Dockerfile"
FALLBACK_IMAGE=scenariobank-sim:latest
DOCKERFILE=docker/Dockerfile

note() { printf '  %s\n' "$*"; }
die() { printf '\n  %s\n\n' "$*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "no docker on PATH. Everything below needs it."
[[ -f "$DOCKERFILE" ]] || die "no $DOCKERFILE -- this checkout is missing the sim image's recipe."

# The groups a Dockerfile's uv sync lines name, unioned. Comment lines are dropped first, since
# the comments discuss --group themselves.
wanted_groups() {
    awk '/^[^#]*uv sync/' "${1:-$DOCKERFILE}" | grep -o -- '--group [a-z]*' | awk '{print $2}' | sort -u | tr '\n' ' '
}

image_groups() {
    docker image inspect "${1:-$IMAGE}" --format '{{index .Config.Labels "wingfin.groups"}}' 2>/dev/null || true
}

# Which recipe `build` will use, and why: the converter's when its checkout is there.
have_converter() { [[ -f "$CONVERTER_DOCKERFILE" ]]; }

cmd_build() {
    local tag recipe context
    if have_converter; then
        tag="$CONVERTER_IMAGE"; recipe="$CONVERTER_DOCKERFILE"; context="$CONVERTER_DIR"
        note "source     the converter checkout at $CONVERTER_DIR ($(git -C "$CONVERTER_DIR" rev-parse --short HEAD 2>/dev/null || echo 'not a git checkout'))"
        note "           the same build its own 'docker compose build' runs; one image for both projects"
    else
        tag="$FALLBACK_IMAGE"; recipe="$DOCKERFILE"; context=.
        note "source     no converter checkout at $CONVERTER_DIR -- building this repo's FALLBACK"
        note "           (it runs this repo and not the converter; export SIM_IMAGE=$FALLBACK_IMAGE to use it)"
    fi
    note "tag        $tag"
    note "recipe     $recipe, groups: $(wanted_groups "$recipe")"
    note "budget     10-20 minutes and ~4 GB of wheels the first time; seconds when only src/ changed"
    printf '\n'
    docker build -t "$tag" -f "$recipe" "$context"
    printf '\n'
    # The label check on what was just built, so a recipe that lost a group is named here and
    # not four minutes into a run.
    local groups want w
    groups="$(image_groups "$tag")"; want="$(wanted_groups "$recipe")"
    for w in $want; do
        case " $groups " in *" $w "*) ;; *) die "built, but the label lacks '$w' (label: '$groups') -- the recipe's LABEL is out of step with its uv sync line." ;; esac
    done
    note "built $tag, label: $groups"
    [[ "$tag" == "$FALLBACK_IMAGE" ]] && note "to use it:  export SIM_IMAGE=$FALLBACK_IMAGE"
    note "bash scripts/sim-image.sh    # confirm"
}

cmd_status() {
    local fail=0
    echo "== sim image =="
    note "tag        $IMAGE"
    if [[ -z "$(docker images -q "$IMAGE" 2>/dev/null)" ]]; then
        note "           NOT PRESENT -- bash scripts/sim-image.sh build"
        if have_converter; then
            note "           (builds $CONVERTER_IMAGE from the converter checkout at $CONVERTER_DIR)"
        else
            note "           (no converter checkout at $CONVERTER_DIR, so that builds the fallback"
            note "            $FALLBACK_IMAGE; then export SIM_IMAGE=$FALLBACK_IMAGE)"
        fi
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
    -h|--help|help) sed -n '2,32p' "$0" | sed 's/^#\s\?//'; exit 0 ;;
    build) cmd_build ;;
    status|--doctor) cmd_status "${1:-}" ;;
    *) die "unknown command: $1
  bash scripts/sim-image.sh [status|build|--doctor]" ;;
esac
