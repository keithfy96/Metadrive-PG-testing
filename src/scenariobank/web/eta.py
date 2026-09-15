"""How long a run will take, from what the rigs have already delivered.

The number a submit screen shows beside the button, and the one a rig page turns into "about
forty minutes left". It is derived from the results index and nothing else is stored for it:
every scored row carries its wall time, the job it came from carries the policy, the host and
the option levels, and an estimate is a **median** of the newest such rows -- median, not mean,
because one degraded run is a 5x outlier that would poison a mean for weeks.

**Narrowing, then widening.** The wall time of one scenario depends first on the policy (a
camera model through the bridge is two orders slower than the bundled expert), then on the rig
(two rigs, two speeds -- an estimate that is not per rig is wrong on the slower one), then on
the difficulty (traffic density moves the expert's time several-fold), and last on the road.
So the samples for one `(policy, category)` are narrowed to the requested host and levels when
enough of them exist, and widened one step at a time when they do not, and the answer says
which subset it came from. A number from the other rig at another difficulty is still better
than none, provided it says so.

**The bootstraps.** Before any real run has been delivered there are two measured sources.
The calibration sweeps under `docs/reference/calibration/` are the expert driving every
scenario of a bank once per value of one axis, with the others held at `none`; their wall
times bootstrap the estimate **for the expert only**, because they say nothing about a camera
model's speed, and a bootstrap fifty times too small is worse than none. Each swept axis was
measured alone, so for a difficulty that sets several the slowest axis is taken, not the sum
-- the base cost of the drive is in every sweep once. For the camera model there is
`docs/reference/wall-times.json`: the per-scenario times Phase 4 Step 8 measured on the rig
and on the laptop, copied there with their provenance rather than re-measured (the plan's own
exception to the rule, because a bank of that model is an hour and an absent estimate is a
visible gap). They are one road's figure, `t_junction_0000`, and say so; the first delivered
run of that model on each road replaces them.

Nothing here imports FastAPI, MetaDrive or the agent: the store is a file on local disk and
the records are JSON, so the same estimate is available to a terminal.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from scenariobank.calibration import EXPERT, CalibrationError, Sweep, load_records
from scenariobank.options import LEVELS, NUMERIC_AXES
from scenariobank.web.results import ResultsStore, Sample

#: The measured wall times, relative to the directory the studio runs in, like the sweeps.
WALL_TIMES = Path("docs/reference/wall-times.json")

#: How many of the newest samples a median is over. Enough to ride out one odd run, few enough
#: that a rig that was upgraded last week is what the estimate describes.
LAST = 20

#: Fewer samples than this in a narrowed subset and the estimate widens instead: a median of
#: one is that one run.
ENOUGH = 3


class Measurement(BaseModel):
    """One measured per-scenario wall time: a policy on a host, with its provenance."""

    model_config = ConfigDict(extra="forbid")

    policy: str
    host: str
    #: What a submit screen assumes when the rig is not yet known. The rigs' entries, never a
    #: laptop's: an hour-long bank priced off a laptop would be nine hours.
    default: bool = False
    wall_time_s: list[float] = Field(min_length=1)
    scenario: str
    simulated_s: float | None = None
    measured_utc: str
    source: str
    note: str | None = None


class WallTimes(BaseModel):
    """`docs/reference/wall-times.json`."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    about: str | None = None
    measurements: list[Measurement]


@dataclass(frozen=True)
class Guess:
    """One category's per-scenario time, and where it came from."""

    #: Seconds per scenario, or `None` when nothing measured applies.
    seconds_each: float | None
    #: `rows` (delivered results), `calibration` (the sweeps), or `None`.
    source: str | None
    #: How many samples the number is over.
    n: int
    #: The host the samples came from when they were narrowed to one; `None` when they were
    #: not, which is what a caller reads as "not this rig's own number".
    host: str | None
    #: Whether the samples ran at the requested option levels.
    levels_matched: bool
    #: Why there is no number, or what the number is standing in for.
    note: str | None = None


@dataclass(frozen=True)
class Estimate:
    """A whole run: the sum over its categories, and each category's own answer."""

    #: Seconds for the run, summed over the categories that have a number. `None` when none has.
    seconds: float | None
    #: Whether every category contributed. When false, `seconds` is a floor and `missing`
    #: says which categories it leaves out.
    complete: bool
    scenarios: int
    policy: str
    host: str | None
    per_category: dict[str, dict[str, Any]]
    missing: list[str]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_bootstrap(directory: Path) -> list[Sweep]:
    """The calibration records, or none: a missing or unreadable directory is no bootstrap,
    not an error, because an estimate is never the reason a page fails to load."""
    try:
        return load_records(Path(directory)) if Path(directory).is_dir() else []
    except CalibrationError:
        return []


def load_measurements(path: Path) -> list[Measurement]:
    """The measured wall times, or none: a missing or unreadable file is no bootstrap."""
    try:
        return WallTimes.model_validate_json(Path(path).read_bytes()).measurements
    except (OSError, ValueError, ValidationError):
        return []


def estimate(
    store: ResultsStore,
    *,
    policy: str,
    categories: Mapping[str, int],
    levels: Mapping[str, str] | None = None,
    host: str | None = None,
    records: Sequence[Sweep] = (),
    measurements: Sequence[Measurement] = (),
    last: int = LAST,
) -> Estimate:
    """The estimate for `categories` (category -> how many scenarios) under `policy`.

    `levels` are the six option levels the run will use, or `None` to match any; `host` is
    the rig the run will land on, or `None` when that is not yet known -- which at submit time
    it never is, so the submit screen's number is over both rigs and the rig page's is one's.
    Sources in order: the delivered rows, the sweeps (`records`, the expert only), the
    measured wall times (`measurements`), and then an honest none.
    """
    wanted = {axis: level for axis, level in (levels or {}).items()}
    per_category: dict[str, dict[str, Any]] = {}
    total = 0.0
    missing: list[str] = []
    for category, count in categories.items():
        guess = _from_rows(store.samples(policy, category), host=host, levels=wanted, last=last)
        if guess is None and policy == EXPERT:
            guess = _from_calibration(records, category, wanted)
        if guess is None:
            guess = _from_measurements(measurements, policy, host)
        if guess is None:
            guess = Guess(
                seconds_each=None, source=None, n=0, host=None, levels_matched=False,
                note=f"no scored run of {policy} on {category} has been delivered yet, and "
                "nothing measured stands in for one",
            )
        per_category[category] = {"count": count, **asdict(guess)}
        if guess.seconds_each is None:
            missing.append(category)
        else:
            total += guess.seconds_each * count
    known = len(missing) < len(categories)
    return Estimate(
        seconds=round(total, 1) if known else None,
        complete=known and not missing,
        scenarios=sum(categories.values()),
        policy=policy,
        host=host,
        per_category=per_category,
        missing=missing,
    )


def _from_rows(
    samples: Sequence[Sample], *, host: str | None, levels: Mapping[str, str], last: int
) -> Guess | None:
    """The median of the newest `last` samples in the narrowest subset with enough of them.

    Narrowest first: this host at these levels, this host, these levels, everything. A subset
    with fewer than `ENOUGH` samples is passed over unless it is the last one, because a median
    of one run is that run, and the wider subset's median is the better guess until then.
    """
    if not samples:
        return None
    steps: list[tuple[str | None, bool]] = []
    if host is not None and levels:
        steps.append((host, True))
    if host is not None:
        steps.append((host, False))
    if levels:
        steps.append((None, True))
    steps.append((None, False))
    for index, (narrow_host, narrow_levels) in enumerate(steps):
        chosen = [
            sample
            for sample in samples
            if (narrow_host is None or sample.host == narrow_host)
            and (not narrow_levels or sample.levels == dict(levels))
        ]
        if not chosen or (len(chosen) < ENOUGH and index < len(steps) - 1):
            continue
        newest = chosen[:last]
        return Guess(
            seconds_each=round(statistics.median(s.wall_time_s for s in newest), 2),
            source="rows",
            n=len(newest),
            host=narrow_host,
            levels_matched=narrow_levels,
            note=None if narrow_levels or not levels else "at other option levels",
        )
    return None


def _from_calibration(
    records: Sequence[Sweep], category: str, levels: Mapping[str, str]
) -> Guess | None:
    """The expert's per-scenario time on `category` from the sweeps, at `levels`.

    Each record swept one axis with the rest at `none`, so the point taken from it is the one
    at the number behind the requested level of that axis, and the answer is the slowest axis's
    time per scenario rather than a sum of them. A level no sweep ran on this category leaves
    that axis out and says so.
    """
    per_axis: list[float] = []
    unmatched: list[str] = []
    for record in records:
        if category not in record.categories or record.axis not in NUMERIC_AXES:
            continue
        level = levels.get(record.axis, "none")
        value = LEVELS[record.axis].get(level)
        point = next((p for p in record.points if p.value == value and p.n), None)
        if point is None:
            unmatched.append(f"{record.axis}={level}")
            continue
        per_axis.append(point.wall_time_s / point.n)
    if not per_axis:
        return None
    return Guess(
        seconds_each=round(max(per_axis), 2),
        source="calibration",
        n=len(per_axis),
        host=None,
        levels_matched=not unmatched,
        note=(
            "the expert on the calibration machine, one axis at a time"
            + (f"; no sweep ran {', '.join(unmatched)} on {category}" if unmatched else "")
        ),
    )


def _from_measurements(
    measurements: Sequence[Measurement], policy: str, host: str | None
) -> Guess | None:
    """The measured figure for `policy`: this host's own when there is one, else the default
    entries' (the rigs'). One road's number, and the note says which road."""
    mine = [m for m in measurements if m.policy == policy]
    if not mine:
        return None
    own = [m for m in mine if host is not None and m.host == host]
    chosen = own or [m for m in mine if m.default]
    if not chosen:
        return None
    times = [t for m in chosen for t in m.wall_time_s]
    hosts = sorted({m.host for m in chosen})
    return Guess(
        seconds_each=round(statistics.median(times), 2),
        source="measured",
        n=len(times),
        host=host if own else None,
        levels_matched=False,
        note=(
            f"measured on {', '.join(sorted({m.scenario for m in chosen}))} on "
            f"{', '.join(hosts)} ({', '.join(sorted({m.measured_utc for m in chosen}))}); "
            "other roads scale with their length"
        ),
    )


__all__ = [
    "ENOUGH",
    "LAST",
    "WALL_TIMES",
    "Estimate",
    "Guess",
    "Measurement",
    "WallTimes",
    "estimate",
    "load_bootstrap",
    "load_measurements",
]
