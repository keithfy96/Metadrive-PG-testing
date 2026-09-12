# openpilot control-stack sidecar -- the zapeta fork (~v0.9.3, path + dense lateral MPC).
# Runs plannerd + controlsd behind a TCP bridge, which is what tier 4 of docs/running-a-test.md
# calls "the controller". This is the second container: `docker/Dockerfile` is the `sim` one.
#
# Build it with `scripts/bridge.sh build` rather than by hand -- the build context is THIS
# DIRECTORY, not the repo root, because `COPY deps/openpilot/` and `COPY bridge/` below are
# relative to it. `deps/openpilot/` is the fork checkout, ~920 MB and not in git; `pull.sh`
# beside this file clones it at the pinned SHA, and `bridge.sh build` runs that first.
#
# Python 3.8 is not a choice: the fork's own deps pin it (casadi==3.5.5 ships no cp310 wheel)
# and it carries cp38 compiled extensions. That is the whole reason this cannot be one image
# with `sim` -- torch 2.8, which the AV3 model needs, has no 3.8 wheel.
#
# Carried verbatim from wing-sim's docker/Dockerfile.openpilot. The layout under this directory
# mirrors the build context it was written for, so not one line of the recipe below changed.
FROM ubuntu:20.04 AS base

ENV PYTHONUNBUFFERED=1
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install -y --no-install-recommends sudo tzdata locales ssh ca-certificates curl git && \
    rm -rf /var/lib/apt/lists/*

RUN sed -i -e 's/# en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen && locale-gen
ENV LANG=en_US.UTF-8 LANGUAGE=en_US:en LC_ALL=en_US.UTF-8

ENV POETRY_VIRTUALENVS_CREATE=false
ENV PYENV_VERSION=3.8.10
ENV PYENV_ROOT=/root/.pyenv
ENV PATH=$PYENV_ROOT/bin:$PYENV_ROOT/shims:$PATH

COPY deps/openpilot/pyproject.toml deps/openpilot/poetry.lock \
     deps/openpilot/.python-version deps/openpilot/update_requirements.sh /tmp/
COPY deps/openpilot/tools/ubuntu_setup.sh /tmp/tools/
RUN echo "Cython<3.0" > /tmp/pip-constraints.txt
ENV PIP_CONSTRAINT=/tmp/pip-constraints.txt
ENV POETRY_INSTALLER_PARALLEL=false
RUN cd /tmp && \
    tools/ubuntu_setup.sh && \
    rm -rf /var/lib/apt/lists/* /tmp/* /root/.cache && \
    pip uninstall -y poetry
ENV PIP_CONSTRAINT=

# imported by controlsd, not in the fork's pyproject.toml
RUN pip install cantools

# stub so the import resolves without a mount; compose overrides with the eval snapshot at runtime
RUN mkdir -p /opt/project/common && \
    printf 'class InferenceConfig:\n    o_flag = False\n' > /opt/project/common/inference_config.py
ENV PYTHONPATH=/opt/project/common:${PYTHONPATH}

# ---------- app ----------
FROM base AS app

ENV OPENPILOT_PATH=/opt/openpilot
ENV PYTHONPATH=${OPENPILOT_PATH}:${PYTHONPATH}

# runtime env owned by docker-compose.openpilot-zapeta.yaml; not meant to run standalone
ENV SIMULATION=1
ENV NOBOARD=1
ENV SKIP_FW_QUERY=1
# 5-point T_IDXS = [0, 0.5, 1.0, 1.5, 2.0] s — matches AV3 model horizon
ENV OPENPILOT_TRAJECTORY_TYPE=0

# Lateral-MPC menu: space-separated waypoint counts to PREBUILD (one acados solver
# per N over AV3_MPC_HORIZON_S). The active N is chosen at RUNTIME from the model's
# waypoint count; counts not in the menu are generated+compiled on first use.
# AV3_MPC_N is the legacy single-N baked grid (0 = unused when the menu drives things).
ARG AV3_MPC_N=0
ARG AV3_MPC_HORIZON_S=2.0
ARG AV3_MPC_MENU="4 16 20 32"
ENV AV3_MPC_N=${AV3_MPC_N}
ENV AV3_MPC_HORIZON_S=${AV3_MPC_HORIZON_S}
ENV AV3_MPC_MENU=${AV3_MPC_MENU}

# acados code-gen env — needed BOTH by the build-time menu prebuild below AND by the
# runtime fallback (SCons set these only inside its own env, so bake them here).
# openpilot ships acados arch-split (include/ + x86_64/lib/), but the acados ctypes
# solver's self-build/CDLL want a unified root with lib/ + include/ — so point
# ACADOS_SOURCE_DIR at a symlinked root assembled below.
ENV ACADOS_SOURCE_DIR=/opt/acados_root
ENV ACADOS_PYTHON_INTERFACE_PATH=${OPENPILOT_PATH}/third_party/acados/acados_template
ENV TERA_PATH=${OPENPILOT_PATH}/third_party/acados/x86_64/t_renderer
ENV LD_LIBRARY_PATH=${OPENPILOT_PATH}/third_party/acados/x86_64/lib:${LD_LIBRARY_PATH}

WORKDIR ${OPENPILOT_PATH}

COPY deps/openpilot/ ${OPENPILOT_PATH}/

RUN git config --global --add safe.directory ${OPENPILOT_PATH}

# unified acados root (lib/ + include/) for the ctypes self-build + CDLL load
RUN mkdir -p /opt/acados_root && \
    ln -sfn ${OPENPILOT_PATH}/third_party/acados/x86_64/lib /opt/acados_root/lib && \
    ln -sfn ${OPENPILOT_PATH}/third_party/acados/include   /opt/acados_root/include

# build only what controlsd + plannerd need
RUN scons -j$(nproc) cereal/ common/ opendbc/can/ selfdrive/boardd/ && \
    scons -j$(nproc) \
    selfdrive/controls/lib/lateral_mpc_lib/ \
    selfdrive/controls/lib/longitudinal_mpc_lib/

# prebuild the lateral-MPC menu (one acados solver per waypoint count in AV3_MPC_MENU)
RUN python3 selfdrive/controls/lib/lateral_mpc_lib/prebuild_lat_menu.py

# ---------- bridge ----------
# Last on purpose: everything above is a slow scons/acados compile, so a bridge
# edit invalidates only this layer. The fork imports a top-level
# `inference_config`; this overwrites the stub baked into the base stage.
COPY bridge/ /opt/bridge/
COPY bridge/zapeta/inference_config.py /opt/project/common/inference_config.py

ENV PYTHONPATH=/opt/bridge:${OPENPILOT_PATH}:/opt/project/common
WORKDIR /opt/project
CMD ["python3", "-m", "zapeta.server"]
