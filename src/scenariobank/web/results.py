"""The results store: the tree on the share is the truth, the index beside the studio is a cache.

Phase 7 Step 6. The agent's `deliver()` is the only writer of `results/` on the share -- a copy
to `<job_id>.partial`, a rename to `<job_id>`, and `<job_id>.attempt<N>` for a run that did not
run -- and this module is the reader. It never writes into that tree.

**The SQLite file lives on the studio's own disk (`.studio/results.sqlite`), never on the
share.** SQLite's own documentation says so: locking over NFS and SMB is unreliable and corrupts
databases. So the tree is the source of truth and this index is derived from it, rebuildable at
any time from any directory -- which is also what makes the store testable on a laptop against
`out/results` and deployable on the NAS against `/mnt/scenariobank/results` with no change but
the path (`IMPLEMENTATION_PLAN.md`, Phase 7 Step 6, difference 1).

**Ingest is incremental by name and idempotent by key.** A delivered directory is immutable
(`deliver` refuses an existing name), so a name already in the index is skipped without being
opened; `rebuild()` drops the index and rescans. Within a job the key is `(job_id, scenario_id)`
-- the same scenario legitimately scores under many jobs -- and `INSERT OR REPLACE` on it is
what makes reading the same `results.json` twice leave the count where it was. A `.partial`
directory is a delivery in flight and is never read; `status/` holds the rigs' status files and
is read by `rigs()`, never as a job.

**Validation happens here, at ingest.** `results.json` is parsed against `Results`, the same
model that wrote it, and a file that fails is one job row with `status="invalid"` and the error
in `error` -- listed, never skipped, and never a reason to stop the scan. This is the
`validate --results` Step 5 deferred to the store. An `.attempt<N>` directory is evidence of a
run that did not run: one row of `kind="attempt"`, its exit code in `error`, and no scores.

Imports `scenariobank.results` for the models and nothing of MetaDrive (the studio rule) and
nothing of `scenariobank.agent` (the agent is imported by nobody; the studio and the CLI hand
this module the path the agent's `Roots` resolved).
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from scenariobank.results import Results

#: The index file, under the studio's state directory. Local disk, never the share (above).
INDEX_NAME = "results.sqlite"

#: `results/status/` on the share: one file per card, written by the agent's worker. Named here
#: as well as in `agent/worker.py` (`STATUS_DIR`) because this module may not import the agent;
#: `tests/unit/test_results_store.py` pins the two equal.
STATUS_DIR = "status"

#: A delivery in flight: `deliver()` copies here and renames into place. Never read.
PARTIAL_SUFFIX = ".partial"

#: A run that did not run, delivered beside the job's name so the redelivery guard cannot
#: mistake it for the result (`session.delivery_name`).
_ATTEMPT = re.compile(r"^(?P<job_id>.+)\.attempt(?P<attempt>\d+)$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    name          TEXT PRIMARY KEY,
    job_id        TEXT NOT NULL,
    attempt       INTEGER,
    kind          TEXT NOT NULL,
    status        TEXT NOT NULL,
    bank_id       TEXT,
    policy        TEXT,
    model         TEXT,
    options_json  TEXT,
    n             INTEGER,
    summary_json  TEXT,
    delivered_at  TEXT NOT NULL,
    error         TEXT
);
CREATE TABLE IF NOT EXISTS rows (
    job_id              TEXT NOT NULL,
    scenario_id         TEXT NOT NULL,
    status              TEXT NOT NULL,
    success             INTEGER NOT NULL,
    steps               INTEGER NOT NULL,
    route_completion    REAL,
    cost                REAL NOT NULL,
    collisions          TEXT NOT NULL,
    failure_reason      TEXT,
    wall_time_s         REAL NOT NULL,
    actor_layout_digest TEXT,
    PRIMARY KEY (job_id, scenario_id)
);
"""

_JOB_COLUMNS = (
    "name", "job_id", "attempt", "kind", "status", "bank_id", "policy", "model",
    "options_json", "n", "summary_json", "delivered_at", "error",
)
_ROW_COLUMNS = (
    "job_id", "scenario_id", "status", "success", "steps", "route_completion", "cost",
    "collisions", "failure_reason", "wall_time_s", "actor_layout_digest",
)


class UnknownJob(KeyError):
    """No job of that name is in the index."""


@dataclass(frozen=True)
class Ingested:
    """What one scan did: directories added, left alone, and added as invalid."""

    added: int
    skipped: int
    invalid: int
    #: Jobs in the index after the scan, so the caller need not count again.
    total: int

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True)
class _Read:
    """One directory, read: the job row and the score rows that go with it."""

    job: dict[str, Any]
    rows: list[dict[str, Any]]


class ResultsStore:
    """The index of one results tree, at `index`, read from `results_root`."""

    def __init__(self, index: Path, results_root: Path) -> None:
        self.index = Path(index)
        self.results_root = Path(results_root)
        self.index.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        # A connection per call rather than one held: the studio serves from a thread pool, and
        # a sqlite connection belongs to the thread that opened it.
        connection = sqlite3.connect(self.index)
        connection.row_factory = sqlite3.Row
        return connection

    # -- reading the tree -------------------------------------------------------------------

    def ingest(self) -> Ingested:
        """Index every delivered directory not yet indexed. Reads the tree, never writes it.

        One transaction per directory, so a scan that dies leaves whole entries and no half
        ones, and the next scan picks up where it stopped.
        """
        added = skipped = invalid = 0
        with self._connect() as connection:
            known = {row["name"] for row in connection.execute("SELECT name FROM jobs")}
            for entry in self._entries():
                if entry.name in known:
                    skipped += 1
                    continue
                read = _read(entry)
                with connection:
                    connection.execute(
                        f"INSERT OR REPLACE INTO jobs ({', '.join(_JOB_COLUMNS)}) "
                        f"VALUES ({', '.join('?' for _ in _JOB_COLUMNS)})",
                        tuple(read.job[column] for column in _JOB_COLUMNS),
                    )
                    connection.executemany(
                        f"INSERT OR REPLACE INTO rows ({', '.join(_ROW_COLUMNS)}) "
                        f"VALUES ({', '.join('?' for _ in _ROW_COLUMNS)})",
                        [tuple(row[column] for column in _ROW_COLUMNS) for row in read.rows],
                    )
                added += 1
                invalid += read.job["status"] == "invalid"
            total = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        return Ingested(added=added, skipped=skipped, invalid=invalid, total=total)

    def rebuild(self) -> Ingested:
        """Drop the index and read the whole tree again. The tree is the truth; this is cheap."""
        with self._connect() as connection, connection:
            connection.execute("DELETE FROM rows")
            connection.execute("DELETE FROM jobs")
        return self.ingest()

    def _entries(self) -> list[Path]:
        """The directories that are jobs: not the status directory, not a delivery in flight,
        not a dotfile. Sorted, so two scans of one tree read it in one order."""
        if not self.results_root.is_dir():
            return []
        return sorted(
            entry
            for entry in self.results_root.iterdir()
            if entry.is_dir()
            and entry.name != STATUS_DIR
            and not entry.name.endswith(PARTIAL_SUFFIX)
            and not entry.name.startswith(".")
        )

    # -- reading the index ------------------------------------------------------------------

    def jobs(self) -> list[dict[str, Any]]:
        """Every indexed directory, newest delivery first."""
        with self._connect() as connection:
            found = connection.execute(
                "SELECT * FROM jobs ORDER BY delivered_at DESC, name DESC"
            ).fetchall()
        return [_job_dict(row) for row in found]

    def job(self, name: str) -> dict[str, Any]:
        """One indexed directory by its name, or `UnknownJob`."""
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise UnknownJob(name)
        return _job_dict(row)

    def rows(self, job_id: str) -> list[dict[str, Any]]:
        """One job's scenario rows, in the bank's order (the order they were written in)."""
        with self._connect() as connection:
            found = connection.execute(
                "SELECT * FROM rows WHERE job_id = ? ORDER BY rowid", (job_id,)
            ).fetchall()
        return [_row_dict(row) for row in found]

    def rigs(self) -> list[dict[str, Any]]:
        """Each card's status file under `results/status/`, as the agents last wrote them.

        A file that will not parse is listed with its error rather than dropped, for the same
        reason a bank that will not validate is: a card missing from the list is the one failure
        nobody can see from the page.
        """
        directory = self.results_root / STATUS_DIR
        if not directory.is_dir():
            return []
        listed = []
        for path in sorted(directory.glob("*.json")):
            try:
                record = json.loads(path.read_text())
            except (OSError, ValueError) as error:
                listed.append({"file": path.name, "error": str(error)})
                continue
            listed.append({"file": path.name, **record})
        return listed


# -- one directory --------------------------------------------------------------------------


def _read(directory: Path) -> _Read:
    """A delivered directory as a job row and its score rows. Never raises on the file."""
    name = directory.name
    delivered_at = _mtime(directory)
    base: dict[str, Any] = {
        "name": name, "job_id": name, "attempt": None, "kind": "result", "status": "invalid",
        "bank_id": None, "policy": None, "model": _model(directory), "options_json": None,
        "n": None, "summary_json": None, "delivered_at": delivered_at, "error": None,
    }
    attempt = _ATTEMPT.match(name)
    if attempt:
        base.update(
            job_id=attempt["job_id"], attempt=int(attempt["attempt"]), kind="attempt",
            status="failed", error=_attempt_error(directory),
        )
        return _Read(job=base, rows=[])

    path = directory / "results.json"
    if not path.is_file():
        base["error"] = "no results.json"
        return _Read(job=base, rows=[])
    try:
        results = Results.model_validate_json(path.read_bytes())
    except (OSError, ValueError, ValidationError) as error:
        base["error"] = str(error)
        return _Read(job=base, rows=[])

    base.update(
        attempt=results.attempt,
        status="stopped" if results.stopped else "complete",
        bank_id=results.bank.id,
        policy=results.policy,
        options_json=json.dumps(results.options.model_dump(mode="json"), sort_keys=True),
        n=results.summary.n,
        summary_json=json.dumps(results.summary.model_dump(mode="json"), sort_keys=True),
    )
    rows = [
        {
            "job_id": name,
            "scenario_id": result.scenario_id,
            "status": result.status,
            "success": int(result.success),
            "steps": result.steps,
            "route_completion": result.route_completion,
            "cost": result.cost,
            "collisions": json.dumps(result.collisions, sort_keys=True),
            "failure_reason": result.failure_reason,
            "wall_time_s": result.wall_time_s,
            "actor_layout_digest": result.actor_layout_digest,
        }
        for result in results.results
    ]
    return _Read(job=base, rows=rows)


def _model(directory: Path) -> str | None:
    """The checkpoint the job named, off the delivered `job.json`; `None` without one."""
    try:
        return json.loads((directory / "job.json").read_text()).get("checkpoint_path")
    except (OSError, ValueError, AttributeError):
        return None


def _attempt_error(directory: Path) -> str:
    """What an attempt directory says about itself: its exit code and whether a log exists."""
    try:
        code = (directory / "exit_code").read_text().strip() or "?"
    except OSError:
        code = "?"
    log = directory / "container.log"
    return f"exit code {code}; " + (
        f"container.log {log.stat().st_size} bytes" if log.is_file() else "no container.log"
    )


def _mtime(path: Path) -> str:
    """When the directory was last written, as the UTC second every record here stamps."""
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _job_dict(row: sqlite3.Row) -> dict[str, Any]:
    record = dict(row)
    record["options"] = _loads(record.pop("options_json"))
    record["summary"] = _loads(record.pop("summary_json"))
    return record


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    record = dict(row)
    record["success"] = bool(record["success"])
    record["collisions"] = _loads(record["collisions"]) or {}
    return record


def _loads(text: str | None) -> Any:
    return None if text is None else json.loads(text)


__all__ = ["INDEX_NAME", "PARTIAL_SUFFIX", "STATUS_DIR", "Ingested", "ResultsStore", "UnknownJob"]
