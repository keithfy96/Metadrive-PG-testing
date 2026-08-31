"""Top-level command-line interface."""

from typing import Annotated

import typer

from scenariobank.doctor import DoctorError, collect, format_report
from scenariobank.doctor import check as check_report
from scenariobank.logging import configure_logging

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
