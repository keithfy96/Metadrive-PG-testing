"""Top-level command-line interface."""

import json
from pathlib import Path
from typing import Annotated

import typer

from scenariobank.categories import CATEGORIES, SEEDS, CategoryError, get_category
from scenariobank.doctor import DoctorError, collect, format_report, has_simulator
from scenariobank.doctor import check as check_report
from scenariobank.logging import configure_logging
from scenariobank.sockets import SocketError, read_sockets, select_exit

Seed = Annotated[int, typer.Option("--seed", "-s", help="Map seed.")]
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

    if as_json:
        typer.echo(json.dumps([vars(reading) for reading in readings], indent=2, sort_keys=True))
        return
    typer.echo(f"{block_seq} seed {seed}")
    typer.echo(f"  {'socket':<16} {'node':<12} {'angle':>7}  turn")
    for reading in readings:
        typer.echo(f"  {reading.describe()}")
    for rule_name in ("left", "right", "straight", "sharpest", "only"):
        try:
            from scenariobank.categories import ExitRule

            chosen = select_exit(readings, ExitRule(rule_name))
        except SocketError:
            continue
        typer.echo(f"  rule {rule_name:<9} -> {chosen.node} ({chosen.angle_deg:+.1f})")


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
) -> None:
    """Draw the pinned route over the road network, so the turn can be seen rather than trusted.

    `--block-seq` draws any sequence at any seed, including ones no category uses -- which is how
    you look at a candidate seed before committing it to `generate --seeds`. It needs `--rule`,
    because a bare sequence has no category to say which exit to drive to. Nothing drawn this way
    reaches a manifest; a bank always holds exactly the seven categories.
    """
    _require_simulator()
    from scenariobank.figures import draw_route

    if (block_seq is None) == (category is None):
        raise typer.BadParameter(
            "provide exactly one of --block-seq or --category", param_hint="block sequence"
        )
    try:
        entry = get_category(category) if category else _ad_hoc_category(block_seq, rule)
        label = category or f"{block_seq}-{entry.exit_rule.value}"
        path = out or Path("docs/reference/figures") / f"{label}-seed{seed}.png"
        result = draw_route(entry, seed, path)
    except (CategoryError, SocketError, ValueError) as error:
        typer.echo(f"inspect failed: {error}", err=True)
        raise typer.Exit(code=1) from error
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
    bank: Annotated[
        Path, typer.Option("--bank", help="Bank directory holding the manifest to correct.")
    ],
    scenario: Annotated[
        str, typer.Option("--scenario", help="Which scenario to rebuild.")
    ],
    seed: Annotated[int, typer.Option("--seed", "-s", help="Seed to rebuild it at.")],
    thumbnails: Annotated[
        bool,
        typer.Option("--thumbnails/--no-thumbnails", help="Redraw the scenario's PNG."),
    ] = True,
) -> None:
    """Rebuild one scenario of an existing bank at a different seed, in place.

    The correction loop: generate a bank, look at it, and swap the scenarios that turned out to
    be poor draws -- without paying to regenerate the other thirty-four.

    **The bank never changes size and never renumbers.** The scenario keeps its id and its
    position; only the seed and what was measured from it change. A seed already used elsewhere
    in the same category is refused, because a bank does not build one seed twice.

    Use `scenariobank seeds` to find a seed worth swapping to, and `scenariobank inspect
    --block-seq` to look at it first.
    """
    _require_simulator()
    from scenariobank.bank import BankError, replace_scenario

    try:
        row = replace_scenario(
            bank,
            scenario,
            seed,
            thumbnails=thumbnails,
            progress=lambda message: typer.echo(message, err=True),
        )
    except (BankError, CategoryError, SocketError) as error:
        typer.echo(f"replace failed: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"{row.scenario_id} rebuilt at seed {row.seed}: {bank / 'manifest.json'}")


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
    from scenariobank.variety import NEAR_DUPLICATE, scan

    try:
        entry = get_category(category)
        kept = _parse_seeds(keep, hint="--keep") if keep else SEEDS
        readings = scan(
            entry,
            kept,
            _parse_scan(scan_range),
            progress=lambda message: typer.echo(message, err=True),
        )
    except (CategoryError, SocketError) as error:
        typer.echo(f"seeds failed: {error}", err=True)
        raise typer.Exit(code=1) from error

    typer.echo(f"{category} ({entry.block_seq}), rule {entry.exit_rule.value}")
    typer.echo(f"  keeping: {', '.join(str(s) for s in kept)}")
    for reading in readings:
        if reading.is_kept:
            typer.echo(f"  {reading.describe()}")
    typer.echo("  --- candidates, most distinct first ---")
    for reading in readings:
        if not reading.is_kept:
            flag = "  <- near-duplicate" if (reading.gap or 0) < NEAR_DUPLICATE else ""
            typer.echo(f"  {reading.describe()}{flag}")


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


@app.command()
def destinations(
    out: Annotated[
        Path, typer.Option("--out", "-o", help="Reference document to write.")
    ] = Path("docs/reference/destinations.md"),
) -> None:
    """Resolve every category at every seed and write the destinations reference.

    Two env builds per category per seed -- one to read the sockets, one to prove the chosen
    destination is reachable and measure the route.
    """
    _require_simulator()
    from scenariobank.destinations import write

    try:
        path = write(out, SEEDS)
    except SocketError as error:
        typer.echo(f"destinations failed: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"destinations written: {path}")


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

    A second front door onto these same commands, not a second implementation of them. Every
    simulator command it offers runs as a subprocess of this CLI, so what the page does and what
    `docs/reference/commands.md` describes cannot drift apart.

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
