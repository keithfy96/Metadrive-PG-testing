"""The rig agent's loop: lock, lease, run, deliver, ack. One worker per card.

Phase 7 Step 5. `scenariobank agent` is one process per rig with one `Worker` thread per card,
and each worker is Step 3's `RunSession` with a queue around it: the lease before, the extend
timer during, and the ack or nack after. The session itself does not change -- `agent --once
job.json` and this loop are the same code path, which is the whole reason the recovery path here
gets exercised on every job rather than only after a crash.

**Lock first, then lease, and the lock is held for milliseconds between jobs.** The queue knows
messages and has no word for a GPU, so the only process that can know a card is free is the one
holding it: a worker takes its card and only then asks for one message, `wait=0`. Nothing
found, it gives the card straight back and sleeps *outside* the lock. The plan's own sketch had
the worker long-polling for 20 s with the card held, and that would have shut CARLA out of the
rig for as long as our queue was empty -- the rig lock is shared between our cards and exclusive
for theirs, so a worker idling on it is a worker denying the machine to everybody else.

**Three answers to a job, and they are not interchangeable** (Phase 7, "Three properties"):

- **Ran** (`completed`, `stopped`): delivered, then `ack`. The rows are real, the partial ones
  included.
- **Can never run** (refused, `permanent: true`): delivered, then `nack(dead=True)`. The other
  rig would refuse it identically, and spending its attempts to find that out is waste.
- **Broke** (failed, vanished, the rig could not start it): delivered, then
  `nack(retry_after=…)`. The card, the driver or the bridge -- worth another go elsewhere.
- **The card is somebody's**: nothing, because no lease was taken. Busy is not failure, and an
  attempt spent on it is one step nearer the dead pile -- the single most likely wrong
  behaviour in this phase, and the reason the lock comes before the lease.

Ack only after delivery, so a lost copy is a retried job and never a lost result; and a failure
is delivered under `<job_id>.attempt<N>` rather than the job's own name, because
`results/<job_id>` existing *is* the redelivery guard (Step 3's finding on the rig).

**A lease is a clock, and the run is longer than it.** `on_tick` extends the lease every
`Timing.extend_every_s` while the container runs and stops the moment it ends. A missed extend
does not lose the job, it duplicates it, which is why the visibility timeout asked for is minutes
and not the server's 30 s default: an agent restarting has that long to find its lease again.

**Restart recovery is adoption, and adoption keeps the lease.** Before a worker leases anything
it looks for a container of ours on its card (`session.adoptable`) and supervises that first.
The holder record beside the card lock carries the message id and the lease id it was leased
under (lock schema 2), so the adopting worker calls `extend` -- accepted while the lease is
alive -- and then acks the job itself. Only if the lease has already expired is the message
redelivered, and then `results/<job_id>` is what turns the redelivery into an ack without a run.
Stopping the agent with a run in flight is the mirror image: the worker extends the lease once
more, leaves the holder record where it is, closes its descriptors and exits. The container keeps
driving. That is what `docker stop` on the agent container does, and it is deliberate.

**The status file is derived, never stored.** `results/status/<host>-gpu<N>.json` on the share
is rewritten from the same reads the supervisor makes -- progress off the record directory, the
lock's own verdict, disk off `statvfs` -- so the studio's "what is running where" is a file read
and no port is open on a rig. The sweep of the rig's local out directory is the one destructive
thing here, and it deletes only what has been delivered: `SCENARIOBANK_OUT/<job_id>` older than
a day, and only where `results/<job_id>` exists on the share, because the local copy is the
evidence until the delivered one is real.

Imports this package's `jobs`, `lock`, `session` and the queue's own client, and nothing else of
ours but `events.utc_now`.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from scenariobank import __version__
from scenariobank.agent.jobs import JobRefused, Resolved, Roots, parse_job, resolve
from scenariobank.agent.lock import Busy, CardLock, Held, LockError
from scenariobank.agent.session import (
    Commands,
    Outcome,
    Progress,
    RunSession,
    SessionError,
    SessionResult,
    adoptable,
    managed,
)
from scenariobank.agent.wfqueue_client import (
    LeasedMessage,
    QueueClient,
    QueueClientError,
    QueueHTTPError,
)
from scenariobank.events import EXIT_CODE_FILE, utc_now

#: The one topic, as the plan names it. The studio puts onto it; every rig's workers lease it.
TOPIC = "metadrive"
#: Where the queue is, and the token if the server was started with one. Both from the
#: environment, both the compose file's business.
QUEUE_URL_VAR = "WFQUEUE_URL"
QUEUE_TOKEN_VAR = "WFQUEUE_TOKEN"
DEFAULT_QUEUE_URL = "http://localhost:9090"
#: Which cards this rig's agent runs a worker for, `0,1`; the CLI's `--gpu` overrides it.
GPUS_VAR = "SCENARIOBANK_GPUS"

#: Under the results root: one file per card, rewritten as the worker goes.
STATUS_DIR = "status"
STATUS_SCHEMA_VERSION = 1

log = structlog.get_logger("scenariobank.agent")


@dataclass(frozen=True)
class Timing:
    """Every wait, in one place, so a test can make the loop run in milliseconds.

    The defaults are the rig's. `lease_s` is the visibility timeout asked for and is minutes,
    not the server's 30 s default, because it is also how long a restarting agent has to pick
    its lease back up before the message is redelivered. `extend_every_s` keeps it well inside
    that. `handover_s` is the last extend before the agent exits with a run in flight.
    """

    lease_s: float = 180.0
    extend_every_s: float = 30.0
    handover_s: float = 600.0
    #: Between empty polls, with the card released. Latency to pick up a job, and the whole cost
    #: of not idling on the rig lock.
    idle_sleep_s: float = 5.0
    #: When the card is somebody's. Busy is not failure; this is the wait.
    busy_sleep_s: float = 15.0
    #: When the queue does not answer. It is on the NAS; the NAS reboots.
    queue_down_sleep_s: float = 15.0
    #: The nack for a run that broke: back to `ready` after this, on this rig or the other.
    retry_after_s: float = 60.0
    #: The session's own poll, `RunSession.supervise(poll_s=)`.
    poll_s: float = 1.0
    status_every_s: float = 10.0
    sweep_every_s: float = 600.0
    sweep_age_s: float = 86400.0


class Handover(Exception):
    """The worker was told to stop while a run was in flight. The run stays; the worker goes."""


@dataclass
class Settled:
    """What the worker told the queue about one message. A record, for the log and the tests."""

    job_id: str
    message_id: int
    what: str  # ack | dead | retry | lost | unreachable
    detail: str = ""


class Worker:
    """One card: its lock, its share of the queue, and one run at a time.

    Made once per card and kept for the life of the agent. Holds the roots, the queue client and
    the seam every subprocess goes through, and no state about any run -- that is read off disk
    by the session, which is what lets a restarted worker describe an adopted run exactly.
    """

    def __init__(
        self,
        gpu: int,
        roots: Roots,
        queue: QueueClient,
        *,
        topic: str = TOPIC,
        consumer: str | None = None,
        commands: Commands | None = None,
        sim_image: str | None = None,
        no_gpu: bool = False,
        bridge: bool = True,
        timing: Timing | None = None,
        stop: threading.Event | None = None,
        lock_root: str | Path | None = None,
        host: str | None = None,
    ) -> None:
        self.gpu = gpu
        self.roots = roots
        self.queue = queue
        self.topic = topic
        self.host = host or socket.gethostname()
        #: The label the queue records on the message: `GET /messages?state=leased` then says
        #: which rig and which card hold every job, and that listing is the "what is running
        #: where" view.
        self.consumer = consumer or f"{self.host}:gpu{gpu}"
        self.commands = commands or Commands()
        self.sim_image = sim_image or os.environ.get("SIM_IMAGE") or None
        self.no_gpu = no_gpu
        self.bridge = bridge
        self.timing = timing or Timing()
        self.stop = stop or threading.Event()
        self.lock = CardLock(gpu, lock_root)
        self.log = log.bind(gpu=gpu, consumer=self.consumer)
        #: Every message this worker settled, in order. The tests read it; the rig reads the log.
        self.settled: list[Settled] = []
        self.jobs_done = 0
        self.queue_ok = True
        self._state = "starting"
        self._status_written = 0.0
        self._swept = 0.0

    # -- the loop -----------------------------------------------------------------------------

    def run(self, *, max_jobs: int = 0) -> int:
        """Adopt, then lock-lease-run-deliver-settle until stopped. Returns the jobs settled.

        `max_jobs` is for a test and for a laptop check: stop after that many settled jobs,
        adopted ones included. `0` is the rig's setting, forever.
        """
        self.adopt_all()
        while not self.stop.is_set() and not (max_jobs and self.jobs_done >= max_jobs):
            self.sweep()
            try:
                held = self.lock.acquire()
            except Busy as busy:
                self.status("busy", detail=busy.holding.sentence())
                self.wait(self.timing.busy_sleep_s)
                continue
            except LockError as error:
                self.log.error("lock unusable", error=str(error))
                self.status("lock unusable", detail=str(error))
                self.wait(self.timing.busy_sleep_s)
                continue

            message = self.lease()
            if message is None:
                held.release()
                self.status("idle" if self.queue_ok else "queue unreachable")
                timing = self.timing
                self.wait(timing.idle_sleep_s if self.queue_ok else timing.queue_down_sleep_s)
                continue

            try:
                self.handle(message, held)
            except Handover:
                return self.jobs_done
            self.jobs_done += 1
        self.status("stopped")
        return self.jobs_done

    def wait(self, seconds: float) -> None:
        """Sleep, but wake the moment the agent is told to stop."""
        self.stop.wait(seconds)

    # -- the queue ----------------------------------------------------------------------------

    def lease(self) -> LeasedMessage | None:
        """One message, now, or `None`. Never raises: a queue that is down is a wait."""
        try:
            message = self.queue.get_one(
                self.topic,
                visibility_timeout=self.timing.lease_s,
                consumer=self.consumer,
                wait=0,
            )
        except QueueHTTPError as error:
            if error.status == 404:
                # The topic is not there yet -- the replica answers 404 for a topic nobody
                # created. Make it and ask again next time round rather than assume the studio
                # went first.
                self.ensure_topic()
            else:
                self.log.warning("lease refused", status=error.status, error=str(error))
            self.queue_ok = False
            return None
        except QueueClientError as error:
            if self.queue_ok:
                self.log.warning("queue unreachable", error=str(error))
            self.queue_ok = False
            return None
        if not self.queue_ok:
            self.log.info("queue reachable again")
        self.queue_ok = True
        return message

    def ensure_topic(self) -> bool:
        """`create_topic` is idempotent, and the agent does it rather than assume the studio did."""
        try:
            self.queue.create_topic(self.topic)
        except QueueClientError as error:
            self.log.warning("cannot create the topic", topic=self.topic, error=str(error))
            return False
        return True

    def _settle(self, message: LeasedMessage, job_id: str, what: str, detail: str = "",
                call: Callable[[], Any] | None = None) -> Settled:
        """Tell the queue, and cope with it disagreeing.

        A 409 is the one answer that carries information: the lease is not ours any more. After
        a run that completed, `GET /messages/{id}` saying `done` means the first ack landed and
        this is the client's own retry; anything else is a real lease loss -- the job was
        redelivered, and `results/<job_id>` on the other worker is what keeps it from running
        twice. A queue that cannot be reached at all is logged and left: the message will come
        back by itself, and the guard answers it then.
        """
        record = Settled(job_id=job_id, message_id=message.id, what=what, detail=detail)
        try:
            if call is not None:
                call()
        except QueueHTTPError as error:
            if error.status == 409:
                state = self._state_of(message.id)
                record.what = "ack" if (what == "ack" and state == "done") else "lost"
                record.detail = f"lease lost ({error}); the message is now {state or 'unknown'}"
            else:
                record.what = "lost"
                record.detail = f"{what} refused: {error}"
        except QueueClientError as error:
            record.what = "unreachable"
            record.detail = f"{what} could not be sent: {error}"
        self.settled.append(record)
        level = self.log.info if record.what in ("ack", "dead", "retry") else self.log.warning
        level("settled", job=job_id, message=message.id, what=record.what, detail=record.detail)
        return record

    def _state_of(self, message_id: int) -> str | None:
        try:
            return str(self.queue.get(message_id).get("state"))
        except (QueueClientError, AttributeError, TypeError):
            return None

    def ack(self, message: LeasedMessage, job_id: str, detail: str = "") -> Settled:
        return self._settle(message, job_id, "ack", detail, message.ack)

    def dead(self, message: LeasedMessage, job_id: str, why: str) -> Settled:
        return self._settle(
            message, job_id, "dead", why, lambda: message.nack(why[:2000], dead=True)
        )

    def retry(self, message: LeasedMessage, job_id: str, why: str) -> Settled:
        return self._settle(
            message, job_id, "retry", why,
            lambda: message.nack(why[:2000], retry_after=self.timing.retry_after_s),
        )  # fmt: skip

    def settle(self, message: LeasedMessage, job_id: str, result: SessionResult) -> Settled:
        """`Outcome` to ack, dead or retry -- the table in the module docstring."""
        if result.ok:
            return self.ack(message, job_id, result.sentence())
        if result.outcome is Outcome.REFUSED:
            return self.dead(message, job_id, result.detail or result.sentence())
        return self.retry(message, job_id, result.sentence())

    # -- one job ------------------------------------------------------------------------------

    def session_for(self, resolved: Resolved) -> RunSession:
        return RunSession(
            resolved,
            self.roots,
            commands=self.commands,
            sim_image=self.sim_image,
            no_gpu=self.no_gpu,
            bridge=self.bridge,
        )

    def handle(self, message: LeasedMessage, held: Held) -> None:
        """One leased message, from validation to a settled queue and a released card.

        Every exit from here releases the card, except the handover, which abandons it on
        purpose (see `Held.abandon`).
        """
        try:
            job = parse_job(message.payload)
            # The queue's count is the attempt. The payload's is the submitter's guess, and the
            # container name, the holder record and a failed run's delivery name all follow the
            # real one.
            job = job.model_copy(update={"attempt": message.attempts or job.attempt or 1})
            resolved = resolve(job, self.roots, gpu=self.gpu)
        except JobRefused as error:
            self.log.warning("job refused", message=message.id, error=str(error))
            payload = message.payload if isinstance(message.payload, dict) else {}
            self.dead(message, str(payload.get("job_id") or "?"), str(error))
            held.release()
            return

        session = self.session_for(resolved)
        job_id = resolved.job_id
        if session.already_delivered():
            self.log.info("already delivered", job=job_id, message=message.id)
            self.ack(message, job_id, f"already delivered to {self.roots.delivered(job_id)}")
            held.release()
            return

        held.publish(
            job_id=job_id,
            attempt=resolved.attempt,
            container=session.name,
            out=str(resolved.out_dir),
            message_id=message.id,
            lease_id=message.lease_id,
        )
        self.log.info("leased", job=job_id, message=message.id, attempt=resolved.attempt)
        try:
            result = self.supervised(session, message, resolved)
        except Handover:
            self.hand_over(message, held, session)
            raise
        except SessionError as error:
            # The rig, not the job: no image, a bridge that will not start, a card the rig does
            # not have, a share that would not take the copy. Whatever is on disk is evidence
            # and goes to the share as an attempt; then the job goes back for another rig.
            self.log.warning("the rig could not run this job", job=job_id, error=str(error))
            self.deliver_evidence(session, Outcome.LAUNCH_FAILED)
            self.retry(message, job_id, f"rig: {error}")
            held.release()
            return
        self.settle(message, job_id, result)
        held.release()

    def supervised(
        self, session: RunSession, message: LeasedMessage | None, resolved: Resolved
    ) -> SessionResult:
        """`session.run()` with the lease extended on every tick. The one place the timer lives."""
        last_extend = time.monotonic()
        lease_lost = False

        def tick(progress: Progress) -> None:
            nonlocal last_extend, lease_lost
            if self.stop.is_set():
                raise Handover()
            self.status("running", resolved=resolved, progress=progress, message=message)
            if message is None or lease_lost:
                return
            now = time.monotonic()
            if now - last_extend < self.timing.extend_every_s:
                return
            try:
                message.extend(visibility_timeout=self.timing.lease_s)
                last_extend = now
            except QueueHTTPError as error:
                if error.status == 409:
                    # Not ours any more. Nothing to do about it mid-drive -- stopping the run
                    # would turn a duplicate into a loss -- so finish, deliver, and let the ack
                    # say what happened.
                    lease_lost = True
                    self.log.warning("lease lost mid-run", job=resolved.job_id, error=str(error))
                else:
                    self.log.warning("extend refused", job=resolved.job_id, error=str(error))
            except QueueClientError as error:
                self.log.warning("extend could not be sent", job=resolved.job_id, error=str(error))

        return session.run(on_tick=tick, poll_s=self.timing.poll_s)

    def deliver_evidence(self, session: RunSession, outcome: Outcome) -> Path | None:
        """Deliver what a run that never properly ran left behind, if anything. Never raises."""
        if not session.out_dir.is_dir():
            return None
        try:
            return session.deliver(outcome)
        except SessionError as error:
            self.log.warning("could not deliver the evidence", error=str(error))
            return None

    def hand_over(
        self, message: LeasedMessage | None, held: Held | None, session: RunSession
    ) -> None:
        """Leave the run driving and the record beside the lock; go.

        The run is a sibling and outlives this process by design. The one thing worth doing on
        the way out is buying the restarted agent time: one more extend, for `handover_s`, so
        the message is not redelivered to the other rig while this one is still driving it.
        """
        if message is not None:
            try:
                message.extend(visibility_timeout=self.timing.handover_s)
            except QueueClientError as error:
                self.log.warning("handover extend failed", job=session.resolved.job_id,
                                 error=str(error))
        self.log.info("handing over", job=session.resolved.job_id, container=session.name)
        self.status("handing over", resolved=session.resolved)
        if held is not None:
            held.abandon()

    # -- adoption -----------------------------------------------------------------------------

    def adopt_all(self) -> int:
        """Finish what this card was doing when the agent was last stopped. Before any lease.

        Each run in flight is supervised, harvested, delivered and -- when the holder record
        names a lease that is still alive -- acked by this worker. An expired lease is not an
        error: the message comes back on its own and `results/<job_id>` answers it.
        """
        count = 0
        for resolved in adoptable(self.roots, self.gpu, self.commands):
            if self.stop.is_set():
                break
            self.adopt(resolved)
            count += 1
        return count

    def adopt(self, resolved: Resolved) -> SessionResult | None:
        session = self.session_for(resolved)
        job_id = resolved.job_id
        message = self._lease_from_record(job_id)
        self.log.info("adopting", job=job_id, container=session.name,
                      lease="live" if message else "none")
        held: Held | None = None
        try:
            held = self.lock.acquire(
                job_id=job_id,
                attempt=resolved.attempt,
                container=session.name,
                out=str(resolved.out_dir),
                message_id=None if message is None else message.id,
                lease_id=None if message is None else message.lease_id,
            )
        except Busy as busy:
            # Somebody took the card between the old agent's death and this one's start. The run
            # is still on it and still ours to harvest; nothing is launched, so nothing here
            # double-books. Say so and carry on.
            self.log.warning("adopting without the card", job=job_id,
                             holder=busy.holding.sentence())
        except LockError as error:
            self.log.warning("adopting without the card", job=job_id, error=str(error))

        result: SessionResult | None = None
        try:
            result = self.supervised(session, message, resolved)
        except Handover:
            self.hand_over(message, held, session)
            raise
        except SessionError as error:
            self.log.warning("adopted run could not be finished", job=job_id, error=str(error))
            if message is not None:
                if session.already_delivered():
                    self.ack(message, job_id, "delivered by another worker")
                else:
                    self.retry(message, job_id, f"rig: {error}")
        else:
            if message is not None:
                self.settle(message, job_id, result)
        finally:
            if held is not None:
                held.release()
        self.jobs_done += 1
        return result

    def _lease_from_record(self, job_id: str) -> LeasedMessage | None:
        """The lease the dead agent held for this job, if the record names one and it is alive."""
        record = self.lock.read_holder()
        if record is None or record.job_id != job_id or record.message_id is None:
            return None
        message = LeasedMessage(
            self.queue,
            {
                "id": record.message_id,
                "lease_id": record.lease_id,
                "topic": self.topic,
                "attempts": record.attempt or 0,
            },
        )
        try:
            message.extend(visibility_timeout=self.timing.lease_s)
        except QueueHTTPError as error:
            self.log.info("the old lease is gone", job=job_id, status=error.status,
                          error=str(error))
            return None
        except QueueClientError as error:
            self.log.warning("cannot reach the queue to keep the old lease", job=job_id,
                             error=str(error))
            return None
        return message

    # -- the status file ----------------------------------------------------------------------

    @property
    def status_file(self) -> Path:
        return self.roots.results / STATUS_DIR / f"{self.host}-gpu{self.gpu}.json"

    def status(
        self,
        state: str,
        *,
        resolved: Resolved | None = None,
        progress: Progress | None = None,
        message: LeasedMessage | None = None,
        detail: str = "",
    ) -> None:
        """Rewrite this card's status file, atomically, when the state changes or the time is up.

        Derived from what was just read and stored nowhere else. Never raises: the share being
        away is a reason to keep running, not to stop.
        """
        now = time.monotonic()
        if state == self._state and now - self._status_written < self.timing.status_every_s:
            return
        self._state = state
        self._status_written = now
        record: dict[str, Any] = {
            "schema_version": STATUS_SCHEMA_VERSION,
            "host": self.host,
            "gpu": self.gpu,
            "consumer": self.consumer,
            "agent_version": __version__,
            "image": self.sim_image,
            "state": state,
            "job_id": None if resolved is None else resolved.job_id,
            "attempt": None if resolved is None else resolved.attempt,
            "message_id": None if message is None else message.id,
            "progress": None if progress is None else {
                "done": progress.done, "n": progress.n, "running": progress.running,
            },
            "detail": detail,
            "jobs_done": self.jobs_done,
            "disk_free_bytes": _disk_free(self.roots.out),
            "updated": utc_now(),
        }
        try:
            self.status_file.parent.mkdir(parents=True, exist_ok=True)
            handle, staged = tempfile.mkstemp(
                dir=str(self.status_file.parent), prefix=".status.", suffix=".tmp"
            )
            with os.fdopen(handle, "w") as stream:
                json.dump(record, stream, indent=2, sort_keys=True)
                stream.write("\n")
            os.chmod(staged, 0o644)
            os.replace(staged, self.status_file)
        except OSError as error:
            self.log.warning("cannot write the status file", path=str(self.status_file),
                             error=str(error))

    # -- the sweep ----------------------------------------------------------------------------

    def sweep(self, *, force: bool = False) -> list[Path]:
        """Delete delivered runs from the rig's own disk, by age. Returns what went.

        Only `SCENARIOBANK_OUT/<job_id>` where `results/<job_id>` exists on the share -- the
        local copy is the evidence until the delivered one is real -- and only when it is older
        than `sweep_age_s`. A run a container of ours still names is never touched, whatever its
        age. Nothing else in the agent removes a run directory, deliberately: it is what an
        adopted run is read from.
        """
        now = time.monotonic()
        if not force and now - self._swept < self.timing.sweep_every_s:
            return []
        self._swept = now
        out, results = self.roots.out, self.roots.results
        if not out.is_dir():
            return []
        try:
            keep = {item.job_id for item in managed(self.commands, self.gpu)}
        except SessionError:
            return []
        gone = []
        cutoff = time.time() - self.timing.sweep_age_s
        share = results.resolve()
        for path in sorted(out.iterdir()):
            if not path.is_dir() or path.name.startswith(".") or path.name in keep:
                continue
            here = path.resolve()
            # The laptop's layout puts the results root INSIDE the out root (`out/results`), and
            # a job called "results" delivered there would make it look like a delivered run.
            # The share is never a run, whatever it is called.
            if here == share or share.is_relative_to(here):
                continue
            if not (results / path.name).is_dir():
                continue
            marker = path / EXIT_CODE_FILE
            try:
                age_of = marker if marker.exists() else path
                if age_of.stat().st_mtime > cutoff:
                    continue
                shutil.rmtree(path)
            except OSError as error:
                self.log.warning("could not sweep", path=str(path), error=str(error))
                continue
            gone.append(path)
            self.log.info("swept", path=str(path))
        return gone


def _disk_free(path: Path) -> int | None:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


class Agent:
    """One process per rig: a worker thread per card, one stop event, one queue client.

    The threads are what let two cards run two jobs at once from one container. The stop event
    is set by the CLI's signal handler; every worker checks it between jobs, on every tick, and
    in every sleep.
    """

    def __init__(
        self,
        gpus: list[int],
        roots: Roots,
        queue: QueueClient,
        *,
        topic: str = TOPIC,
        stop: threading.Event | None = None,
        **worker_options: Any,
    ) -> None:
        if not gpus:
            raise ValueError("an agent needs at least one card")
        self.stop = stop or threading.Event()
        self.queue = queue
        self.topic = topic
        self.workers = [
            Worker(gpu, roots, queue, topic=topic, stop=self.stop, **worker_options)
            for gpu in gpus
        ]

    def run(self, *, max_jobs: int = 0) -> int:
        """Create the topic, start the workers, wait for them. Returns the jobs settled in all."""
        # Once, before anything leases; then again until the queue answers, unless stopped.
        while not self.workers[0].ensure_topic():
            if self.stop.wait(self.workers[0].timing.queue_down_sleep_s):
                break
        threads = [
            threading.Thread(
                target=worker.run, kwargs={"max_jobs": max_jobs},
                name=f"worker-gpu{worker.gpu}", daemon=True,
            )
            for worker in self.workers
        ]
        for thread in threads:
            thread.start()
        # `join` with a timeout, in a loop, so a signal delivered to the main thread is acted on
        # rather than queued behind an uninterruptible join.
        while any(thread.is_alive() for thread in threads):
            for thread in threads:
                thread.join(timeout=0.5)
        return sum(worker.jobs_done for worker in self.workers)


def gpus_from_environment(environ: dict[str, str] | None = None) -> list[int]:
    """`SCENARIOBANK_GPUS`, `0,1`, or card 0 alone when it is unset."""
    env = os.environ if environ is None else environ
    raw = env.get(GPUS_VAR, "").strip()
    if not raw:
        return [0]
    try:
        return sorted({int(item) for item in raw.split(",") if item.strip()})
    except ValueError as error:
        raise ValueError(
            f"{GPUS_VAR}={raw!r} is not a comma-separated list of card indices"
        ) from error


def queue_from_environment(
    url: str | None = None, environ: dict[str, str] | None = None
) -> QueueClient:
    """The queue's client, at `--queue`, else `WFQUEUE_URL`, else localhost."""
    env = os.environ if environ is None else environ
    base = url or env.get(QUEUE_URL_VAR) or DEFAULT_QUEUE_URL
    token = env.get(QUEUE_TOKEN_VAR) or None
    # Short: a `lease` with `wait=0` answers at once, and a queue that does not is a queue that
    # is down, which the loop treats as a wait of its own.
    return QueueClient(base, token=token, timeout=30.0, retries=1)


__all__ = [
    "DEFAULT_QUEUE_URL",
    "GPUS_VAR",
    "QUEUE_TOKEN_VAR",
    "QUEUE_URL_VAR",
    "STATUS_DIR",
    "STATUS_SCHEMA_VERSION",
    "TOPIC",
    "Agent",
    "Handover",
    "Settled",
    "Timing",
    "Worker",
    "gpus_from_environment",
    "queue_from_environment",
]
