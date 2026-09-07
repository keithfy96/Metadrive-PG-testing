"""What is actually in a bank, read off the manifest without building anything.

A bank of 55 rows is not automatically 55 scenarios, and until this module there was nothing that
said so. `banks/t-junction-left-intersection` holds five `intersection_left` scenarios and **two**
distinct ones: every seed resolves to the same destination, the same 111.7 m route and the same
+90.0 degree turn, and the only thing separating any of them is the spawn lane. That is a property
of the category rather than a defect -- MetaDrive's `X` junction does not vary with the seed
(`bank.py`, `spawn_lane_index`: *"for the `X` categories it is the only thing separating the five
seeds -- the road is identical at all five"*) -- so five seeds *cannot* draw five scenarios. A bank
that is 60% padding would otherwise reach the frontend team looking like 55.

**Nothing here builds an environment.** Every field the comparison needs is already in
`ScenarioRow`, because the manifest was written to explain itself, so a whole bank is reviewable
off disk in milliseconds and banks generated before this module was written review fine.

That is also why `variety.shape_gap` is *not* what this uses, despite being the similarity measure
this package already owns. `shape_gap` compares the **road** -- total lane length and bounding box
-- and for the `X` and `T` categories the road is identical across every seed by construction, so
it scores 0.00 for every pair and says nothing exactly where the trouble is. What a thumbnail shows
and what actually differs is the **route**.
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict

from scenariobank.bank import (
    BankError,
    CategoryEntry,
    Manifest,
    OptionLevels,
    RealWorldEntry,
    RealWorldRow,
    ScenarioRow,
)
from scenariobank.categories import step_budget
from scenariobank.options import AXES

#: Below this gap, two scenarios of one category are reported as near-duplicates rather than as two
#: scenarios. Measured, not chosen: `curve` seeds 0 and 4 sit at 0.071 and are visibly the same
#: corner, while the next-closest `curve` pair -- seeds 1 and 3, only 5% apart in route length --
#: scores 1.0 because it drives `LR` against `RL`.
#:
#: Deliberately **not** imported from `variety.NEAR_DUPLICATE`, which happens to hold the same
#: number. That one calibrates `shape_gap` over roads and this one calibrates `gap` over routes;
#: one constant serving two different measures is a coincidence the next change would break.
NEAR_DUPLICATE = 0.10

#: A full turn. Rotation is normalised against this rather than compared relatively, because a
#: relative difference on an angle breaks near zero: +1 against -1 degree would read as 200% apart.
FULL_TURN_DEG = 360.0

#: Every turn-pair string a two-curve sequence can draw. Coverage is reported against this, so a
#: `curve` bank missing `RR` is a stated blind spot rather than something you have to notice.
TURN_PAIRS = ("LL", "LR", "RL", "RR")

IDENTICAL = "identical"
SAME_DRIVE = "same-drive"
NEAR_DUPLICATE_VERDICT = "near-duplicate"
DISTINCT = "distinct"
#: Not a fifth degree of similarity -- the absence of one. Two scenarios of different categories
#: have no gap between them, because `gap` measures a drive and they differ by *declaration*: a
#: different road and a different exit rule. `compare` answers with this rather than refusing,
#: since clicking two cards is a fair thing to do and "there is no number here, and here is why"
#: is a better answer than an error.
INCOMPARABLE = "incomparable"


def gap(left: ScenarioRow, right: ScenarioRow) -> float:
    """How far apart two scenarios of one category drive, in [0, 1]. 0.0 is the same drive.

    Destination and turn pairs are **hard discriminators**, not terms in an average: a different
    exit is a different drive, and `LR` against `RL` is a mirror image rather than a near miss.
    Route length alone would miss both -- `curve` seeds 1 and 3 are 5% apart in length and drive
    opposite shapes -- which is why this is multi-field.

    The spawn lane is deliberately absent. It separates two scenarios (see `verdict`), but it is
    not a distance: lane 0 against lane 3 is no further apart than lane 0 against lane 1.
    """
    if left.destination != right.destination or left.turn_pairs != right.turn_pairs:
        return 1.0
    longer = max(left.route_length_m, right.route_length_m, 1e-9)
    return max(
        abs(left.route_length_m - right.route_length_m) / longer,
        abs(left.net_rotation_deg - right.net_rotation_deg) / FULL_TURN_DEG,
    )


def verdict(left: ScenarioRow, right: ScenarioRow) -> str:
    """Name the relationship between two scenarios of one category.

    Four outcomes rather than a threshold on one number, because the data shows they are genuinely
    different things and a person acts differently on each. `same-drive` is the one worth having:
    two rows that agree on every measurement and differ only in which lane the car starts in are
    not duplicates -- a policy meets them differently -- but they are the weakest difference a bank
    can be built on, and for the `X` categories they are the *only* difference available.
    """
    distance = gap(left, right)
    if distance > 0.0:
        return NEAR_DUPLICATE_VERDICT if distance < NEAR_DUPLICATE else DISTINCT
    if left.spawn_lane_index != right.spawn_lane_index:
        return SAME_DRIVE
    return IDENTICAL


def _route_key(row: ScenarioRow) -> tuple:
    """The drive itself: where it goes and what shape it is, ignoring which lane it starts in."""
    return (
        row.destination,
        round(row.route_length_m, 1),
        round(row.net_rotation_deg, 1),
        row.turn_pairs,
    )


def _drive_key(row: ScenarioRow) -> tuple:
    """Everything measured about a scenario. Two rows sharing this are indistinguishable."""
    return (*_route_key(row), row.spawn_lane_index)


class Pair(BaseModel):
    """Two scenarios of one category, and what separates them."""

    model_config = ConfigDict(extra="forbid")

    left: str
    right: str
    gap: float
    verdict: str


class Duplicates(BaseModel):
    """How much of a category is repetition."""

    model_config = ConfigDict(extra="forbid")

    total: int
    #: Rows sharing every measured field collapse to one. The headline: `2` against a total of `5`
    #: is a category that cannot be made more varied by asking for more seeds.
    distinct: int
    #: Each group is two or more ids that are the same drive, in manifest order.
    identical_groups: list[list[str]]
    #: Groups agreeing on every measurement but the spawn lane. Grouped rather than paired: five
    #: rows on one route are ten same-drive pairs and one useful sentence, and the pair count grows
    #: as the square of a category someone asked thirty seeds of.
    same_drive_groups: list[list[str]]
    #: Pairs closer than `NEAR_DUPLICATE` without being equal.
    near_duplicates: list[Pair]
    #: The least distinct pair in the category, whatever its verdict. `None` below two scenarios.
    closest: Pair | None


class Coverage(BaseModel):
    """What the category exercises, and what it leaves untested."""

    model_config = ConfigDict(extra="forbid")

    destinations: dict[str, int]
    spawn_lanes: dict[int, int]
    #: Present and missing are reported against `TURN_PAIRS`. Empty for a sequence with no curves,
    #: where the question does not arise.
    turn_pairs_present: list[str]
    turn_pairs_missing: list[str]
    #: Net rotation counted by sign. A bank of only left turns tests half the problem.
    turns_left: int
    turns_right: int
    turns_straight: int


class Budget(BaseModel):
    """Whether a policy has the steps to finish these routes.

    The only *correctness* statistic here. `replace_scenario` already warns when one swap earns
    more steps than its category allows; this is the same check across a whole bank, before a run
    rather than during an edit.
    """

    model_config = ConfigDict(extra="forbid")

    #: The category's cap: what applies to a scenario that does not set its own.
    max_steps: int
    #: The cap that actually applies to each id, which is the row's own where schema 1.1 gave it
    #: one. Carried rather than left to the reader to work out, for the same reason `earned` is:
    #: `CategoryEntry.budget_for` is the one place an override is resolved.
    caps: dict[str, int]
    #: What each route earns from `step_budget`, by id. Carried per scenario as well as summarised
    #: because the panel that reads one scenario back needs this number, and `step_budget` is the
    #: only thing entitled to compute it -- a page dividing metres by a constant would be a second
    #: rounding rule, and the two would part company the day this one changed.
    earned: dict[str, int]
    #: The largest budget any route in the category earns from `step_budget`.
    worst_earned: int
    worst_scenario: str | None
    #: Ids whose route earns more steps than the cap allows: a policy may run out before arriving.
    over_budget: list[str]


class Spread(BaseModel):
    """The range a category covers, which is what a single mean would hide."""

    model_config = ConfigDict(extra="forbid")

    route_length_min_m: float
    route_length_median_m: float
    route_length_max_m: float
    rotation_min_deg: float
    rotation_max_deg: float
    #: Routes turning through more than a half circle. A consequence of how `curve` pairs its two
    #: blocks rather than a fault, but worth stating: `curve_0000` sweeps +239.5 degrees.
    past_a_u_turn: list[str]


class CategoryReview(BaseModel):
    """One category of a bank, reviewed."""

    model_config = ConfigDict(extra="forbid")

    category: str
    block_seq: str
    exit_rule: str
    duplicates: Duplicates
    coverage: Coverage
    budget: Budget
    spread: Spread
    #: Plain sentences, worst first, ready to show. The page and the CLI print these rather than
    #: each deriving its own wording from the numbers above and drifting apart.
    warnings: list[str]


class Drive(BaseModel):
    """What the recorded drive did. The real-world sibling of `Spread` and `Coverage` at once.

    Every number here was measured off the recording by the converter, so this reports rather
    than computes -- except the medians and the waiting fraction, which are arithmetic over rows.
    The spread fields keep `Spread`'s names because they are the same measurement, and a reader
    that had to know which kind of bank it held to ask "how long is the route" would be the
    report failing to explain itself.
    """

    model_config = ConfigDict(extra="forbid")

    route_length_min_m: float
    route_length_median_m: float
    route_length_max_m: float
    #: A PG bank has no equivalent: nothing was driven, so nothing took any time.
    duration_min_s: float
    duration_median_s: float
    duration_max_s: float
    #: The fastest and the slowest the recordings go. A conversion averaging 8 kph is a traffic
    #: jam rather than a drive, and neither number alone says so.
    speed_kph: float | None
    slowest_kph: float | None
    #: How much of the recording is stationary, in seconds and as a fraction of its duration.
    #: This is the measure that says a scenario is mostly a red light.
    waiting_s: float
    waiting_fraction: float
    stop_count: int
    #: What the drive actually did -- the nearest thing a recording has to `turn_pairs`.
    lane_changes: int
    junction_movements: int
    #: Ids whose row carries no `route` block: nothing about the drive was measured for them.
    routeless: list[str]


class Actors(BaseModel):
    """Who else is in the recording. What `Coverage` is for a bank whose contents were chosen.

    Summed across the entry's rows, and the ego is one of the `VEHICLE`s -- the same convention
    `RealWorldRow.tracks` records and `workspace` prints, so the two never disagree by a count
    of one.
    """

    model_config = ConfigDict(extra="forbid")

    tracks: dict[str, int]
    busiest: str | None
    busiest_tracks: int
    #: Ids holding one `VEHICLE` and nothing else: no traffic to react to.
    ego_only: list[str]


class SignalCover(BaseModel):
    """The lights in the recording, and the plan they were invented from.

    `lights` is counted off the rows and everything below it is read off `entry.signals`, which
    is the converter's own record. The two are separate on purpose: `lane_model_signals` is what
    the reviewed lane model declared and `phase_groups` is what stage 6 built, and they can
    disagree -- `mosque` declares four and built none.
    """

    model_config = ConfigDict(extra="forbid")

    lights: dict[str, int]
    phase_groups: int | None
    signalled_lanes: int | None
    lane_model_signals: int | None
    cycle_seconds: float | None
    source: str | None
    #: The converter's caveat, carried verbatim. Surfaced in `warnings` too, because a result
    #: scored against these lights is scored against a plan nobody surveyed.
    note: str | None


class Replay(BaseModel):
    """What replaying this bank costs. The honest analogue of `Budget`.

    Not a budget: nothing here is a cap a policy could exceed. Replay advances one recorded frame
    per `env.step`, so the recording's own length *is* how long it runs, and the only question
    left is how much of it there is.
    """

    model_config = ConfigDict(extra="forbid")

    at_hz: float
    #: Frames across the entry, and the longest single recording in it.
    frames: int
    longest: int
    longest_scenario: str | None
    #: `frames / at_hz`: how many seconds of driving the entry holds.
    seconds: float


class MapSize(BaseModel):
    """How big the map is, and what kind of map it is. Schema 1.4.

    `features` is `None` on a bank imported before 1.4 -- the converter's own meaning for the
    field, "it did not say", rather than a map with nothing in it. A reader that drew a zero
    there would be inventing a finding out of a missing field.
    """

    model_config = ConfigDict(extra="forbid")

    features: int | None
    by_type: dict[str, int]


class RealWorldReview(BaseModel):
    """One imported conversion, reviewed. The sibling of `CategoryReview`.

    Two models rather than one with half its fields null, which is the rule `source` set when
    schema 1.3 split the manifest. Nothing here is a PG measure in disguise: there is no
    `duplicates` block, because `import` writes one workspace as one bank as one category and
    every conversion in the four workspaces holds exactly one scenario, so there is no second
    drive to be distinct from. Reporting "1 distinct of 1" would be the computation that never
    ran, which is what the refusal this replaces existed to prevent.
    """

    model_config = ConfigDict(extra="forbid")

    category: str
    #: The rate the tracks were sampled at, which is the rate replay runs at.
    step_hz: float
    total: int
    drive: Drive
    actors: Actors
    signals: SignalCover
    replay: Replay
    map_size: MapSize
    #: Plain sentences, worst first, ready to show -- same contract as `CategoryReview.warnings`.
    warnings: list[str]


class Report(BaseModel):
    """A whole bank, reviewed. Nothing here was simulated."""

    model_config = ConfigDict(extra="forbid")

    bank_id: str
    #: Which kind of bank this reviewed, straight off the manifest. The discriminator for
    #: `categories` below, so a reader picks a shape rather than guessing from which fields
    #: happen to be present.
    source: str = "pg"
    total: int
    #: `None` on a recording, and not zero and not the total: there is nothing in an imported
    #: bank to be distinct from, and a number equal to `total` would read as a computation that
    #: ran and found no repetition. Readers must test for `None` explicitly -- in JavaScript
    #: `null < 4` is `true`, so a `distinct < total` check draws the pill on every recording.
    distinct: int | None
    #: What runs of this bank default to, as a sentence. Written here rather than in the CLI and
    #: the page separately, for the same reason the warnings are: two renderers describing one
    #: manifest would be two places for the description to go wrong.
    options_line: str
    categories: list[CategoryReview | RealWorldReview]


class FieldPair(BaseModel):
    """One field of two scenarios, side by side and already formatted.

    Formatted here rather than in the caller because a comparison is a sentence about numbers, and
    two surfaces rounding "420.34 m" differently would be two different answers to one question.
    """

    model_config = ConfigDict(extra="forbid")

    field: str
    left: str
    right: str
    same: bool
    #: How far apart, when the difference is a quantity. `None` when the field is a fact that
    #: either matches or does not -- there is no "40% of a destination".
    apart: str | None


class Comparison(BaseModel):
    """Two scenarios of one bank, read against each other."""

    model_config = ConfigDict(extra="forbid")

    left: str
    right: str
    #: The category both sit in, or `None` when they are not the same one.
    category: str | None
    #: `None` when `verdict` is `INCOMPARABLE`: a gap is only defined inside a category.
    gap: float | None
    verdict: str
    #: One sentence, written here for the same reason `CategoryReview.warnings` are.
    summary: str
    fields: list[FieldPair]


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _duplicates(rows: Sequence[ScenarioRow]) -> Duplicates:
    groups: dict[tuple, list[str]] = {}
    routes: dict[tuple, list[str]] = {}
    for row in rows:
        groups.setdefault(_drive_key(row), []).append(row.scenario_id)
        routes.setdefault(_route_key(row), []).append(row.scenario_id)

    pairs = [
        Pair(
            left=left.scenario_id,
            right=right.scenario_id,
            gap=round(gap(left, right), 4),
            verdict=verdict(left, right),
        )
        for left, right in itertools.combinations(rows, 2)
    ]
    identical = [ids for ids in groups.values() if len(ids) > 1]
    return Duplicates(
        total=len(rows),
        distinct=len(groups),
        identical_groups=identical,
        # A route shared by rows that are not already all identical: the lane is what is left
        # separating them.
        same_drive_groups=[
            ids for ids in routes.values() if len(ids) > 1 and ids not in identical
        ],
        near_duplicates=[p for p in pairs if p.verdict == NEAR_DUPLICATE_VERDICT],
        # Ties broken by the pair that comes first in manifest order, so the report is stable
        # across runs rather than depending on how `min` happened to scan.
        closest=min(pairs, key=lambda p: p.gap) if pairs else None,
    )


def _coverage(rows: Sequence[ScenarioRow]) -> Coverage:
    present = sorted({row.turn_pairs for row in rows if row.turn_pairs})
    # A sequence with no curves has no turn pairs to cover, so nothing is "missing" -- reporting
    # all four as absent would be a warning about a question that does not apply.
    missing = [p for p in TURN_PAIRS if p not in present] if present else []
    return Coverage(
        destinations={
            node: sum(1 for row in rows if row.destination == node)
            for node in sorted({row.destination for row in rows})
        },
        spawn_lanes={
            lane: sum(1 for row in rows if row.spawn_lane_index == lane)
            for lane in sorted({row.spawn_lane_index for row in rows})
        },
        turn_pairs_present=present,
        turn_pairs_missing=missing,
        turns_left=sum(1 for row in rows if row.net_rotation_deg > 0),
        turns_right=sum(1 for row in rows if row.net_rotation_deg < 0),
        turns_straight=sum(1 for row in rows if row.net_rotation_deg == 0),
    )


def _budget(rows: Sequence[ScenarioRow], entry: CategoryEntry) -> Budget:
    earned = {row.scenario_id: step_budget(row.route_length_m) for row in rows}
    caps = {row.scenario_id: entry.budget_for(row) for row in rows}
    worst = max(earned, key=lambda name: earned[name]) if earned else None
    return Budget(
        max_steps=entry.max_steps,
        caps=caps,
        earned=earned,
        worst_earned=earned[worst] if worst else 0,
        worst_scenario=worst,
        over_budget=[name for name, steps in earned.items() if steps > caps[name]],
    )


def _spread(rows: Sequence[ScenarioRow]) -> Spread:
    lengths = [row.route_length_m for row in rows]
    rotations = [row.net_rotation_deg for row in rows]
    return Spread(
        route_length_min_m=min(lengths),
        route_length_median_m=round(_median(lengths), 2),
        route_length_max_m=max(lengths),
        rotation_min_deg=min(rotations),
        rotation_max_deg=max(rotations),
        past_a_u_turn=[row.scenario_id for row in rows if abs(row.net_rotation_deg) > 180],
    )


def _warnings(
    name: str, entry: CategoryEntry, dupes: Duplicates, cover: Coverage, budget: Budget
) -> list[str]:
    """Sentences, worst first. Written here so the page and the CLI say the same thing.

    Ordered by what would cost you most: a scenario that cannot finish, then repetition, then a
    gap in what the category exercises.
    """
    lines = []
    if budget.over_budget:
        # Each id with the cap that applies to *it*: since schema 1.1 a scenario may carry its
        # own, and a sentence naming only the category's would be wrong about those rows.
        over = ", ".join(
            f"{one} ({budget.earned[one]}/{budget.caps[one]})" for one in budget.over_budget
        )
        lines.append(
            f"{len(budget.over_budget)} scenario(s) earn more steps than the cap they run on: "
            f"{over}. {name}'s cap of {budget.max_steps} applies unless a scenario sets its own. "
            "A policy may run out of steps before reaching the destination."
        )
    if dupes.distinct < dupes.total:
        spare = dupes.total - dupes.distinct
        lines.append(
            f"{dupes.total} scenarios, {dupes.distinct} distinct. "
            f"{spare} of them {'repeats' if spare == 1 else 'repeat'} a drive already in the bank."
        )
        if entry.block_seq in {"X", "T"} and dupes.distinct <= len(cover.spawn_lanes):
            # Worth saying outright rather than leaving as a puzzle: the ceiling is the number of
            # lanes, so asking for more seeds cannot help and swapping seeds cannot either.
            lines.append(
                f"`{entry.block_seq}` junctions do not vary with the seed, so only the spawn lane "
                f"separates these. {len(cover.spawn_lanes)} lane(s) is the ceiling however many "
                "seeds you build."
            )
    for pair in dupes.near_duplicates:
        lines.append(
            f"{pair.left} and {pair.right} are {pair.gap * 100:.0f}% apart -- close enough that "
            "the two thumbnails are the same picture."
        )
    if cover.turn_pairs_missing:
        lines.append(
            f"no scenario drives {', '.join(cover.turn_pairs_missing)}: "
            f"{name} covers {', '.join(cover.turn_pairs_present)} only."
        )
    if len(cover.spawn_lanes) == 1 and dupes.total > 1:
        lines.append(
            f"every scenario spawns in lane {next(iter(cover.spawn_lanes))}."
        )
    return lines


def review_category(name: str, entry: CategoryEntry) -> CategoryReview:
    """Review one category. Raises nothing: an empty category is a report saying it is empty."""
    rows = entry.scenarios
    if not rows:
        empty = Duplicates(
            total=0, distinct=0, identical_groups=[], same_drive_groups=[],
            near_duplicates=[], closest=None,
        )
        return CategoryReview(
            category=name,
            block_seq=entry.block_seq,
            exit_rule=entry.exit_rule,
            duplicates=empty,
            coverage=Coverage(
                destinations={}, spawn_lanes={}, turn_pairs_present=[], turn_pairs_missing=[],
                turns_left=0, turns_right=0, turns_straight=0,
            ),
            budget=Budget(
                max_steps=entry.max_steps, caps={}, earned={}, worst_earned=0,
                worst_scenario=None, over_budget=[],
            ),
            spread=Spread(
                route_length_min_m=0.0, route_length_median_m=0.0, route_length_max_m=0.0,
                rotation_min_deg=0.0, rotation_max_deg=0.0, past_a_u_turn=[],
            ),
            warnings=[f"{name} holds no scenarios."],
        )

    dupes = _duplicates(rows)
    cover = _coverage(rows)
    budget = _budget(rows, entry)
    return CategoryReview(
        category=name,
        block_seq=entry.block_seq,
        exit_rule=entry.exit_rule,
        duplicates=dupes,
        coverage=cover,
        budget=budget,
        spread=_spread(rows),
        warnings=_warnings(name, entry, dupes, cover, budget),
    )


def _drive(rows: Sequence[RealWorldRow]) -> Drive:
    """The recorded drive, summarised. Reports what the converter measured; averages nothing away.

    Speed is the fastest any recording reaches and `slowest_kph` the slowest any of them crawls,
    rather than a mean of either: a bank whose fastest is 50 and whose slowest is 10 is one that
    contains a queue, and a mean of 30 would describe a drive that never happened.
    """
    lengths = [row.route_length_m for row in rows]
    durations = [row.duration_s for row in rows]
    routes = [row.route for row in rows if row.route]
    speeds = [route.speed_kph for route in routes if route.speed_kph is not None]
    slowest = [route.slowest_kph for route in routes if route.slowest_kph is not None]
    waiting = sum(route.waiting_s or 0.0 for route in routes)
    total_s = sum(durations)
    return Drive(
        route_length_min_m=round(min(lengths), 2),
        route_length_median_m=round(_median(lengths), 2),
        route_length_max_m=round(max(lengths), 2),
        duration_min_s=round(min(durations), 2),
        duration_median_s=round(_median(durations), 2),
        duration_max_s=round(max(durations), 2),
        speed_kph=max(speeds) if speeds else None,
        slowest_kph=min(slowest) if slowest else None,
        waiting_s=round(waiting, 2),
        # Against the recorded duration rather than `driving_duration_s`, which already has the
        # waiting taken out: dividing by it would compare the waiting to the not-waiting.
        waiting_fraction=round(waiting / total_s, 4) if total_s else 0.0,
        stop_count=sum(route.stop_count for route in routes),
        lane_changes=sum(route.lane_changes or 0 for route in routes),
        junction_movements=sum(route.junction_movements or 0 for route in routes),
        routeless=[row.scenario_id for row in rows if not row.route],
    )


def _actors(rows: Sequence[RealWorldRow]) -> Actors:
    """Everyone in the entry, by ScenarioNet type. The ego is one of the `VEHICLE`s."""
    tracks: dict[str, int] = {}
    for row in rows:
        for kind, count in row.tracks.items():
            tracks[kind] = tracks.get(kind, 0) + count
    # Sorted so the report reads the same on every run rather than in whichever order the first
    # row happened to list its types.
    tracks = dict(sorted(tracks.items()))
    busiest = max(rows, key=lambda row: sum(row.tracks.values()), default=None)
    return Actors(
        tracks=tracks,
        busiest=busiest.scenario_id if busiest else None,
        busiest_tracks=sum(busiest.tracks.values()) if busiest else 0,
        ego_only=[
            row.scenario_id
            for row in rows
            if not {kind: n for kind, n in row.tracks.items() if kind != "VEHICLE"}
            and not row.lights
        ],
    )


def _signals(rows: Sequence[RealWorldRow], entry: RealWorldEntry) -> SignalCover:
    lights: dict[str, int] = {}
    for row in rows:
        for kind, count in row.lights.items():
            lights[kind] = lights.get(kind, 0) + count
    plan = entry.signals
    return SignalCover(
        lights=dict(sorted(lights.items())),
        phase_groups=plan.phase_groups if plan else None,
        signalled_lanes=plan.signalled_lanes if plan else None,
        lane_model_signals=plan.lane_model_signals if plan else None,
        cycle_seconds=plan.cycle_seconds if plan else None,
        source=plan.source if plan else None,
        note=plan.note if plan else None,
    )


def _replay(rows: Sequence[RealWorldRow], entry: RealWorldEntry) -> Replay:
    frames = sum(row.max_steps for row in rows)
    longest = max(rows, key=lambda row: row.max_steps, default=None)
    return Replay(
        at_hz=entry.step_hz,
        frames=frames,
        longest=longest.max_steps if longest else 0,
        longest_scenario=longest.scenario_id if longest else None,
        seconds=round(frames / entry.step_hz, 2) if entry.step_hz else 0.0,
    )


def _map_size(rows: Sequence[RealWorldRow]) -> MapSize:
    """The map, as a feature count and a split by kind. `None` where the bank predates 1.4.

    A single row that does not record it makes the total `None` rather than an undercount: a
    number that silently omits one conversion's map is worse than saying nothing.
    """
    counted = [row.map_features for row in rows]
    by_type: dict[str, int] = {}
    for row in rows:
        for kind, count in row.map_feature_types.items():
            by_type[kind] = by_type.get(kind, 0) + count
    return MapSize(
        features=sum(counted) if counted and all(one is not None for one in counted) else None,
        by_type=dict(sorted(by_type.items())),
    )


def _real_world_warnings(
    name: str,
    entry: RealWorldEntry,
    drive: Drive,
    actors: Actors,
    signals: SignalCover,
) -> list[str]:
    """Sentences, worst first. Same contract as `_warnings`, different failures.

    Ordered by what would cost you most: a result scored against invented lights, then a junction
    whose lights were declared and never built, then a recording that is mostly a red light, then
    one with nothing in it to react to, then a licence obligation that would not survive, then a
    row nothing was measured for.
    """
    lines = []
    if signals.lights and signals.note:
        # Verbatim. It is a caveat about the data rather than about us, and paraphrasing it would
        # make it ours.
        lines.append(signals.note)
    if (signals.lane_model_signals or 0) > 0 and not signals.phase_groups:
        lines.append(
            f"the lane model declares {signals.lane_model_signals} signal(s) and stage 6 built "
            "no phase groups from them, so nothing in this recording changes state. A junction "
            "whose lights were never built reads exactly like a junction with no lights."
        )
    if drive.waiting_fraction > 0.5:
        lines.append(
            f"{drive.waiting_fraction * 100:.0f}% of this recording is spent stopped "
            f"({drive.waiting_s:g} s of {drive.duration_max_s:g} s, {drive.stop_count} stop(s)). "
            "Most of what a policy replays here is a queue rather than a drive."
        )
    if actors.ego_only:
        lines.append(
            f"{', '.join(actors.ego_only)}: the ego and nothing else -- no pedestrians, no "
            "cyclists, no barriers, no lights were baked into this conversion"
        )
    if not entry.attribution:
        lines.append(
            f"{name} carries no attribution. It is a licence obligation and it has to survive "
            "into a result, so a bank without one cannot be published from."
        )
    if drive.routeless:
        lines.append(
            f"{', '.join(drive.routeless)}: no route was recorded, so nothing about the drive "
            "was measured -- no distance, no duration, no speeds."
        )
    return lines


def review_real_world(name: str, entry: RealWorldEntry) -> RealWorldReview:
    """Review one imported conversion. Raises nothing, the way `review_category` does not."""
    rows = entry.scenarios
    if not rows:
        return RealWorldReview(
            category=name,
            step_hz=entry.step_hz,
            total=0,
            drive=Drive(
                route_length_min_m=0.0, route_length_median_m=0.0, route_length_max_m=0.0,
                duration_min_s=0.0, duration_median_s=0.0, duration_max_s=0.0,
                speed_kph=None, slowest_kph=None, waiting_s=0.0, waiting_fraction=0.0,
                stop_count=0, lane_changes=0, junction_movements=0, routeless=[],
            ),
            actors=Actors(tracks={}, busiest=None, busiest_tracks=0, ego_only=[]),
            signals=_signals(rows, entry),
            replay=_replay(rows, entry),
            map_size=MapSize(features=None, by_type={}),
            warnings=[f"{name} holds no recordings."],
        )

    drive = _drive(rows)
    actors = _actors(rows)
    signals = _signals(rows, entry)
    return RealWorldReview(
        category=name,
        step_hz=entry.step_hz,
        total=len(rows),
        drive=drive,
        actors=actors,
        signals=signals,
        replay=_replay(rows, entry),
        map_size=_map_size(rows),
        warnings=_real_world_warnings(name, entry, drive, actors, signals),
    )


def describe_options(options: OptionLevels) -> str:
    """The bank's pinned option levels, as one line.

    Names only what is set. A bank with five axes at `none` and one at `medium` is a bank with one
    interesting fact about it, and listing the five zeroes would bury it.
    """
    levels = options.model_dump()
    pinned = [(axis, levels[axis]) for axis in AXES if levels[axis] != "none"]
    if not pinned:
        return "pins no option levels: every axis is none"
    named = [f"{axis}={level}" for axis, level in pinned]
    if len(pinned) < len(AXES):
        named.append("everything else none")
    return f"runs at {', '.join(named)}"


#: What `describe_options` would say about an imported bank, and why saying it would mislead. The
#: wording is `Manifest._one_kind_of_bank`'s, because it is the same fact: a recording's traffic is
#: not a level that was left at `none`, it is not a level at all. `describe_options` is not given a
#: branch of its own -- `cli.py` calls it with an `OptionLevels` read straight off a manifest and
#: has no manifest in hand to branch on.
NO_OPTIONS = (
    "pins no option levels, and cannot: traffic, pedestrians and the rest are contents of a "
    "recording here rather than knobs a run sets"
)


def _procedural(manifest: Manifest) -> None:
    """Refuse something this comparison cannot describe.

    `gap` is built on a seed, a block sequence and a resolved exit: it is a distance between two
    drives that were *chosen*, and a recording chose nothing. `review` no longer calls this --
    Phase 3 Step 5 gave a recording a review of its own -- but `compare` still does, because
    every conversion in every workspace here holds exactly one scenario and there is no second
    recording in a bank to compare the first against. Building it now would be code with nothing
    to act on.
    """
    if manifest.source != "pg":
        raise BankError(
            f"{manifest.bank_id} is an imported ({manifest.source}) bank, and this comparison is "
            "built on seeds, block sequences and resolved exits. A recording has none of them. "
            "An imported bank holds one recording, so there is no second drive here to compare "
            "it against; `scenariobank review` describes the one it has."
        )


def review(manifest: Manifest) -> Report:
    """Review a whole bank, of either kind. Pure: no filesystem, no environment, no simulator.

    Which half runs is decided by `manifest.source`, the discriminator schema 1.3 added, and the
    two never mix: a procedural report carries `CategoryReview`s and a recording's carries
    `RealWorldReview`s. `Report.source` says which, so a reader picks a shape rather than
    inferring it from whichever fields happen to be present.
    """
    if manifest.source != "pg":
        recordings = [
            review_real_world(name, entry) for name, entry in manifest.categories.items()
        ]
        return Report(
            bank_id=manifest.bank_id,
            source=manifest.source,
            total=sum(one.total for one in recordings),
            # Not zero and not the total. There is no second drive in an imported bank to be
            # distinct from, so the count is absent rather than a number that would read as a
            # computation that ran.
            distinct=None,
            options_line=NO_OPTIONS,
            categories=recordings,
        )

    categories = [review_category(name, entry) for name, entry in manifest.categories.items()]
    return Report(
        bank_id=manifest.bank_id,
        source=manifest.source,
        total=sum(one.duplicates.total for one in categories),
        # Summed per category rather than compared across them: two categories are different by
        # declaration -- different road, different exit rule -- so a cross-category gap would be a
        # number with no meaning behind it.
        distinct=sum(one.duplicates.distinct for one in categories),
        options_line=describe_options(manifest.options),
        categories=categories,
    )


def find(manifest: Manifest, scenario_id: str) -> tuple[str, CategoryEntry, ScenarioRow] | None:
    """The category name, its entry and the row for one `scenario_id`, or `None`.

    Searched rather than parsed out of the id. An id happens to start with its category name, but
    that is a naming convention and a convention is not a lookup: `intersection_left_0000` would
    still be found if the categories were ever renamed underneath it.
    """
    for name, entry in manifest.categories.items():
        for row in entry.scenarios:
            if row.scenario_id == scenario_id:
                return name, entry, row
    return None


def _fields(
    left_cat: str,
    left_entry: CategoryEntry,
    left: ScenarioRow,
    right_cat: str,
    right_entry: CategoryEntry,
    right: ScenarioRow,
) -> list[FieldPair]:
    """Every measured field of two scenarios, in the order a person reads them.

    Seed first, because it is the handle you would change; the route in the middle, because that is
    what the picture shows; the step budget last, because it is a consequence of the route rather
    than a property of the scenario.
    """

    def pair(field: str, left_value: object, right_value: object, apart: str | None = None):
        one, two = str(left_value), str(right_value)
        return FieldPair(field=field, left=one, right=two, same=one == two, apart=apart)

    longer = max(left.route_length_m, right.route_length_m, 1e-9)
    length_apart = abs(left.route_length_m - right.route_length_m)
    turn_apart = abs(left.net_rotation_deg - right.net_rotation_deg)
    steps_apart = abs(step_budget(left.route_length_m) - step_budget(right.route_length_m))

    rows = []
    if left_cat != right_cat:
        # Only shown when they differ. Within one category these three are the category, and a row
        # that always reads the same on both sides is noise in a table about differences.
        rows += [
            pair("scenario type", left_cat, right_cat),
            pair("road", left_entry.block_seq, right_entry.block_seq),
            pair("exit rule", left_entry.exit_rule, right_entry.exit_rule),
        ]
    rows += [
        pair("seed", left.seed, right.seed),
        pair("destination", left.destination, right.destination),
        pair("spawn lane", left.spawn_lane_index, right.spawn_lane_index),
        pair(
            "route length",
            f"{left.route_length_m:.1f} m",
            f"{right.route_length_m:.1f} m",
            None if length_apart == 0 else f"{length_apart:.1f} m, {length_apart / longer:.0%}",
        ),
        pair(
            "net rotation",
            f"{left.net_rotation_deg:+.1f}\N{DEGREE SIGN}",
            f"{right.net_rotation_deg:+.1f}\N{DEGREE SIGN}",
            None if turn_apart == 0 else f"{turn_apart:.1f}\N{DEGREE SIGN}",
        ),
        # An empty string is a real value here -- a sequence with no curves draws no turn pairs --
        # so it is spelled rather than left blank, which would read as a missing field.
        pair("turn pairs", left.turn_pairs or "none", right.turn_pairs or "none"),
        pair(
            "step budget",
            f"{step_budget(left.route_length_m)} / {left_entry.budget_for(left)}",
            f"{step_budget(right.route_length_m)} / {right_entry.budget_for(right)}",
            None if steps_apart == 0 else f"{steps_apart} steps",
        ),
    ]
    return rows


def _summary(
    call: str, distance: float | None, left: ScenarioRow, right: ScenarioRow,
    left_cat: str, right_cat: str,
) -> str:
    """The one sentence that says what the verdict means for these two in particular."""
    if call == INCOMPARABLE:
        return (
            f"{left.scenario_id} is {left_cat} and {right.scenario_id} is {right_cat}. "
            "Two categories differ by declaration -- a different road and a "
            "different exit rule -- so there is no gap to measure between them. The fields below "
            "are side by side, not scored."
        )
    if call == IDENTICAL:
        return (
            "Every measured field matches, the spawn lane included: this is one drive stored "
            "twice, and no seed change to either will make the two pictures differ."
        )
    if call == SAME_DRIVE:
        return (
            f"The same drive from a different lane -- lane {left.spawn_lane_index} against lane "
            f"{right.spawn_lane_index}. A policy meets these differently, so they are not "
            "duplicates, but the spawn lane is the weakest difference a bank can be built on."
        )
    percent = f"{distance:.0%}" if distance is not None else "?"
    if call == NEAR_DUPLICATE_VERDICT:
        return (
            f"{percent} apart -- close enough that the two thumbnails are the same picture, and "
            "close enough that a policy learning one has largely learnt the other."
        )
    if left.destination != right.destination:
        return (
            f"Different drives: this route ends at {left.destination} and that one at "
            f"{right.destination}. A different exit is a different scenario rather than a near "
            "miss, which is why the gap is 100% and not the difference in their lengths."
        )
    if left.turn_pairs != right.turn_pairs:
        return (
            f"Different drives: {left.turn_pairs or 'no curves'} against "
            f"{right.turn_pairs or 'no curves'}. Mirror images are as far apart as this measure "
            "goes, however close the two route lengths happen to be."
        )
    return (
        f"{percent} apart, past the {NEAR_DUPLICATE:.0%} under which two scenarios of one "
        "category are called near-duplicates. Two scenarios, not one drawn twice."
    )


def compare(manifest: Manifest, left_id: str, right_id: str) -> Comparison:
    """Two scenarios of one bank, read against each other with the review's own measure.

    Lives here rather than in the page for the reason the warning sentences do: `gap` and
    `verdict` are one measure, and a copy of them in JavaScript would be a second one that drifts
    the first time either changes. The page holds every field already; what it must not invent is
    what the fields mean together.

    Raises `LookupError` for an id this bank does not hold and `ValueError` for a scenario
    compared with itself, which the studio turns into a 404 and a 400.
    """
    _procedural(manifest)
    if left_id == right_id:
        raise ValueError(f"{left_id!r} compared with itself is not a comparison")
    both = []
    for scenario_id in (left_id, right_id):
        hit = find(manifest, scenario_id)
        if hit is None:
            raise LookupError(f"no scenario named {scenario_id!r} in this bank")
        both.append(hit)
    (left_cat, left_entry, left), (right_cat, right_entry, right) = both

    together = left_cat == right_cat
    distance = round(gap(left, right), 4) if together else None
    call = verdict(left, right) if together else INCOMPARABLE
    return Comparison(
        left=left_id,
        right=right_id,
        category=left_cat if together else None,
        gap=distance,
        verdict=call,
        summary=_summary(call, distance, left, right, left_cat, right_cat),
        fields=_fields(left_cat, left_entry, left, right_cat, right_entry, right),
    )


__all__ = [
    "DISTINCT",
    "FULL_TURN_DEG",
    "IDENTICAL",
    "INCOMPARABLE",
    "NEAR_DUPLICATE",
    "NEAR_DUPLICATE_VERDICT",
    "NO_OPTIONS",
    "SAME_DRIVE",
    "TURN_PAIRS",
    "Actors",
    "Budget",
    "CategoryReview",
    "Comparison",
    "Coverage",
    "Drive",
    "Duplicates",
    "FieldPair",
    "MapSize",
    "Pair",
    "RealWorldReview",
    "Replay",
    "Report",
    "SignalCover",
    "Spread",
    "compare",
    "find",
    "gap",
    "review",
    "review_category",
    "review_real_world",
    "verdict",
]
