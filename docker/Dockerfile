# The FALLBACK sim image: MetaDrive, scenariobank, and the AV3 model stack, in one environment.
#
#   bash scripts/sim-image.sh build      # -> scenariobank-sim:latest, 10-15 min, ~4 GB of wheels
#   SIM_IMAGE=scenariobank-sim:latest docker compose run --rm run ...     # then select it
#
# The image this repo runs on by default is the converter's, `metadrive-wingfin-sim:latest`,
# built in that repo with `docker compose build`; it is a superset of this one and serves both
# projects (compose.yaml says why). This file exists for a machine that has no converter checkout.
# It never writes the converter's tag: an image built from here lacks that repo's libraries.
#
# Ported 2026-09-12 from ../wingfin-osm-scenarionet-converter/docker/Dockerfile, which built
# `metadrive-wingfin-sim` for that repo; this one builds from *this* repo's pyproject.toml and
# uv.lock and carries its own tag so the two can never be confused under one name. Dropped from
# the original: the `ros` group (bag writing; nothing here writes bags). Everything else -- the
# apt list, the headless GL switch, the EGL vendor file -- is the converter's, unchanged, because
# each line there was the fix for a failure it names.
#
# The three groups sync into one venv because the model has to run where MetaDrive runs: a
# rendered frame goes straight from the camera to the tensor. `sim` is metadrive + matplotlib;
# `gpu` is what `image_on_cuda` imports; `model` is torch 2.8.0+cu128 / torch-tensorrt /
# tensorrt, pinned to what compiled the checkpoint (pyproject.toml says why).
#
# What is NOT in here: the checkpoint (1.2 GB, mounted at /models), the banks (mounted with the
# repo at /work), and any result (written to /out). The image is code and its environment.

FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# MetaDrive renders through panda3d, which needs a GL stack even offscreen; the libx* set is what
# panda3d's .so files link against and refuse to load without. git is for the metadrive source
# install; curl for uv's python download.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        libegl1 \
        libgl1 \
        libgles2 \
        libglib2.0-0 \
        libglvnd0 \
        libgomp1 \
        libsm6 \
        libx11-6 \
        libxcursor1 \
        libxext6 \
        libxi6 \
        libxrandr2 \
        libxrender1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# The docker CLI, **client only**, for one reason: the rig agent (Phase 7) runs in this image and
# starts each job as a SIBLING container through the host's mounted socket, and the line it runs
# is `scripts/sim-run.sh`. Without this the agent container has a socket it cannot speak to, and
# `sim-run.sh` dies on its own first line -- measured 2026-09-15, on both sim images.
#
# The static binary rather than `docker.io`, which drags in the daemon, containerd and runc for a
# container that must never run one; and pinned, because a client is a protocol and a surprise
# there is a surprise on a rig. Nothing else in this image uses it: a run does not start
# containers, it IS one.
ARG DOCKER_CLI_VERSION=27.3.1
RUN curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_CLI_VERSION}.tgz" \
        | tar -xzC /usr/local/bin --strip-components=1 docker/docker \
    && docker --version

# The venv lives outside /work so the bind mount of the repo at run time cannot shadow it, and
# UV_NO_SYNC so a `uv run` inside the container never tries to resync against the mounted lock.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_LINK_MODE=copy \
    UV_NO_SYNC=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /work

# Only what the sync reads. src/ goes in because the project is installed editable: the venv
# carries a single path line, `/work/src`, so at run time the repo mounted at /work is what
# `import scenariobank` finds -- the same source, live, no PYTHONPATH.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src

# --frozen: the lock is the contract; a build that would change it is a build that should fail.
RUN uv venv --python 3.10 /opt/venv \
    && uv sync --frozen --group sim --group gpu --group model \
    && uv cache clean \
    && chmod -R a+rX /opt/python /opt/venv

# MetaDrive's assets (~300 MB) at build time, so the first run does not download them into a
# read-only mount and fail.
RUN python -m metadrive.pull_asset

# Headless GL. panda3d's default display module wants an X server; p3headlessgl renders to an
# EGL pbuffer, which is what the camera rig reads from in a container with no display. The
# grep at the end fails the build if the two lines did not take.
RUN PRC="$(python -c 'import panda3d, os; print(os.path.join(os.path.dirname(panda3d.__file__), "etc", "Config.prc"))')" \
    && sed -i 's/^load-display pandagl$/load-display p3headlessgl/; s/^aux-display p3headlessgl$/aux-display pandagl/' "$PRC" \
    && grep -E '^(load|aux)-display' "$PRC"

# `graphics` in the capabilities is the one that matters: without it the NVIDIA runtime mounts
# libcuda and not libEGL_nvidia, and the headless GL above silently falls back to software
# rendering at a frame a second.
ENV NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=graphics,compute,utility

# glvnd needs to be told the NVIDIA EGL vendor exists; the runtime mounts the library but not
# this file.
RUN mkdir -p /usr/share/glvnd/egl_vendor.d \
    && printf '%s\n' \
        '{' \
        '    "file_format_version" : "1.0.0",' \
        '    "ICD" : {' \
        '        "library_path" : "libEGL_nvidia.so.0"' \
        '    }' \
        '}' > /usr/share/glvnd/egl_vendor.d/10_nvidia.json

# The container runs as whoever compose or `docker run --user` says, and that uid has no home
# here: panda3d caches shaders and matplotlib writes a font list, both under HOME.
ENV HOME=/tmp \
    MPLCONFIGDIR=/tmp/matplotlib \
    METADRIVE_PYTHON=/opt/venv/bin/python

CMD ["bash"]

# The groups the uv sync line above names, readable with `docker inspect` and no container.
# scripts/sim-image.sh compares this against the Dockerfile so an image built before a group was
# added is named as stale before a run fails four minutes in on a missing module.
LABEL wingfin.groups="sim gpu model"

# What is in here besides the venv, for a `docker inspect` that has to answer "can this image
# be the agent?" without starting a container.
LABEL wingfin.tools="docker-cli"
