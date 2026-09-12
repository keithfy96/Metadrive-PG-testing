# The authoring studio, and the only image this repository builds.
#
#   docker compose build studio && docker compose up studio    # http://127.0.0.1:8770/
#
# **Two lines of substance, because everything else is inherited.** The base already carries
# MetaDrive at 85e5dadc, the 168 MB asset pack, the panda3d Config.prc patch that prefers EGL over
# GLX so there is no X server to find, the glvnd ICD manifest without which every EGL context
# lands silently on llvmpipe, HOME=/tmp, and a /opt/venv outside /work that a bind mount cannot
# shadow. Re-deriving any of that here would be a 13 GB build to arrive back where we started; see
# `compose.yaml` for why the base is an image from the converter repo and what that does not cost.
#
# **Why the studio image needs the whole simulator.** It does not serve the work, it spawns it:
# `web/invoke.py` launches every job as `sys.executable -m scenariobank <cmd>`, a subprocess of
# this process. So a `generate` started from the page builds maps and renders thumbnails inside
# THIS container. An image holding only FastAPI and uvicorn would draw the page, accept the form,
# and fail every job at `import metadrive`.
#
# **And it costs almost nothing.** Measured: this layer is ~40 MB on a 13.4 GB base that any
# machine running the runner already has, and image size does not reach startup -- `docker run` on
# the 13.4 GB base exits in 0.29 s against 0.27 s for a 78 MB ubuntu:22.04, because the layers are
# already unpacked. What the studio actually pays at boot is `import scenariobank.cli`, 204 ms. It
# never imports MetaDrive itself: the engine is one per process, so a server holding one could
# serve exactly one simulator request and would die with it.
FROM scenariobank-sim:latest

# `uv pip install` and NOT `uv sync`. A sync makes the environment match the lock *exactly* --
# it would strip MetaDrive, torch, TensorRT and CuPy back out of /opt/venv, which is the entire
# thing being inherited. `uv pip install` is additive.
#
# The versions still come from `uv.lock` rather than from a second list written here: `uv export
# --only-group web` resolves that group out of the lock, so the three packages named in
# `pyproject.toml` and the seventeen they actually pull are declared once, in the file that
# already declares them. `--frozen` means a lock that no longer matches `pyproject.toml` fails the
# build instead of quietly resolving something else.
#
# Only two files are copied, so editing any source file leaves this layer cached.
WORKDIR /tmp/web
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --only-group web --no-hashes --no-emit-project -o requirements.txt \
    && uv pip install --python /opt/venv --no-cache -r requirements.txt \
    && rm -rf /tmp/web

# Back to the base's working directory: `compose.yaml` mounts this repo over it, and the image's
# own editable-install `.pth` is the single line `/work/src`, so whatever is mounted there has its
# `src/` on `sys.path` with no PYTHONPATH.
#
# No `chmod -R /opt/venv` here, and that is deliberate rather than forgotten. chmod modifies every
# file it touches, and on overlayfs modifying a file in a lower layer copies it up -- so a
# recursive chmod would write a fresh 13 GB copy of the venv into a layer whose actual content is
# 40 MB. uv writes as root under the build's 022 umask, so the files land 644 and the directories
# 755, which the non-root runtime uid reads.
WORKDIR /work

# The command lives in `compose.yaml` beside the host-networking comment that explains why the
# studio cannot be reached through a published port. Left as the base's shell so that
# `docker run -it scenariobank-studio:85e5dad` is still a way in.
