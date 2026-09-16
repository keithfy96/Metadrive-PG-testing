"""Writing `docs/reference/level-calibration.md` -- the Phase 4b sweep, one axis at a time.

`options.LEVELS` gives each level name a number, and until this module ran those numbers were a
guess. A `high` traffic that no policy can pass is not a hard test point, it is a broken
scenario; a `low` that the expert never notices is the same as `none` with a different label. So
the table is measured: one axis is swept across a list of raw values with **every other axis held
at `none`**, the bundled expert drives the bank once per value, and the success rate per value is
what the reader picks the four levels from. Single-axis on purpose: a bank pins levels, not
numbers, so the numbers can move here without touching a bank on disk, and a sweep that moved
two axes at once could not say which one the rate answered to.

Each sweep is `run_bank` once per value -- the same loop, the same record, one env per row -- so
a calibration point is exactly what a `run` at that raw value would have scored. The sweep's
own record is `Sweep`, written as JSON under `docs/reference/calibration/` next to the page,
and the page is rendered from those records alone: `tests/unit/test_calibration.py` holds the
checked-in page to the render and holds `LEVELS` to the swept values, which is the "done when"
of Phase 4b made mechanical. Re-measure after a MetaDrive bump the way `destinations` is.

Three facts the reader of the page needs, carried here so the page can say them:

* The traffic floor. `PGTrafficManager.reset` places nothing under `abs(density) < 1e-2`, so a
  traffic value in `(0, 0.01)` is refused before any run starts rather than measured as a `none`
  that the record calls something else (`options.TRAFFIC_FLOOR`).
* The collisions column is the **ego's own**. `crash_vehicle` and its siblings are flags on the
  ego (`base_vehicle.py:43-45`), counted on the rising edge by the runner; a traffic car that hits
  another traffic car does not appear in it. PG has neither traffic lights nor a give-way rule, so
  at higher densities that happens, and it shows here as `max_step` rows -- the ego stuck behind
  the wreck -- rather than as a collision. Read the collisions column for what the ego did and the
  `ended by` column for what the scene did.
* `placed` is read off the scene after the reset, not off the level. Cones and barriers land only
  on `Straight` and `Curve` blocks, so on an `X`, `T` or `O` road the axis places nothing at any
  value and the success rate does not move; `break_down_scene` under the cones axis spawns a
  vehicle, so `cones` above zero puts a car on the road at `traffic=0`. Both are visible in that
  column, which is why it is in the table.

Nothing here imports MetaDrive at module scope; `sweep` reaches the simulator through
`runner.run_bank`, and the render and the record work on any machine.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from scenariobank.bank import Manifest, read_manifest
from scenariobank.options import (
    AXES,
    LEVEL_NAMES,
    LEVELS,
    NUMERIC_AXES,
    TRAFFIC_FLOOR,
    OptionError,
    nearest_level,
    resolve_options,
)
from scenariobank.results import (
    JOB_SCHEMA_VERSION,
    Job,
    JobBank,
    JobOptions,
    Results,
    write_json,
)

#: Where a sweep's record lands, one file per axis and bank, and the page rendered from them.
CALIBRATION_DIR = Path("docs/reference/calibration")
CALIBRATION_DOC = Path("docs/reference/level-calibration.md")

#: Bumped when a field of `Sweep` or `Point` is added, removed or changes meaning.
SWEEP_SCHEMA_VERSION = 1

#: The policy a sweep drives with unless told otherwise: the ceiling. A floor policy fails at
#: every value and separates nothing.
EXPERT = "scenariobank.policies:ExpertPolicy"


class Point(BaseModel):
    """One value of the axis: the batch it ran as, in the numbers the page shows."""

    model_config = ConfigDict(extra="forbid")

    value: float
    n: int
    successes: int
    success_rate: float
    by_failure_reason: dict[str, int] = Field(default_factory=dict)
    by_status: dict[str, int] = Field(default_factory=dict)
    #: The ego's collisions by `runner.COLLISION_FLAGS` name, summed over the rows.
    collisions: dict[str, int] = Field(default_factory=dict)
    #: Every object in the scene after each reset, by MetaDrive class, summed over the rows.
    placed: dict[str, int] = Field(default_factory=dict)
    steps_mean: float
    reward_mean: float
    wall_time_s: float
    #: Where this value's `results.json` was written.
    out: str


class Sweep(BaseModel):
    """One axis over one bank: what was held, what was driven, and a `Point` per value."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    axis: str
    bank_id: str
    bank_path: str
    #: The categories the rows came from, in manifest order.
    categories: list[str]
    scenarios: list[str]
    policy: str
    #: Every other axis and the level it was held at: `none`, all five.
    held: dict[str, str]
    measured_utc: str
    metadrive_commit: str | None = None
    points: list[Point]


class CalibrationError(ValueError):
    """A sweep that cannot be run, or a record that cannot be read. Says which and why."""


def held_levels(axis: str) -> dict[str, str]:
    """Every axis but the swept one, at `none`."""
    return {other: "none" for other in AXES if other != axis}


def check_axis(axis: str) -> None:
    if axis not in NUMERIC_AXES:
        raise CalibrationError(
            f"{axis!r} is not an axis a number can be swept over. The five are: "
            f"{', '.join(NUMERIC_AXES)}; lights is a schedule and has no number to sweep."
        )


def check_values(manifest: Manifest, axis: str, values: Sequence[float]) -> None:
    """Refuse every bad value before the first env is built, naming it.

    Resolving each value against the bank is the same check a `run` would make -- the floor,
    the whole-number rule for a count, a recorded bank having no axes -- so a sweep cannot be
    refused at value four after three values' worth of driving.
    """
    check_axis(axis)
    if not values:
        raise CalibrationError("no values to sweep; give at least one, e.g. --values 0,0.05,0.1")
    if len(set(values)) != len(values):
        raise CalibrationError(f"a value repeats in {list(values)}; each value is run once")
    for value in values:
        try:
            resolve_options(manifest, levels=held_levels(axis), raw={axis: value})
        except OptionError as error:
            raise CalibrationError(f"{axis}={value:g}: {error}") from error


def job_for(
    manifest: Manifest,
    bank_dir: Path,
    axis: str,
    value: float,
    *,
    scenarios: list[str] | None,
    policy: str = EXPERT,
) -> Job:
    """The job one value runs as: the bank, the rows, the axis as a raw number, the rest at none."""
    return Job(
        schema_version=JOB_SCHEMA_VERSION,
        bank=JobBank(id=manifest.bank_id, path=str(bank_dir)),
        scenarios=scenarios,
        options=JobOptions(levels=held_levels(axis), raw={axis: float(value)}),
        policy=policy,
    )


def _rows_for(manifest: Manifest, categories: list[str] | None) -> tuple[list[str], list[str]]:
    """The categories to run and their scenario ids, in manifest order."""
    if categories is not None:
        unknown = sorted(set(categories) - set(manifest.categories))
        if unknown:
            raise CalibrationError(
                f"{manifest.bank_id} has no category named {', '.join(unknown)}; it has "
                f"{', '.join(manifest.categories)}"
            )
    chosen = [name for name in manifest.categories if categories is None or name in categories]
    ids = [row.scenario_id for name in chosen for row in manifest.categories[name].scenarios]
    return chosen, ids


def _sum_counts(rows: list[dict[str, int]]) -> dict[str, int]:
    total: dict[str, int] = {}
    for counts in rows:
        for name, count in counts.items():
            total[name] = total.get(name, 0) + count
    return dict(sorted(total.items()))


def point_for(value: float, report: Results, out: Path) -> Point:
    """A `Results` folded into the one row of the table this value is."""
    rows = report.results
    scored = [row for row in rows if row.status == "ok"]
    return Point(
        value=float(value),
        n=report.summary.n,
        successes=sum(1 for row in rows if row.success),
        success_rate=report.summary.success_rate,
        by_failure_reason=dict(report.summary.by_failure_reason),
        by_status=dict(report.summary.by_status),
        collisions=_sum_counts([row.collisions for row in rows]),
        placed=_sum_counts([row.placed for row in rows]),
        steps_mean=round(sum(row.steps for row in scored) / len(scored), 1) if scored else 0.0,
        reward_mean=round(sum(row.reward for row in scored) / len(scored), 3) if scored else 0.0,
        wall_time_s=round(sum(row.wall_time_s for row in rows), 2),
        out=str(out),
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sweep(
    bank_dir: Path,
    axis: str,
    values: Sequence[float],
    *,
    categories: list[str] | None = None,
    policy: str = EXPERT,
    out: Path,
    progress: Callable[[str], None] | None = None,
    run: Callable[..., Results] | None = None,
) -> Sweep:
    """Drive the bank once per value of `axis`, every other axis at `none`, and fold the results.

    Every refusal comes first and builds no env: the axis, each value, each category. Then one
    `run_bank` per value, its results under `<out>/<axis>/<bank_id>/<axis>=<value>/`, reported
    through `progress` as `[k/m] axis=value ...` before and a one-line score after, with the
    runner's own per-row lines between. `run` is `runner.run_bank` unless a test hands in another.
    """
    manifest = read_manifest(bank_dir)
    check_values(manifest, axis, values)
    chosen, ids = _rows_for(manifest, categories)
    if not ids:
        raise CalibrationError(f"{manifest.bank_id} has no scenarios to sweep")
    say = progress or (lambda _message: None)
    if run is None:
        from scenariobank.runner import run_bank

        run = run_bank
    from scenariobank.runner import _metadrive_commit

    started = _utc_now()
    points: list[Point] = []
    for index, value in enumerate(values, start=1):
        target = out / axis / manifest.bank_id / f"{axis}={value:g}"
        say(f"[{index}/{len(values)}] {axis}={value:g}  {len(ids)} scenarios -> {target}")
        report = run(
            job_for(manifest, bank_dir, axis, value, scenarios=ids, policy=policy),
            target,
            progress=say,
        )
        point = point_for(value, report, target)
        points.append(point)
        say(f"    {axis}={value:g}  {_score_line(point)}")
    return Sweep(
        schema_version=SWEEP_SCHEMA_VERSION,
        axis=axis,
        bank_id=manifest.bank_id,
        bank_path=str(bank_dir),
        categories=chosen,
        scenarios=ids,
        policy=policy,
        held=held_levels(axis),
        measured_utc=started,
        metadrive_commit=_metadrive_commit(),
        points=points,
    )


def _score_line(point: Point) -> str:
    ended = ", ".join(f"{name} {n}" for name, n in point.by_failure_reason.items()) or "arrived"
    hits = ", ".join(f"{name} {n}" for name, n in point.collisions.items() if n) or "none"
    return (
        f"success {point.successes}/{point.n} = {point.success_rate:.2f}  "
        f"ended by: {ended}  ego collisions: {hits}"
    )


# --- picking the four ---------------------------------------------------------------------------


def suggest(points: Sequence[Point]) -> dict[str, float] | None:
    """Four values that spread the success rate apart, or `None` when the sweep offers none.

    `none` is 0, always. `high` is the smallest value at which the rate is lowest: past it the
    axis buys nothing but wall time. `low` and `medium` are the values between whose rates sit
    nearest a third and two thirds of the way down from the rate at 0, taken from candidates
    strictly between 0 and `high`, distinct, in order. A sweep with no drop, or with fewer than
    two values between 0 and the drop, cannot be spread into four levels and says so with `None`
    -- which on an `X` road under the cones axis is the right answer, not a failure.
    """
    by_value = {point.value: point for point in points}
    if 0.0 not in by_value or len(by_value) < 4:
        return None
    top = by_value[0.0].success_rate
    floor = min(point.success_rate for point in points)
    if floor >= top:
        return None
    high = min(point.value for point in points if point.success_rate == floor)
    between = sorted((p for p in points if 0.0 < p.value < high), key=lambda p: p.value)
    if len(between) < 2:
        return None
    targets = [top - (top - floor) / 3, top - 2 * (top - floor) / 3]
    picked: list[Point] = []
    for target in targets:
        candidates = [p for p in between if not picked or p.value > picked[-1].value]
        if not candidates:
            return None
        picked.append(min(candidates, key=lambda p: (abs(p.success_rate - target), p.value)))
    return {"none": 0.0, "low": picked[0].value, "medium": picked[1].value, "high": high}


# --- the record and the page ------------------------------------------------------------------


def record_path(record_dir: Path, sweep_record: Sweep) -> Path:
    return record_dir / f"{sweep_record.axis}.{sweep_record.bank_id}.json"


def load_records(record_dir: Path) -> list[Sweep]:
    """Every sweep record in the directory, in axis order and then by bank id."""
    records = []
    for path in sorted(record_dir.glob("*.json")):
        try:
            records.append(Sweep.model_validate_json(path.read_text()))
        except (ValueError, json.JSONDecodeError) as error:
            raise CalibrationError(f"{path} is not a sweep record: {error}") from error
    order = {axis: index for index, axis in enumerate(AXES)}
    return sorted(records, key=lambda r: (order.get(r.axis, len(order)), r.bank_id))


def _fmt(value: float) -> str:
    return f"{value:g}"


def _level_line(axis: str, records: Sequence[Sweep]) -> str:
    """`LEVELS[axis]` with the rate each level measured, or a flag that it was never swept."""
    parts = []
    for name in LEVEL_NAMES:
        number = LEVELS[axis][name]
        rates = [
            f"{point.success_rate:.2f} on `{record.bank_id}`"
            for record in records
            for point in record.points
            if point.value == number
        ]
        measured = ", ".join(rates) if rates else "**not swept**"
        parts.append(f"`{name}` = {_fmt(number)} ({measured})")
    return "; ".join(parts)


def _table(record: Sweep) -> list[str]:
    # The nearest level is looked up at render time, against the table as it is now, so the
    # column says what a `run` at that raw value would record today rather than what it would
    # have recorded before the levels were baked from this very sweep.
    lines = [
        "| value | nearest level | success | rate | ego collisions | ended by | placed "
        "| steps (mean) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for point in record.points:
        hits = ", ".join(f"{name} {n}" for name, n in point.collisions.items() if n) or "none"
        ended = ", ".join(f"{name} {n}" for name, n in point.by_failure_reason.items()) or "arrived"
        if point.by_status.get("error"):
            ended = f"{ended}, error {point.by_status['error']}"
        placed = ", ".join(f"{name} {n}" for name, n in point.placed.items()) or "nothing"
        level = nearest_level(record.axis, point.value)
        lines.append(
            f"| {_fmt(point.value)} | `{level}` | {point.successes}/{point.n} | "
            f"{point.success_rate:.2f} | {hits} | {ended} | {placed} | {point.steps_mean:g} |"
        )
    return lines


def render(records: Sequence[Sweep]) -> str:
    """The page: one section per swept axis, each record's table, and what `options.py` says."""
    lines = [
        "# Level calibration",
        "",
        "Generated by `scenariobank calibrate`. Do not edit; re-measure.",
        "",
        "Each table is one axis swept over raw values with **every other axis at `none`**, the",
        "bundled expert (`scenariobank.policies:ExpertPolicy`, deterministic) driving every",
        "scenario of the bank once per value, through the same `run_bank` a `run` uses. The",
        "levels in `src/scenariobank/options.py` are picked from these tables: four values whose",
        "success rates sit visibly apart. `tests/unit/test_calibration.py` holds this page to the",
        "records under `docs/reference/calibration/` and holds `LEVELS` to the values swept.",
        "",
        "How to read a table:",
        "",
        f"- **traffic** below {TRAFFIC_FLOOR:g} is refused, not measured: `PGTrafficManager.reset` "
        "places nothing under it, so such a value would run as `none`.",
        "- **ego collisions** are the ego's own flags (`crash_vehicle`, `crash_object`, ...),",
        "  counted on the rising edge and summed over the rows. A traffic car hitting another car",
        "  is not in it; PG has no lights and no give-way rule, so at higher densities that",
        "  happens, and it shows under **ended by** as `max_step` -- the ego stuck behind it.",
        "- **placed** is what was on the road after the reset, by class, summed over the rows.",
        "  Cones and barriers land only on `Straight` and `Curve` blocks, so on an `X`, `T` or",
        "  `O` road the axis places nothing and the rate cannot move; the cones axis'",
        "  breakdown scene spawns a vehicle, which is a car on the road at `traffic=0`.",
        "- **steps (mean)** is over the rows that scored, whatever their outcome.",
        "",
    ]
    if not records:
        lines += ["No sweep has been recorded yet.", ""]
        return "\n".join(lines)
    by_axis: dict[str, list[Sweep]] = {}
    for record in records:
        by_axis.setdefault(record.axis, []).append(record)
    for axis in AXES:
        if axis not in by_axis:
            continue
        lines += [
            f"## {axis}",
            "",
            f"**Levels in `options.py`:** {_level_line(axis, by_axis[axis])}",
            "",
        ]
        for record in by_axis[axis]:
            rows = ", ".join(f"`{name}`" for name in record.categories)
            commit = (
                f", MetaDrive `{record.metadrive_commit[:8]}`" if record.metadrive_commit else ""
            )
            lines += [
                f"### {axis} on `{record.bank_id}`",
                "",
                f"{len(record.scenarios)} scenarios ({rows}), policy `{record.policy}`, "
                f"measured {record.measured_utc}{commit}. Held at `none`: "
                f"{', '.join(record.held)}.",
                "",
                *_table(record),
                "",
            ]
            picked = suggest(record.points)
            if picked is None:
                lines += [
                    "This sweep offers no spread of four: the rate does not fall, or fewer than "
                    "two values sit between 0 and the first value at the floor.",
                    "",
                ]
            else:
                shown = ", ".join(f"`{name}` {_fmt(value)}" for name, value in picked.items())
                lines += [f"Spread the rate suggests: {shown}.", ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def write(record_dir: Path, doc: Path, sweep_record: Sweep) -> tuple[Path, Path]:
    """Write the sweep's record and re-render the page from every record in the directory."""
    path = write_json(record_path(record_dir, sweep_record), sweep_record)
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(render(load_records(record_dir)))
    return path, doc


def levels_match(records: Sequence[Sweep]) -> dict[str, list[str]]:
    """Per swept axis, the level names whose number in `LEVELS` was never a swept value.

    Empty when every non-`none` level of every swept axis is a value some record measured --
    the Phase 4b gate: numbers in `options.py` that came from the tables and not from a guess.
    """
    gaps: dict[str, list[str]] = {}
    swept: dict[str, set[float]] = {}
    for record in records:
        swept.setdefault(record.axis, set()).update(point.value for point in record.points)
    for axis, values in swept.items():
        missing = [name for name in LEVEL_NAMES if float(LEVELS[axis][name]) not in values]
        if missing:
            gaps[axis] = missing
    return gaps


__all__: list[str] = [
    "CALIBRATION_DIR",
    "CALIBRATION_DOC",
    "EXPERT",
    "SWEEP_SCHEMA_VERSION",
    "CalibrationError",
    "Point",
    "Sweep",
    "check_values",
    "held_levels",
    "job_for",
    "levels_match",
    "load_records",
    "point_for",
    "record_path",
    "render",
    "suggest",
    "sweep",
    "write",
]
