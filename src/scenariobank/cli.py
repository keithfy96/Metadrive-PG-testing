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
    str, typer.Option("--category", "-c", help=f"One of: {', '.join(sorted(CATEGORIES))}.")
]

app = typer.Typer(
    name="scenariobank",
    help="Build, verify, and run a bank of procedurally generated MetaDrive scenarios.",
    no_args_is_help=True,
)


@app.callback()
def main(verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False) -> None:
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
        typer.Option("--block-seq", "-b", help="Block sequence, e.g. X. 'I' is prepended."),
    ] = None,
    category: Annotated[
        str | None, typer.Option("--category", "-c", help="Use this category's block sequence.")
    ] = None,
    seed: Seed = 0,
    as_json: Annotated[bool, typer.Option("--json")] = False,
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
    category: CategoryName,
    seed: Seed = 0,
    out: Annotated[
        Path | None, typer.Option("--out", "-o", help="PNG to write. Defaults under docs/.")
    ] = None,
) -> None:
    """Draw the pinned route over the road network, so the turn can be seen rather than trusted."""
    _require_simulator()
    from scenariobank.figures import draw_route

    try:
        entry = get_category(category)
        path = out or Path("docs/reference/figures") / f"{category}-seed{seed}.png"
        result = draw_route(entry, seed, path)
    except (CategoryError, SocketError) as error:
        typer.echo(f"inspect failed: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(
        f"{result['category']} seed {result['seed']} -> {result['destination']} "
        f"({result['angle_deg']:+.1f} deg, {result['route_length_m']} m): {result['path']}"
    )


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
