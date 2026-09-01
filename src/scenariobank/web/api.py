"""The studio's HTTP surface.

**This process never imports MetaDrive, and that is a design constraint rather than an accident.**
`BaseEngine.singleton` (`engine/engine_utils.py:36-59`) is one engine per *process*: a server that
built an env would hold it for its lifetime, could not serve two requests that each need one, and
would die with it -- a panda3d/bullet fault is a segfault, not an exception a handler can catch. So
every simulator-touching command runs as a subprocess of this same CLI, which also means the CLI
stays the single source of truth and `docs/reference/commands.md` keeps describing what runs.

`create_app` takes its roots as arguments rather than reading module globals, so the tests drive it
with `TestClient` against a temp directory -- no server, no port, no simulator.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

#: Where the studio keeps job logs and scratch figures. Gitignored: a job is re-runnable, so
#: nothing here is worth keeping.
STATE_DIR_NAME = ".studio"

_STATIC = Path(__file__).parent / "static"


def create_app(*, banks_root: Path, state_dir: Path) -> FastAPI:
    """Build the studio app rooted at `banks_root`, with scratch state under `state_dir`."""
    banks_root = Path(banks_root)
    state_dir = Path(state_dir)

    app = FastAPI(
        title="scenariobank studio",
        description="Author a scenario bank: look at it, rank seeds, swap the poor draws.",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.banks_root = banks_root
    app.state.state_dir = state_dir

    @app.get("/api/doctor")
    def doctor() -> dict:
        """Which simulator the *subprocesses* will be, reported without building one.

        `probe=False` deliberately: probing costs a reset, and a reset means an engine in this
        process. The header this feeds is about identity, not about the observation space.
        """
        from scenariobank.doctor import collect

        return collect(probe=False).model_dump()

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(_STATIC / "index.html")

    return app
