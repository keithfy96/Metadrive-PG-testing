"""One job, from a free card to a delivered result: the run session.

Phase 7 Step 3. Take the card (Step 2), start this card's bridge if the job needs one, launch the
run as a **sibling container**, follow it to its end, deliver what it wrote, and remove what it
started. `scenariobank agent --once job.json` is this file with a file for a queue; Step 5 is
this file in a loop with a lease around it. The two are the same code path on purpose -- a
recovery path that is only exercised after a crash is a path that is never exercised.

**A sibling container, not a child, and that is the whole shape.** A bank of 35 scenarios with a
model on the car is tens of minutes, and restarting the agent must not kill one. `start_new_session`
escapes a process group and a session; it escapes neither a PID namespace nor a cgroup, so a
detached child of this process dies with `docker stop` on the agent container. wing-sim measured
exactly that (`rig/session.py:236-258`): the child died and released the lock while the CARLA and
eval containers kept running, orphaned. So the run is `docker run --detach`, owned by the daemon,
and this process only watches it.

**Which makes every answer a file read.** The session holds no progress of its own: how far the
run has got is `batch.json`'s `n` against the files in `results/`, and how it ended is
`<out>/exit_code`. Both are read back off disk every poll, so an agent restarted mid-run
describes the run exactly as the one that launched it did, and `adopt()` is a normal launch with
the launching skipped rather than a reconciliation.

**The container is NOT `--rm`.** An exited container is how an agent that was down when the run
ended finds out that it ended, and where the run's stdout still is. `harvest()` writes the log
beside the results and removes the container; nothing else removes it.

**Two paths for two different jobs.** `<out_dir>` holds `events.jsonl` and `exit_code` -- the
process's files -- and `<batch_dir>`, which is the same directory plus the job's tier when it
names one, holds `batch.json`, `starts/`, `results/` and `results.json`. Looking in one place for
both reports a finished run as one that wrote nothing.

**Delivery is a copy and a rename, and it happens whatever the outcome.** The container writes to
this rig's local disk; the agent copies that to `results/<job_id>.partial` on the share and
renames it to `results/<job_id>`, so a directory without `.partial` is always complete and an
existing one means "done, do not run this again". A failed run is delivered too: the evidence of
a failure is worth more than the disk it costs, and Step 5 acks only after delivery so a lost
copy is a retried job rather than a lost result.

Imports `scenariobank.events` and this package's `jobs`, and nothing else of ours.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from scenariobank.agent.jobs import CONTAINER_OUT, Resolved, Roots, checkout
from scenariobank.events import (
    BATCH_FILE,
    EVENTS_FILE,
    STARTS_DIR,
    read_events,
    read_exit_code,
)

#: Every container `scripts/sim-run.sh` starts carries the first label, hand-run ones included,
#: so a sweep for strays finds those too. The other three are the query an agent restarted
#: mid-run uses to find the run it launched -- `docker ps --filter label=` is exact where a name
#: prefix is a guess, and a record this process wrote could be stale.
LABEL_MANAGED = "scenariobank.managed-by"
LABEL_JOB = "scenariobank.job-id"
LABEL_ATTEMPT = "scenariobank.attempt"
LABEL_GPU = "scenariobank.gpu"
MANAGED_BY = "scenariobank"

#: The bridge, one per card: gpu0 on 5600, gpu1 on 5601 *(Keith, 2026-09-15)*. Deliberately not
#: 5558 -- wing-sim's zapeta bridge listens there on the same rig, and both stacks use host
#: networking, so a collision with theirs is an error rather than our simulator driving against
#: their planner, and the two stacks can run at once on different cards.
BRIDGE_BASE_PORT = 5600
BRIDGE_NAME = "bridge-gpu{gpu}"

#: The two policies that talk to the bridge, NAMED rather than imported: importing
#: `scenariobank.av3` pulls torch and a CUDA context into a supervisor whose whole value is that
#: it keeps working when the run it launched has died. `test_session.py` asserts this tuple is
#: every `BridgePolicy` in that package, so a third one fails a test rather than silently
#: running against a bridge nobody started.
BRIDGE_POLICIES = ("scenariobank.av3:BridgePolicy", "scenariobank.av3:AV3Policy")

#: How often the supervisor re-reads the record directory. A scenario is seconds at its fastest
#: and about a second per decision with the model on the car, so this is already finer than the
#: data arrives; it is also how often `on_tick` gets to extend a lease.
POLL_S = 1.0

#: How long `docker stop` waits for the run to finish the scenario it is in before SIGKILL. The
#: runner's SIGTERM path ends that row `stopped`, writes what it scored and closes the env on the
#: normal path -- a scored partial run instead of a lost one -- and 30 s is the budget Phase 7
#: Step 1 measured that path against.
STOP_GRACE_S = 30

#: The job file the agent writes into the run's own directory, in the container's own paths.
JOB_FILE = "job.json"
#: The run container's stdout, kept beside the results when the container is removed.
LOG_FILE = "container.log"
#: `run_bank`'s per-scenario record directory, which is the progress numerator.
RESULTS_DIR = "results"


class SessionError(RuntimeError):
    """The rig failed us, not the job: no image, no docker, a bridge that will not start.

    Never permanent. Step 5 nacks with a retry rather than dead-lettering, because the same job
    on the other rig, or on this one in ten minutes, may be perfectly runnable.
    """


class Outcome(str, Enum):
    """What one run did, after the exit code and the event stream have both been read.

    The exit code is never enough on its own, in both directions: a stopped run exits **0** with
    half its rows scored, and a job file that will not parse exits 1 exactly as a driver failure
    does. `run.finished` carries the judgement that separates them.
    """

    #: Ran to the end of the batch. Rows may still have failed -- that is a score, not an error.
    COMPLETED = "completed"
    #: Told to stop and did. The rows before the stop are real and the one it landed in is
    #: `failure_reason: "stopped"`. Exit code 0, deliberately.
    STOPPED = "stopped"
    #: Nothing ran and nothing this machine does will change that: the bank at that path is a
    #: different bank, a scenario id is not in it, the policy will not import. Dead-letter it.
    REFUSED = "refused"
    #: It started and something broke. May be the card, the driver or the bridge, so it is worth
    #: another attempt somewhere.
    FAILED = "failed"
    #: The container is gone and left no exit code: SIGKILL, the OOM killer, the machine going
    #: down. Distinguished from every code above by ABSENCE, which is why `read_exit_code`
    #: returns `None` rather than raising. wing-sim calls this one VANISHED.
    VANISHED = "vanished"
    #: The container was never created. A rig fault, and the run never began.
    LAUNCH_FAILED = "launch_failed"


@dataclass(frozen=True)
class Progress:
    """How far the run has got, derived from its record directory and stored nowhere.

    The denominator is `batch.json`'s `n`, the numerator is the number of files in `results/`,
    and the row running now is the one in `starts/` with no result beside it. wing-sim's
    `rig/progress.py` keeps this property and pays for it with four prose regexes; we emit the
    files instead, but the property is the same one and it is the one that matters: an agent that
    keeps progress in a variable breaks restart recovery silently.
    """

    started: bool = False
    n: int | None = None
    done: int = 0
    running: str | None = None

    @classmethod
    def read(cls, batch_dir: Path) -> Progress:
        """Read the directory. Never raises: a directory that is not there yet is no progress."""
        try:
            batch = json.loads((batch_dir / BATCH_FILE).read_text())
        except (OSError, ValueError):
            return cls()
        finished = {
            path.stem for path in (batch_dir / RESULTS_DIR).glob("*.json") if path.is_file()
        }
        started = {
            path.stem for path in (batch_dir / STARTS_DIR).glob("*.json") if path.is_file()
        }
        order = [str(name) for name in batch.get("scenarios", [])]
        running = next((name for name in order if name in started and name not in finished), None)
        count = batch.get("n")
        return cls(
            started=True,
            n=int(count) if isinstance(count, int) else len(order) or None,
            done=len(finished),
            running=running,
        )

    def sentence(self) -> str:
        """One line for a log, for a person."""
        if not self.started:
            return "not started"
        total = "?" if self.n is None else str(self.n)
        where = f", running {self.running}" if self.running else ""
        return f"{self.done}/{total}{where}"


@dataclass(frozen=True)
class SessionResult:
    """What the session did, and everything a caller needs to decide what to do about it."""

    outcome: Outcome
    #: The run's own code, from `<out>/exit_code`; `None` means it never got to say.
    exit_code: int | None
    #: `run.finished`'s judgement. `True` is a dead letter, never a retry.
    permanent: bool
    progress: Progress
    #: Where the results were delivered, or `None` when nothing was delivered.
    delivered: Path | None = None
    #: The container's own exit code, read before it was removed. Differs from `exit_code` when
    #: the process was killed outright -- 137 with no file is the OOM killer's signature.
    container_exit: int | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome in (Outcome.COMPLETED, Outcome.STOPPED)

    def sentence(self) -> str:
        parts = [f"{self.outcome.value}", self.progress.sentence()]
        if self.exit_code is not None:
            parts.append(f"exit {self.exit_code}")
        if self.detail:
            parts.append(self.detail)
        return ", ".join(parts)


@dataclass
class Commands:
    """Every process this module starts goes through one call, so a test can replace it.

    Not a docker client: `scripts/sim-run.sh` and `scripts/bridge.sh` are the two lines this
    repo runs, written once and used by the laptop, the Phase 5 Step 4 gate and the rig alike,
    and a session that built its own `docker run` would be a fourth version of them.
    """

    #: Seconds before a command is given up on. Generous: `docker run --detach` returns as soon
    #: as the container is created, but `sim-image.sh status` runs first and an image the daemon
    #: has to load is not instant.
    timeout: float = 300.0

    def __call__(
        self, argv: Sequence[str], *, env: dict[str, str] | None = None, cwd: Path | None = None
    ) -> tuple[int, str]:
        """Run it, capture stdout and stderr together, and return `(code, output)`.

        Nothing is raised for a non-zero code: every caller here has a different answer to a
        failure, and the ones that matter -- a card that is busy, a container that is gone -- are
        not failures at all.
        """
        try:
            done = subprocess.run(
                list(argv),
                env=env,
                cwd=None if cwd is None else str(cwd),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                timeout=self.timeout,
            )
        except FileNotFoundError as error:
            return 127, str(error)
        except subprocess.TimeoutExpired as error:
            return 124, f"timed out after {self.timeout}s: {error}"
        return done.returncode, done.stdout or ""


@dataclass
class ContainerState:
    """What docker says about one container, or `None` from `state()` when there is none."""

    name: str
    running: bool
    exit_code: int | None


class RunSession:
    """One job in one container, supervised from outside it.

    Made per job. It holds the resolved job, the rig's roots and the seam every subprocess goes
    through -- and no state about the run itself, which is read off disk each time it is asked.
    """

    def __init__(
        self,
        resolved: Resolved,
        roots: Roots,
        *,
        commands: Commands | None = None,
        sim_image: str | None = None,
        no_gpu: bool = False,
        bridge: bool = True,
    ) -> None:
        self.resolved = resolved
        self.roots = roots
        self.commands = commands or Commands()
        self.sim_image = sim_image or os.environ.get("SIM_IMAGE") or None
        #: Take the card's lock but run the container without a card: the laptop, and any rig
        #: check that only needs `ExpertPolicy`. `GPU` is still passed, because `sim-run.sh`
        #: reads it for the label even when `NO_GPU` has already decided there is no `--gpus`.
        self.no_gpu = no_gpu
        self.bridge = bridge
        #: Set when `stop()` was asked for, because the exit code afterwards is a 0 like any
        #: other and the intent is the only evidence that it was not a normal end.
        self.stop_requested = False

    # -- names and paths --------------------------------------------------------------------

    @property
    def name(self) -> str:
        return self.resolved.container_name

    @property
    def out_dir(self) -> Path:
        return self.resolved.out_dir

    @property
    def bridge_name(self) -> str:
        return BRIDGE_NAME.format(gpu=self.resolved.gpu)

    @property
    def bridge_port(self) -> int:
        return BRIDGE_BASE_PORT + self.resolved.gpu

    @property
    def needs_bridge(self) -> bool:
        """Does this job's policy talk to the openpilot bridge? By name; see `BRIDGE_POLICIES`."""
        return self.bridge and self.resolved.job.policy in BRIDGE_POLICIES

    def script(self, name: str) -> Path:
        """One of this repo's two launch scripts, as THIS process sees it."""
        return checkout() / "scripts" / name

    # -- the environment a child is given -----------------------------------------------------

    def child_environment(self) -> dict[str, str]:
        """Exactly what `sim-run.sh` is told, and nothing inherited.

        wing-sim's `rig/compose.py::child_environment` passes five variables and the reason it
        gives is the one that matters here too: a developer's exported setting otherwise silently
        changes what a model is scored on, and a score nobody can reproduce is worse than no
        score. `PATH` and `HOME` are the two the script itself cannot do without -- it needs to
        find `docker`, and `docker` needs a config directory.
        """
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/tmp"),
            # The mounts, as the DAEMON must be told them. Every one of these is a host path,
            # and the agent container is why they cannot be this process's own: it reads the
            # share at the same path only because it mounts it at the same path.
            "REPO_DIR": str(self.roots.repo),
            "OUT_DIR": str(self.out_dir),
            "BANK_DIR": str(self.resolved.bank_dir),
            # The run's identity, for the labels a restarted agent queries.
            "JOB_ID": self.resolved.job_id,
            "ATTEMPT": str(self.resolved.attempt),
            "GPU": str(self.resolved.gpu),
            "NAME": self.name,
            # The daemon owns the run, not this process. See the module docstring.
            "DETACH": "1",
        }
        if self.resolved.models_dir is not None:
            env["MODELS_DIR"] = str(self.resolved.models_dir)
        if self.sim_image:
            env["SIM_IMAGE"] = self.sim_image
        if self.no_gpu:
            env["NO_GPU"] = "1"
        if self.needs_bridge:
            env["BRIDGE_PORT"] = str(self.bridge_port)
        return env

    # -- docker, as questions rather than prose -----------------------------------------------

    def state(self, name: str) -> ContainerState | None:
        """What docker says about a container, or `None` for one that does not exist.

        A filter rather than `docker inspect`, which exits 1 for a name that is not there and
        would make "no such container" indistinguishable from "the daemon is down".
        """
        code, output = self.commands(
            [
                "docker", "ps", "--all", "--no-trunc",
                "--filter", f"name=^/{name}$",
                "--format", "{{.State}}\t{{.Names}}",
            ]
        )  # fmt: skip
        if code != 0:
            raise SessionError(f"cannot ask docker about {name}: {output.strip()}")
        line = next((row for row in output.splitlines() if row.strip()), None)
        if line is None:
            return None
        status = line.split("\t", 1)[0].strip()
        running = status == "running"
        return ContainerState(name=name, running=running, exit_code=self._exit_code(name, running))

    def _exit_code(self, name: str, running: bool) -> int | None:
        if running:
            return None
        code, output = self.commands(
            ["docker", "inspect", "-f", "{{.State.ExitCode}}", name]
        )
        if code != 0:
            return None
        try:
            return int(output.strip())
        except ValueError:
            return None

    def ours(self) -> list[tuple[str, str]]:
        """`(container name, job id)` for every container of ours on this card, running or not.

        The adopt query. By label and not by name: the container outlives the supervisor by
        design, and after a restart this is the only thing that says whose it is without
        consulting a record that could be stale.
        """
        code, output = self.commands(
            [
                "docker", "ps", "--all", "--no-trunc",
                "--filter", f"label={LABEL_MANAGED}={MANAGED_BY}",
                "--filter", f"label={LABEL_GPU}={self.resolved.gpu}",
                "--format", "{{.Names}}\t{{.Label \"" + LABEL_JOB + "\"}}",
            ]
        )  # fmt: skip
        if code != 0:
            raise SessionError(f"cannot list our containers: {output.strip()}")
        found = []
        for row in output.splitlines():
            if not row.strip():
                continue
            name, _, job_id = row.partition("\t")
            found.append((name.strip(), job_id.strip()))
        return found

    # -- the bridge ---------------------------------------------------------------------------

    def ensure_bridge(self) -> str | None:
        """Start this card's bridge if the job needs one and it is not already up.

        Asked as a docker question rather than by reading `bridge.sh status`, whose output is
        advice for a person: a stopped container holding the name is a `die` there with two
        commands to run, and a supervisor has to act rather than be advised. A bridge that will
        not start is a **rig** fault -- the job is fine and the other card, or the other rig, may
        have one -- so this raises `SessionError` and never `JobRefused`.
        """
        if not self.needs_bridge:
            return None
        name = self.bridge_name
        state = self.state(name)
        if state is not None and state.running:
            return name
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/tmp"),
            "BRIDGE_PORT": str(self.bridge_port),
            "BRIDGE_NAME": name,
        }
        # The one thing about the bridge that is the deployment's and not this job's, passed
        # through for the same reason `SIM_IMAGE` is: which tag the image was built under is a
        # fact about the machine, and a rig that built it under another name must be able to say
        # so without a second copy of `bridge.sh`.
        if os.environ.get("BRIDGE_IMAGE"):
            env["BRIDGE_IMAGE"] = os.environ["BRIDGE_IMAGE"]
        if state is not None:
            # It started once and stopped, and it is holding the name. Remove it before starting
            # a second: `docker run --name` on a taken name fails, and the failure would read
            # like a missing image.
            self.commands(["bash", str(self.script("bridge.sh")), "stop"], env=env)
        code, output = self.commands(["bash", str(self.script("bridge.sh")), "start"], env=env)
        if code != 0:
            raise SessionError(
                f"the bridge for gpu{self.resolved.gpu} would not start on port "
                f"{self.bridge_port}: {output.strip()}"
            )
        return name

    # -- launching ----------------------------------------------------------------------------

    def write_job(self) -> Path:
        """Write the resolved job where the container will read it, in the container's paths.

        Into the run's own directory rather than anywhere of the agent's, because that directory
        is the one thing mounted into the run and the one thing delivered afterwards: the job a
        result was produced from then travels with the result, and nobody has to reconstruct it.
        """
        self.out_dir.mkdir(parents=True, exist_ok=True)
        path = self.out_dir / JOB_FILE
        payload = json.dumps(self.resolved.job.model_dump(mode="json"), indent=2, sort_keys=True)
        path.write_text(payload + "\n")
        return path

    def launch(self) -> str:
        """Create the run container, detached, and return its name.

        `sim-run.sh` runs the image guard first, so a missing or stale image is named here with
        the build command and no container is created at all -- the alternative, a missing torch
        four minutes into a drive, is what a rig lost a morning to.
        """
        existing = self.state(self.name)
        if existing is not None:
            raise SessionError(
                f"a container named {self.name} already exists "
                f"({'running' if existing.running else 'exited'}). Adopt it or remove it; "
                f"launching a second one on this card is the double-booking the lock prevents."
            )
        self.write_job()
        code, output = self.commands(
            [
                "bash",
                str(self.script("sim-run.sh")),
                "run",
                "--job", f"{CONTAINER_OUT}/{JOB_FILE}",
                "--out", CONTAINER_OUT,
                "--events",
            ],
            env=self.child_environment(),
        )  # fmt: skip
        if code != 0:
            raise SessionError(f"could not start the run container: {output.strip()}")
        return self.name

    def adopt(self) -> str | None:
        """The container already running this job on this card, if there is one.

        The recovery path, and it is the normal path with the launch skipped: a restarted agent
        finds the run by label, supervises it from byte zero of a record it never held in memory,
        and delivers it as if it had started it. Step 5 calls this before it leases anything.
        """
        for name, job_id in self.ours():
            if job_id == self.resolved.job_id:
                return name
        return None

    # -- supervising --------------------------------------------------------------------------

    def progress(self) -> Progress:
        return Progress.read(self.resolved.batch_dir)

    def supervise(
        self,
        *,
        on_tick: Callable[[Progress], None] | None = None,
        poll_s: float = POLL_S,
    ) -> SessionResult:
        """Follow the run to its end and say what it did.

        `on_tick` is called once per poll with the progress read this time round; Step 5 extends
        the queue lease there, because a lease is a clock and our work is longer than it.
        """
        name = self.name
        while True:
            progress = self.progress()
            if on_tick is not None:
                on_tick(progress)
            code = read_exit_code(self.out_dir)
            if code is not None:
                return self._classify(code, progress)

            state = self.state(name)
            if state is None or not state.running:
                # Read the exit code ONE more time before concluding anything. The run writes it
                # and then exits, so the two happen in that order -- but the read above and this
                # check do not, and a run that finished in the gap between them leaves a file
                # that exists and a container that has stopped. Without the re-read the verdict
                # is VANISHED: a job reported failed after every one of its rows was scored.
                # wing-sim caught this one on a stub job whose presets take seconds.
                code = read_exit_code(self.out_dir)
                progress = self.progress()
                if code is not None:
                    return self._classify(code, progress, state)
                return SessionResult(
                    outcome=Outcome.VANISHED,
                    exit_code=None,
                    permanent=False,
                    progress=progress,
                    container_exit=None if state is None else state.exit_code,
                    detail=(
                        "the container is gone and wrote no exit code"
                        if state is None
                        else f"the container exited {state.exit_code} without writing an exit code"
                    ),
                )
            time.sleep(poll_s)

    def _finished_event(self) -> dict:
        """The last `run.finished` in the stream, or an empty dict when there is none."""
        try:
            events = read_events(self.out_dir / EVENTS_FILE)
        except OSError:
            return {}
        finished = [event for event in events if event.get("event") == "run.finished"]
        return finished[-1] if finished else {}

    def _classify(
        self, code: int, progress: Progress, state: ContainerState | None = None
    ) -> SessionResult:
        """Turn an exit code and the event stream into one verdict.

        The code alone is never enough. A stopped run exits 0 with half its rows scored, and a
        job file that will not parse exits 1 exactly as a driver failure does -- so `stopped` and
        `permanent` are read from `run.finished`, which is the one line that carries a judgement.
        """
        finished = self._finished_event()
        permanent = bool(finished.get("permanent"))
        stopped = bool(finished.get("stopped")) or self.stop_requested
        container_exit = state.exit_code if state is not None else None
        detail = str(finished.get("error") or "")
        if code == 0:
            outcome = Outcome.STOPPED if stopped else Outcome.COMPLETED
        elif permanent:
            outcome = Outcome.REFUSED
        else:
            outcome = Outcome.FAILED
        return SessionResult(
            outcome=outcome,
            exit_code=code,
            permanent=permanent,
            progress=progress,
            container_exit=container_exit,
            detail=detail,
        )

    # -- stopping -----------------------------------------------------------------------------

    def stop(self, grace_s: int = STOP_GRACE_S) -> None:
        """Ask the run to stop, and remember that we asked.

        `docker stop`, not a signal to a pid: the holder is a sibling container, so signalling a
        process group would reach nothing -- and on a rig with `--pid host` the pid a record names
        lives in the host's namespace, where killing a group is how you take out somebody's login
        shell by accident. The SIGTERM reaches the batch's flag as pid 1 of the container, the
        row it lands in ends `stopped`, every row so far is written and the process exits 0.

        The intent is recorded before the signal, because the exit code afterwards is a 0 that
        looks like any other.
        """
        self.stop_requested = True
        self.commands(
            ["docker", "stop", "--timeout", str(int(grace_s)), self.name],
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        )

    # -- harvesting and delivering ------------------------------------------------------------

    def harvest(self) -> Path | None:
        """Keep the container's stdout beside the results, then remove the container.

        The one place a container this session started is removed. The log is written first: a
        removal that succeeded and a log that was never read is the diagnosis gone, and the whole
        reason the run is not `--rm` is that its output outlives it.
        """
        if self.state(self.name) is None:
            return None
        written: Path | None = None
        code, output = self.commands(["docker", "logs", self.name])
        if code == 0:
            try:
                self.out_dir.mkdir(parents=True, exist_ok=True)
                written = self.out_dir / LOG_FILE
                written.write_text(output)
            except OSError:
                written = None
        self.commands(["docker", "rm", "--force", self.name])
        return written

    def already_delivered(self) -> bool:
        """Is this job's result already on the share? Then it is done, and must not run again.

        The redelivery guard. The queue is at-least-once by its own documentation, so a message
        can arrive while another rig is still running it or after it has finished; this and the
        card lock are what make that harmless rather than a double-booked GPU.
        """
        return self.roots.delivered(self.resolved.job_id).exists()

    def deliver(self) -> Path:
        """Copy the run's directory to the share and rename it into place. Returns the directory.

        Copy to `<job_id>.partial`, then rename: a rename within one filesystem is atomic, so a
        directory without `.partial` is always complete, and a delivery interrupted half way
        leaves something a reader will never mistake for a result. Step 5 acks only after this
        returns, so a failed copy is a retried job and never a lost result.

        Delivered whatever the outcome, a refusal included: the evidence of a failure is worth
        more than the disk it costs.
        """
        job_id = self.resolved.job_id
        final = self.roots.delivered(job_id)
        staging = self.roots.staging(job_id)
        if final.exists():
            raise SessionError(
                f"{final} already exists. That is the redelivery guard's answer -- this job is "
                f"delivered -- and overwriting it would replace a result nobody asked to lose."
            )
        if not self.out_dir.is_dir():
            raise SessionError(f"nothing to deliver: {self.out_dir} does not exist")
        final.parent.mkdir(parents=True, exist_ok=True)
        if staging.exists():
            shutil.rmtree(staging)
        try:
            shutil.copytree(self.out_dir, staging)
            os.replace(staging, final)
        except OSError as error:
            raise SessionError(
                f"could not deliver {job_id} to {final}: {error}. The run's own record is "
                f"untouched at {self.out_dir}."
            ) from error
        return final

    # -- the whole thing ----------------------------------------------------------------------

    def run(
        self,
        *,
        on_tick: Callable[[Progress], None] | None = None,
        poll_s: float = POLL_S,
        deliver: bool = True,
    ) -> SessionResult:
        """Adopt or launch, supervise, harvest, deliver. The session, in the order it happens.

        Delivery is last and is the only thing after it that a caller acts on: Step 5 acks the
        queue message after this returns with `delivered` set, and never before.
        """
        adopted = self.adopt()
        if adopted is None:
            self.ensure_bridge()
            try:
                self.launch()
            except SessionError:
                # A `docker run` that fails may still have CREATED the container -- asking a
                # one-card rig for `--gpus device=1` exits 128 with the container sitting there,
                # measured on the rig 2026-09-15. Left alone it is adopted by the next attempt
                # and reported as a run that vanished, which is two wrong answers: the run never
                # started, and the reason is in a log nobody kept. So harvest first, then raise.
                self.harvest()
                raise
        result = self.supervise(on_tick=on_tick, poll_s=poll_s)
        self.harvest()
        if not deliver:
            return result
        delivered = self.deliver()
        return SessionResult(
            outcome=result.outcome,
            exit_code=result.exit_code,
            permanent=result.permanent,
            progress=result.progress,
            delivered=delivered,
            container_exit=result.container_exit,
            detail=result.detail,
        )


@dataclass
class OnceReport:
    """What `agent --once` did, as one object it can print as JSON.

    A record and not a log line, for the same reason every other record in this project is one:
    the thing that reads it next may be a person, a test, or the Step 5 loop.
    """

    job_id: str
    gpu: int
    outcome: str
    exit_code: int | None = None
    permanent: bool = False
    delivered: str | None = None
    out: str | None = None
    detail: str = ""
    done: int = 0
    n: int | None = None
    container: str | None = None
    bridge: str | None = None
    holder: dict | None = None

    def as_json(self) -> str:
        return json.dumps(
            {key: value for key, value in self.__dict__.items() if value is not None},
            indent=2,
            sort_keys=True,
        )


__all__ = [
    "BRIDGE_BASE_PORT",
    "BRIDGE_NAME",
    "BRIDGE_POLICIES",
    "JOB_FILE",
    "LABEL_ATTEMPT",
    "LABEL_GPU",
    "LABEL_JOB",
    "LABEL_MANAGED",
    "LOG_FILE",
    "MANAGED_BY",
    "POLL_S",
    "STOP_GRACE_S",
    "Commands",
    "ContainerState",
    "OnceReport",
    "Outcome",
    "Progress",
    "RunSession",
    "SessionError",
    "SessionResult",
]
