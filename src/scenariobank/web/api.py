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

import re
from collections import Counter
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from scenariobank.bank import (
    MANIFEST_NAME,
    THUMBNAIL_DIR,
    BankError,
    Manifest,
    ScenarioNotFound,
    read_manifest,
    set_max_steps,
    set_options,
)
from scenariobank.cli import DESTINATIONS_DOC, EXAMPLES_DIR
from scenariobank.web.invoke import NOT_RUNNABLE, InvokeError, build_argv, catalog
from scenariobank.web.jobs import JobBusy, JobNotFound, Jobs
from scenariobank.web.results import INDEX_NAME, ResultsStore, UnknownJob

#: Where the studio keeps job logs and scratch figures. Gitignored: a job is re-runnable, so
#: nothing here is worth keeping.
STATE_DIR_NAME = ".studio"

#: Where a candidate seed's picture is drawn, under the state directory. Scratch: the seed it
#: draws has not been committed to any bank, and drawing the same one twice is a job away.
LOOKS_DIR = "looks"

_STATIC = Path(__file__).parent / "static"

#: What a bank directory and a thumbnail may be called. A name matching this cannot contain a
#: separator and cannot begin with a dot, so `..` and `../../etc/passwd` are not names that get
#: filtered out -- they are names that never become a path at all. Same reasoning as
#: `/api/examples/{category}.png` resolving through `CATEGORIES`: check the shape of the name
#: before it touches the filesystem, rather than checking where the path landed afterwards.
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class JobRequest(BaseModel):
    """One submitted job: a command name and the flags to give it.

    Deliberately not one model per command. The flags are validated against `docs.reference()` --
    the CLI's own parameters -- so a model here would be a second declaration of them, and the
    first thing to fall out of step when a flag changes.
    """

    command: str
    options: dict[str, object] = Field(default_factory=dict)
    global_options: dict[str, object] = Field(default_factory=dict)


class BudgetRequest(BaseModel):
    """One scenario's own step cap, or `None` to put it back on its category's.

    A model of its own rather than a job submission, because this is the one edit that runs no
    command: there is no flag to validate against `docs.reference()`, only a number.
    """

    model_config = ConfigDict(extra="forbid")

    max_steps: int | None = None


class OptionsRequest(BaseModel):
    """The option axes to pin on a bank, and the levels to pin them at.

    Every axis is optional and only the ones named move, so the six dropdowns on the page can save
    one at a time as they are changed rather than posting the whole block back each time. Like
    `BudgetRequest` this runs no command, so there is no flag to validate against
    `docs.reference()`; `bank.set_options` owns the rule about what is a level.
    """

    model_config = ConfigDict(extra="forbid")

    traffic: str | None = None
    cones: str | None = None
    barriers: str | None = None
    pedestrians: str | None = None
    cyclists: str | None = None
    lights: str | None = None


def _recorded(manifest: Manifest) -> dict:
    """The three things a real-world row shows and a procedural one has no equivalent for.

    Where on earth it is, how fast it was sampled, and who else is in it. A PG bank has no origin,
    its rate is whatever a run chooses rather than a property of the file, and its traffic is a
    level set at run time rather than a count -- so these are added to a real-world entry rather
    than served as nulls on every row.

    Summed across the bank's entries, because the listing is a picker. `import` writes one
    conversion per bank, so the sum is the conversion; the per-conversion breakdown is one click
    away in the manifest itself, and this endpoint stays a summary.
    """
    tracks: Counter = Counter()
    rates: set[float] = set()
    origin = None
    for entry in manifest.categories.values():
        rates.add(entry.step_hz)
        origin = origin or entry.origin
        for row in entry.scenarios:
            tracks.update(row.tracks)
    return {
        # One rate, or none reported. A bank whose conversions disagree has no single rate to
        # replay it at, and saying nothing is better than naming one of them.
        "step_hz": rates.pop() if len(rates) == 1 else None,
        "origin": origin,
        # Sorted so the row reads the same on every reload; `Counter` orders by insertion.
        "tracks": dict(sorted(tracks.items())),
    }


def create_app(
    *,
    banks_root: Path,
    state_dir: Path,
    workdir: Path | None = None,
    results_root: Path | None = None,
) -> FastAPI:
    """Build the studio app rooted at `banks_root`, with scratch state under `state_dir`.

    `workdir` is the directory jobs run in and the boundary they may write inside; it defaults to
    the directory the studio was started in. `results_root` is the share's `results/` tree the
    rigs deliver into (Phase 7 Step 6); `cli.studio` resolves it the way the agent does, from
    `SCENARIOBANK_SHARE`, and it defaults to the laptop's `out/results` under `workdir`. The
    index of that tree is `<state_dir>/results.sqlite` -- on this machine's own disk, never the
    share, because SQLite over a network filesystem corrupts.
    """
    banks_root = Path(banks_root)
    state_dir = Path(state_dir)
    workdir = Path(workdir) if workdir is not None else Path.cwd()
    results_root = (
        Path(results_root) if results_root is not None else workdir / "out" / "results"
    )
    jobs = Jobs(state_dir / "jobs", workdir=workdir)

    def _root() -> Path:
        """`--banks-root`, made absolute. One definition, because `/api/studio` reports this
        directory and `/api/banks` reads it, and the two must be the same place."""
        return banks_root if banks_root.is_absolute() else workdir / banks_root

    def _state() -> Path:
        """`state_dir`, made absolute, for the same reason `_root` exists: `/api/studio` tells the
        page where to write a look and `/api/looks` reads it back, and the two must agree."""
        return state_dir if state_dir.is_absolute() else workdir / state_dir

    app = FastAPI(
        title="scenariobank studio",
        description="Author a scenario bank: look at it, rank seeds, swap the poor draws.",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.banks_root = banks_root
    app.state.state_dir = state_dir
    app.state.workdir = workdir
    app.state.results_root = results_root
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
        """The eleven categories: what `--category` selects, beyond the name.

        Served separately from `/api/commands` because the forms in later steps need the list of
        categories without the whole reference behind it.
        """
        from scenariobank.docs import category_rows

        return category_rows()

    @app.get("/api/blocks")
    def blocks() -> list[dict]:
        """The fifteen block ids a road is spelled from, each with what it is.

        The road builder's palette. Served from `categories.BLOCKS` so the page cannot offer a
        letter the CLI would refuse, or name one differently from the reference.
        """
        from scenariobank.docs import block_rows

        return block_rows()

    @app.get("/api/commands")
    def commands() -> dict:
        """The command reference as data -- the same dict `docs/reference/commands.md` is built
        from, so the page and the file cannot list different flags."""
        from scenariobank.docs import reference

        return reference()

    @app.get("/api/studio")
    def studio() -> dict:
        """Where this studio was started, and where a new bank would go.

        The page has to name a directory for `generate --out`, and it may not invent one: banks
        live under `--banks-root`, which is a flag, and a job may only write inside the directory
        the studio was started in. Both facts live here rather than being assumed by the page.
        """
        root = _root()
        inside = root.resolve().is_relative_to(workdir.resolve())
        return {
            "workdir": str(workdir),
            # Relative when it can be: that is the form `--out` wants, and the form the argv the
            # page shows you reads as.
            "banks_root": str(root.resolve().relative_to(workdir.resolve()))
            if inside
            else str(root),
            # A studio pointed at banks outside its own checkout can still *list* them; it just
            # cannot generate into them, because no job may write out there.
            "writable": inside,
            # Where `inspect --out` should draw a candidate seed. Reported rather than assumed by
            # the page for the same reason `banks_root` is: it is where *this* studio keeps its
            # scratch, and `/api/looks` serves the same directory back.
            "looks": str(_looks().relative_to(workdir.resolve()))
            if _looks().is_relative_to(workdir.resolve())
            else str(_looks()),
        }

    def _bank_dir(bank: str) -> Path:
        """The directory for one bank, or a refusal.

        Two failures, deliberately different: a name that is not a name at all is a 400, because
        nothing could ever be served for it; a well-formed name with no manifest behind it is a
        404, because a bank could be there tomorrow. A directory without a manifest is not a bank
        -- generation writes the manifest last, so that is exactly the interrupted-run case.
        """
        if not _NAME.match(bank):
            raise HTTPException(
                status_code=400,
                detail=f"{bank!r} is not a bank name: letters, digits, dot, dash or underscore",
            )
        path = _root() / bank
        if not (path / MANIFEST_NAME).is_file():
            raise HTTPException(status_code=404, detail=f"no bank named {bank!r}")
        return path

    @app.get("/api/banks")
    def banks() -> list[dict]:
        """Every bank under `--banks-root`, newest first, summarised.

        A summary rather than the manifests: the page opens one bank at a time, and a listing that
        carried every scenario row would grow with the disk. The counts are what a picker shows.

        A directory whose manifest does not validate is **listed with its error** rather than
        skipped. A bank silently missing from the list is the one failure a person cannot debug
        from the page, and a schema bump is exactly when it would happen. Such a row carries no
        `source`: nothing was read, so which kind of bank it is unknown, and the page says that
        rather than filing it under a heading.

        Every row carries `source`, and a real-world one carries three fields more -- see
        `_recorded`. A procedural row is exactly what it was before this, which is why the page
        needs no second endpoint to split the list.
        """
        root = _root()
        if not root.is_dir():
            return []
        found = []
        for path in sorted(root.iterdir()):
            if not path.is_dir() or not _NAME.match(path.name):
                continue
            if not (path / MANIFEST_NAME).is_file():
                continue
            try:
                manifest = read_manifest(path)
            except (BankError, ValueError) as error:
                found.append({"name": path.name, "error": str(error)})
                continue
            entry = {
                "name": path.name,
                "bank_id": manifest.bank_id,
                "created_utc": manifest.created_utc,
                # The discriminator, straight off the manifest. No second endpoint and no second
                # listing: the field that says which kind of bank this is already exists in the
                # file, so the page groups on it rather than asking twice.
                "source": manifest.source,
                "categories": list(manifest.categories),
                "scenarios": sum(len(one.scenarios) for one in manifest.categories.values()),
            }
            if manifest.source != "pg":
                entry.update(_recorded(manifest))
            found.append(entry)
        # Newest first: the bank you just generated is the one you want to look at. An unreadable
        # one has no date, so it sorts to the end rather than to the top.
        found.sort(key=lambda entry: entry.get("created_utc") or "", reverse=True)
        return found

    @app.get("/api/banks/{bank}")
    def bank(bank: str) -> dict:
        """One bank's manifest, as it is on disk.

        Returned unshaped. The manifest was designed to explain itself -- "declare the intent,
        store the fact" -- so a studio that reformatted it here would be inventing a second
        description of a bank for the page to drift away from.
        """
        try:
            return read_manifest(_bank_dir(bank)).model_dump()
        except (BankError, ValueError) as error:
            # A manifest this studio cannot read is the bank's problem, not the request's: 422
            # rather than 404, so the page can say *why* instead of "no such bank".
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/api/banks/{bank}/review")
    def bank_review(bank: str) -> dict:
        """What is actually in this bank: duplicates, coverage, step budgets and spread.

        Separate from `GET /api/banks/{bank}` on purpose. That endpoint serves the manifest
        **unshaped**, and folding a computed report into it would be exactly the second description
        of a bank it exists to avoid. Nothing here builds an environment -- every field the review
        needs is already in the manifest -- so this is a read and some arithmetic.
        """
        from scenariobank.review import review

        try:
            return review(read_manifest(_bank_dir(bank))).model_dump()
        except (BankError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/api/banks/{bank}/compare")
    def bank_compare(bank: str, left: str, right: str) -> dict:
        """Two scenarios of this bank, read against each other.

        Its own endpoint rather than a slice of `/review`, because the review reports the pairs
        that are *worth* reporting -- the near-duplicates and the closest -- and every pair of a
        five-row category is ten of them, most of them the same word repeated. This answers the pair
        a person actually asked about by clicking two cards.

        The measure is `review.compare`, the same one the review and the CLI use. Nothing here
        builds an environment; the answer is a manifest read and some arithmetic.
        """
        from scenariobank.review import compare

        try:
            manifest = read_manifest(_bank_dir(bank))
        except (BankError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        try:
            return compare(manifest, left, right).model_dump()
        except LookupError as error:
            # The bank is fine and the request is well formed; the id names nothing in it.
            raise HTTPException(status_code=404, detail=str(error)) from error
        except BankError as error:
            # An imported bank, which `compare` refuses: it holds one recording, so there is no
            # second drive in it to compare the first against. 422 and not 500 -- the refusal is
            # a fact about the bank rather than a fault here, and the page shows the sentence.
            raise HTTPException(status_code=422, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/banks/{bank}/scenarios/{scenario}/budget")
    def set_budget(bank: str, scenario: str, request: BudgetRequest) -> dict:
        """Give one scenario its own step budget, or clear it. **The one write here that is not
        a job.**

        Every other edit measures something off a road, and a road means an engine, which means a
        subprocess -- the constraint this whole module is arranged around. `max_steps` is
        *declared* rather than measured, so this is a manifest read and a manifest write, and
        making the page start a Python interpreter to change one integer would be a second of
        waiting for nothing. `bank.set_max_steps` still owns the rule; this endpoint only carries
        the number to it.

        Refused while a job is running, because `replace` holds a manifest in memory and writes
        it back when it finishes: an edit slipped in beside it would be overwritten without a
        word, and a lost write is the one failure the page could not show you.
        """
        directory = _bank_dir(bank)
        if not _NAME.match(scenario):
            raise HTTPException(status_code=400, detail=f"{scenario!r} is not a scenario id")
        busy = jobs.running()
        if busy is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{busy['command']} is still running, and it will write this manifest when "
                    "it finishes. Wait for it, then set the budget."
                ),
            )
        try:
            return set_max_steps(directory, scenario, request.max_steps).model_dump()
        except ScenarioNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except (BankError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/banks/{bank}/options")
    def set_bank_options(bank: str, request: OptionsRequest) -> dict:
        """Pin some of a bank's option levels. **The second write here that is not a job.**

        Same class as the budget write above and for the same reason: what changes is declared
        rather than measured off a road, so no environment is built and no subprocess starts.
        What the levels mean is not this endpoint's business -- it carries them to
        `bank.set_options`, which owns the rule.

        Refused while a job is running, for the reason the budget write is: `replace` holds a
        manifest in memory and writes it back when it finishes, so an edit slipped in beside it
        would be lost without a word.
        """
        directory = _bank_dir(bank)
        chosen = request.model_dump(exclude_none=True)
        if not chosen:
            raise HTTPException(status_code=400, detail="name at least one axis to set")
        busy = jobs.running()
        if busy is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{busy['command']} is still running, and it will write this manifest when "
                    "it finishes. Wait for it, then set the options."
                ),
            )
        try:
            return set_options(directory, chosen).model_dump()
        except (BankError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/api/banks/{bank}/thumbs/{name}.png")
    def thumbnail(bank: str, name: str) -> FileResponse:
        """One scenario's picture.

        `name` is guarded the same way the bank is, so the served path is built from two names
        neither of which can hold a separator. `THUMBNAIL_DIR` comes from `bank.py` rather than
        being spelled here, because where thumbnails live is that module's decision.
        """
        directory = _bank_dir(bank)
        if not _NAME.match(name):
            raise HTTPException(status_code=400, detail=f"{name!r} is not a scenario name")
        path = directory / THUMBNAIL_DIR / f"{name}.png"
        if not path.is_file():
            # `generate --no-thumbnails` is a supported way to build a bank, so a missing picture
            # is an ordinary state and says so.
            raise HTTPException(
                status_code=404,
                detail=f"no thumbnail for {name!r} in {bank!r}",
            )
        return FileResponse(path, media_type="image/png")

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

    def _results() -> ResultsStore:
        """The results index, opened per request: the file is on local disk and the schema is
        `CREATE TABLE IF NOT EXISTS`, so opening it is cheap and there is nothing to hold."""
        return ResultsStore(_state() / INDEX_NAME, results_root)

    @app.get("/api/results")
    def results(rebuild: bool = False) -> dict:
        """Every result the rigs have delivered, newest first, indexed on the way in.

        Ingest on request rather than on a timer: a scan is a listdir and a set difference,
        because a delivered directory is immutable and one already indexed is skipped by name.
        The tree on the share is the truth; `?rebuild=true` drops the index and reads the whole
        tree again, and the reply says where both are so nobody has to guess which disk holds
        what.
        """
        store = _results()
        ingested = store.rebuild() if rebuild else store.ingest()
        return {
            "results_root": str(results_root),
            "index": str(store.index),
            "ingested": ingested.as_dict(),
            "jobs": store.jobs(),
        }

    @app.get("/api/results/{name}")
    def result(name: str) -> dict:
        """One delivered directory: its job row and its scenario rows.

        `name` is the directory's name -- the job id, or `<job_id>.attempt<N>` for a run that
        did not run -- and is checked for shape before it reaches the index for the reason
        every other name here is. An attempt has no rows, and says so with an empty list.
        """
        if not _NAME.match(name):
            raise HTTPException(status_code=400, detail=f"{name!r} is not a result name")
        store = _results()
        store.ingest()
        try:
            job = store.job(name)
        except UnknownJob as error:
            raise HTTPException(
                status_code=404, detail=f"no result named {name!r} under {results_root}"
            ) from error
        return {"job": job, "rows": store.rows(name)}

    @app.get("/api/rigs")
    def rigs() -> list[dict]:
        """What each card is doing, as its agent last wrote under `results/status/`.

        A file read and no port open on any rig: the worker rewrites its status file on every
        state change and every ten seconds, and this is the studio's whole view of the rigs.
        """
        return _results().rigs()

    def _looks() -> Path:
        """Where a candidate seed's picture is drawn. Under the state directory rather than in a
        bank: nothing here belongs to a bank, and a picture of a seed nobody committed would be a
        thumbnail of a scenario that does not exist."""
        return (_state() / LOOKS_DIR).resolve()

    @app.get("/api/looks/{name}.png")
    def look(name: str) -> FileResponse:
        """A candidate seed, drawn by `inspect` before anyone commits it to a bank.

        Guarded on the shape of the name the same way a thumbnail is, so the served path is built
        from a name that cannot hold a separator. A missing picture is a 404 rather than a blank:
        the drawing is a job, and a job can fail or be cancelled.
        """
        if not _NAME.match(name):
            raise HTTPException(status_code=400, detail=f"{name!r} is not a look")
        path = _looks() / f"{name}.png"
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"nothing drawn for {name!r} yet")
        return FileResponse(path, media_type="image/png")

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

    @app.get("/api/reference/destinations")
    def destinations_doc() -> dict:
        """The checked-in destinations reference, as text, so the page can show what was measured.

        Text rather than a parsed document: it is generated markdown and the generator is the
        authority on its shape. Re-parsing it here to hand the page a structure would be a second
        opinion about a file `destinations.render` already decided the layout of.

        Absent is not an error. A studio started outside the repo, or one whose reference has
        never been measured, gets `written: false` and the button that fixes it.
        """
        path = workdir / DESTINATIONS_DOC
        if not path.is_file():
            return {"path": str(DESTINATIONS_DOC), "written": False, "text": None, "at": None}
        return {
            "path": str(DESTINATIONS_DOC),
            "written": True,
            "text": path.read_text(),
            "at": path.stat().st_mtime,
        }

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(_STATIC / "index.html")

    return app
