"""The studio's HTTP surface.

**This process never imports MetaDrive, and that is a design constraint rather than an accident.**
`BaseEngine.singleton` (`engine/engine_utils.py:36-59`) is one engine per *process*: a server that
built an env would hold it for its lifetime, could not serve two requests that each need one, and
would die with it -- a panda3d/bullet fault is a segfault, not an exception a handler can catch. So
every simulator-touching command runs as a subprocess of this same CLI. That is also what keeps
this page honest: its forms and the validation in `invoke.py` are both derived from the CLI's own
parameters, so the page cannot offer a flag that does not exist.

`create_app` takes its roots as arguments rather than reading module globals, so the tests drive it
with `TestClient` against a temp directory -- no server, no port, no simulator.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from scenariobank.cli import EXAMPLES_DIR
from scenariobank.web.invoke import NOT_RUNNABLE, InvokeError, build_argv, catalog
from scenariobank.web.jobs import JobBusy, JobNotFound, Jobs

#: Where the studio keeps job logs and scratch figures. Gitignored: a job is re-runnable, so
#: nothing here is worth keeping.
STATE_DIR_NAME = ".studio"

_STATIC = Path(__file__).parent / "static"


class JobRequest(BaseModel):
    """One submitted job: a command name and the flags to give it.

    Deliberately not one model per command. The flags are validated against `docs.reference()` --
    the CLI's own parameters -- so a model here would be a second declaration of them, and the
    first thing to fall out of step when a flag changes.
    """

    command: str
    options: dict[str, object] = Field(default_factory=dict)
    global_options: dict[str, object] = Field(default_factory=dict)


def create_app(*, banks_root: Path, state_dir: Path, workdir: Path | None = None) -> FastAPI:
    """Build the studio app rooted at `banks_root`, with scratch state under `state_dir`.

    `workdir` is the directory jobs run in and the boundary they may write inside; it defaults to
    the directory the studio was started in.
    """
    banks_root = Path(banks_root)
    state_dir = Path(state_dir)
    workdir = Path(workdir) if workdir is not None else Path.cwd()
    jobs = Jobs(state_dir / "jobs", workdir=workdir)

    app = FastAPI(
        title="scenariobank studio",
        description="Author a scenario bank: look at it, rank seeds, swap the poor draws.",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.banks_root = banks_root
    app.state.state_dir = state_dir
    app.state.workdir = workdir
    app.state.jobs = jobs

    @app.get("/api/doctor")
    def doctor() -> dict:
        """Which simulator the *subprocesses* will be, reported without building one.

        `probe=False` deliberately: probing costs a reset, and a reset means an engine in this
        process. The header this feeds is about identity, not about the observation space.
        """
        from scenariobank.doctor import collect

        return collect(probe=False).model_dump()

    @app.get("/api/categories")
    def categories() -> list[dict]:
        """The seven categories: what `--category` selects, beyond the name.

        Served separately from `/api/commands` because the forms in later steps need the list of
        categories without the whole reference behind it.
        """
        from scenariobank.docs import category_rows

        return category_rows()

    @app.get("/api/commands")
    def commands() -> dict:
        """The command reference as data -- the same dict `docs/reference/commands.md` is built
        from, so the page and the file cannot list different flags."""
        from scenariobank.docs import reference

        return reference()

    @app.get("/api/runnable")
    def runnable() -> dict:
        """Which commands the page may offer, and why the others are missing.

        The page filters the reference by this rather than carrying its own list.
        """
        commands, global_params = catalog()
        return {
            "commands": sorted(commands),
            "global_options": list(global_params.values()),
            "not_runnable": NOT_RUNNABLE,
        }

    @app.post("/api/jobs", status_code=201)
    def start_job(request: JobRequest) -> dict:
        """Run one command as a subprocess of this same CLI.

        A refusal here is a readable sentence about one flag, because the alternative -- letting it
        through and reading a Typer traceback out of the log -- is how a page teaches you nothing.
        """
        try:
            argv = build_argv(
                request.command,
                request.options,
                request.global_options,
                workdir=workdir,
            )
        except InvokeError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        try:
            return jobs.submit(argv, command=request.command)
        except JobBusy as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.get("/api/jobs")
    def list_jobs() -> list[dict]:
        """The last few jobs, newest first, without their logs."""
        return jobs.recent()

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str, since: int = 0) -> dict:
        """One job's state, plus whatever it has logged since byte `since`.

        Polled rather than streamed: the offset makes a reload cost nothing to resume from, and
        there is no reconnect logic to get wrong.
        """
        try:
            return jobs.status(job_id, since=since)
        except JobNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.delete("/api/jobs/{job_id}")
    def cancel_job(job_id: str) -> dict:
        """Terminate a running job's process group."""
        try:
            return jobs.cancel(job_id)
        except JobNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.get("/api/examples/{category}.png")
    def example(category: str) -> FileResponse:
        """The checked-in example picture for one category, which is what the gallery shows.

        The name is looked up in `CATEGORIES` *before* anything touches the filesystem, so a path
        that escapes the examples directory cannot be spelled -- it is not filtered out, it never
        becomes a path at all.
        """
        from scenariobank.categories import CATEGORIES

        if category not in CATEGORIES:
            raise HTTPException(status_code=404, detail=f"no category named {category!r}")
        path = workdir / EXAMPLES_DIR / f"{category}.png"
        if not path.is_file():
            # Named rather than blank: a studio started outside the repo, or a category added
            # since the last redraw, is a command away from being fixed.
            raise HTTPException(
                status_code=404,
                detail=f"no example picture for {category!r} yet -- run: scenariobank examples",
            )
        return FileResponse(path, media_type="image/png")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(_STATIC / "index.html")

    return app
