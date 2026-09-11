"""Top-level command-line interface."""

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from scenariobank.categories import CATEGORIES, SEEDS, CategoryError, get_category
from scenariobank.doctor import DoctorError, collect, format_report, has_simulator
from scenariobank.doctor import check as check_report
from scenariobank.importing import DEFAULT_RATE, EXAMPLE_WORKSPACE, IMPORTING_DOC
from scenariobank.logging import configure_logging
from scenariobank.sockets import SocketError, read_sockets, select_exit
from scenariobank.workspace import WorkspaceError

Seed = Annotated[int, typer.Option("--seed", "-s", help="Map seed.")]
BankDir = Annotated[
    Path, typer.Option("--bank", help="Bank directory holding the manifest to edit.")
]
ScenarioKey = Annotated[str, typer.Option("--scenario", help="Which scenario, by its id.")]
CategoryName = Annotated[
    str, typer.Option("--category", "-c", help="The category to scan.")
]

app = typer.Typer(
    name="scenariobank",
    help="Build, verify, and run a bank of procedurally generated MetaDrive scenarios.",
    no_args_is_help=True,
)


@app.callback()
def main(
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Log at DEBUG rather than INFO.")
    ] = False,
) -> None:
    """Configure process-wide CLI behavior."""
    configure_logging(verbose=verbose)


@app.command()
def doctor(
    require_commit: Annotated[
        str | None,
        typer.Option(
            "--require-commit",
            help="Fail unless the installed MetaDrive resolves to a commit with this prefix.",
        ),
    ] = None,
    probe: Annotated[
        bool,
        typer.Option(
            "--probe/--no-probe",
            help="Build a throwaway env to report the observation space. Costs one reset.",
        ),
    ] = True,
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit the report as JSON instead of aligned text.")
    ] = False,
) -> None:
    """Report exactly which simulator this environment is."""
    try:
        report = collect(probe=probe)
    except DoctorError as error:
        typer.echo(f"doctor failed: {error}", err=True)
        raise typer.Exit(code=1) from error

    if as_json:
        typer.echo(report.model_dump_json(indent=2))
    else:
        typer.echo(format_report(report))

    problems = check_report(report, require_commit=require_commit)
    for problem in problems:
        typer.echo(f"FAIL: {problem}", err=True)
    if problems:
        raise typer.Exit(code=1)


def _require_simulator() -> None:
    """Exit cleanly, not with a traceback, when a sim-dependent command has no simulator."""
    if not has_simulator():
        typer.echo(
            "this command needs MetaDrive. Install the sim group: `uv sync --group sim`",
            err=True,
        )
        raise typer.Exit(code=1)


@app.command()
def categories() -> None:
    """List the categories, their block sequences, and the exit each one is pinned to."""
    for name, category in CATEGORIES.items():
        typer.echo(
            f"{name:<22} {category.block_seq:<4} {category.exit_rule.value:<9} "
            f"max_steps={category.max_steps:<5} {category.description}"
        )


def _rule_outcomes(readings: list) -> list[dict[str, object]]:
    """What each exit rule resolves to on these sockets, and why it does not where it does not.

    Every rule, including the ones that find nothing: "nothing here turns right. The closest is
    1T1_1_, which carries straight on" is the useful half of the answer when a road is not the
    shape a category assumes, and dropping it was what made the terminal's table quieter than it
    should be.

    A list rather than a dict so the order survives `json.dumps(sort_keys=True)`: the studio
    prints these in the order the terminal does, and `ExitRule` is where that order is declared.
    """
    from scenariobank.categories import ExitRule

    outcomes: list[dict[str, object]] = []
    for rule in ExitRule:
        try:
            chosen = select_exit(readings, rule)
        except SocketError as error:
            outcomes.append(
                {
                    "rule": rule.value,
                    "node": None,
                    "angle_deg": None,
                    "turn_deg": None,
                    "refused": str(error),
                }
            )
        else:
            outcomes.append(
                {
                    "rule": rule.value,
                    "node": chosen.node,
                    "angle_deg": chosen.angle_deg,
                    "turn_deg": chosen.turn_deg,
                    "refused": None,
                }
            )
    return outcomes


@app.command()
def sockets(
    block_seq: Annotated[
        str | None,
        typer.Option("--block-seq", "-b", help="Read the exits of this road."),
    ] = None,
    category: Annotated[
        str | None, typer.Option("--category", "-c", help="Use this category's block sequence.")
    ] = None,
    seed: Seed = 0,
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit the readings as JSON instead of a table.")
    ] = False,
) -> None:
    """Enumerate the exits of a block sequence's final block, with the turn each one implies.

    A discovery command: it is how the destination for a category was chosen, and it is what to
    re-run when MetaDrive changes shape. Expect one exit near +90, one near -90 and one near 0
    for a four-way junction. Two exits of the same sign and similar magnitude mean the block is
    not what you think it is -- stop and look before pinning anything to it.
    """
    _require_simulator()
    if (block_seq is None) == (category is None):
        raise typer.BadParameter(
            "provide exactly one of --block-seq or --category", param_hint="block sequence"
        )
    try:
        if category is not None:
            block_seq = get_category(category).block_seq
        readings = read_sockets(block_seq, seed)
    except (CategoryError, SocketError) as error:
        typer.echo(f"sockets failed: {error}", err=True)
        raise typer.Exit(code=1) from error

    rules = _rule_outcomes(readings)
    if as_json:
        # A document about *this road at this seed*, not a bare list of sockets: the rules are
        # what turn an exit into a destination, and a rule that finds nothing says why. Computed
        # here, where the table already computed it, so the page cannot reach a different answer
        # from the terminal.
        document = {
            "block_seq": block_seq,
            "seed": seed,
            "sockets": [vars(reading) for reading in readings],
            "rules": rules,
        }
        typer.echo(json.dumps(document, indent=2, sort_keys=True))
        return
    typer.echo(f"{block_seq} seed {seed}")
    # Only worth saying when the two angle columns disagree, which is exactly when the road
    # rotates the car before its last block. Silent for every single-block road.
    rotation = readings[0].entry_heading_deg if readings else 0.0
    if abs(rotation) >= 0.05:
        typer.echo(
            f"  the road turns the car {rotation:+.1f} deg before its last block, "
            f"so the two angles differ; the rules use 'turn'"
        )
    typer.echo(f"  {'socket':<16} {'node':<12} {'turn':>7} {'from spawn':>11}   which way")
    for reading in readings:
        typer.echo(f"  {reading.describe()}")
    for outcome in rules:
        name = outcome["rule"]
        if outcome["refused"] is None:
            typer.echo(f"  rule {name:<9} -> {outcome['node']} (turn {outcome['turn_deg']:+.1f})")
        else:
            typer.echo(f"  rule {name:<9} -- {outcome['refused']}")


@app.command("inspect")
def inspect_route(
    category: Annotated[
        str | None,
        typer.Option("--category", "-c", help="Use this category's road and exit rule."),
    ] = None,
    block_seq: Annotated[
        str | None,
        typer.Option("--block-seq", "-b", help="Build this road instead of a category's."),
    ] = None,
    rule: Annotated[
        str | None,
        typer.Option(
            "--rule", help="Which exit to drive to. Required with --block-seq."
        ),
    ] = None,
    seed: Seed = 0,
    out: Annotated[
        Path | None, typer.Option("--out", "-o", help="PNG to write. Defaults under docs/.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit what was drawn as JSON instead of a line.")
    ] = False,
) -> None:
    """Draw the pinned route over the road network, so the turn can be seen rather than trusted.

    `--block-seq` draws any sequence at any seed, including ones no category uses -- which is how
    you look at a candidate seed before committing it to `generate --seeds`, and what the studio's
    road builder runs. It needs `--rule`, because a bare sequence has no category to say which
    exit to drive to. Drawing writes no manifest: a road reaches a bank only through `add
    --block-seq`, which files it under a name derived from the road and the rule. `generate`
    still builds the eleven and only the eleven.

    `--json` adds `earned_max_steps`: the budget a category on this road would be given, from
    the same `step_budget` the eleven shipped ones were. It is the number to have in hand when
    deciding whether a road drawn here deserves to become a twelfth. With `--block-seq` it also
    adds `composed_name`, the category `add --block-seq` would file this road under.
    """
    _require_simulator()
    from scenariobank.categories import composed_name, step_budget
    from scenariobank.figures import FigureError, draw_route

    if (block_seq is None) == (category is None):
        raise typer.BadParameter(
            "provide exactly one of --block-seq or --category", param_hint="block sequence"
        )
    try:
        entry = get_category(category) if category else _ad_hoc_category(block_seq, rule)
        label = category or f"{block_seq}-{entry.exit_rule.value}"
        path = out or Path("docs/reference/figures") / f"{label}-seed{seed}.png"
        result = draw_route(entry, seed, path)
    except (CategoryError, SocketError, FigureError, ValueError) as error:
        typer.echo(f"inspect failed: {error}", err=True)
        raise typer.Exit(code=1) from error
    if as_json:
        result = {**result, "earned_max_steps": step_budget(result["route_length_m"])}
        if block_seq is not None:
            # The name `add --block-seq` would file this road under. Reported rather than
            # spelled out again by whoever asks, so the studio's card and the manifest cannot
            # come to different conclusions about what a road is called.
            result["composed_name"] = composed_name(block_seq, entry.exit_rule)
        typer.echo(json.dumps(result, indent=2, sort_keys=True))
        return
    typer.echo(
        f"{result['category']} seed {result['seed']} -> {result['destination']} "
        f"({result['net_rotation_deg']:+.1f} deg, {result['route_length_m']} m): {result['path']}"
    )


def _ad_hoc_category(block_seq: str, rule: str | None):
    """A throwaway `Category` so `--block-seq` can reuse the whole category-driven drawing path.

    It never leaves this process. `max_steps` is set from nothing measurable and is unused by
    `figures.render_route`, which reads only the name and the rule for its title.
    """
    from scenariobank.categories import Category, ExitRule

    if rule is None:
        raise typer.BadParameter(
            f"--block-seq needs --rule: choose one of "
            f"{', '.join(member.value for member in ExitRule)}",
            param_hint="--rule",
        )
    try:
        chosen = ExitRule(rule)
    except ValueError as error:
        raise typer.BadParameter(
            f"unknown rule {rule!r}: choose one of "
            f"{', '.join(member.value for member in ExitRule)}",
            param_hint="--rule",
        ) from error
    return Category(
        name=block_seq,
        block_seq=block_seq,
        exit_rule=chosen,
        max_steps=0,
        description=f"ad-hoc {block_seq} inspected at rule {chosen.value}",
    )


@app.command()
def generate(
    out: Annotated[
        Path, typer.Option("--out", "-o", help="Bank directory to write. Created if absent.")
    ],
    bank_id: Annotated[
        str, typer.Option("--bank-id", help="Name recorded in the manifest, e.g. pg-bank-2026-08.")
    ],
    category: Annotated[
        list[str] | None,
        typer.Option("--category", "-c", help="Repeatable. Defaults to every category."),
    ] = None,
    seeds: Annotated[
        list[str] | None,
        typer.Option(
            "--seeds",
            help="Which seeds to build each category at.",
        ),
    ] = None,
    thumbnails: Annotated[
        bool,
        typer.Option(
            "--thumbnails/--no-thumbnails",
            help="Draw a route figure per scenario: road, route, spawn arrow, destination.",
        ),
    ] = True,
) -> None:
    """Build the scenarios and write the bank a runner consumes.

    A bank is a disposable, per-batch artifact: regenerate it whenever you want scenarios, and
    do not expect a road to match a previous run's. What makes one batch self-consistent is the
    container pinning one MetaDrive commit -- run `scenariobank doctor` to see which.

    Progress goes to stderr, one line per scenario, because generation of a large bank is
    minutes of silence otherwise.
    """
    _require_simulator()
    from scenariobank.bank import BankError
    from scenariobank.bank import generate as build

    try:
        shared, per_category = _parse_seed_options(seeds)
        manifest = build(
            out,
            bank_id=bank_id,
            category_names=category or (),
            seeds=shared,
            category_seeds=per_category,
            thumbnails=thumbnails,
            progress=lambda message: typer.echo(message, err=True),
        )
    except (BankError, CategoryError, SocketError) as error:
        typer.echo(f"generate failed: {error}", err=True)
        raise typer.Exit(code=1) from error

    count = sum(len(entry.scenarios) for entry in manifest.categories.values())
    typer.echo(
        f"{count} scenarios in {len(manifest.categories)} categories "
        f"-> {out / 'manifest.json'}"
    )


def _parse_seeds(raw: str, *, hint: str = "--seeds") -> tuple[int, ...]:
    """Parse a comma-separated seed list, failing on the argument rather than in a map build."""
    try:
        return tuple(int(part) for part in raw.split(",") if part.strip())
    except ValueError as error:
        raise typer.BadParameter(f"seeds must be integers: {raw!r}", param_hint=hint) from error


def _parse_seed_options(
    raw: list[str] | None,
) -> tuple[tuple[int, ...], dict[str, tuple[int, ...]]]:
    """Split `--seeds` into the shared default and the per-category overrides.

    A bare list sets the default for every category; `name=list` overrides one. Later flags win,
    so a shared list can be given first and then contradicted for a single category. The category
    name is *not* validated here -- `bank.generate` owns that message, and owns it for the
    programmatic caller too.
    """
    shared = SEEDS
    overrides: dict[str, tuple[int, ...]] = {}
    for entry in raw or []:
        name, separator, values = entry.partition("=")
        if separator:
            overrides[name.strip()] = _parse_seeds(values, hint=f"--seeds {name.strip()}=")
        else:
            shared = _parse_seeds(entry)
    return shared, overrides


@app.command()
def replace(
    bank: BankDir,
    scenario: ScenarioKey,
    seed: Annotated[
        int | None,
        typer.Option("--seed", "-s", help="Seed to rebuild it at. Defaults to the one it has."),
    ] = None,
    exit_rule: Annotated[
        str | None,
        typer.Option("--exit-rule", help="Give this scenario its own exit rule."),
    ] = None,
    destination: Annotated[
        str | None,
        typer.Option("--destination", help="Pin an exact exit node instead of resolving one."),
    ] = None,
    inherit_exit: Annotated[
        bool,
        typer.Option("--inherit-exit", help="Drop this scenario's exit override."),
    ] = False,
    thumbnails: Annotated[
        bool,
        typer.Option("--thumbnails/--no-thumbnails", help="Redraw the scenario's PNG."),
    ] = True,
) -> None:
    """Rebuild one scenario of an existing bank, in place: a new seed, or a new destination.

    The correction loop: generate a bank, look at it, and swap the scenarios that turned out to
    be poor draws -- without paying to regenerate the other thirty-four.

    **The bank never changes size and never renumbers.** The scenario keeps its id and its
    position; only the seed and what was measured from it change. A seed already used elsewhere
    in the same category is refused, because a bank does not build one seed twice.

    Omit `--seed` to rebuild at the seed it already has, which is what changing where it drives
    to means. `--exit-rule` and `--destination` are recorded on the row as its own declared
    intent, and `--inherit-exit` puts it back on the category's; a pinned exit is dropped when the
    seed moves, because a node names an arm of that seed's road.

    Use `scenariobank seeds` to find a seed worth swapping to, `scenariobank sockets` to see the
    exits a road offers, and `scenariobank inspect --block-seq` to look at one first.
    """
    _require_simulator()
    from scenariobank.bank import BankError, replace_scenario

    try:
        row = replace_scenario(
            bank,
            scenario,
            seed,
            exit_rule=exit_rule,
            destination=destination,
            inherit_exit=inherit_exit,
            thumbnails=thumbnails,
            progress=lambda message: typer.echo(message, err=True),
        )
    except (BankError, CategoryError, SocketError) as error:
        typer.echo(f"replace failed: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(
        f"{row.scenario_id} rebuilt at seed {row.seed} -> {row.destination}: "
        f"{bank / 'manifest.json'}"
    )


@app.command("add")
def add_scenario_cmd(
    bank: BankDir,
    seed: Annotated[int, typer.Option("--seed", "-s", help="Seed to build it at.")],
    category: Annotated[
        str | None, typer.Option("--category", "-c", help="Which category to add a scenario to.")
    ] = None,
    block_seq: Annotated[
        str | None,
        typer.Option("--block-seq", "-b", help="Compose a road instead of naming a category."),
    ] = None,
    rule: Annotated[
        str | None,
        typer.Option("--rule", help="Which exit to drive to. Required with --block-seq."),
    ] = None,
    thumbnails: Annotated[
        bool,
        typer.Option("--thumbnails/--no-thumbnails", help="Draw the new scenario's PNG."),
    ] = True,
) -> None:
    """Add one more scenario to a bank: to a category it holds, or to a road you compose.

    **The id is one past the highest, never the row count.** Removing leaves a gap, and re-using
    an id would make every result already keyed on it ambiguous. So a `t_junction` holding
    `_0000` to `_0004` gains `t_junction_0005` even if one of those five is missing.

    With `--category`, the road and the rule come from the bank's own manifest, so a bank
    generated before a code change grows the way it was built. A category this bank no longer
    holds is re-created from this build's `categories.py`, which is what makes removing a
    category's last scenario an edit you can undo.

    With `--block-seq` and `--rule` the road is composed here, and **its category is named after
    it**: `CCX` driven to the `sharpest` exit is `CCX_sharpest`, always, so the same road never
    arrives twice under two names. A category created this way is capped at what its first route
    earns -- `step_budget` of the length just measured, the number `inspect --json` reports as
    `earned_max_steps`. This is what the studio's road builder runs behind **Add to bank**.
    """
    _require_simulator()
    from scenariobank.bank import BankError, add_scenario

    try:
        row = add_scenario(
            bank,
            category,
            seed,
            block_seq=block_seq,
            rule=rule,
            thumbnails=thumbnails,
            progress=lambda message: typer.echo(message, err=True),
        )
    except (BankError, CategoryError, SocketError) as error:
        typer.echo(f"add failed: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(
        f"added {row.scenario_id} at seed {row.seed} -> {row.destination}: "
        f"{bank / 'manifest.json'}"
    )


@app.command("remove")
def remove_scenario_cmd(bank: BankDir, scenario: ScenarioKey) -> None:
    """Take one scenario out of a bank, with its picture. No simulator.

    **The ids that remain do not move.** Renumbering the rows after it would change the id of a
    scenario nobody touched, and an id already written into a result is not this command's to
    re-point -- so the position is left empty and an id stops being a row number.

    A category whose last scenario goes is removed with it. The bank's last scenario is refused:
    an empty bank is a manifest describing nothing, and `generate` is how a new one is made.
    """
    from scenariobank.bank import BankError, remove_scenario

    try:
        row = remove_scenario(
            bank, scenario, progress=lambda message: typer.echo(message, err=True)
        )
    except BankError as error:
        typer.echo(f"remove failed: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"removed {row.scenario_id}: {bank / 'manifest.json'}")


@app.command("budget")
def budget_cmd(
    bank: BankDir,
    scenario: ScenarioKey,
    max_steps: Annotated[
        int | None,
        typer.Option("--max-steps", help="Steps this scenario gets, instead of its category's."),
    ] = None,
    inherit: Annotated[
        bool,
        typer.Option("--inherit", help="Drop the override and use the category's cap again."),
    ] = False,
) -> None:
    """Set or clear one scenario's own step budget. The only edit here that builds nothing.

    Every other field of a scenario is measured off a road, so changing it means building that
    road again. `max_steps` is a cap somebody chose -- a bound on a stuck episode, not a
    measurement -- so choosing a different one is an edit to the manifest and nothing else.

    It is still checked against what the route earns from `step_budget`: a budget under that is
    the one setting here that can leave a scenario unfinishable, and the warning says so.
    """
    from scenariobank.bank import BankError, set_max_steps

    if (max_steps is None) == (not inherit):
        raise typer.BadParameter(
            "provide exactly one of --max-steps or --inherit", param_hint="--max-steps"
        )
    try:
        row = set_max_steps(
            bank,
            scenario,
            None if inherit else max_steps,
            progress=lambda message: typer.echo(message, err=True),
        )
    except BankError as error:
        typer.echo(f"budget failed: {error}", err=True)
        raise typer.Exit(code=1) from error
    whose = "its own" if row.max_steps is not None else "its category's"
    typer.echo(f"{row.scenario_id} runs on {whose} budget: {bank / 'manifest.json'}")


#: One axis's flag, declared six times because a flag is what the reference, the studio's form and
#: `invoke.build_argv` all key on -- a single `--option axis=level` would be one flag with six
#: meanings, and none of the three could offer a dropdown for it.
def _axis(axis: str, label: str) -> Any:
    return Annotated[
        str | None,
        typer.Option(f"--{axis}", help=f"{label} level runs of this bank default to."),
    ]


@app.command("options")
def options_cmd(
    bank: BankDir,
    traffic: _axis("traffic", "Moving traffic") = None,
    cones: _axis("cones", "Coned-off lanes") = None,
    barriers: _axis("barriers", "Barriers and breakdowns") = None,
    pedestrians: _axis("pedestrians", "People on foot") = None,
    cyclists: _axis("cyclists", "People on bikes") = None,
    lights: _axis("lights", "Traffic lights") = None,
    show: Annotated[
        bool, typer.Option("--show", help="Print the levels this bank pins and change nothing.")
    ] = False,
) -> None:
    """Pin the option levels runs of this bank default to. Builds nothing.

    **These are applied when a run happens, not when the bank was built.** The roads, the routes
    and the thumbnails are the same at every level -- the map is generated before any object is
    placed, and the thumbnail draws lanes rather than objects -- so what is set here is *declared
    intent* and changing it is a manifest edit, in the same class as `budget`. Pinning options at
    generation time instead would mean regenerating 35 scenarios to change a traffic level.

    Set once, here, rather than typed into every run. A run flag still overrides what is pinned,
    and the result records the levels it actually used, so an override stays visible afterwards.
    """
    from scenariobank.bank import BankError, read_manifest, set_options
    from scenariobank.review import describe_options

    chosen = {
        axis: level
        for axis, level in (
            ("traffic", traffic),
            ("cones", cones),
            ("barriers", barriers),
            ("pedestrians", pedestrians),
            ("cyclists", cyclists),
            ("lights", lights),
        )
        if level is not None
    }
    if show and chosen:
        raise typer.BadParameter(
            "--show reads the levels; drop it to set them", param_hint="--show"
        )
    if not show and not chosen:
        raise typer.BadParameter(
            "name at least one axis to set, or pass --show to read them", param_hint="--show"
        )

    try:
        options = (
            read_manifest(bank).options
            if show
            else set_options(bank, chosen, progress=lambda m: typer.echo(m, err=True))
        )
    except (BankError, ValueError) as error:
        typer.echo(f"options failed: {error}", err=True)
        raise typer.Exit(code=1) from error

    for axis, level in options.model_dump().items():
        typer.echo(f"  {axis:<12} {level}")
    typer.echo(describe_options(options))
    if not show:
        typer.echo(f"pinned in {bank / 'manifest.json'}; nothing was rebuilt")


@app.command()
def seeds(
    category: CategoryName,
    keep: Annotated[
        str | None,
        typer.Option(
            "--keep", help="The seeds you are keeping, which candidates are measured against."
        ),
    ] = None,
    scan_range: Annotated[
        str, typer.Option("--scan", help="The candidate seeds to consider.")
    ] = "0-30",
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit the ranking as JSON instead of an aligned table.")
    ] = False,
) -> None:
    """Rank candidate seeds by how unlike the ones you are keeping they are.

    **A seed is not automatically a scenario.** Two seeds of one block sequence can draw roads a
    few percent apart, close enough that their thumbnails are the same picture -- `curve` seeds 0
    and 4 differ by 7%, and `roundabout` seeds 0 and 4 by nothing measurable. The distinct-roads
    count in `destinations.md` cannot see that, because it counts hash equality.

    So this measures each candidate against its *nearest* kept seed and ranks by the gap, largest
    first. Look at the top of the list with `inspect --block-seq`, then commit what you like with
    `generate --seeds` or `replace`.
    """
    _require_simulator()
    from scenariobank.variety import scan

    try:
        entry = get_category(category)
        kept = _parse_seeds(keep, hint="--keep") if keep else SEEDS
        readings = scan(
            entry,
            kept,
            _parse_scan(scan_range),
            # Progress on stderr, so `--json` writes a document to stdout and nothing else.
            progress=lambda message: typer.echo(message, err=True),
        )
    except (CategoryError, SocketError) as error:
        typer.echo(f"seeds failed: {error}", err=True)
        raise typer.Exit(code=1) from error

    if as_json:
        typer.echo(json.dumps(_seeds_report(category, entry, kept, readings), indent=2))
        return

    typer.echo(f"{category} ({entry.block_seq}), rule {entry.exit_rule.value}")
    typer.echo(f"  keeping: {', '.join(str(s) for s in kept)}")
    for reading in readings:
        if reading.is_kept:
            typer.echo(f"  {reading.describe()}")
    typer.echo("  --- candidates, most distinct first ---")
    for reading in readings:
        if not reading.is_kept:
            flag = "  <- near-duplicate" if reading.is_near_duplicate else ""
            typer.echo(f"  {reading.describe()}{flag}")


def _seeds_report(name: str, entry, kept, readings) -> dict:
    """`seeds` as one document: the same readings, in the order the table prints them.

    **The order is the answer**, so the list `scan` returned is kept as a list -- kept seeds
    first, then candidates most distinct first -- rather than being re-sorted or keyed by seed.
    `--json` and the aligned table are two renderings of one list, which is what
    `test_the_json_and_the_table_rank_the_same_seeds` holds them to.

    The road and the rule are carried because a ranking is only meaningful for the road it was
    measured on, and the studio swaps a seed into a bank that records its own.
    """
    return {
        "category": name,
        "block_seq": entry.block_seq,
        "exit_rule": entry.exit_rule.value,
        "keep": [int(seed) for seed in kept],
        "scan": [reading.as_dict() for reading in readings],
    }


def _parse_scan(raw: str) -> tuple[int, ...]:
    """Parse `--scan`: either an inclusive `a-b` range or a comma-separated list."""
    if "-" in raw and "," not in raw:
        low, _, high = raw.partition("-")
        try:
            return tuple(range(int(low), int(high) + 1))
        except ValueError as error:
            raise typer.BadParameter(
                f"expected a range like '0-30', got {raw!r}", param_hint="--scan"
            ) from error
    return _parse_seeds(raw, hint="--scan")


def _print_recorded(report) -> None:
    """The `review` printer for an imported bank. The PG one above is untouched.

    Deliberately the same field selection and the same wording `scenariobank workspace` uses for
    the conversion this was imported from (`workspace.format_report`). A bank that described its
    recording differently from the workspace it came from would be a second description of one
    thing, and the two would part company the day either was edited.

    No "N distinct of N" headline: `Report.distinct` is `None` here, because an imported bank
    holds one recording and there is nothing for it to be distinct from.
    """
    from scenariobank.workspace import _counts_line

    typer.echo(f"{report.bank_id}: {report.total} recording(s)")
    for one in report.categories:
        drive, replay = one.drive, one.replay
        typer.echo(
            f"\n{one.category}  {one.step_hz:g} Hz  {replay.frames} frames"
            f"  {replay.seconds:.1f} s"
        )
        typer.echo(
            f"  route:      {drive.route_length_min_m:.1f} to {drive.route_length_max_m:.1f} m"
            f"  {drive.lane_changes} lane changes, {drive.junction_movements} junction moves"
        )
        speed = f"{drive.speed_kph:g}" if drive.speed_kph is not None else "?"
        slowest = f"{drive.slowest_kph:g}" if drive.slowest_kph is not None else "?"
        typer.echo(
            f"  drive:      {drive.duration_min_s:.1f} to {drive.duration_max_s:.1f} s at up to "
            f"{speed} kph (slowest {slowest}), waiting {drive.waiting_s:g} s, "
            f"{drive.stop_count} stops"
        )
        typer.echo(f"  actors:     {_counts_line(one.actors.tracks)}")
        signals = one.signals
        cycle = f"{signals.cycle_seconds:g} s cycle" if signals.cycle_seconds else "no cycle"
        typer.echo(
            f"  lights:     {_counts_line(signals.lights)}"
            f"   ({signals.phase_groups} phase groups over {signals.signalled_lanes} lanes, "
            f"{cycle})"
        )
        # `None` rather than 0 on a bank imported before schema 1.4, and said as such: a zero
        # here would be a finding invented out of a field that was never written.
        size = one.map_size
        features = f"{size.features} map features" if size.features is not None else (
            "map size not recorded (imported before schema 1.4; re-import to gain it)"
        )
        kinds = f"  ({_counts_line(size.by_type)})" if size.by_type else ""
        typer.echo(f"  map:        {features}{kinds}")
        typer.echo(
            f"  replay:     {replay.longest} frames at {replay.at_hz:g} Hz"
            f"  ({replay.seconds:.1f} s of driving)"
        )
        for line in one.warnings:
            typer.echo(f"  ! {line}")

    typer.echo(f"\n{report.options_line}")


@app.command("review")
def review_bank(
    bank: Annotated[
        Path, typer.Option("--bank", help="Bank directory holding the manifest to review.")
    ],
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit the report as JSON instead of aligned text.")
    ] = False,
) -> None:
    """Say what is actually in a bank -- either kind, and they are not the same question.

    **A procedural bank of 55 rows is not automatically 55 scenarios.** `intersection_left`
    resolves every seed to the same destination, the same route and the same turn -- the `X`
    junction does not vary with the seed -- so five seeds draw two scenarios, one per spawn lane,
    and the other three are padding. Nothing said so until this command. That half reports
    duplicates, coverage, step budgets and spread.

    **An imported bank was driven rather than built**, so none of those apply: there is no seed to
    repeat, no exit to resolve and no budget to exceed, and it holds one recording. What it
    reports instead is what was measured off the recording -- the route and how long it took, the
    speeds and how much of it was spent stopped, who else is in it, the traffic lights and the
    invented plan behind them, the map size, and what replaying it costs.

    Reads the manifest and nothing else: **no simulator, no environment, and no rebuild**, so it
    runs on a machine with no MetaDrive and answers in milliseconds. The companion to `seeds`,
    which needs the simulator and asks the other question -- that one is *what should I build*,
    this one is *what did I build*. For a recording the companion is `workspace`, which says the
    same things about the conversion the bank was imported from.
    """
    from scenariobank.bank import BankError, read_manifest
    from scenariobank.review import review

    try:
        report = review(read_manifest(bank))
    except (BankError, ValueError) as error:
        typer.echo(f"review failed: {error}", err=True)
        raise typer.Exit(code=1) from error

    if as_json:
        typer.echo(report.model_dump_json(indent=2))
        return

    if report.source != "pg":
        _print_recorded(report)
        return

    typer.echo(f"{report.bank_id}: {report.distinct} distinct of {report.total}")
    for one in report.categories:
        dupes = one.duplicates
        typer.echo(
            f"\n{one.category} ({one.block_seq}, rule {one.exit_rule})"
            f"  {dupes.distinct} distinct of {dupes.total}"
        )
        for ids in dupes.identical_groups:
            typer.echo(f"  identical:  {' = '.join(ids)}")
        for ids in dupes.same_drive_groups:
            typer.echo(f"  same drive, different lane:  {', '.join(ids)}")
        if dupes.closest:
            typer.echo(
                f"  closest:    {dupes.closest.left} / {dupes.closest.right}"
                f"  {dupes.closest.gap * 100:.0f}%  ({dupes.closest.verdict})"
            )
        cover = one.coverage
        if cover.turn_pairs_present:
            covered = ", ".join(cover.turn_pairs_present)
            missing = f", missing {', '.join(cover.turn_pairs_missing)}" if (
                cover.turn_pairs_missing
            ) else ""
            typer.echo(f"  turn pairs: {covered}{missing}")
        typer.echo(
            f"  exits:      {', '.join(f'{k} x{v}' for k, v in cover.destinations.items())}"
            f"   lanes: {', '.join(f'{k} x{v}' for k, v in cover.spawn_lanes.items())}"
            f"   turns: {cover.turns_left}L/{cover.turns_right}R"
        )
        typer.echo(
            f"  route:      {one.spread.route_length_min_m:.1f} to "
            f"{one.spread.route_length_max_m:.1f} m"
            f"   rotation {one.spread.rotation_min_deg:+.1f} to "
            f"{one.spread.rotation_max_deg:+.1f} deg"
        )
        typer.echo(
            f"  budget:     worst earns {one.budget.worst_earned} of {one.budget.max_steps} steps"
            + (f"  OVER: {', '.join(one.budget.over_budget)}" if one.budget.over_budget else "")
        )
        for line in one.warnings:
            typer.echo(f"  ! {line}")

    # Last, and about the whole bank rather than one category: what is pinned here applies to every
    # run of every scenario above, and it is the one line on this page that no road was read for.
    typer.echo(f"\n{report.options_line}")


@app.command("workspace")
def workspace_cmd(
    path: Annotated[
        Path,
        typer.Option(
            "--path",
            "-p",
            help="A converter workspace directory, the one holding source/manifest.json.",
        ),
    ],
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit the report as JSON instead of aligned text.")
    ] = False,
) -> None:
    """Read a converter workspace: what a stored scenario is, before importing anything.

    A workspace is what `converter-scenarionet` leaves on disk for one place on earth. This says
    what is in one -- identity and provenance, which side of the road it drives on, every dataset
    directory it holds, the rate each was sampled at, the ego's route, and who else is recorded in
    it -- and **writes nothing**. No environment is built and no simulator is imported, so it runs
    on a machine with neither.

    The datasets are found by walking the workspace rather than read out of `stage_6`, and each
    one's rate is measured from its own timestamps. `stage_6` records the conversion that ran last,
    which on `junction-1` is one of three; trusting it would hide the other two and report the
    wrong rate for both.
    """
    from scenariobank.workspace import format_report, read_workspace

    try:
        report = read_workspace(path)
    except WorkspaceError as error:
        typer.echo(f"workspace failed: {error}", err=True)
        raise typer.Exit(code=1) from error

    typer.echo(report.model_dump_json(indent=2) if as_json else format_report(report))


@app.command()
def importing(
    path: Annotated[
        Path,
        typer.Option(
            "--path",
            "-p",
            help="The converter workspace the checklist is measured on.",
        ),
    ] = EXAMPLE_WORKSPACE,
    out: Annotated[
        Path, typer.Option("--out", "-o", help="Reference document to write.")
    ] = IMPORTING_DOC,
) -> None:
    """Write the checklist of what must come over when a workspace becomes a bank.

    Generated rather than hand-written, and generated against a real workspace: every row names a
    field `scenariobank workspace` reads and every value in it was measured at render time. A field
    the reader gains with no row here is an error, not a blank cell -- which is what keeps the
    checklist and the reader from drifting apart while `import` is still being written.

    Needs no simulator. It does need a converter workspace to read, the way `destinations` needs
    MetaDrive.
    """
    from scenariobank.importing import write

    try:
        written = write(out, path)
    except (WorkspaceError, ValueError) as error:
        typer.echo(f"importing failed: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"importing written: {written}")


@app.command("import")
def import_cmd(
    path: Annotated[
        Path,
        typer.Option(
            "--path",
            "-p",
            help="The converter workspace to import, the one holding source/manifest.json.",
        ),
    ],
    out: Annotated[
        Path, typer.Option("--out", "-o", help="Bank directory to write. Created if absent.")
    ],
    bank_id: Annotated[
        str | None,
        typer.Option(
            "--bank-id", help="Name recorded in the manifest. Defaults to the workspace's."
        ),
    ] = None,
    rate: Annotated[
        float,
        typer.Option(
            "--rate",
            help="Which conversion to take, in Hz. 100 keeps every decision rate that divides it.",
        ),
    ] = DEFAULT_RATE,
) -> None:
    """Turn a converter workspace into a bank, beside the procedural ones.

    One workspace becomes one bank holding one category, named after the workspace. The dataset is
    **copied in, not referenced**: a bank is mounted into a container and shipped to a rig, and a
    path into somebody's home directory is not. `junction-1` costs 50 MB at 100 Hz and 5.5 MB at
    10 Hz, which is the first thing in this project that makes a bank expensive to move.

    The rate is the one choice here that cannot be undone. `--decision-hz` is a stride in the
    runner's own loop and is never written into a bank, so it stays adjustable per run forever;
    `step_hz` is fixed when the pickle is written. Importing at 100 Hz keeps every decision rate
    that divides 100 available, and importing at 10 caps every future run at 10.

    Refused rather than imported: a workspace whose stage 5 did not pass, one that drives on the
    other side of the road, a rate it holds no conversion at, and an output directory that already
    holds a procedurally generated bank. Builds no environment and imports no simulator.

    What comes over and what is left behind is `docs/reference/importing.md`, which is generated
    from the same module as this command.
    """
    from scenariobank.bank import BankError
    from scenariobank.importing import import_workspace

    try:
        manifest = import_workspace(
            path,
            out,
            bank_id=bank_id,
            rate=rate,
            progress=lambda message: typer.echo(message, err=True),
        )
    except (BankError, WorkspaceError, ValueError) as error:
        typer.echo(f"import failed: {error}", err=True)
        raise typer.Exit(code=1) from error

    entry = next(iter(manifest.categories.values()))
    count = len(entry.scenarios)
    typer.echo(
        f"{count} {'scenario' if count == 1 else 'scenarios'} at {entry.step_hz:g} Hz "
        f"-> {out / 'manifest.json'}"
    )


@app.command()
def replay(
    bank: Annotated[
        Path, typer.Option("--bank", help="Bank directory holding the scenario to drive.")
    ],
    scenario: Annotated[
        str | None,
        typer.Option("--scenario", help="Which scenario, by its id. Defaults to the first."),
    ] = None,
    decision_hz: Annotated[
        float | None,
        typer.Option(
            "--decision-hz",
            help="Hold each action for this decision rate. Defaults to every step.",
        ),
    ] = None,
    steps: Annotated[
        int | None,
        typer.Option("--steps", help="Stop after this many steps, for a quick check."),
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit the report as JSON instead of aligned text.")
    ] = False,
    record_video: Annotated[
        Path | None,
        typer.Option(
            "--record-video",
            help="Write a top-down film of the drive to this .mp4, for looking at it. "
            "Changes nothing the report measures.",
        ),
    ] = None,
    camera_rig: Annotated[
        Path | None,
        typer.Option(
            "--camera-rig",
            help="Mount this camera spec on the ego (rigs/av3.txt) and read it at every "
            "decision; the report then says which cameras were alive and what a read cost.",
        ),
    ] = None,
    ignore_rig_rate: Annotated[
        bool,
        typer.Option(
            "--ignore-rig-rate",
            help="Mount the rig even though its tick_rate is not the interval it is read at. "
            "For looking at the cameras; the report still shows both rates.",
        ),
    ] = False,
    step_hz: Annotated[
        float | None,
        typer.Option(
            "--step-hz",
            help="Step a procedural road at this rate instead of MetaDrive's 10 Hz, with the "
            "budget scaled to match. A recording steps at its own rate and refuses any other.",
        ),
    ] = None,
) -> None:
    """Drive one scenario of a bank end to end and report what the drive measured.

    A **diagnostic**, and the companion to `review`: that one says what a bank claims about
    itself off the manifest, this one opens the simulator and checks the claim holds. It writes
    no result file and loads no policy -- the action is zero throttle and zero steering, which is
    enough to measure how long the episode is, how wide the observation is and what ends it.
    Running a bank for results is `run`'s job, which Phase 4 builds on the same loop.

    Drives both kinds of bank through one loop (`runner.py`) and one env builder (`env.py`). On a
    procedural road it resets onto the row's seed, pins the route to the row's destination, and
    runs to the entry's own `max_steps` rather than MetaDrive's 1000; a row's own budget caps it
    shorter. The options are the bank's pinned block. On a recording it opens the dataset at the
    rate the tracks were sampled at and runs to the recording's own length.

    Three things it settles that nothing else can. **A stored episode does not end by itself:**
    the env replays past the last recorded frame indefinitely unless `horizon` is set to the
    recording's own length, which this does. **The observation is 31 wide on a recording and 19
    on a road** -- same sensors, different navigation -- so a policy trained against one bank kind
    cannot be handed the other. **`--decision-hz` is a stride in the loop**, not a MetaDrive
    setting: deciding at 20 Hz on a 100 Hz recording holds each action for five steps and changes
    how many actions were issued, never how long the episode was. A road steps at 10 Hz, so a
    faster decision rate than that is refused there.

    **`--camera-rig` is the check that a rig's cameras are alive** (Phase 4 Step 6). MetaDrive
    deletes every camera from a headless env's sensors unless `image_observation` is on
    (`base_env.py:343`), so a rig that was never mounted looks exactly like one that was until
    something reads it; this reads every camera at every decision and reports the sensors the
    env held, how many image buffers, and the shape of each frame. The cameras never enter the
    observation, so nothing else in the report moves. A spec's `tick_rate` has to equal the
    interval it is read at -- the decision stride over the step rate -- and a road steps at
    10 Hz, so `rigs/av3.txt` at 0.05 s is refused there unless `--ignore-rig-rate` says the
    mismatch is understood -- or unless `--step-hz 100 --decision-hz 20` steps the road at the
    rig's own rate (Phase 4 Step 7), which is the AV3 stack's, and what `run` does for it.

    Needs the simulator. A full replay of `banks/junction-1` costs about 11 s; a road is well
    under a second, and about 15 s more with a six-camera rig, which is the offscreen window.
    Use `--steps` to check the round trip without paying for the whole episode.
    """
    from scenariobank.av3.camera_rig import RigError
    from scenariobank.bank import BankError, read_manifest
    from scenariobank.replay import drive, format_episode

    try:
        episode = drive(
            bank,
            read_manifest(bank),
            scenario=scenario,
            decision_hz=decision_hz,
            steps=steps,
            record_video=record_video,
            camera_rig=camera_rig,
            ignore_rig_rate=ignore_rig_rate,
            step_hz=step_hz,
        )
    except (BankError, RigError, ValueError) as error:
        typer.echo(f"replay failed: {error}", err=True)
        raise typer.Exit(code=1) from error

    typer.echo(episode.model_dump_json(indent=2) if as_json else format_episode(episode))


@app.command()
def rig(
    camera_rig: Annotated[
        Path, typer.Option("--camera-rig", help="The camera spec to read (rigs/av3.txt).")
    ],
    check_frame: Annotated[
        bool,
        typer.Option(
            "--check-frame",
            help="Also measure MetaDrive's vehicle frame on a real env, the facts the "
            "conversion rests on. Needs --bank and the simulator.",
        ),
    ] = False,
    bank: Annotated[
        Path | None,
        typer.Option("--bank", help="The bank whose first scenario the frame is measured on."),
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit the report as JSON instead of aligned text.")
    ] = False,
) -> None:
    """Read a camera-rig spec, convert it into MetaDrive's frame, and say where each camera aims.

    The spec is CARLA's (x forward, y right, +yaw right) and MetaDrive's vehicle frame is not
    (x right, y forward, +heading left), so the conversion is an x/y swap and a sign flip on yaw
    -- `av3/camera_rig.py` carries the measurements it was derived from. This prints every
    camera's resolved mount, heading and pitch beside its name and the direction it aims in
    words, so a camera named `front_left` that looks right is visible rather than baked in.
    Offline: reading a spec needs no simulator.

    `--check-frame` re-measures the frame itself on a real env: a `NodePath` parented to the
    ego is given a local offset or angle and read back in world coordinates against the car's
    own heading and attitude. Six rows -- +y forward, +x right, +H left, -H right, +P up, -P
    down -- and every rig mount and aim is wrong until all six pass. That is the sign-convention
    probe of Phase 4 Step 6; the model's own conversions are Step 7's, measured beside it.
    """
    from scenariobank.av3.camera_rig import (
        RigError,
        format_frame,
        load_rig,
        report_for,
    )
    from scenariobank.av3.camera_rig import (
        check_frame as measure_frame,
    )

    try:
        loaded = load_rig(camera_rig, read_interval_s=None)
    except RigError as error:
        typer.echo(f"rig spec rejected: {error}", err=True)
        raise typer.Exit(code=1) from error

    rows = None
    if check_frame:
        if bank is None:
            typer.echo("rig failed: --check-frame needs --bank, the road to measure on", err=True)
            raise typer.Exit(code=2)
        _require_simulator()
        from scenariobank.bank import BankError, read_manifest
        from scenariobank.env import build_env, seed_for
        from scenariobank.options import resolve_options
        from scenariobank.replay import select

        try:
            manifest = read_manifest(bank)
            _, entry, row = select(manifest)
        except BankError as error:
            typer.echo(f"rig failed: {error}", err=True)
            raise typer.Exit(code=1) from error
        env, prepare = build_env(bank, entry, resolve_options(manifest), rig=loaded)
        try:
            env.reset(seed=seed_for(row))
            prepare(env, row)
            rows = measure_frame(env)
        finally:
            env.close()

    report = report_for(loaded, rows)
    if as_json:
        typer.echo(report.model_dump_json(indent=2))
    else:
        typer.echo("\n".join(loaded.describe()))
        if rows is not None:
            typer.echo(f"MetaDrive's vehicle frame, measured on {bank}:")
            typer.echo("\n".join(format_frame(rows)))
    if rows is not None and not all(check.ok for check in rows):
        raise typer.Exit(code=1)


@app.command()
def av3(
    bank: Annotated[
        Path, typer.Option("--bank", help="Bank directory holding the scenario to drive.")
    ],
    camera_rig: Annotated[
        Path, typer.Option("--camera-rig", help="The camera spec the model reads (rigs/av3.txt).")
    ],
    scenario: Annotated[
        str | None,
        typer.Option("--scenario", help="Which scenario, by its id. Defaults to the first."),
    ] = None,
    model_config: Annotated[
        Path | None,
        typer.Option(
            "--model-config",
            help="The submission's model_dev.yml. MODEL_CONFIG in the environment otherwise.",
        ),
    ] = None,
    checkpoint: Annotated[
        Path | None,
        typer.Option(
            "--checkpoint",
            help="The .ep to load. MODEL_CHECKPOINT in the environment otherwise.",
        ),
    ] = None,
    no_model: Annotated[
        bool,
        typer.Option(
            "--no-model",
            help="Skip the checkpoint. Conversions 2, 4 and 5 are still checked, in seconds, "
            "on a machine with no torch.",
        ),
    ] = False,
    step_hz: Annotated[
        float | None,
        typer.Option("--step-hz", help="Step the road at this rate (100 for the AV3 stack)."),
    ] = None,
    decision_hz: Annotated[
        float | None,
        typer.Option("--decision-hz", help="Decide, read the rig and predict at this rate."),
    ] = None,
    ignore_rig_rate: Annotated[
        bool,
        typer.Option(
            "--ignore-rig-rate",
            help="Mount the rig even though its tick_rate is not the decision interval.",
        ),
    ] = False,
    decisions: Annotated[
        int,
        typer.Option(
            "--decisions",
            help="How many forward passes to run, spread over the drive; 0 for every decision, "
            "at about a second each.",
        ),
    ] = 40,
    nav_sweep: Annotated[
        float,
        typer.Option(
            "--nav-sweep",
            help="Radius in metres of the synthetic arc fed to the navigation-response test; "
            "0 turns the test off.",
        ),
    ] = 30.0,
    driver: Annotated[
        str,
        typer.Option("--driver", help="What drives while the model watches, as `pkg.mod:Name`."),
    ] = "scenariobank.policies:ExpertPolicy",
) -> None:
    """Run the AV3 model beside a drive and check every conversion into it. Nothing steers.

    The model half of the sign-convention probe (Phase 4 Step 7; `rig --check-frame` is the
    rig half). `av3/av3_model.py` writes five conversions into the checkpoint -- pixels, camera
    order, frame history, ego speed, route -- and not one of them raises when it is wrong: a
    mirrored route or a swapped camera pair is a model that runs, returns twenty plausible
    waypoints and drives into the oncoming carriageway. So each is measured here first, on a
    car the bundled expert is driving, where the answer is known: the camera map, the ego
    state against the car's own speed, the navigation block against the bridge's route
    points, the predicted waypoints against where the car went under both sign conventions,
    and the model's answer to a synthetic right-hand and left-hand bend.

    `--no-model` checks the three conversions that need no forward pass on a machine with no
    torch. With the model, `--step-hz 100 --decision-hz 20` is the rate the run will use; a
    pass is about a second, so `--decisions` bounds the count. Exit 0 when every checked
    conversion agrees, 1 when one fails, 2 when the probe cannot be set up.

    Needs the simulator, and for the model the sim image's torch and a GPU.
    """
    _require_simulator()
    from scenariobank.av3.probe import ProbeError, probe
    from scenariobank.bank import BankError, read_manifest

    try:
        code = probe(
            bank,
            read_manifest(bank),
            camera_rig=camera_rig,
            scenario=scenario,
            model_config=None if model_config is None else str(model_config),
            checkpoint=None if checkpoint is None else str(checkpoint),
            no_model=no_model,
            step_hz=step_hz,
            decision_hz=decision_hz,
            ignore_rig_rate=ignore_rig_rate,
            decisions=decisions,
            nav_sweep_m=nav_sweep,
            driver=driver,
            say=typer.echo,
        )
    except (BankError, ProbeError) as error:
        typer.echo(f"av3 failed: {error}", err=True)
        raise typer.Exit(code=2) from error
    if code:
        raise typer.Exit(code=code)


#: One axis's level flag on `run`, where it overrides the bank's pinned level for this run only.
def _run_axis(axis: str, label: str) -> Any:
    return Annotated[
        str | None,
        typer.Option(f"--{axis}", help=f"{label} level for this run, over the bank's pinned one."),
    ]


#: One axis's raw-number flag on `run`. The converter repo's house style is raw values, so both
#: spellings work; the result records the number and the nearest level name.
def _run_raw(flag: str, axis: str, label: str) -> Any:
    return Annotated[
        float | None,
        typer.Option(f"--{flag}", help=f"{label} as a number, instead of a level name for {axis}."),
    ]


#: Where a relative `--out` on `run` lands. `out/` is the one path the container can write
#: (`compose.yaml` mounts it at `/out`) and the one `.gitignore` covers, so a run named by hand
#: from the terminal lands in the same place as a run from the container -- and not in the repo
#: root, where fourteen verify-run directories had accumulated and been committed by 2026-09-10.
OUT_ROOT = Path("out")


def under_out(path: Path, tier: str | None = None) -> Path:
    """`easy1` -> `out/easy1`, and with `--tier hard`, `out/easy1/hard`.

    An absolute path, or one already under `out/`, is left where it is. The tier becomes a
    subdirectory so that the same `--out` run at `easy`, `medium` and `hard` gives three
    records side by side rather than the last one standing (2026-09-10; they had been
    overwriting each other). A run with no tier writes to the directory itself.
    """
    if not (path.is_absolute() or path.parts[:1] == (OUT_ROOT.name,)):
        path = OUT_ROOT / path
    return path if tier is None else path / tier


def _parse_ids(raw: list[str] | None) -> list[str] | None:
    """`--scenarios a,b --scenarios c` -> `[a, b, c]`; nothing given -> `None`."""
    if not raw:
        return None
    return [item.strip() for chunk in raw for item in chunk.split(",") if item.strip()]


@app.command()
def run(
    out: Annotated[
        Path,
        typer.Option(
            "--out",
            help="Directory to write results.json and results/<id>.json into. A relative path "
            "lands under out/, and a run with a tier goes into a subdirectory named after it: "
            "`--out film --tier hard` writes out/film/hard/.",
        ),
    ],
    bank: Annotated[
        Path | None, typer.Option("--bank", help="Bank directory holding the scenarios to run.")
    ] = None,
    job: Annotated[
        Path | None,
        typer.Option(
            "--job",
            help="A Job file to run instead of flags. The container's entrypoint and the queue "
            "hand the runner one of these; every flag but --out is then refused.",
        ),
    ] = None,
    categories: Annotated[
        list[str] | None,
        typer.Option(
            "--categories", help="Run only these categories. Comma-separated, or repeated."
        ),
    ] = None,
    scenarios: Annotated[
        list[str] | None,
        typer.Option(
            "--scenarios", help="Run only these scenario ids. Comma-separated, or repeated."
        ),
    ] = None,
    policy: Annotated[
        str, typer.Option("--policy", help="What drives, as `pkg.mod:Name`.")
    ] = "scenariobank.policies:ConstantPolicy",
    checkpoint: Annotated[
        Path | None,
        typer.Option("--checkpoint", help="Handed to the policy as `checkpoint_path`."),
    ] = None,
    tier: Annotated[
        str | None, typer.Option("--tier", help="Expand a tier to its six levels first.")
    ] = None,
    traffic: _run_axis("traffic", "Moving traffic") = None,
    cones: _run_axis("cones", "Coned-off lanes") = None,
    barriers: _run_axis("barriers", "Barriers and breakdowns") = None,
    pedestrians: _run_axis("pedestrians", "People on foot") = None,
    cyclists: _run_axis("cyclists", "People on bikes") = None,
    lights: _run_axis("lights", "Traffic lights") = None,
    traffic_density: _run_raw("traffic-density", "traffic", "Traffic density") = None,
    cones_count: _run_raw("cones-count", "cones", "How many cone corridors") = None,
    barriers_count: _run_raw("barriers-count", "barriers", "How many barrier scenes") = None,
    pedestrians_count: _run_raw("pedestrians-count", "pedestrians", "How many pedestrians") = None,
    cyclists_count: _run_raw("cyclists-count", "cyclists", "How many cyclists") = None,
    decision_hz: Annotated[
        float | None,
        typer.Option(
            "--decision-hz",
            help="Hold each action for this decision rate. Defaults to every step.",
        ),
    ] = None,
    save_trajectories: Annotated[
        bool,
        typer.Option(
            "--save-trajectories",
            help="Also write each scenario's per-decision actions under trajectories/.",
        ),
    ] = False,
    record_video: Annotated[
        bool,
        typer.Option(
            "--record-video",
            help="Write a top-down film of every row to <out>/videos/<scenario_id>.mp4, for "
            "looking at a run, and with --camera-rig one film per camera and a mosaic of them "
            "all beside it. Off by default; changes nothing the result records.",
        ),
    ] = False,
    camera_rig: Annotated[
        Path | None,
        typer.Option(
            "--camera-rig",
            help="Mount this camera spec on the ego for every row (rigs/av3.txt); the record "
            "names it, and --record-video then films every camera too.",
        ),
    ] = None,
    ignore_rig_rate: Annotated[
        bool,
        typer.Option(
            "--ignore-rig-rate",
            help="Mount the rig even though its tick_rate is not the interval it is read at. "
            "For filming: a film reads at the step rate whatever the spec says. A policy "
            "that reads the rig refuses it.",
        ),
    ] = False,
    heartbeat: Annotated[
        float,
        typer.Option(
            "--heartbeat",
            help="Print a progress line to stderr every this many seconds while a row runs: "
            "step, decision, speed, metres moved, route completed, the action held. Tells a "
            "slow row from a hung one. 0 turns it off.",
        ),
    ] = 10.0,
    step_hz: Annotated[
        float | None,
        typer.Option(
            "--step-hz",
            help="Step a procedural road at this rate instead of MetaDrive's 10 Hz, with every "
            "budget scaled to match; `--step-hz 100 --decision-hz 20` is the AV3 stack's. A "
            "recording steps at its own rate and refuses any other.",
        ),
    ] = None,
    model_config: Annotated[
        Path | None,
        typer.Option(
            "--model-config",
            help="The submission's model_dev.yml, for a policy that reads one "
            "(scenariobank.av3:AV3Policy). Every field is required; nothing is defaulted.",
        ),
    ] = None,
) -> None:
    """Score a policy against a bank, one result per scenario, and never abort the batch.

    The runner proper. `replay` drives one scenario and prints a report; this drives every
    scenario a job names, through the same env builder and the same loop, and writes the record
    everything downstream reads: `results.json` at the end, and `results/<scenario_id>.json` the
    moment each scenario ends, so a run that is killed part way is a scored partial run rather
    than a lost one.

    **The flags build a `Job`, and a `Job` is the one input.** The same model is what the
    container's entrypoint reads from a file (`--job`) and what a message on the queue carries,
    so a run submitted from the studio, from a terminal and from the NAS is the same run. Options
    travel as names and are resolved against the bank here: the bank's pinned levels, then
    `--tier`, then a level or a raw number per axis, the same precedence `resolve_options` pins.

    **A scenario that raises is a row, not an abort**: `status: "error"` with its traceback, and
    the next scenario runs. **A SIGTERM or Ctrl-C ends the scenario it lands in** with
    `failure_reason: "stopped"`, writes what has been scored, closes the env cleanly and exits 0
    -- an interrupt raised into `env.close()` has wedged a GPU before, so the signal sets a flag
    and nothing is ever raised into teardown. Every refusal -- the wrong bank at a path, a level
    the axis does not have, an unknown scenario id, a policy that will not load, a decision rate
    the env cannot step at -- comes before the simulator is opened.

    **A relative `--out` lands under `out/`, and a tier names a subdirectory.** `out/` is the
    directory the container writes and the one git ignores, so `--out easy1` from a terminal
    and `--out /out/easy1` from `docker compose` are the same place, and a run never lands in
    the repository root. An absolute path goes where it says. `--out film --tier hard` writes
    `out/film/hard/`, so the three tiers of one bank sit side by side instead of overwriting
    each other; a run without a tier writes to the directory itself.

    **`--camera-rig` puts the model's cameras on the car, and `--record-video` then films what
    they see** (Phase 4 Step 6b): `<out>/videos/<scenario_id>.<camera>.mp4` per camera and
    `<scenario_id>.rig.mp4` with every view tiled, beside the top-down film, at the step rate.
    The cameras never enter the observation, so a row with a rig scores exactly as the row
    without one. The AV3 spec declares 0.05 s and a road steps at 10 Hz, so a film needs
    `--ignore-rig-rate`; a film is a look, not a model input, and the record keeps both rates.

    **`--step-hz 100 --decision-hz 20` is the AV3 stack's clock** (Phase 4 Step 7): the road
    stepped at the rig's own 0.05 s and the bridge ticked at its 20 Hz, every step budget
    scaled with it. `--policy scenariobank.av3:AV3Policy` with `--camera-rig`, `--model-config`
    and `--checkpoint` is the submission -- six cameras into the checkpoint, its waypoints into
    the openpilot bridge (`scripts/bridge.sh start`), the bridge's pedals onto the car -- and
    it refuses `--ignore-rig-rate`, since a model reading a 20 Hz rig at 10 Hz is the silently
    wrong frame rate. `scenariobank.av3:BridgePolicy` is the same path with the model taken
    out, for a machine with no GPU. `scenariobank av3` measures every conversion first.

    Needs the simulator. A `T` road is well under a second per scenario; `banks/junction-1` is
    about 11 s; a rig adds about 15 s per row to open the offscreen window, and filming six
    cameras about 60 ms a step on the host. The AV3 forward pass is about a second a decision.
    """
    from pydantic import ValidationError

    from scenariobank.bank import BankError, read_manifest
    from scenariobank.options import OptionError
    from scenariobank.policies import PolicyError
    from scenariobank.results import JOB_SCHEMA_VERSION, Job, JobBank, JobOptions
    from scenariobank.runner import RunError, run_bank

    levels = {
        axis: level
        for axis, level in (
            ("traffic", traffic),
            ("cones", cones),
            ("barriers", barriers),
            ("pedestrians", pedestrians),
            ("cyclists", cyclists),
            ("lights", lights),
        )
        if level is not None
    }
    raw = {
        axis: value
        for axis, value in (
            ("traffic", traffic_density),
            ("cones", cones_count),
            ("barriers", barriers_count),
            ("pedestrians", pedestrians_count),
            ("cyclists", cyclists_count),
        )
        if value is not None
    }
    flags_given = [
        name
        for name, value in (
            ("--bank", bank),
            ("--categories", categories),
            ("--scenarios", scenarios),
            ("--checkpoint", checkpoint),
            ("--tier", tier),
            ("--decision-hz", decision_hz),
            ("--step-hz", step_hz),
            ("--model-config", model_config),
        )
        if value
    ] + [f"--{axis}" for axis in levels] + [f"--{axis}" for axis in raw]
    if policy != "scenariobank.policies:ConstantPolicy":
        flags_given.append("--policy")
    if save_trajectories:
        flags_given.append("--save-trajectories")

    try:
        if job is not None:
            if flags_given:
                raise typer.BadParameter(
                    f"--job carries the whole job; drop {', '.join(flags_given)}",
                    param_hint="--job",
                )
            what = Job.model_validate_json(job.read_text())
        else:
            if bank is None:
                raise typer.BadParameter("name a bank to run, or a --job file", param_hint="--bank")
            manifest = read_manifest(bank)
            wanted = _parse_ids(scenarios)
            chosen = _parse_ids(categories)
            if chosen is not None:
                unknown = sorted(set(chosen) - set(manifest.categories))
                if unknown:
                    raise typer.BadParameter(
                        f"{manifest.bank_id} has no category named {', '.join(unknown)}",
                        param_hint="--categories",
                    )
                in_categories = [
                    row.scenario_id
                    for name, entry in manifest.categories.items()
                    if name in chosen
                    for row in entry.scenarios
                ]
                wanted = in_categories if wanted is None else [
                    scenario_id for scenario_id in wanted if scenario_id in in_categories
                ]
            what = Job(
                schema_version=JOB_SCHEMA_VERSION,
                bank=JobBank(id=manifest.bank_id, path=str(bank)),
                scenarios=wanted,
                options=JobOptions(tier=tier, levels=levels, raw=raw),
                policy=policy,
                checkpoint_path=None if checkpoint is None else str(checkpoint),
                decision_hz=decision_hz,
                step_hz=step_hz,
                model_config_path=None if model_config is None else str(model_config),
                save_trajectories=save_trajectories,
            )
        out = under_out(out, what.options.tier)
        report = run_bank(
            what,
            out,
            progress=lambda line: typer.echo(line, err=True),
            record_video=record_video,
            camera_rig=camera_rig,
            ignore_rig_rate=ignore_rig_rate,
            heartbeat_s=heartbeat if heartbeat > 0 else None,
        )
    except (BankError, OptionError, PolicyError, RunError, ValidationError, ValueError) as error:
        typer.echo(f"run failed: {error}", err=True)
        raise typer.Exit(code=1) from error

    summary = report.summary
    stopped = "  (stopped before the end)" if report.stopped else ""
    typer.echo(
        f"results written: {out / 'results.json'}  "
        f"{summary.n} scenarios, success rate {summary.success_rate:.2f}{stopped}"
    )


@app.command()
def commands(
    out: Annotated[
        Path, typer.Option("--out", "-o", help="Reference document to write.")
    ] = Path("docs/reference/commands.md"),
) -> None:
    """Write the command reference, read off this CLI and out of `categories.py`.

    Generated rather than hand-written for the reason `destinations` is: a retyped table goes
    stale. In two days `--seeds` became repeatable, `inspect` grew `--block-seq`, and two commands
    appeared -- a hand-maintained page would have been wrong about all four with nothing to say so.
    `tests/unit/test_docs.py` fails if the checked-in file falls behind the code.

    Needs no simulator.
    """
    from scenariobank.docs import write

    typer.echo(f"commands written: {write(out)}")


#: Where the destinations reference lives, relative to the directory the studio runs in. Named
#: once and shared with `web.api`, which reads the document back out to show it on the page:
#: a command that writes somewhere the page does not look would be a screen that never updates.
DESTINATIONS_DOC = Path("docs/reference/destinations.md")


@app.command()
def destinations(
    out: Annotated[
        Path, typer.Option("--out", "-o", help="Reference document to write.")
    ] = DESTINATIONS_DOC,
) -> None:
    """Resolve every category at every seed and write the destinations reference.

    Two env builds per category per seed -- one to read the sockets, one to prove the chosen
    destination is reachable and measure the route.
    """
    _require_simulator()
    from scenariobank.destinations import write

    try:
        # One line per road and per category as it lands, for the reason `examples` reports the
        # same way: this is twenty seconds of work and the studio shows what it says while it runs.
        path = write(out, SEEDS, progress=typer.echo)
    except SocketError as error:
        typer.echo(f"destinations failed: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"destinations written: {path}")


#: Where the studio's gallery reads its example pictures from. Separate from
#: `docs/reference/figures/`, which is where an ad-hoc `inspect` lands by default and so holds
#: whatever anyone happened to draw -- not a directory a page could safely show.
EXAMPLES_DIR = Path("docs/reference/examples")


@app.command()
def examples(
    out: Annotated[
        Path, typer.Option("--out", "-o", help="Directory to write the example pictures into.")
    ] = EXAMPLES_DIR,
    category: Annotated[
        str | None,
        typer.Option("--category", "-c", help="Redraw one category instead of all of them."),
    ] = None,
    seed: Seed = 0,
) -> None:
    """Draw one example picture per category, which is what the studio's gallery shows.

    Checked in rather than drawn on demand: this is the screen you meet before you have a bank, a
    simulator, or any patience, and the web group installs neither MetaDrive nor matplotlib. Re-run
    it when a category's road or exit rule changes -- a picture nobody regenerates goes stale, and
    the failure is silent.
    """
    _require_simulator()
    from scenariobank.figures import FigureError, draw_route

    try:
        wanted = {category: get_category(category)} if category else dict(CATEGORIES)
    except CategoryError as error:
        typer.echo(f"examples failed: {error}", err=True)
        raise typer.Exit(code=1) from error

    out.mkdir(parents=True, exist_ok=True)
    for name, entry in wanted.items():
        try:
            result = draw_route(entry, seed, out / f"{name}.png")
        except (CategoryError, SocketError, FigureError, ValueError) as error:
            typer.echo(f"examples failed on {name}: {error}", err=True)
            raise typer.Exit(code=1) from error
        # One line per figure as it lands: the studio reads this log while the job runs, so
        # progress costs nothing beyond saying what was just drawn.
        typer.echo(f"{name} -> {result['path']} ({result['destination']})")


#: Addresses the studio will bind. Nothing else, because these routes run subprocesses that write
#: into the repo and there is no authentication -- so the only safe listener is one nothing else
#: can reach. Refused loudly rather than silently rewritten, so an attempt to expose it is an
#: error message and not a surprise.
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


@app.command()
def studio(
    banks_root: Annotated[
        Path, typer.Option("--banks-root", help="Directory the bank list is read from.")
    ] = Path("banks"),
    host: Annotated[str, typer.Option("--host", help="Address to bind.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Port to bind.")] = 8770,
) -> None:
    """Serve the local authoring studio: the bank in a page instead of an image viewer.

    This is the way in. Every simulation runs as a subprocess of this same CLI, because a
    MetaDrive engine is one per process and a server holding one would die with it -- so the page's
    forms and its validation are derived from this CLI's own parameters rather than declared a
    second time, and the two cannot drift apart.

    Needs the web group: `uv sync --group web`.
    """
    if host not in _LOOPBACK:
        raise typer.BadParameter(
            f"the studio binds loopback only, and {host!r} is not. It runs commands that write "
            "into this repository and it has no authentication.",
            param_hint="--host",
        )
    try:
        import uvicorn
    except ModuleNotFoundError as error:
        typer.echo(
            "the studio needs FastAPI and uvicorn. Install the web group: "
            "`uv sync --group web`",
            err=True,
        )
        raise typer.Exit(code=1) from error

    from scenariobank.web.api import STATE_DIR_NAME, create_app

    application = create_app(
        banks_root=banks_root, state_dir=Path(STATE_DIR_NAME), workdir=Path.cwd()
    )
    typer.echo(f"studio on http://{host}:{port}/  (banks: {banks_root})")
    uvicorn.run(application, host=host, port=port, log_level="warning")
