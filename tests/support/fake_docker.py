"""A docker daemon that is a dictionary, and the two launch scripts that talk to it.

Phase 7 Steps 3 and 5. Every subprocess the run session starts goes through one seam
(`scenariobank.agent.session.Commands`), and this is what a test puts behind it: containers are
a dictionary, `sim-run.sh` is a recorded argv, and a run's output is whatever the test wrote into
the out directory. It answers the same questions the real daemon does, in the same shapes --
`docker ps --format` lines, `docker inspect -f`, and a non-zero code with output for a script
that refused -- and nothing more.

Never imported by `src/`.
"""

from __future__ import annotations

import json
from pathlib import Path

from scenariobank.agent.session import (
    LABEL_ATTEMPT,
    LABEL_GPU,
    LABEL_JOB,
    LABEL_MANAGED,
    MANAGED_BY,
    Commands,
)
from scenariobank.events import EVENTS_FILE, write_exit_code


class FakeDocker(Commands):
    """A daemon that is a dictionary, and the two launch scripts that talk to it.

    Answers the same questions the real one does, in the same shapes: `docker ps --format` lines,
    `docker inspect -f`, and a non-zero code with output on stderr for a script that refused.
    """

    def __init__(self, *, on_launch=None, on_state=None) -> None:
        super().__init__()
        self.containers: dict[str, dict] = {}
        self.calls: list[tuple[list[str], dict]] = []
        self.launched: dict | None = None
        #: Called when `sim-run.sh` is invoked, so a test can simulate what the run wrote.
        self.on_launch = on_launch
        #: Called on every state query, so a test can make the container stop mid-supervision.
        self.on_state = on_state
        self.launch_code = 0
        self.launch_output = ""
        #: `docker run` that creates the container and then fails, which is what the nvidia hook
        #: does for a device the machine does not have.
        self.launch_creates_first = False

    # -- the daemon's own state ---------------------------------------------------------------

    def add(self, name, *, running=True, exit_code=None, labels=None, log="") -> None:
        self.containers[name] = {
            "running": running,
            "exit": exit_code,
            "labels": labels or {},
            "log": log,
        }

    def __call__(self, argv, *, env=None, cwd=None):
        argv = list(argv)
        self.calls.append((argv, dict(env or {})))
        if argv[0] == "bash":
            script = Path(argv[1]).name
            if script == "sim-run.sh":
                return self._sim_run(argv, env or {})
            if script == "bridge.sh":
                return self._bridge(argv, env or {})
            raise AssertionError(f"unexpected script {script}")
        if argv[0] == "docker":
            return getattr(self, f"_docker_{argv[1]}")(argv)
        raise AssertionError(f"unexpected command {argv}")

    # -- the two scripts ----------------------------------------------------------------------

    def _sim_run(self, argv, env):
        name = env.get("NAME", "unnamed")
        if self.launch_code != 0 and not self.launch_creates_first:
            return self.launch_code, self.launch_output
        self.launched = {"argv": argv, "env": dict(env)}
        self.add(
            name,
            running=True,
            labels={
                LABEL_MANAGED: MANAGED_BY,
                LABEL_JOB: env.get("JOB_ID", ""),
                LABEL_ATTEMPT: env.get("ATTEMPT", ""),
                LABEL_GPU: env.get("GPU", ""),
            },
        )
        if self.on_launch is not None:
            self.on_launch(self)
        if self.launch_code != 0:
            return self.launch_code, self.launch_output
        return 0, "c0ffee\n"

    def _bridge(self, argv, env):
        name = env["BRIDGE_NAME"]
        if argv[2] == "start":
            self.add(name, running=True)
        elif argv[2] == "stop":
            self.containers.pop(name, None)
        return 0, ""

    # -- docker --------------------------------------------------------------------------------

    def _docker_ps(self, argv):
        filters = [argv[index + 1] for index, item in enumerate(argv) if item == "--filter"]
        if self.on_state is not None and "{{.State}}" in argv[-1]:
            # `state(name)`: the supervisor asking after one container. The hook is how a test
            # makes the run finish some number of polls in.
            for item in filters:
                name = item[len("name="):].strip("^$/") if item.startswith("name=") else None
                if name in self.containers:
                    self.on_state(self, name)
        rows = []
        for name, container in self.containers.items():
            if not all(self._matches(name, container, item) for item in filters):
                continue
            state = "running" if container["running"] else "exited"
            if "{{.State}}" in argv[-1]:
                rows.append(f"{state}\t{name}")
                continue
            labels = container["labels"]
            row = f"{name}\t{labels.get(LABEL_JOB, '')}"
            if LABEL_ATTEMPT in argv[-1]:
                row += f"\t{labels.get(LABEL_ATTEMPT, '')}"
            rows.append(row)
        return 0, "\n".join(rows) + ("\n" if rows else "")

    @staticmethod
    def _matches(name, container, item):
        if item.startswith("name="):
            return item[len("name="):].strip("^$/") == name
        key, _, value = item[len("label="):].partition("=")
        return container["labels"].get(key) == value

    def _docker_inspect(self, argv):
        name = argv[-1]
        if name not in self.containers:
            return 1, f"Error: No such object: {name}\n"
        return 0, f"{self.containers[name]['exit'] or 0}\n"

    def _docker_logs(self, argv):
        name = argv[-1]
        if name not in self.containers:
            return 1, "no such container\n"
        return 0, self.containers[name]["log"]

    def _docker_rm(self, argv):
        self.containers.pop(argv[-1], None)
        return 0, ""

    def _docker_stop(self, argv):
        name = argv[-1]
        if name in self.containers:
            self.containers[name]["running"] = False
            self.containers[name]["exit"] = 0
        return 0, ""


def wrote(out_dir: Path, *, n=2, done=0, running=None, exit_code=None, finished=None) -> None:
    """Stand in for what a run leaves behind: the record directory and its two process files."""
    out_dir.mkdir(parents=True, exist_ok=True)
    ids = [f"t_junction_{index:04d}" for index in range(n)]
    (out_dir / "batch.json").write_text(json.dumps({"n": n, "scenarios": ids}))
    (out_dir / "results").mkdir(exist_ok=True)
    (out_dir / "starts").mkdir(exist_ok=True)
    for scenario_id in ids[:done]:
        (out_dir / "starts" / f"{scenario_id}.json").write_text("{}")
        (out_dir / "results" / f"{scenario_id}.json").write_text("{}")
    if running is not None:
        (out_dir / "starts" / f"{running}.json").write_text("{}")
    if finished is not None:
        line = {"schema_version": 1, "event": "run.finished", **finished}
        (out_dir / EVENTS_FILE).write_text(json.dumps(line) + "\n")
    if exit_code is not None:
        write_exit_code(out_dir, exit_code)

__all__ = ["FakeDocker", "wrote"]
