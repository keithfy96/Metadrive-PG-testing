"""A submitted job, checked and resolved into the paths one container will be given.

Phase 7 Step 3, the half that happens **before the card is taken**. A job that can never run
must be refused while the GPU is still free: taking a card to discover that the bank id is not
on this share is a card idle for the length of a docker pull, and on the queue it is an attempt
spent for nothing.

**A job carries names; a rig carries paths.** `bank.id` is a name on the share, a checkpoint is
a file name under the models root, and neither says anything about where those live on the
machine that runs them -- which is the point, because the two rigs need not agree. So this
module takes the submitted `Job` and returns a `Resolved`: the same job with every path rewritten
to what the *container* will see (`/bank`, `/models/...`, `/out/<job_id>`), beside the *host*
directories the `docker run` must bind-mount to make those true. `Job.bank.path` as submitted is
the submitter's own machine's and is ignored here; `bank.id` is read instead, and a job without
one is refused rather than run against whatever happens to be mounted.

**Three kinds of path, and they are never interchangeable:**

| kind | example | who resolves it |
|---|---|---|
| the agent's own view | `/share/banks/b`, `/work/scripts/sim-run.sh` | this process's open() |
| the host's view | `/mnt/scenariobank/banks/b` | the docker daemon, for `-v` |
| the container's view | `/bank`, `/out/j7/job.json` | the run, inside its own mounts |

The agent container mounts the share and the local out directory **at their own host paths**, so
the first two are equal there and only the third differs. That is wing-sim's trick
(`rig/session.py:296` mounts the simulation root at the same path inside and out) and the reason
is the same: a service that must hand the daemon a path it can also read itself has exactly two
honest options, and the other one is a second variable per root that somebody will one day set
to the wrong half.

**Refusal is a verdict, not an error.** `JobRefused` means *nothing this machine does will make
this job run* -- the bank is not here, the id disagrees with the manifest, the checkpoint name
matches two files. Step 5 dead-letters that (`nack(dead=True)`) rather than spending the job's
remaining attempts on a rig that would refuse it identically. Anything that might be the card,
the driver or the bridge is not a refusal and does not belong here.

Imports `scenariobank.results` for the `Job` model and `scenariobank.bank` for the manifest
reader, and nothing else of ours. Both are pydantic and json; neither reaches the simulator
(measured: 0.14 s, no `metadrive` in `sys.modules`), so the agent stays a process that can read
a run without being able to perform one.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from scenariobank.results import Job

#: The environment names, and what each one is. `SCENARIOBANK_SHARE` is the one a rig sets: the
#: other three default to a directory under it, and each may still be overridden on its own for
#: a machine whose models live elsewhere.
SHARE_VAR = "SCENARIOBANK_SHARE"
BANKS_VAR = "SCENARIOBANK_BANKS"
MODELS_VAR = "SCENARIOBANK_MODELS"
RESULTS_VAR = "SCENARIOBANK_RESULTS"
#: Where the run containers write on this rig's own disk. Local, never the share: a run writes
#: its record a file at a time and an NFS round trip per scenario is a cost for nothing.
OUT_VAR = "SCENARIOBANK_OUT"
#: This checkout as the DAEMON must be told to mount it. Unset means "the same path this process
#: sees", which is right everywhere except inside the agent container.
REPO_VAR = "SCENARIOBANK_REPO"
#: This checkout as THIS process sees it. Derived from where this file is, which is correct for
#: the editable install in the image (`/work/src` is the venv's one path line) and for a laptop
#: `uv run`; the variable is an escape hatch for neither.
CHECKOUT_VAR = "SCENARIOBANK_CHECKOUT"

#: Where the container sees the things the agent mounts for it. Constants because `sim-run.sh`
#: names the same three and a string typed twice is a string that will one day differ.
CONTAINER_BANK = "/bank"
CONTAINER_MODELS = "/models"
CONTAINER_OUT = "/out"

#: A checkpoint or a config is named, not pathed. `**` so a models root with one directory per
#: model still answers a bare file name.
_MODEL_GLOB = "**/{name}"

#: What docker accepts in a container name: `[a-zA-Z0-9][a-zA-Z0-9_.-]*`. A job id from the
#: queue is a uuid today, but a studio-minted one is whatever the studio mints.
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


class JobRefused(ValueError):
    """This job cannot run here, and could not run on the other rig either.

    Permanent by construction: every check that raises it is about the job's own contents
    against a share both rigs mount. A card is never taken to find one of these out.
    """


def checkout() -> Path:
    """This repository, as this process sees it: where `scripts/` and `docker/` are."""
    given = os.environ.get(CHECKOUT_VAR)
    if given:
        return Path(given).expanduser()
    # src/scenariobank/agent/jobs.py -> src/scenariobank/agent -> src/scenariobank -> src -> .
    return Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class Roots:
    """The four directories a rig's agent works in, as this process sees them, plus one it does not.

    Made once per agent and kept: they are the deployment, not the job. `banks`, `models` and
    `results` default to subdirectories of `SCENARIOBANK_SHARE` when it is set, and to the
    laptop's own layout when it is not -- which is what makes `agent --once job.json` runnable
    in a clone with nothing mounted at all (Open question 8 is about the share's protocol and
    mount, and this step does not wait on it).

    `repo` is the exception and the only host-side value here: it is this checkout as the
    *daemon* must be told to mount it, which inside the agent container is not the path this
    process reads it at. Everything else is a path this process opens AND hands to the daemon,
    which is true because the agent container mounts them at their own host paths.
    """

    banks: Path
    models: Path
    results: Path
    out: Path
    repo: Path

    @classmethod
    def from_environment(cls, environ: dict[str, str] | None = None) -> Roots:
        """The roots this machine is configured for. Nothing here touches the filesystem."""
        env = os.environ if environ is None else environ
        here = checkout()
        share = env.get(SHARE_VAR)
        base = Path(share).expanduser() if share else None

        def root(variable: str, under: str, laptop: Path) -> Path:
            given = env.get(variable)
            if given:
                return Path(given).expanduser()
            return (base / under) if base is not None else laptop

        return cls(
            banks=root(BANKS_VAR, "banks", here / "banks"),
            # Beside the repo on the laptop and on the rig, which is `sim-run.sh`'s own default
            # for MODELS_DIR and must stay equal to it.
            models=root(MODELS_VAR, "models", here.parent / "models"),
            results=root(RESULTS_VAR, "results", here / "out" / "results"),
            out=Path(env.get(OUT_VAR) or here / "out").expanduser(),
            repo=Path(env.get(REPO_VAR) or here).expanduser(),
        )

    def delivered(self, job_id: str) -> Path:
        """Where a finished job's results land, and the existence check that says "done"."""
        return self.results / job_id

    def staging(self, job_id: str) -> Path:
        """The name the copy carries until it is complete. A rename is what commits it."""
        return self.results / f"{job_id}.partial"


@dataclass(frozen=True)
class Resolved:
    """One job, ready to launch: what the container is told, and what must be mounted for it.

    Every `str` field ending in the container's own root (`/bank`, `/models`, `/out`) is a path
    that exists only inside the run; every `Path` field is the host's. The two are kept apart by
    type on purpose, because the failure they cause is silent -- a bind mount of a path the
    daemon cannot find creates an empty directory and the run fails on a missing manifest, four
    minutes and one card later.
    """

    #: The job as the container will read it: `bank.path` is `/bank`, the model paths are under
    #: `/models`. Written to `<out_dir>/job.json` and passed as `run --job /out/job.json`.
    job: Job
    job_id: str
    attempt: int
    #: The one bank, on the host. Mounted at `/bank`.
    bank_dir: Path
    #: The models root, on the host, or `None` for a job that names no model at all -- the
    #: laptop's diagnostic policies. `sim-run.sh` omits the mount rather than mounting nothing.
    models_dir: Path | None
    #: This run's own directory on the rig's local disk, on the host. Mounted AS `/out`, so the
    #: container's `--out` is `/out` itself and the job file inside it is `/out/job.json`.
    out_dir: Path
    #: Which card. Names the container and the bridge, and is `docker run --gpus device=`.
    gpu: int = 0

    @property
    def batch_dir(self) -> Path:
        """Where `results.json` lands: `out_dir`, plus the tier when the job names one.

        `cli.under_out` does this on the inside; the agent must know it on the outside, because
        the two process files (`events.jsonl`, `exit_code`) stay in `out_dir` while everything
        the batch writes moves down a level. A supervisor that looked in one place for both
        would report a finished run as one that wrote nothing.
        """
        tier = self.job.options.tier
        return self.out_dir if tier is None else self.out_dir / tier

    @property
    def container_name(self) -> str:
        """`scenariobank-gpu<n>-<job id>-<attempt>`, sanitised for docker.

        The name is for a person reading `docker ps`. The labels are what code queries -- a
        name is a prefix match and a guess, and this one is truncated.
        """
        safe = _UNSAFE.sub("-", self.job_id)[:40].strip("-") or "job"
        return f"scenariobank-gpu{self.gpu}-{safe}-{self.attempt}"


def _one_match(root: Path, name: str, what: str) -> Path:
    """The single file under `root` called `name`, or a refusal saying which way it failed.

    A name and not a path, and exactly one and not the first: two checkpoints with the same file
    name under one models root is a submission whose weights nobody can name afterwards, and
    picking either would put a number on a model that was not scored.
    """
    candidate = Path(name)
    if candidate.is_absolute():
        raise JobRefused(
            f"{what} {name!r} is an absolute path. A job names a file and the rig finds it: "
            f"the two rigs need not mount the models root at the same place."
        )
    matches = sorted(path for path in root.glob(_MODEL_GLOB.format(name=name)) if path.is_file())
    if not matches:
        raise JobRefused(f"no {what} named {name!r} under {root}")
    if len(matches) > 1:
        found = ", ".join(str(path.relative_to(root)) for path in matches[:5])
        raise JobRefused(f"{len(matches)} files named {name!r} under {root}: {found}")
    return matches[0]


def _manifest_ids(bank_dir: Path, bank_id: str) -> set[str]:
    """Every scenario id in the bank, having first checked the bank is the one asked for."""
    from scenariobank.bank import BankError, read_manifest

    try:
        manifest = read_manifest(bank_dir)
    except BankError as error:
        raise JobRefused(f"{bank_dir} is not a readable bank: {error}") from error
    except ValueError as error:  # pydantic: a manifest of a shape we do not know
        raise JobRefused(f"{bank_dir}/manifest.json does not validate: {error}") from error
    if manifest.bank_id != bank_id:
        raise JobRefused(
            f"the job asks for bank {bank_id!r} and {bank_dir} holds {manifest.bank_id!r}. "
            f"The share has the wrong bank under that name, or the job names the wrong one."
        )
    return {row.scenario_id for entry in manifest.categories.values() for row in entry.scenarios}


def read_job(path: Path) -> Job:
    """Parse a job file, refusing anything that is not one. The first check of all."""
    try:
        raw = path.read_text()
    except OSError as error:
        raise JobRefused(f"cannot read the job at {path}: {error}") from error
    return parse_job(raw)


def parse_job(payload: str | dict) -> Job:
    """A `Job` out of a file's text or a queue message's payload -- the same model either way."""
    try:
        if isinstance(payload, str):
            return Job.model_validate_json(payload)
        return Job.model_validate(payload)
    except ValueError as error:
        raise JobRefused(f"this is not a job: {error}") from error


def resolve(job: Job, roots: Roots, *, gpu: int = 0, job_id: str | None = None) -> Resolved:
    """Check a job against this rig's roots and rewrite it into the container's paths.

    Every refusal here is permanent, and every one of them costs no GPU. In order: the job has
    an id and a bank id; the bank is under the banks root and its manifest agrees about which
    bank it is; every scenario the job names is in it; and each model file the job names resolves
    to exactly one file under the models root.

    The policy spec is checked for **shape only**. Importing it to see whether it loads is what
    the run itself does (`run.finished` reports `permanent: true` when it will not), and doing it
    here would pull torch and a CUDA context into the supervisor -- a process whose whole value
    is that it keeps working when the run it launched has died.
    """
    identity = job_id or job.job_id
    if not identity:
        raise JobRefused(
            "the job has no job_id. It names the results directory, the container and the "
            "redelivery guard, and a rig cannot mint one: two rigs would mint two."
        )
    if _UNSAFE.sub("", identity) == "" or "/" in identity or identity.startswith("."):
        raise JobRefused(f"job_id {identity!r} is not usable as a directory name")
    if not job.bank.id:
        raise JobRefused(
            "the job names no bank.id. A job carries names and the rig carries paths, so "
            "bank.path as submitted is the submitter's own machine's and is not read here."
        )
    if ":" not in job.policy or job.policy.startswith(":") or job.policy.endswith(":"):
        raise JobRefused(f"policy {job.policy!r} is not a spec; the form is `pkg.mod:Name`")

    bank_dir = (roots.banks / job.bank.id).resolve()
    try:
        bank_dir.relative_to(roots.banks.resolve())
    except ValueError as error:
        raise JobRefused(
            f"bank id {job.bank.id!r} resolves outside the banks root {roots.banks}"
        ) from error
    if not bank_dir.is_dir():
        raise JobRefused(f"no bank {job.bank.id!r} under {roots.banks}")

    known = _manifest_ids(bank_dir, job.bank.id)
    if job.scenarios is not None:
        missing = sorted(set(job.scenarios) - known)
        if missing:
            raise JobRefused(
                f"bank {job.bank.id} has no scenario named {', '.join(missing)}. A job that "
                f"asked for {len(job.scenarios)} and could get {len(job.scenarios) - len(missing)} "
                f"is a partial run nobody asked for."
            )

    models_dir: Path | None = None
    fields: dict[str, object] = {
        "bank": job.bank.model_copy(update={"path": CONTAINER_BANK}),
        "job_id": identity,
        "attempt": job.attempt or 1,
    }
    for field, what in (("checkpoint_path", "checkpoint"), ("model_config_path", "model config")):
        name = getattr(job, field)
        if name is None:
            continue
        if not roots.models.is_dir():
            raise JobRefused(
                f"the job names a {what} ({name}) and there is no models root at {roots.models}"
            )
        found = _one_match(roots.models, name, what)
        models_dir = roots.models
        fields[field] = f"{CONTAINER_MODELS}/{found.relative_to(roots.models)}"

    return Resolved(
        job=job.model_copy(update=fields),
        job_id=identity,
        attempt=job.attempt or 1,
        bank_dir=bank_dir,
        models_dir=models_dir,
        out_dir=roots.out / identity,
        gpu=gpu,
    )


__all__ = [
    "BANKS_VAR",
    "CHECKOUT_VAR",
    "CONTAINER_BANK",
    "CONTAINER_MODELS",
    "CONTAINER_OUT",
    "MODELS_VAR",
    "OUT_VAR",
    "REPO_VAR",
    "RESULTS_VAR",
    "SHARE_VAR",
    "JobRefused",
    "Resolved",
    "Roots",
    "checkout",
    "parse_job",
    "read_job",
    "resolve",
]
