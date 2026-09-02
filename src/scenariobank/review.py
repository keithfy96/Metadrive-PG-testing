"""What is actually in a bank, read off the manifest without building anything.

A bank of 35 rows is not automatically 35 scenarios, and until this module there was nothing that
said so. `banks/t-junction-left-intersection` holds five `intersection_left` scenarios and **two**
distinct ones: every seed resolves to the same destination, the same 111.7 m route and the same
+90.0 degree turn, and the only thing separating any of them is the spawn lane. That is a property
of the category rather than a defect -- MetaDrive's `X` junction does not vary with the seed
(`bank.py`, `spawn_lane_index`: *"for the `X` categories it is the only thing separating the five
seeds -- the road is identical at all five"*) -- so five seeds *cannot* draw five scenarios. A bank
that is 60% padding would otherwise reach the frontend team looking like 35.

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

from scenariobank.bank import CategoryEntry, Manifest, ScenarioRow
from scenariobank.categories import step_budget

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

    max_steps: int
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


class Report(BaseModel):
    """A whole bank, reviewed. Nothing here was simulated."""

    model_config = ConfigDict(extra="forbid")

    bank_id: str
    total: int
    distinct: int
    categories: list[CategoryReview]


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


def _budget(rows: Sequence[ScenarioRow], max_steps: int) -> Budget:
    earned = {row.scenario_id: step_budget(row.route_length_m) for row in rows}
    worst = max(earned, key=lambda name: earned[name]) if earned else None
    return Budget(
        max_steps=max_steps,
        worst_earned=earned[worst] if worst else 0,
        worst_scenario=worst,
        over_budget=[name for name, steps in earned.items() if steps > max_steps],
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
        lines.append(
            f"{len(budget.over_budget)} scenario(s) earn more steps than {name}'s cap of "
            f"{budget.max_steps}: {', '.join(budget.over_budget)}. A policy may run out of steps "
            "before reaching the destination."
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
                max_steps=entry.max_steps, worst_earned=0, worst_scenario=None, over_budget=[],
            ),
            spread=Spread(
                route_length_min_m=0.0, route_length_median_m=0.0, route_length_max_m=0.0,
                rotation_min_deg=0.0, rotation_max_deg=0.0, past_a_u_turn=[],
            ),
            warnings=[f"{name} holds no scenarios."],
        )

    dupes = _duplicates(rows)
    cover = _coverage(rows)
    budget = _budget(rows, entry.max_steps)
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


def review(manifest: Manifest) -> Report:
    """Review a whole bank. Pure: no filesystem, no environment, no simulator."""
    categories = [review_category(name, entry) for name, entry in manifest.categories.items()]
    return Report(
        bank_id=manifest.bank_id,
        total=sum(one.duplicates.total for one in categories),
        # Summed per category rather than compared across them: two categories are different by
        # declaration -- different road, different exit rule -- so a cross-category gap would be a
        # number with no meaning behind it.
        distinct=sum(one.duplicates.distinct for one in categories),
        categories=categories,
    )


__all__ = [
    "DISTINCT",
    "FULL_TURN_DEG",
    "IDENTICAL",
    "NEAR_DUPLICATE",
    "NEAR_DUPLICATE_VERDICT",
    "SAME_DRIVE",
    "TURN_PAIRS",
    "Budget",
    "CategoryReview",
    "Coverage",
    "Duplicates",
    "Pair",
    "Report",
    "Spread",
    "gap",
    "review",
    "review_category",
    "verdict",
]
