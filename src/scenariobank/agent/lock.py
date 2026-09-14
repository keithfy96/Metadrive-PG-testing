"""One card at a time, and never while the rig belongs to somebody else.

Two GPUs on a rig are two resources, and the rig is one. A CARLA evaluation takes the whole
machine -- its synchronous mode permits one ticking client, its bridge is `listen(1)`, and its
host ports are host ports -- while a scenariobank run takes one card and leaves the other free.
Both are true at once, so this module holds **two locks per run** and they are not the same kind
of lock:

    ~/simulation/.wing-sim.gpu.lock      the RIG,  taken SHARED     (LOCK_SH)
    ~/simulation/.wing-sim.gpu<N>.lock   the CARD, taken EXCLUSIVE  (LOCK_EX)

The rig lock is Tyrone's, by name and by path. `deployment/with_rig_lock.sh` takes it
*exclusive*, and so does every other entry point of his -- `run_local.sh` by hand, the GitLab
run-eval job, his orchestrator's queue. Taking it **shared** is what lets our two workers hold
it at the same time while still shutting CARLA out: a reader-writer lock is exactly the relation
between "several of our cards may run together" and "CARLA may not run beside any of them".
Measured, not assumed -- with one shared holder, `flock -n` is refused (exit 1) and `flock -s -n`
is granted.

So, until wing-sim names its lock per device, **a two-GPU rig behaves as a one-GPU rig whenever
CARLA is running**, and not one second longer. That is the cost of his single
`.wing-sim.gpu.lock`, it is a question for him rather than a thing to work around, and nothing
here assumes he will change it: if he ever does, he takes `.wing-sim.gpu<N>.lock` -- this exact
name -- and our shared rig lock keeps working unchanged.

**Exclusion is a property of the INODE.** A lock file is created once and then never removed,
never renamed, never replaced. Anything that swaps the inode destroys mutual exclusion in
complete silence -- two stacks running, no error anywhere -- which is why the holder record is a
*separate* file: it needs atomic replacement, and atomic replacement is a rename. It is also why
the path is computed on both sides from one root (`SIMULATION_ROOT`, defaulting to
`~/simulation`, as his script computes it): a relative path resolved from a different working
directory is a different file and gives exactly zero exclusion while appearing to work. For the
same reason the lock directory must be **local disk** -- on an NFS or SMB mount `flock(2)` is
emulated or ignored, and the emulation does not show up in `/proc/locks` at all.

**`flock` is the authority; `/proc/locks` is a witness, and in a container it is a blind one.**
Both were measured here, on this kernel, with the image the agent actually runs
(`scenariobank-sim:latest`), against a lock held by a process on the host:

| what the container did | no `--pid host` | `--pid host` |
|---|---|---|
| a non-blocking `flock(LOCK_SH)` | refused -- correct | refused -- correct |
| rows in `/proc/locks` | **0, for the whole machine** | 436, ours among them |

`locks_show()` skips every row whose pid it cannot translate into the reading process's pid
namespace, so a private namespace does not see a *filtered* list, it sees an empty one. Two
consequences run through everything below. Acquisition is always an attempt to take the lock and
never a look at who has it -- an attempt is answered correctly across namespaces, and a look is
not. And the agent's own container must run with `--pid host`, or it can still never
double-book a card but can no longer say *who* has one.

**Confirm the lock, do not trust the call.** After `flock(2)` returns we look for our own pid
against this inode in `/proc/locks`. Three outcomes, and each is a different thing to do:
our row is there (confirmed); no row anywhere is there (we are blind -- keep the lock, say so,
and name `--pid host`); rows are there but not ours (a contradiction -- the lock is not what we
think it is, most likely a lock directory on a network mount, so raise and release).

**A pid in `/proc/locks` may be dead, and that never means the lock is stale.** The kernel
records the pid that created the open file description; a child that inherited the descriptor
keeps the lock alive long after that pid exits -- `flock -n 9` in a sourced shell, whose `flock`
process is gone the moment it returns, leaves a row naming it. The only safe reading of a row is
"still locked". Nothing here ever deletes, steals or breaks a lock.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import socket
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from scenariobank.events import utc_now

#: Bumped when a field of `Holder` is added, removed or changes meaning, for the reason every
#: other record in this project carries one: a reader validates rather than coerces.
LOCK_SCHEMA_VERSION = 1

#: The environment variable `deployment/with_rig_lock.sh` reads, and the default it falls back
#: to. Named identically on both sides on purpose -- one root, computed twice, never configured
#: twice.
LOCK_ROOT_VAR = "SIMULATION_ROOT"
DEFAULT_LOCK_ROOT = "~/simulation"

#: Tyrone's, and the whole machine. Ours are the per-device ones beside it.
RIG_LOCK_FILE = ".wing-sim.gpu.lock"
CARD_LOCK_FILE = ".wing-sim.gpu{gpu}.lock"
#: Ours alone, and deliberately not named `.wing-sim.*`: the lock files are a shared path, the
#: holder record is a private format, and a file that looks like his but parses like ours is the
#: kind of thing that costs somebody an afternoon.
HOLDER_FILE = ".scenariobank.gpu{gpu}.holder.json"

PROC_LOCKS = Path("/proc/locks")
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")


class LockError(RuntimeError):
    """The lock cannot be trusted. Not "somebody else has it" -- that is `Busy`."""


def lock_root(root: str | Path | None = None) -> Path:
    """Where the lock files live: the argument, else `$SIMULATION_ROOT`, else `~/simulation`."""
    if root is not None:
        return Path(root).expanduser()
    return Path(os.environ.get(LOCK_ROOT_VAR) or DEFAULT_LOCK_ROOT).expanduser()


def _boot_id() -> str:
    """Identifies this boot, so a pid recorded before a reboot can never be believed."""
    try:
        return BOOT_ID_PATH.read_text().strip()
    except OSError:
        return ""


def _starttime(pid: int) -> str:
    """Field 22 of `/proc/<pid>/stat` -- when this pid was created.

    With the boot id it pins a pid to one process. Pids are recycled, and a recycled pid that
    matches a record is a stranger wearing the holder's name. The comm field is parenthesised and
    may itself contain spaces and parentheses, so the split is on the LAST `") "`.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return ""
    try:
        return stat.rsplit(") ", 1)[1].split()[19]
    except IndexError:
        return ""


def _proc_locks() -> list[str] | None:
    """Every line of `/proc/locks`, or `None` when it cannot be read at all."""
    try:
        return PROC_LOCKS.read_text().splitlines()
    except OSError:
        return None


def holders(path: Path, rows: list[str] | None = None) -> list[int]:
    """The pids holding an `flock(2)` on this file, as `/proc/locks` reports them.

    Non-invasive by construction: it opens no descriptor on the lock, so it can never itself be
    the reason another caller is refused. A `trylock` used as a liveness poll makes every poll a
    contender, and a spurious refusal is indistinguishable from a busy rig.

    Two things it is not. It is not an authority -- see the module docstring; in a private pid
    namespace it returns `[]` for a lock that is firmly held. And it is not a way to find a
    *stale* lock: a dead pid here is normal (the descriptor outlives its creator), so a row is
    only ever read as "still locked".

    POSIX record locks (`fcntl.lockf`) are a separate lock space entirely and are reported as
    `POSIX` rows: one acquires happily while an `flock(2)` is held, so a probe made with the
    wrong kind reports "free" every single time. Only `FLOCK` rows are read here, shared
    (`READ`) and exclusive (`WRITE`) alike, since our rig lock is a shared one.
    """
    try:
        stat = path.stat()
    except OSError:
        return []
    # /proc/locks renders the device as hex major:minor, then the inode. Measured to be identical
    # inside a container for a bind-mounted file, because a bind mount shares the superblock.
    target = f"{os.major(stat.st_dev):02x}:{os.minor(stat.st_dev):02x}:{stat.st_ino}"

    lines = _proc_locks() if rows is None else rows
    found: list[int] = []
    for line in lines or []:
        fields = line.split()[1:]
        # A blocked waiter is listed under the holder it waits on, as `-> FLOCK ...`. It is not a
        # holder, and counting it as one means reporting a lock acquired that is still being
        # waited for.
        if not fields or fields[0] == "->":
            continue
        if len(fields) < 5 or fields[0] != "FLOCK" or fields[4] != target:
            continue
        try:
            found.append(int(fields[3]))
        except ValueError:
            continue
    return found


class Holder(BaseModel):
    """Who has this card, published beside the lock once the lock is CONFIRMED held.

    Advisory, never authoritative, and always potentially stale -- a holder killed with SIGKILL
    cannot withdraw its own record. So it is read only when the lock is held, and believed only
    when the boot id and the process's start time still agree with it. The lock answers *whether*;
    this file answers *who*, and only ever as a second question.

    `pgid` is recorded rather than looked up later: `os.getpgid()` raises as soon as the group
    leader exits, even though the group still exists and still holds the lock, so deriving it at
    teardown time throws exactly when the teardown needs it.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = LOCK_SCHEMA_VERSION
    gpu: int
    #: The queue message and its attempt, when this worker has leased one. `None` between taking
    #: the card and leasing a job, which is a state that exists on purpose: the worker locks the
    #: card first and only then asks the queue for work.
    job_id: str | None = None
    attempt: int | None = None
    pid: int
    pgid: int
    boot_id: str
    starttime: str
    host: str
    #: The container the job is running in, once there is one; the name a sweep matches on.
    container: str | None = None
    #: Where the run writes, so a human reading this file knows where to look next.
    out: str | None = None
    acquired: str
    #: Rewritten on every republish, so "when did it last say anything" is answerable.
    updated: str


def _record(gpu: int, **fields) -> Holder:
    pid = os.getpid()
    now = utc_now()
    return Holder(
        gpu=gpu,
        pid=pid,
        pgid=os.getpgrp(),
        boot_id=_boot_id(),
        starttime=_starttime(pid),
        host=socket.gethostname(),
        acquired=now,
        updated=now,
        **fields,
    )


@dataclass(frozen=True)
class Holding:
    """What this process can tell about a lock somebody else has.

    Three verdicts and no fourth, because there are three genuinely different situations and
    conflating any two of them is how a supervisor ends up either tearing down a stranger's run
    or waiting forever on its own.
    """

    #: `"rig"` or `"card"` -- which of the two locks refused us, which is most of the answer.
    scope: Literal["rig", "card"]
    path: Path
    #: From `/proc/locks`; empty when it told us nothing, which is not the same as nobody.
    pids: list[int] = field(default_factory=list)
    holder: Holder | None = None
    #: Whether `/proc/locks` said anything at all about any lock on this machine.
    visible: bool = True

    @property
    def verdict(self) -> Literal["ours", "foreign", "unknown"]:
        """`ours` when a live record of ours matches; `foreign` when we can see and it is not
        ours; `unknown` when we cannot see -- a container without `--pid host`.

        `foreign` is the one that must exist. Somebody ran `run_local.sh` by hand, or a GitLab
        pipeline is mid-eval, and the only correct response is to wait. Never tear that stack
        down, and never signal it: a hand-run `flock` sits in the operator's own shell process
        group, so killing "the holder's group" takes out their interactive shell with it.
        """
        if self.holder is not None:
            return "ours"
        return "foreign" if self.visible else "unknown"

    def sentence(self) -> str:
        """One line for a log, naming what is knowable and no more than that."""
        who = f" pid(s) {', '.join(str(pid) for pid in self.pids)}" if self.pids else ""
        if self.verdict == "ours":
            job = self.holder.job_id if self.holder else None
            return f"{self.path.name} is held by this agent{who}" + (f", job {job}" if job else "")
        if self.verdict == "foreign":
            return f"{self.path.name} is held by somebody else{who} -- waiting, not touching it"
        return (
            f"{self.path.name} is held, by whom this process cannot see "
            f"(no rows in /proc/locks -- a container without --pid host)"
        )


class Busy(RuntimeError):
    """The card, or the rig, is somebody's. Nothing was taken and nothing needs releasing.

    Not a failed attempt: no job was leased, so there is nothing to nack and nothing to report
    upstream. The worker sleeps and asks again.
    """

    def __init__(self, holding: Holding) -> None:
        super().__init__(holding.sentence())
        self.holding = holding


class CardLock:
    """The two lock files that stand between a worker and one GPU.

    One instance per card, made once and kept for the life of the worker. It holds no state
    between acquisitions except the paths: everything that outlives a call is on disk, which is
    what lets an agent restarted mid-run describe the rig correctly.
    """

    def __init__(self, gpu: int, root: str | Path | None = None) -> None:
        self.gpu = gpu
        self.root = lock_root(root)
        self.rig_file = self.root / RIG_LOCK_FILE
        self.card_file = self.root / CARD_LOCK_FILE.format(gpu=gpu)
        self.holder_file = self.root / HOLDER_FILE.format(gpu=gpu)

    def ensure_exists(self) -> None:
        """Create either lock file if it is missing, without disturbing one that is not.

        `touch` semantics, deliberately: it preserves the inode. Mode 0666 because the rig is
        shared -- we run as one uid and the GitLab shell runner may not -- and whoever creates
        the file first under a 022 umask would otherwise lock the other user out of a lock that
        is free. `flock(2)` itself needs only read access, so this is about the file's creation
        and never about who may take it.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        for path in (self.rig_file, self.card_file):
            if not path.exists():
                os.close(os.open(path, os.O_CREAT | os.O_RDONLY, 0o666))
            # open(2) applies the umask; the rig needs the mode it was asked for. Suppressed
            # because the file may be somebody else's, made with a mode of their choosing.
            with contextlib.suppress(OSError):
                os.chmod(path, 0o666)

    # -- reading, without ever becoming a contender ---------------------------------------------

    def read_holder(self) -> Holder | None:
        """The record beside the card lock, or `None` when there is none that parses."""
        try:
            raw = self.holder_file.read_text()
        except OSError:
            return None
        try:
            return Holder.model_validate_json(raw)
        except ValueError:
            return None

    def _live_holder(self, pids: list[int]) -> Holder | None:
        """The record, accepted only if it still describes a process the kernel agrees exists.

        The lock is tested first by the caller, because a readable record proves nothing on its
        own -- the process that wrote it may have been killed an hour ago. When the lock is held,
        the record is believed only if it names a pid holding it, on this boot, created at the
        recorded moment. Anything less is a foreign holder, which is a perfectly good answer.
        """
        record = self.read_holder()
        if record is None:
            return None
        if record.pid not in pids:
            return None
        if record.boot_id != _boot_id() or record.starttime != _starttime(record.pid):
            return None
        return record

    def look(self, scope: Literal["rig", "card"] = "card") -> Holding | None:
        """Who has a lock, or `None` when nothing visibly does. Opens no descriptor on it.

        `None` means "nothing is visible", which on a machine where `/proc/locks` is blind means
        nothing at all. It is a diagnosis, never a decision: the decision is `acquire()`.
        """
        path = self.rig_file if scope == "rig" else self.card_file
        rows = _proc_locks()
        pids = holders(path, rows)
        if not pids:
            return None
        return Holding(
            scope=scope,
            path=path,
            pids=pids,
            holder=self._live_holder(pids) if scope == "card" else None,
            visible=True,
        )

    # -- taking it ------------------------------------------------------------------------------

    def acquire(
        self,
        *,
        job_id: str | None = None,
        attempt: int | None = None,
        container: str | None = None,
        out: str | None = None,
    ) -> Held:
        """Take the rig lock shared and this card exclusive, or raise `Busy` having taken neither.

        Never blocks. A worker that waits on a lock is a second queue -- one that is not FIFO,
        that the real queue cannot see, and that can therefore overtake it -- so a held lock is a
        sleep and a retry in the worker's own loop, where the wait is visible.

        Order is rig first, then card, and the same order everywhere so two of our own workers
        cannot arrange a cycle between them. Both are non-blocking, so neither can deadlock; if
        the card is refused the rig lock is dropped again before returning, because holding it
        alone locks CARLA out of a machine we are not using.
        """
        self.ensure_exists()
        rig = self._take(self.rig_file, fcntl.LOCK_SH, "rig")
        try:
            card = self._take(self.card_file, fcntl.LOCK_EX, "card")
        except BaseException:
            os.close(rig)
            raise

        held = Held(self, rig, card)
        try:
            held.confirmed, held.note = self._confirm()
            held.publish(job_id=job_id, attempt=attempt, container=container, out=out)
        except BaseException:
            held.release()
            raise
        return held

    def _take(self, path: Path, how: int, scope: Literal["rig", "card"]) -> int:
        """`flock` one file, returning the descriptor that now holds it.

        Opened READ-ONLY. A write or append open needs write permission, which the other user on
        this rig may not have -- and an unavailable lock reported as a broken deployment, on a
        machine where the lock is in fact free, is a morning gone.
        """
        try:
            handle = os.open(path, os.O_RDONLY)
        except OSError as error:
            raise LockError(
                f"cannot open {path}: {error} -- check its mode, it wants 0666"
            ) from error
        try:
            fcntl.flock(handle, how | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(handle)
            refused = self.look(scope) or Holding(scope=scope, path=path, visible=False)
            raise Busy(refused) from error
        except OSError as error:
            os.close(handle)
            raise LockError(
                f"cannot lock {path}: {error} -- is the lock directory local disk?"
            ) from error
        return handle

    def _confirm(self) -> tuple[bool, str]:
        """Find our own lock in `/proc/locks`, and say plainly when we cannot.

        The step this implements says to confirm by finding the process in `/proc/locks` rather
        than by trusting a return value. That is right, and it has one honest failure: in a pid
        namespace the file is empty for every lock on the machine, our own included. So an empty
        file is not a contradiction -- it is a witness that cannot see, and a run must not be
        refused over it, because the `flock` that refuses a double-booking works there anyway.

        Rows present without ours among them IS a contradiction, and the likeliest cause by far
        is a lock directory on a network mount, where `flock(2)` is emulated and excludes nobody.
        """
        rows = _proc_locks()
        if rows is None:
            return False, "/proc/locks cannot be read, so the lock is taken but unconfirmed"
        if not rows:
            return False, (
                "/proc/locks is empty for the whole machine, so the lock is taken but "
                "unconfirmed and no foreign holder can be named -- run this container with "
                "--pid host"
            )
        mine = os.getpid()
        missing = [
            path
            for path in (self.rig_file, self.card_file)
            if mine not in holders(path, rows)
        ]
        if missing:
            raise LockError(
                f"flock(2) granted {', '.join(path.name for path in missing)} but /proc/locks "
                f"does not report pid {mine} holding it. The lock excludes nobody -- is "
                f"{self.root} on a network mount? It must be local disk."
            )
        return True, ""


class Held:
    """A card this process has, until it says otherwise.

    A context manager, and also an object a long-lived worker keeps across a whole job: the lock
    is taken before the queue is asked for work and released after the results are delivered, so
    the card is never free for the instant between two of ours.
    """

    def __init__(self, lock: CardLock, rig: int, card: int) -> None:
        self.lock = lock
        self._rig = rig
        self._card = card
        self.confirmed = False
        self.note = ""
        self.holder: Holder | None = None
        self.released = False

    @property
    def gpu(self) -> int:
        return self.lock.gpu

    def publish(self, **fields) -> Holder:
        """Write who we are beside the lock, atomically. Call again when the job is known.

        Only the fields named here change; `acquired` stands and `updated` moves, so "when did
        this worker take the card" and "when did it last say anything" stay two different
        questions. Publishing after `release()` is refused rather than done: a record beside a
        lock nobody holds is the one misleading state this file has.

        `mkstemp` + `fsync` + `rename`, into the same directory, because a plain
        truncate-and-write is observable half-finished: an agent restarting on a torn record
        concludes that no job of its own is running and starts a second one on top of the live
        one. The rename is why this is a separate file from the lock -- it gives the path a new
        inode, which is the one thing a lock file must never survive.
        """
        if self.released:
            raise LockError("the card was released; a record beside no lock says nothing true")
        holder = self.holder
        if holder is None:
            holder = _record(self.lock.gpu, **fields)
        else:
            holder = holder.model_copy(update={**fields, "updated": utc_now()})
        handle, staged = tempfile.mkstemp(dir=str(self.lock.root), prefix=".holder.", suffix=".tmp")
        try:
            with os.fdopen(handle, "w") as stream:
                json.dump(holder.model_dump(mode="json"), stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(staged, 0o644)  # mkstemp makes it 0600; the rig is shared and reads it
            os.replace(staged, self.lock.holder_file)
        except BaseException:
            Path(staged).unlink(missing_ok=True)
            raise
        self.holder = holder
        return holder

    def release(self) -> None:
        """Drop the card, then the rig, and take the holder record with them.

        The record goes first: a record with no lock is history, and a lock with no record is
        merely an unnamed holder -- the first is misleading and the second is not. Closing the
        descriptors is what releases the locks; the files themselves are never touched.
        """
        self.released = True
        self.lock.holder_file.unlink(missing_ok=True)
        for handle in (self._card, self._rig):
            with contextlib.suppress(OSError):
                os.close(handle)
        self.holder = None

    def __enter__(self) -> Held:
        return self

    def __exit__(self, *_exception) -> None:
        self.release()


__all__ = [
    "CARD_LOCK_FILE",
    "DEFAULT_LOCK_ROOT",
    "HOLDER_FILE",
    "LOCK_ROOT_VAR",
    "LOCK_SCHEMA_VERSION",
    "RIG_LOCK_FILE",
    "Busy",
    "CardLock",
    "Held",
    "Holder",
    "Holding",
    "LockError",
    "holders",
    "lock_root",
]
