"""Writing `docs/reference/commands.md` -- every command, every flag, read off the CLI itself.

Generated for the same reason `destinations.md` is: a reference that is retyped goes stale, and
this one's subject changes often. In two days `--seeds` became repeatable, `inspect` grew
`--block-seq`, and three commands appeared. A hand-written table would already have been wrong
about all of it, and nothing would have said so.

So the flags, their defaults and whether they repeat are read from the Typer app at render time,
and the values they accept -- the seven categories, the five exit rules, the block ids -- from
`categories.py`. `tests/unit/test_docs.py` fails if the checked-in page disagrees with either.

Three things here are hand-written, because none can be introspected: which group a command
belongs in, its examples, and what each exit rule means. All three are keyed so the renderer
**raises** rather than publishing a gap -- a command with no group, a command with no example, or
an `ExitRule` member with no description is an error, not a blank cell.

This module imports no simulator, and neither does `cli`. The drift test therefore runs on every
machine rather than only where MetaDrive is installed, which is what makes the guarantee worth
having.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

#: Typer adds these itself. They are the same on every Typer program and say nothing about this one.
_SKIP = frozenset({"--help", "--install-completion", "--show-completion"})

#: Commands in reading order, grouped by what you are trying to do.
GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "Find out where you are",
        "What simulator is this, and what is in the bank. None of these change anything.",
        ("doctor", "categories", "sockets"),
    ),
    (
        "Look before you commit",
        "Measure and draw. Still nothing written into a bank.",
        ("inspect", "seeds", "destinations"),
    ),
    (
        "Build and correct",
        "The two that write scenarios, and the one that writes this page.",
        ("generate", "replace", "commands"),
    ),
    (
        "Do all of it in a page",
        "The same commands behind a local web page, for the parts of the job that are pictures.",
        ("studio",),
    ),
)

#: Worked examples, shown **inside each command's own section** rather than collected at the end,
#: where they were too far from the flags they demonstrate to be readable.
EXAMPLES: dict[str, tuple[tuple[str, str], ...]] = {
    "doctor": (
        ("uv run scenariobank doctor", ""),
        ("uv run scenariobank doctor --require-commit 85e5dadc", "what CI runs"),
        ("uv run scenariobank doctor --json > doctor-host.json", "to diff against a container"),
    ),
    "categories": (("uv run scenariobank categories", ""),),
    "sockets": (
        ("uv run scenariobank sockets -b X -s 0", "every exit of a four-way junction"),
        ("uv run scenariobank sockets -c roundabout -s 3 --json", "the same, as JSON"),
    ),
    "inspect": (
        ("uv run scenariobank inspect -c intersection_left -s 0", "a category at a seed"),
        (
            "uv run scenariobank inspect -b CC -s 22 --rule only -o /tmp/cc22.png",
            "any road, no category needed",
        ),
    ),
    "seeds": (
        (
            "uv run scenariobank seeds -c curve --keep 0,1,2,3 --scan 0-30",
            "what would a fifth seed add?",
        ),
        ("uv run scenariobank seeds -c roundabout --scan 0,7,22", "score three named seeds"),
    ),
    "destinations": (("uv run scenariobank destinations", ""),),
    "generate": (
        ("uv run scenariobank generate -o ./banks/b --bank-id b", "all 35 scenarios"),
        (
            "uv run scenariobank generate -o ./banks/b --bank-id b \\\n"
            "    --seeds 0,1,2,3,4 --seeds curve=0,1,2,3,22",
            "one category on its own seeds",
        ),
        (
            "uv run scenariobank generate -o /tmp/b --bank-id b -c curve --no-thumbnails",
            "one category, no PNGs",
        ),
    ),
    "replace": (
        (
            "uv run scenariobank replace --bank ./banks/b --scenario curve_0004 --seed 22",
            "rebuild one scenario; the other 34 are untouched",
        ),
    ),
    "commands": (("uv run scenariobank commands", "rewrite this page"),),
    "studio": (
        ("uv run --group web scenariobank studio", "then open http://127.0.0.1:8770/"),
        (
            "uv run --group web scenariobank studio --banks-root /tmp --port 9000",
            "look at banks somewhere else",
        ),
    ),
}

#: The task-shaped index. The flag tables are flag-shaped, which is the wrong shape to start from
#: when the question is "can I change just one thing?".
INDEX: tuple[tuple[str, str], ...] = (
    ("change one seed in a bank I already built", "replace"),
    ("build a bank with one category on its own seeds", "generate"),
    ("find out which seed is worth swapping to", "seeds"),
    ("look at a road before committing to it", "inspect"),
    ("see which exits a road offers", "sockets"),
    ("check which simulator this is", "doctor"),
    ("re-measure the destinations reference", "destinations"),
    ("do all of that by looking rather than typing", "studio"),
)

HEADER = """# Commands

Generated by `scenariobank commands`. Do not edit by hand -- every flag, default and value list
below is read off the CLI and out of `categories.py`, so this page cannot disagree with the code.
`tests/unit/test_docs.py` fails if it does.

Prose about *why* each command exists is in `README.md`. This page is the exhaustive reference:
each command's flags, the values those flags accept, and worked examples in the same section.

Every command is `uv run scenariobank <command>`."""


def _rule_meanings() -> dict:
    """What each exit rule picks. Keyed by the member, so an undescribed rule raises."""
    from scenariobank.categories import ExitRule

    meanings = {
        ExitRule.ONLY: "the single exit. An error if the road offers more than one",
        ExitRule.LEFT: "the exit nearest +90 degrees",
        ExitRule.RIGHT: "the exit nearest -90 degrees",
        ExitRule.STRAIGHT: "the exit nearest 0 degrees",
        ExitRule.SHARPEST: "whichever exit turns hardest, either way",
    }
    undescribed = [rule.value for rule in ExitRule if rule not in meanings]
    if undescribed:
        raise ValueError(
            f"exit rule(s) {undescribed} have no description in docs._rule_meanings; "
            "the reference would list them blank"
        )
    return meanings


def _value_notes() -> dict[str, str]:
    """What each value-taking flag accepts, appended to its row in the table.

    Generated, so the seven category names and fifteen block ids in the tables are the ones the
    code actually accepts. This is the half of a flag table that usually goes missing: knowing a
    flag exists is no use without knowing what may follow it.
    """
    from scenariobank.categories import CATEGORIES, VALID_BLOCK_IDS, ExitRule

    categories = ", ".join(f"`{name}`" for name in CATEGORIES)
    rules = ", ".join(f"`{rule.value}`" for rule in ExitRule)
    blocks = " ".join(f"`{block}`" for block in sorted(VALID_BLOCK_IDS))
    return {
        "--category": f"One of: {categories}.",
        "--rule": f"One of: {rules}.",
        "--block-seq": (
            f"Any string of these {len(VALID_BLOCK_IDS)} block ids: {blocks}. "
            "`I` is prepended automatically and is never written into a sequence."
        ),
        "--seed": "Any non-negative integer.",
        "--seeds": (
            "A comma-separated list. Also takes `category=list` to override one category, and "
            "repeats, so later flags win. Omitted, every category uses `0,1,2,3,4`."
        ),
        "--keep": "A comma-separated list. Defaults to the bank's seeds, `0,1,2,3,4`.",
        "--scan": "A comma-separated list, or an inclusive range like `0-30`.",
        "--scenario": "A `scenario_id` from the bank's manifest, e.g. `curve_0004`.",
    }


def _rows(command: Any, notes: dict[str, str]) -> list[tuple[str, str, str, str]]:
    """One row per flag: what to type, whether you must, whether it repeats, what it takes."""
    rows = []
    for param in command.params:
        flags = list(param.opts) + list(param.secondary_opts or [])
        if not flags or any(flag in _SKIP for flag in flags):
            continue
        kind = param.type.name
        shown = "/".join(flags)
        if kind != "boolean":
            shown = f"{shown} <{kind}>"
        if param.required:
            need = "**required**"
        elif isinstance(param.default, bool):
            need = f"default `{str(param.default).lower()}`"
        elif param.default is None:
            need = "optional"
        else:
            need = f"default `{param.default}`"
        long = next((flag for flag in param.opts if flag.startswith("--")), None)
        meaning = " ".join(filter(None, [(param.help or "").strip(), notes.get(long, "")]))
        rows.append(
            (
                f"`{shown}`",
                need,
                "yes" if getattr(param, "multiple", False) else "",
                meaning,
            )
        )
    return rows


def _table(rows: list[tuple[str, str, str, str]]) -> list[str]:
    return [
        "| flag | | repeats | meaning |",
        "|---|---|---|---|",
        *(f"| {flag} | {need} | {repeats} | {meaning} |" for flag, need, repeats, meaning in rows),
        "",
    ]


def _command_section(name: str, command: Any, notes: dict[str, str]) -> list[str]:
    """One command: prose, then its flags, then its examples -- in that order, in one place."""
    lines = [f"### `{name}`", ""]
    if command.help:
        lines += [line.rstrip() for line in command.help.strip().splitlines()]
        lines.append("")
    rows = _rows(command, notes)
    lines += _table(rows) if rows else ["Takes no options.", ""]

    examples = EXAMPLES.get(name)
    if not examples:
        raise ValueError(
            f"command {name!r} has no examples in docs.EXAMPLES; its section would be flags only"
        )
    lines.append("```bash")
    # Comments line up on a common column, so the block scans as two columns rather than as
    # ragged prose. A wrapped command carries its comment on its last line.
    width = max(
        (len(line.splitlines()[-1]) for line, comment in examples if comment),
        default=0,
    )
    for command_line, comment in examples:
        if not comment:
            lines.append(command_line)
            continue
        head, newline, tail = command_line.rpartition("\n")
        lines.append(f"{head}{newline}{tail.ljust(width)}  # {comment}")
    lines += ["```", ""]
    return lines


def _categories_section() -> list[str]:
    """The seven categories in full. The inline `--category` list gives the names; this gives
    the road, the rule and the budget that come with each."""
    from scenariobank.categories import CATEGORIES

    lines = [
        "## The seven categories",
        "",
        "What `--category` selects, beyond the name. `block_seq` is the road, the rule fixes which",
        "exit the route drives to, and `max_steps` is the cap on an episode.",
        "",
        "| name | block_seq | exit rule | max_steps | |",
        "|---|---|---|---|---|",
    ]
    for name, category in CATEGORIES.items():
        summary = category.description.split(".")[0]
        lines.append(
            f"| `{name}` | `{category.block_seq}` | `{category.exit_rule.value}` "
            f"| {category.max_steps} | {summary} |"
        )
    meanings = _rule_meanings()
    lines += [
        "",
        "The exit rules, which `--rule` also takes:",
        "",
        "| rule | picks |",
        "|---|---|",
        *(f"| `{rule.value}` | {meaning} |" for rule, meaning in meanings.items()),
        "",
        "Angles are counter-clockwise-positive, so positive is a left turn. `left`, `right` and",
        "`straight` refuse an exit more than 45 degrees from what they asked for rather than",
        "returning the least-wrong one.",
        "",
        "A seed is an index into the layout generator, not a quality: two seeds can draw roads a",
        "few percent apart, near enough that their figures are the same picture. `seeds` is the",
        "command that tells you which ones actually differ.",
        "",
    ]
    return lines


def _index_section() -> list[str]:
    return [
        "## How do I...",
        "",
        "| I want to | |",
        "|---|---|",
        *(f"| {want} | [`{command}`](#{command}) |" for want, command in INDEX),
        "",
        "**Changing one scenario does not mean rebuilding the bank.** `replace` rebuilds exactly",
        "the scenario you name and rewrites `manifest.json`; the rest are not touched. It keeps",
        "the scenario's id and its position, so the bank never changes size and nothing",
        "downstream has to be renumbered.",
        "",
    ]


def render_commands() -> str:
    """Render the whole CLI as the reference document."""
    import typer

    from scenariobank.cli import app

    group = typer.main.get_command(app)
    placed = [name for _, _, names in GROUPS for name in names]
    registered = set(group.commands)
    missing = sorted(registered - set(placed))
    unknown = sorted(set(placed) - registered)
    if missing or unknown:
        raise ValueError(
            f"docs.GROUPS is out of step with the CLI: {missing} registered but ungrouped, "
            f"{unknown} grouped but not registered. Place every command in a group."
        )
    if len(placed) != len(set(placed)):
        raise ValueError("docs.GROUPS lists a command in more than one group")

    notes = _value_notes()
    lines = [HEADER, ""]
    lines += _index_section()
    lines += ["## Global options", ""]
    lines += _table(_rows(group, notes))
    lines += ["Given before the command: `uv run scenariobank -v doctor`.", ""]

    for title, blurb, names in GROUPS:
        lines += [f"## {title}", "", blurb, ""]
        for name in names:
            lines += _command_section(name, group.commands[name], notes)

    lines += _categories_section()
    return "\n".join(lines).rstrip() + "\n"


def write(path: Path) -> Path:
    """Render and write the reference."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_commands())
    return path


__all__ = ["EXAMPLES", "GROUPS", "INDEX", "render_commands", "write"]
