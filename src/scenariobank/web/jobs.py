"""One job at a time, and every answer read back off disk.

Two rules, and the second is the one that matters:

**One slot.** Two `generate`s into one bank directory is a corrupt manifest, so a second
submission is refused while one is running, naming what holds the slot.

**No state in memory.** A job's state is rebuilt by reading two files -- `<id>/log` and
`<id>/exit` -- every time it is asked for. The exit file appearing is what "finished" means. That
is why a page reload shows a running job rather than losing it, why restarting the studio does not
orphan one, and why nothing here has to be kept consistent with anything. It is the same property
Phase 7's rig runner turns on, for the same reason, and it is worth getting right here where the
consequence of getting it wrong is a lost log rather than a lost GPU hour.

A process is alive if `/proc/<pid>` exists *and* still reports the start time recorded when it was
launched -- pids are reused, and a reused pid reading as "still running" would be a job that never
finishes.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

#: `<sortable timestamp>-<random>`: sorts by age, and never collides within a second.
_ID = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")

#: The child is a Python process writing to a file, so it block-buffers by default and a long job
#: would show nothing until it ended. Unbuffered output is what makes the log a live one.
_CHILD_ENV = {
    "PYTHONUNBUFFERED": "1",
    "NO_COLOR": "1",
    "TERM": "dumb",
    "COLUMNS": "110",
}


class JobBusy(RuntimeError):
    """The single slot is taken. Carries the job holding it, so the refusal can name it."""

    def __init__(self, holder: dict[str, Any]) -> None:
        super().__init__(
            f"{holder['command']} is already running (job {holder['job_id']}). "
            "The studio runs one job at a time."
        )
        self.holder = holder


class JobNotFound(LookupError):
    """No such job id. Answered as a 404."""


def _start_time(pid: int) -> str | None:
    """Field 22 of `/proc/<pid>/stat`: when this pid began.

    Read after the process name, which is parenthesised and may itself contain spaces -- splitting
    the whole line is the classic way to misread this file.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    fields = stat.rpartition(")")[2].split()
    return fields[19] if len(fields) > 19 else None


def _alive(pid: int | None, start_time: str | None) -> bool:
    """Is *that* process still running -- the one started then, not whatever holds the pid now."""
    return pid is not None and start_time is not None and _start_time(pid) == start_time


class Jobs:
    """The studio's one job slot, rooted at a directory of job directories."""

    def __init__(self, root: Path, *, workdir: Path) -> None:
        self.root = Path(root)
        self.workdir = Path(workdir)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ reading

    def _dir(self, job_id: str) -> Path:
        """A job's directory, refusing an id that is not one. The id reaches this from a URL."""
        if not _ID.match(job_id):
            raise JobNotFound(f"{job_id!r} is not a job id")
        return self.root / job_id

    def status(self, job_id: str, *, since: int = 0) -> dict[str, Any]:
        """One job, read off disk: its state now, and the log written since byte `since`."""
        path = self._dir(job_id)
        try:
            meta = json.loads((path / "meta.json").read_text())
        except (OSError, ValueError) as error:
            raise JobNotFound(f"no job {job_id}") from error

        exit_file = path / "exit"
        state, code = "running", None
        if exit_file.exists():
            state = "finished"
            code = int(exit_file.read_text().strip() or -1)
        elif _start_time(meta["pid"]) == meta["start_time"]:
            state = "running"
        elif _alive(meta.get("studio_pid"), meta.get("studio_start_time")):
            # The process is gone but the studio that launched it is not, so its reaper is between
            # `wait()` returning and the exit file appearing -- microseconds, but a job that flashed
            # "lost" in that window and then went green would teach you to distrust the word.
            state = "running"
        else:
            # Gone, and nothing is left that would ever record how it ended. That is the honest
            # meaning of lost: not "it failed", but "no one was there to say".
            state = "lost"

        log = path / "log"
        text, size = "", 0
        if log.exists():
            size = log.stat().st_size
            if size > since:
                with log.open("rb") as handle:
                    handle.seek(since)
                    text = handle.read().decode("utf-8", "replace")

        return {
            "job_id": job_id,
            "command": meta["command"],
            "argv": meta["argv"],
            "started": meta["started"],
            "state": state,
            "exit_code": code,
            "log": text,
            "log_size": size,
        }

    def running(self) -> dict[str, Any] | None:
        """The job holding the slot, or None. Newest first: the holder is almost always the last."""
        if not self.root.exists():
            return None
        for path in sorted(self.root.iterdir(), reverse=True):
            if not path.is_dir() or not _ID.match(path.name):
                continue
            try:
                status = self.status(path.name)
            except JobNotFound:
                continue
            if status["state"] == "running":
                return status
        return None

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        """The last few jobs, newest first, without their logs."""
        if not self.root.exists():
            return []
        out = []
        for path in sorted(self.root.iterdir(), reverse=True):
            if not path.is_dir() or not _ID.match(path.name):
                continue
            try:
                status = self.status(path.name)
            except JobNotFound:
                continue
            out.append({key: value for key, value in status.items() if key != "log"})
            if len(out) >= limit:
                break
        return out

    # ------------------------------------------------------------------ writing

    def submit(self, argv: list[str], *, command: str) -> dict[str, Any]:
        """Start one job, or raise `JobBusy` naming the one already running."""
        with self._lock:
            holder = self.running()
            if holder is not None:
                raise JobBusy(holder)

            job_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(3).hex()}"
            path = self.root / job_id
            path.mkdir(parents=True)

            log = (path / "log").open("wb")
            try:
                process = subprocess.Popen(  # noqa: S603 - argv is built by invoke.build_argv
                    argv,
                    cwd=self.workdir,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    env={**os.environ, **_CHILD_ENV},
                    # Its own process group, so cancelling kills the whole tree rather than a
                    # shell that has already handed off, and so the job outlives the studio.
                    start_new_session=True,
                )
            finally:
                log.close()

            (path / "meta.json").write_text(
                json.dumps(
                    {
                        "job_id": job_id,
                        "command": command,
                        "argv": argv,
                        "pid": process.pid,
                        "start_time": _start_time(process.pid),
                        # Who owes this job an exit code. Recorded so that a job whose process has
                        # ended can be told apart from one nobody is waiting on any more.
                        "studio_pid": os.getpid(),
                        "studio_start_time": _start_time(os.getpid()),
                        "started": time.time(),
                    }
                )
            )

        threading.Thread(target=self._reap, args=(process, path), daemon=True).start()
        return self.status(job_id)

    def _reap(self, process: subprocess.Popen, path: Path) -> None:
        """Wait for the job and write its exit code. Written last, and atomically: the file
        appearing is the only signal that the job ended, so it must never appear early."""
        code = process.wait()
        temporary = path / "exit.tmp"
        temporary.write_text(f"{code}\n")
        temporary.replace(path / "exit")

    def cancel(self, job_id: str) -> dict[str, Any]:
        """Terminate a running job's whole process group."""
        status = self.status(job_id)
        if status["state"] != "running":
            return status
        meta = json.loads((self._dir(job_id) / "meta.json").read_text())
        # It may have exited between the read above and here; that is a finished job, not an
        # error, and the status re-read below is the answer either way.
        with contextlib.suppress(OSError):
            os.killpg(os.getpgid(meta["pid"]), signal.SIGTERM)
        return self.status(job_id)


__all__ = ["JobBusy", "JobNotFound", "Jobs"]
