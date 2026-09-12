#!/usr/bin/env bash
# Re-vendor the zapeta openpilot fork at a different commit.
#
# **You almost certainly do not need this.** The fork is already vendored -- 309 MB of tracked
# files at deps/openpilot, pinned at the commit `deps/openpilot/VENDORED.md` records. A fresh
# clone of this repo can build the bridge image with no access to the private zapetaai org, and
# `scripts/bridge.sh build` does not run this script when that tree is present.
#
# This exists for one job: moving to a different fork commit. That is not routine -- the commit
# must match the environment the AV3 .ep checkpoint was compiled against.
#
# Needs SSH access to the zapetaai org: the fork AND its cereal/opendbc/panda submodules are
# private. A download-zip will not do -- it carries no submodules, and the bridge server imports
# cereal.messaging on its first line. It also loses the ten symlinks this script repairs below.
#
# Usage:
#   rm -rf docker/openpilot/deps/openpilot          # the vendored tree it is replacing
#   REF=<sha> ./docker/openpilot/pull.sh
#
# Then finish the job, which this script deliberately does not do for you -- see
# deps/openpilot/VENDORED.md "Updating it":
#   1. remove the .git and the seven submodule gitdir pointers
#   2. comment out the `filter=lfs` lines in the two .gitattributes
#   3. `git add -f docker/openpilot/deps` -- the fork's own .gitignore files exclude the
#      prebuilt binaries the build links against, libacados.so among them
#   4. update VENDORED.md with the new commit
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

REPO="git@github.com:zapetaai/openpilot.git"
DEST="deps/openpilot"

# tip of av3-dense-lat-mpc (dense per-waypoint lateral MPC + variable-waypoint
# planners). Must match the environment the .ep checkpoint was compiled against.
REF="${REF:-c767ace885e64015fe58a7e2074ef51f79085a7b}"

info()  { printf '\033[1;34m==> %s\033[0m\n' "$*"; }
error() { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# The ten paths the fork tracks as mode 120000. A checkout that lost them -- a zip, or git with
# core.symlinks=false -- builds until scons dies on `Missing SConscript 'rednose/SConscript'`,
# which reads like a broken Dockerfile and is not. Re-derived from the index rather than named,
# so it repairs whatever is actually missing and cannot go stale against the fork.
repair_symlinks() {
    local missing
    missing=$(git -C "$DEST" ls-files -s \
        | awk '$1=="120000"{ $1=$2=$3=""; sub(/^ +/,""); print }' \
        | while IFS= read -r path; do [ -L "$DEST/$path" ] || printf '%s\n' "$path"; done)
    if [ -n "$missing" ]; then
        info "restoring $(printf '%s\n' "$missing" | wc -l) missing symlink(s)"
        printf '%s\n' "$missing" | xargs -d '\n' git -C "$DEST" checkout --
    fi
}

# submodules hydrated == cereal has files; that is the thing that actually breaks
if [ -f "$DEST/SConstruct" ] && [ -n "$(ls -A "$DEST/cereal" 2>/dev/null)" ]; then
    repair_symlinks
    info "fork already at $DEST ($(git -C "$DEST" rev-parse --short HEAD)) -- nothing to do"
    exit 0
fi

if [ ! -d "$DEST/.git" ]; then
    info "cloning $REPO (~920 MB, several minutes)"
    mkdir -p "$(dirname "$DEST")"
    git clone "$REPO" "$DEST"
fi

info "checking out $REF"
git -C "$DEST" fetch origin
git -C "$DEST" checkout "$REF"

info "fetching submodules (cereal, opendbc, panda are private too)"
git -C "$DEST" submodule update --init --recursive

repair_symlinks

# No LFS pull. The fork LFS-tracks selfdrive/modeld/models/*.onnx and *.dlc, but the bridge
# never builds or runs modeld -- scons compiles only cereal, common, opendbc/can, boardd and the
# two MPC libs, and the container runs `python3 -m zapeta.server`. Its one modeld reference is
# `from selfdrive.modeld.constants import T_IDXS`, a plain list of timestamps. So those five
# files stay as pointer text and `git status` shows them ` M` forever. That is expected -- they
# are mode 100644, the symlinks above are mode 120000, and only the latter are repaired.

[ -f "$DEST/SConstruct" ] || error "no SConstruct at $DEST -- clone looks wrong"
[ -n "$(ls -A "$DEST/cereal" 2>/dev/null)" ] \
    || error "cereal/ is empty -- submodules did not hydrate; check SSH access to zapetaai"

info "fork ready at $DEST ($(git -C "$DEST" rev-parse --short HEAD))"
