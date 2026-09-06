"""Writing `docs/reference/commands.md` -- every command, every flag, read off the CLI itself.

Generated for the same reason `destinations.md` is: a reference that is retyped goes stale, and
this one's subject changes often. In two days `--seeds` became repeatable, `inspect` grew
`--block-seq`, and three commands appeared. A hand-written table would already have been wrong
about all of it, and nothing would have said so.

So the flags, their defaults and whether they repeat are read from the Typer app at render time,
and the values they accept -- the eleven categories, the five exit rules, the block ids -- from
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
        ("inspect", "examples", "seeds", "review", "destinations", "workspace", "importing"),
    ),
    (
        "Build and correct",
        "The ones that write into a bank, and the one that writes this page. `generate` makes a "
        "bank; the four after it change one scenario of one that exists.",
        ("generate", "replace", "add", "remove", "budget", "options", "commands"),
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
    "examples": (
        ("uv run scenariobank examples", "redraw the studio's gallery"),
        ("uv run scenariobank examples -c roundabout", "just the one that changed"),
    ),
    "options": (
        (
            "uv run scenariobank options --bank ./banks/b --traffic medium --pedestrians low",
            "pin two axes; nothing is rebuilt",
        ),
        ("uv run scenariobank options --bank ./banks/b --show", "what this bank pins"),
    ),
    "seeds": (
        (
            "uv run scenariobank seeds -c curve --keep 0,1,2,3 --scan 0-30",
            "what would a fifth seed add?",
        ),
        ("uv run scenariobank seeds -c roundabout --scan 0,7,22", "score three named seeds"),
    ),
    "review": (
        ("uv run scenariobank review --bank ./banks/b", ""),
        (
            "uv run scenariobank review --bank ./banks/b --json"
            " | jq '.categories[].duplicates'",
            "the duplicate counts alone",
        ),
    ),
    "destinations": (("uv run scenariobank destinations", ""),),
    "workspace": (
        (
            "uv run scenariobank workspace"
            " -p ../wingfin-osm-scenarionet-converter/workspaces/junction-1",
            "what is in a stored conversion",
        ),
        (
            "uv run scenariobank workspace"
            " -p ../wingfin-osm-scenarionet-converter/workspaces/mosque"
            " --json | jq '.datasets[].scenarios[].step_hz'",
            "the rate each dataset was sampled at",
        ),
    ),
    "importing": (
        ("uv run scenariobank importing", "the checklist, measured on `junction-1`"),
        (
            "uv run scenariobank importing"
            " -p ../wingfin-osm-scenarionet-converter/workspaces/mosque -o /tmp/mosque.md",
            "the same checklist against another workspace",
        ),
    ),
    "generate": (
        ("uv run scenariobank generate -o ./banks/b --bank-id b", "all 55 scenarios"),
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
        (
            "uv run scenariobank replace --bank ./banks/b --scenario t_junction_0002"
            " --exit-rule left",
            "same seed, its own exit rule",
        ),
        (
            "uv run scenariobank replace --bank ./banks/b --scenario t_junction_0002"
            " --destination 1T0_1_",
            "same seed, that exact exit",
        ),
    ),
    "add": (
        (
            "uv run scenariobank add --bank ./banks/b -c t_junction -s 7",
            "a sixth t_junction, numbered past the highest id",
        ),
        (
            "uv run scenariobank add --bank ./banks/b -b CCX --rule sharpest -s 0",
            "a road composed by hand, filed as CCX_sharpest",
        ),
    ),
    "remove": (
        (
            "uv run scenariobank remove --bank ./banks/b --scenario curve_0002",
            "the ids after it keep their numbers",
        ),
    ),
    "budget": (
        (
            "uv run scenariobank budget --bank ./banks/b --scenario curve_0003 --max-steps 500",
            "a tighter cap on one scenario, no rebuild",
        ),
        (
            "uv run scenariobank budget --bank ./banks/b --scenario curve_0003 --inherit",
            "back to the category's cap",
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
    ("see what is in a converter workspace before importing it", "workspace"),
    ("know what must be brought over when I import one", "importing"),
    ("set the traffic level once instead of on every run", "options"),
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

    Generated, so the eleven category names and fifteen block ids in the tables are the ones the
    code actually accepts. This is the half of a flag table that usually goes missing: knowing a
    flag exists is no use without knowing what may follow it.
    """
    from scenariobank.categories import BLOCKS, CATEGORIES, SEEDS, VALID_BLOCK_IDS, ExitRule
    from scenariobank.options import AXES, LEVEL_NAMES

    seeds = ",".join(str(seed) for seed in SEEDS)
    levels = ", ".join(f"`{level}`" for level in LEVEL_NAMES)
    categories = ", ".join(f"`{name}`" for name in CATEGORIES)
    rules = ", ".join(f"`{rule.value}`" for rule in ExitRule)
    blocks = ", ".join(f"`{block.id}` {block.label}" for block in BLOCKS)
    # Generated from the blocks that declare a `BlockNeeds`, so the reference cannot claim a
    # different set of caveats from the one the CLI refuses on and the studio's palette explains.
    # One clause per block rather than grouped: two blocks sharing a caveat would need the verb
    # to agree with the count, and a table cell is not worth a pluraliser.
    caveats = " ".join(
        f"`{block.id}`: {block.needs.short}." for block in BLOCKS if block.needs is not None
    )
    return {
        "--category": f"One of: {categories}.",
        "--rule": f"One of: {rules}.",
        "--block-seq": (
            f"Any string of these {len(VALID_BLOCK_IDS)} block ids: {blocks}. "
            f"`I` is prepended automatically and is never written into a sequence. {caveats}"
        ),
        "--seed": "Any non-negative integer.",
        "--seeds": (
            "A comma-separated list. Also takes `category=list` to override one category, and "
            f"repeats, so later flags win. Omitted, every category uses `{seeds}`."
        ),
        "--keep": f"A comma-separated list. Defaults to the bank's seeds, `{seeds}`.",
        "--scan": "A comma-separated list, or an inclusive range like `0-30`.",
        "--scenario": "A `scenario_id` from the bank's manifest, e.g. `curve_0004`.",
        "--exit-rule": f"One of: {rules}. Recorded on the scenario, not on its category.",
        "--destination": (
            "An exit node as `sockets` reports it, e.g. `1T2_1_`. Only the exits **this seed's** "
            "road offers: a node is resolved per seed, so one pinned at a seed is dropped if the "
            "seed changes."
        ),
        "--max-steps": "A whole number of steps, at least 1.",
        **{
            f"--{axis}": (
                f"One of: {levels}. Applied when a run happens, not when the bank was built."
            )
            for axis in AXES
        },
    }


def _choices() -> dict[str, list[str]]:
    """The flags whose values are a closed set, and what that set is.

    The same lists `_value_notes` writes into the prose, as data: the studio turns these into
    dropdowns, so a category cannot be mistyped in the page. Read from `categories.py` for the
    reason everything else here is -- a hand-listed set goes stale the day a category is added.
    """
    from scenariobank.categories import CATEGORIES, ExitRule
    from scenariobank.options import AXES, LEVEL_NAMES

    return {
        "--category": list(CATEGORIES),
        "--rule": [rule.value for rule in ExitRule],
        # The six option axes, all drawing on the same four levels. Listed per flag rather than
        # once, because the studio keys its dropdowns on the flag name.
        **{f"--{axis}": list(LEVEL_NAMES) for axis in AXES},
        # The same closed set as `--rule`, under the flag that writes it onto a scenario rather
        # than into a drawing. Listed separately because a flag is what the studio keys on.
        "--exit-rule": [rule.value for rule in ExitRule],
    }


def _params(command: Any, choices: dict[str, list[str]]) -> list[dict[str, Any]]:
    """One entry per flag, unrendered: the machine-readable half of `_rows`.

    `_rows` describes a flag to a reader; this describes it to a program. The studio builds its
    form from these and the studio's API validates a submitted job against them, so a flag the
    page offers and a flag the CLI accepts are the same flag by construction -- there is no second
    list of flags anywhere to fall out of step.
    """
    params = []
    for param in command.params:
        flags = list(param.opts)
        if not flags or any(flag in _SKIP for flag in flags):
            continue
        long = next((flag for flag in flags if flag.startswith("--")), flags[0])
        default = param.default
        if isinstance(default, Path):
            default = str(default)
        elif isinstance(default, (tuple, list)):
            default = [str(value) for value in default]
        params.append(
            {
                "flag": long,
                "aliases": [flag for flag in flags if flag != long],
                "off_flag": next(iter(param.secondary_opts or []), None),
                "type": param.type.name,
                "required": bool(param.required),
                "multiple": bool(getattr(param, "multiple", False)),
                "default": default,
                "help": (param.help or "").strip(),
                "choices": choices.get(long),
            }
        )
    return params


def _rows(command: Any, notes: dict[str, str]) -> list[dict[str, str]]:
    """One row per flag: what to type, whether you must, whether it repeats, what it takes.

    Dicts rather than tuples because two things render these: the markdown table below, and the
    studio's reference tab, which builds real HTML from the same rows. One source, two renderers,
    and neither can list a flag the other does not.
    """
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
            {
                "flag": f"`{shown}`",
                "need": need,
                "repeats": "yes" if getattr(param, "multiple", False) else "",
                "meaning": meaning,
            }
        )
    return rows


def _table(rows: list[dict[str, str]]) -> list[str]:
    return [
        "| flag | | repeats | meaning |",
        "|---|---|---|---|",
        *(
            f"| {row['flag']} | {row['need']} | {row['repeats']} | {row['meaning']} |"
            for row in rows
        ),
        "",
    ]


def _example_lines(name: str) -> list[str]:
    """A command's examples, comments aligned on a common column.

    Aligned once, here, rather than by each renderer: the markdown fence and the studio's `<pre>`
    show the same block, and a block that lines up in one and not the other would read as two
    different sets of examples.
    """
    examples = EXAMPLES.get(name)
    if not examples:
        raise ValueError(
            f"command {name!r} has no examples in docs.EXAMPLES; its section would be flags only"
        )
    # A wrapped command carries its comment on its last line, so measure that line.
    width = max(
        (len(line.splitlines()[-1]) for line, comment in examples if comment),
        default=0,
    )
    lines = []
    for command_line, comment in examples:
        if not comment:
            lines.append(command_line)
            continue
        head, newline, tail = command_line.rpartition("\n")
        lines.append(f"{head}{newline}{tail.ljust(width)}  # {comment}")
    return lines


def _command_entry(
    name: str, command: Any, notes: dict[str, str], choices: dict[str, list[str]]
) -> dict[str, Any]:
    """One command as data: its prose, its flags and its examples. Raises if it has no examples.

    `options` is for reading and `params` is for running. Both are read off the same Typer command
    in the same pass, which is the only reason the reference tab and the run form can be trusted to
    describe one program.
    """
    return {
        "name": name,
        "help": command.help.strip() if command.help else None,
        "options": _rows(command, notes),
        "params": _params(command, choices),
        "examples": _example_lines(name),
    }


def _command_section(entry: dict[str, Any]) -> list[str]:
    """One command: prose, then its flags, then its examples -- in that order, in one place."""
    lines = [f"### `{entry['name']}`", ""]
    if entry["help"]:
        lines += [line.rstrip() for line in entry["help"].splitlines()]
        lines.append("")
    lines += _table(entry["options"]) if entry["options"] else ["Takes no options.", ""]
    lines += ["```bash", *entry["examples"], "```", ""]
    return lines


def block_rows() -> list[dict[str, Any]]:
    """The fifteen blocks as data: what `--block-seq` is spelled from, with what each letter is.

    The studio's road builder lays its palette out from this, in this order. `categories.BLOCKS`
    owns the table; the test that asserts it against MetaDrive is what keeps the palette honest.

    `needs` carries the whole `BlockNeeds`, not just its sentence, so the page can *offer* the
    repair rather than describe it -- and so the rule the CLI refuses on and the rule the page
    explains are one declaration read twice.
    """
    from scenariobank.categories import BLOCKS

    return [
        {
            "id": block.id,
            "cls": block.cls,
            "label": block.label,
            "needs": None
            if block.needs is None
            else {
                "text": block.needs.text,
                "after_any": block.needs.after_any,
                "insert": block.needs.insert,
            },
        }
        for block in BLOCKS
    ]


def category_rows() -> list[dict[str, Any]]:
    """The eleven categories as data. The one description of a category in this package.

    `summary` is the first sentence of `description`; the tables want a line, not a paragraph.
    """
    from scenariobank.categories import CATEGORIES

    return [
        {
            "name": name,
            "block_seq": category.block_seq,
            "exit_rule": category.exit_rule.value,
            "max_steps": category.max_steps,
            "summary": category.description.split(".")[0],
            "description": category.description,
        }
        for name, category in CATEGORIES.items()
    ]


def _categories_section() -> list[str]:
    """The eleven categories in full. The inline `--category` list gives the names; this gives
    the road, the rule and the budget that come with each."""
    lines = [
        "## The eleven categories",
        "",
        "What `--category` selects, beyond the name. `block_seq` is the road, the rule fixes which",
        "exit the route drives to, and `max_steps` is the cap on an episode.",
        "",
        "| name | block_seq | exit rule | max_steps | |",
        "|---|---|---|---|---|",
    ]
    for row in category_rows():
        lines.append(
            f"| `{row['name']}` | `{row['block_seq']}` | `{row['exit_rule']}` "
            f"| {row['max_steps']} | {row['summary']} |"
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


def reference() -> dict[str, Any]:
    """The whole reference as data, read off the Typer app and `categories.py`.

    Two things render this and neither may invent anything: `render_commands` writes
    `docs/reference/commands.md`, and the studio's reference tab builds HTML from the same dict.
    Gathering it once is what stops the page and the file from listing different flags -- the
    failure this module was written to prevent, reappearing one layer up.

    Raises rather than returning a gap: a command in no group, a command in two, and a command
    with no examples are all errors here.
    """
    import typer

    from scenariobank.categories import SEEDS
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
    choices = _choices()
    return {
        "index": [{"want": want, "command": command} for want, command in INDEX],
        "global_options": _rows(group, notes),
        "global_params": _params(group, choices),
        "groups": [
            {
                "title": title,
                "blurb": blurb,
                "commands": [
                    _command_entry(name, group.commands[name], notes, choices)
                    for name in names
                ],
            }
            for title, blurb, names in GROUPS
        ],
        "categories": category_rows(),
        # The default `--seeds` reads as `None` in the parameter table, because the CLI
        # resolves it inside `_parse_seed_options`. The studio needs the real list to say how
        # many scenarios a selection asks for before the first one is built, so it is carried
        # here rather than counted a second time in the page.
        "default_seeds": list(SEEDS),
        "rules": [
            {"rule": rule.value, "picks": picks} for rule, picks in _rule_meanings().items()
        ],
    }


def render_commands() -> str:
    """Render the whole CLI as the reference document."""
    data = reference()

    lines = [HEADER, ""]
    lines += _index_section()
    lines += ["## Global options", ""]
    lines += _table(data["global_options"])
    lines += ["Given before the command: `uv run scenariobank -v doctor`.", ""]

    for group in data["groups"]:
        lines += [f"## {group['title']}", "", group["blurb"], ""]
        for entry in group["commands"]:
            lines += _command_section(entry)

    lines += _categories_section()
    return "\n".join(lines).rstrip() + "\n"


def write(path: Path) -> Path:
    """Render and write the reference."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_commands())
    return path


__all__ = [
    "EXAMPLES",
    "GROUPS",
    "INDEX",
    "category_rows",
    "reference",
    "render_commands",
    "write",
]
