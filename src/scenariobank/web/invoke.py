"""Turning a form submission into an argv, checked against the CLI's own flags.

Nothing here has a list of flags in it. `docs.reference()` reads them off the Typer app, the page
builds its form from that same dict, and this module validates a submission against it -- so a
flag the page offers, a flag the reference documents and a flag the CLI accepts are one flag by
construction. A hand-maintained schema would be a fourth list to keep in step, and the first one
to go wrong.

A job is `sys.executable -m scenariobank <command> ...` with `shell=False`: values become separate
argv entries and no string is ever handed to a shell.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

#: Commands the studio will not run, and why. `studio` would bind a second port and serve this
#: same page from inside a job, which is a confusing way to say "no".
NOT_RUNNABLE: dict[str, str] = {
    "studio": "the studio is already running -- a second one would just bind another port",
}


class InvokeError(ValueError):
    """A submission that does not describe a command this CLI would accept. Answered as a 400."""


def catalog() -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Every runnable command keyed by name, and the global options, both keyed by flag.

    Built fresh from `docs.reference()` rather than cached: it is a dictionary walk over data the
    CLI already holds in memory, and a cache here would be a fifth place for a flag to go stale.
    """
    from scenariobank.docs import reference

    data = reference()
    commands = {}
    for group in data["groups"]:
        for entry in group["commands"]:
            if entry["name"] in NOT_RUNNABLE:
                continue
            commands[entry["name"]] = {
                "name": entry["name"],
                "help": entry["help"],
                "params": {param["flag"]: param for param in entry["params"]},
            }
    return commands, {param["flag"]: param for param in data["global_params"]}


def _one_value(param: dict[str, Any], raw: Any, *, workdir: Path) -> str:
    """One flag value, coerced and checked the way the CLI would have."""
    flag = param["flag"]
    text = str(raw).strip()
    if not text:
        raise InvokeError(f"{flag} was left empty")

    if param["choices"] and text not in param["choices"]:
        allowed = ", ".join(param["choices"])
        raise InvokeError(f"{flag} does not accept {text!r}. One of: {allowed}")

    if param["type"] in {"int", "integer"}:
        try:
            return str(int(text))
        except ValueError:
            raise InvokeError(f"{flag} takes a whole number, not {text!r}") from None

    if param["type"] in {"float"}:
        try:
            return str(float(text))
        except ValueError:
            raise InvokeError(f"{flag} takes a number, not {text!r}") from None

    if param["type"] == "path":
        # The studio writes into the directory it was started in and nowhere else. These jobs run
        # as you, with your permissions; the containment check is what keeps a typo in a text box
        # from writing outside the checkout.
        root = workdir.resolve()
        resolved = (root / text).resolve() if not Path(text).is_absolute() else Path(text).resolve()
        if not resolved.is_relative_to(root):
            raise InvokeError(
                f"{flag} must stay inside {root} -- the studio does not write outside the "
                f"directory it was started in, and {text!r} resolves to {resolved}"
            )
        return str(resolved)

    return text


def build_argv(
    command: str,
    options: dict[str, Any] | None = None,
    globals_: dict[str, Any] | None = None,
    *,
    workdir: Path,
) -> list[str]:
    """The argv for one job, or `InvokeError` saying which flag was wrong.

    Booleans send their flag when true and their `--no-` form when false, which is how the CLI
    spells them; a repeatable flag takes a list and is emitted once per entry.
    """
    commands, global_params = catalog()
    if command not in commands:
        if command in NOT_RUNNABLE:
            raise InvokeError(f"{command!r} cannot be run from the page: {NOT_RUNNABLE[command]}")
        known = ", ".join(sorted(commands))
        raise InvokeError(f"no such command {command!r}. One of: {known}")

    params = commands[command]["params"]
    options = options or {}
    unknown = sorted(set(options) - set(params))
    if unknown:
        raise InvokeError(
            f"{command} does not take {', '.join(unknown)}. Its flags are: "
            f"{', '.join(sorted(params))}"
        )

    def render(spec: dict[str, Any], raw: Any) -> list[str]:
        flag = spec["flag"]
        if spec["type"] == "boolean":
            if bool(raw):
                return [flag]
            return [spec["off_flag"]] if spec["off_flag"] else []
        if spec["multiple"]:
            values = raw if isinstance(raw, list) else [raw]
            argv: list[str] = []
            for value in values:
                argv += [flag, _one_value(spec, value, workdir=workdir)]
            return argv
        if isinstance(raw, list):
            raise InvokeError(f"{flag} takes one value, not {len(raw)}")
        return [flag, _one_value(spec, raw, workdir=workdir)]

    flags: list[str] = []
    for flag, spec in params.items():
        if flag not in options or options[flag] is None:
            if spec["required"]:
                raise InvokeError(f"{command} needs {flag}: {spec['help']}")
            continue
        flags += render(spec, options[flag])

    lead: list[str] = []
    for flag, raw in (globals_ or {}).items():
        if flag not in global_params:
            raise InvokeError(f"{flag} is not a global option")
        lead += render(global_params[flag], raw)

    return [sys.executable, "-m", "scenariobank", *lead, command, *flags]


__all__ = ["NOT_RUNNABLE", "InvokeError", "build_argv", "catalog"]
