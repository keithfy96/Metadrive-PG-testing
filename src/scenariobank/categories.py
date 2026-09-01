"""The seven categories, and the rule that fixes each one's exit.

The point of this module is that MetaDrive never chooses the route. `auto_assign_task`
(`node_network_navigation.py:72-91`) draws a destination socket at random when
`vehicle_config["destination"]` is unset, and falls back to `map.blocks[0]` entirely when the
ego spawns on a negative road. Both are avoided by naming the destination, so a category is a
*specified* turn rather than a measured one.

What a category stores is the **rule**, not the node name. `StdTInterSection` does not expose the
same two arms on every seed -- seeds 0, 1 and 4 offer right-and-straight, seeds 2 and 3 offer
left-and-straight -- so a hardcoded node would be unresolvable on 2 of the 5 seeds. The rule
resolves to a node at generation time and the resolved node is what gets recorded, which is the
same discipline the manifest follows throughout: declare the intent, store the fact.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

#: The **default** seeds every category is built at. The same five for all seven, so the three
#: intersection categories are the same junction driven three ways.
#:
#: A default, not a constant: `scenariobank generate --seeds` overrides it, per category if
#: wanted, and `scenariobank replace` swaps one scenario's seed in an existing bank. That
#: matters because **seed 4 is a poor draw for two categories** -- it builds a `curve` road 7%
#: from seed 0's and a `roundabout` indistinguishable from seed 0's. Kept as the default
#: anyway: 0-4 is the set every measurement in this repo was taken at, and which seeds a bank
#: uses is a decision for whoever builds it. `scenariobank seeds` ranks the alternatives.
SEEDS: tuple[int, ...] = (0, 1, 2, 3, 4)

#: Every block ID `BLOCK_TYPE_DISTRIBUTION_V2` can produce (`blocks_prob_dist.py:22-41`), by the
#: `ID` attribute of each registered class rather than by the class name the dict is keyed on.
#:
#: `I` (`FirstPGBlock`) is deliberately absent: it is prepended to every map automatically and is
#: never written into a sequence. Held as a literal so this module stays importable without the
#: `sim` group; `tests/unit/test_categories.py` asserts it against MetaDrive so it cannot drift.
VALID_BLOCK_IDS: frozenset[str] = frozenset("CSrRXTOfFyYP$BU")


class CategoryError(RuntimeError):
    """Raised when a category is malformed -- typically a typo'd block sequence."""


class ExitRule(str, Enum):
    """How a category picks its destination socket out of the ones the block offers."""

    #: The block has exactly one exit. Anything else is a bug in the sequence.
    ONLY = "only"
    #: Nearest +90 degrees. Heading is counter-clockwise-positive (`straight_lane.py:56`).
    LEFT = "left"
    #: Nearest -90 degrees.
    RIGHT = "right"
    #: Nearest 0 degrees.
    STRAIGHT = "straight"
    #: Whichever exit turns hardest, in either direction. For a T junction, where the arm on
    #: offer changes with the seed, this is the only rule that always means "turn".
    SHARPEST = "sharpest"


#: The heading each angle-seeking rule aims at, in degrees from the spawn heading.
TARGET_ANGLE: dict[ExitRule, float] = {
    ExitRule.LEFT: 90.0,
    ExitRule.RIGHT: -90.0,
    ExitRule.STRAIGHT: 0.0,
}

#: Assumed mean speed when converting a route length into a step budget, in m/s. Deliberately
#: pessimistic: it is a junction-driving average with slowdowns, not a cruise speed.
REFERENCE_SPEED_MPS = 6.0

#: Seconds of simulated time per `env.step()`: `physics_world_step_size` 0.02 multiplied by
#: `decision_repeat` 5 (`base_env.py:189-191`).
SECONDS_PER_STEP = 0.1

#: How much slack the step budget gets over the reference-speed estimate. A cap, not a target --
#: it exists to bound a stuck run, so being generous costs only wall clock on a failure.
STEP_BUDGET_MARGIN = 1.5


def step_budget(route_length_m: float) -> int:
    """Return the `max_steps` a route of this length earns, rounded up to the next 20."""
    steps = route_length_m / (REFERENCE_SPEED_MPS * SECONDS_PER_STEP) * STEP_BUDGET_MARGIN
    return int(-(-steps // 20) * 20)


@dataclass(frozen=True)
class Category:
    """One row of the bank: a road, a rule for the exit, and a budget to reach it."""

    name: str
    block_seq: str
    exit_rule: ExitRule
    max_steps: int
    description: str

    def __post_init__(self) -> None:
        validate_block_seq(self.block_seq, category=self.name)


def validate_block_seq(block_seq: str, *, category: str | None = None) -> None:
    """Raise `CategoryError` unless every character is a block MetaDrive can actually build.

    Worth doing at load time rather than at generation: an unknown ID surfaces as a failure deep
    inside `BIG`'s backtracking search, where it reads as "this seed could not be laid out"
    rather than as "this character is not a block".
    """
    where = f" for category {category!r}" if category else ""
    if not block_seq:
        raise CategoryError(f"empty block sequence{where}")
    unknown = sorted(set(block_seq) - VALID_BLOCK_IDS)
    if unknown:
        raise CategoryError(
            f"unknown block id(s) {unknown}{where}: valid ids are "
            f"{''.join(sorted(VALID_BLOCK_IDS))}. 'I' is prepended automatically and is never "
            "written into a sequence."
        )


#: **A seed does not always mean a different road.** Measured by `lane_geometry_digest` over
#: seeds 0-4: `X` produces **one** road, `T` produces **two**, and `O`, `CC` and `rS` produce
#: five each -- 18 distinct roads across the 35 scenarios. `StdInterSection` has a fixed radius
#: and the map pins `lane_num=3` and `lane_width=3.5`, so the junction has no seeded degree of
#: freedom left. Accepted deliberately: the five seeds of an intersection category vary the
#: *scene* -- traffic and hazard placement, which the option axes drive -- on a controlled road.
#: Phase 2 must therefore not assert 35 distinct roads. `scenariobank destinations` re-measures
#: it.
#:
#: **And 18 flatters the bank**, because it counts roads that are not *identical*. Measured by
#: `fingerprint.shape_gap` instead -- total lane length and map extent -- the closest pair of
#: every sequence is a near-duplicate: `CC` seeds 0 and 4 are 7% apart, `rS` seeds 0 and 4 are
#: 2% apart, and `O` seeds 0 and 4 are not measurably apart at all. Only `CC` has real spread
#: available (median pair gap 40% over seeds 0-25, against 12% for `O` and under 14% for every
#: `rS` pair); `X` and `T` have none by construction. **The bank's variety is in the scene the
#: Phase 4 options build, not in the road** -- already the accepted position for `X`, and true
#: of the bank as a whole. `scenariobank seeds` is how you find a seed that would add more.
#:
#: Those five runs are still not identical: `random_spawn_lane_index` is left on (see
#: `config.base_config`), so the ego starts in lane 0, 1, 0, 1, 1 across seeds 0-4. Until Phase 4's
#: options arrive that is the *only* thing separating them, and `route_length` will not show it --
#: `navigation.total_length` is measured on a reference lane. The drawn lane is recorded per
#: scenario instead of being left implicit.
#:
#: Route lengths measured on this simulator at seeds 0-4, in metres, longest of the five, and
#: **measured on the mirrored, left-side-traffic map** (`scenariobank.handedness`):
#: intersection 122.5, t_junction 117.2, roundabout 268.5, curve 452.1, ramp_traffic_merge 275.0.
#: `max_steps` is `step_budget()` of those. Provisional until Phase 4b measures what a policy
#: actually needs -- the house rule is to re-measure a figure, never to quote one.
CATEGORIES: dict[str, Category] = {
    category.name: category
    for category in (
        Category(
            name="intersection_left",
            block_seq="X",
            exit_rule=ExitRule.LEFT,
            max_steps=320,
            description=(
                "Four-way junction, turning left. Traffic keeps left here, so this is the "
                "near turn: it does not cross the opposing direction."
            ),
        ),
        Category(
            name="intersection_right",
            block_seq="X",
            exit_rule=ExitRule.RIGHT,
            max_steps=320,
            description=(
                "Four-way junction, turning right across the opposing direction. In left-side "
                "traffic this is the unprotected turn, not the left one."
            ),
        ),
        Category(
            name="intersection_straight",
            block_seq="X",
            exit_rule=ExitRule.STRAIGHT,
            max_steps=320,
            description="Four-way junction, driving straight through.",
        ),
        Category(
            name="t_junction",
            block_seq="T",
            exit_rule=ExitRule.SHARPEST,
            max_steps=320,
            description=(
                "Three-way junction, taking whichever turn the seed offers. The arm that "
                "exists alternates with the seed, so the direction varies and the turn does not."
            ),
        ),
        Category(
            name="roundabout",
            block_seq="O",
            exit_rule=ExitRule.RIGHT,
            max_steps=700,
            description=(
                "Roundabout, leaving by the right-hand exit -- the longest way round, and so "
                "the most time spent in the circulating lanes. Traffic keeps left and the "
                "roundabout circulates clockwise, which makes the *right* turn the one that "
                "has to circulate past three exits and give way from the right. Turn direction "
                "is already covered by the three intersection categories; this one exists to "
                "test the traversal."
            ),
        ),
        Category(
            name="curve",
            block_seq="CC",
            exit_rule=ExitRule.ONLY,
            max_steps=1200,
            description=(
                "Two consecutive curves, each followed by a straight. A MetaDrive `Curve` "
                "block is an arc *and* a trailing straight of drawn length -- "
                "`create_bend_straight` returns both and `pgblock/curve.py` builds both -- so "
                "the road runs straight, arc, straight, arc, straight. The two blocks draw "
                "radius, arc and *direction* independently, so what the seed picks is a pair: "
                "seeds 0-4 cover all four of left-left, left-right, right-right and "
                "right-left, with left-left drawn twice. Net rotation runs from +67.5 to "
                "+239.5 degrees -- two of the five sweep past a U-turn, which is a consequence "
                "of the pairing rather than an accident of it."
            ),
        ),
        Category(
            name="ramp_traffic_merge",
            block_seq="rS",
            exit_rule=ExitRule.ONLY,
            max_steps=700,
            description=(
                "Holds the through lane of a carriageway while an on-ramp joins from the "
                "left. The ego does not itself merge: it spawns on the main road and the "
                "route never enters the ramp."
            ),
        ),
    )
}


def get_category(name: str) -> Category:
    """Return a category by name, or raise `CategoryError` naming the ones that exist."""
    try:
        return CATEGORIES[name]
    except KeyError as error:
        raise CategoryError(
            f"unknown category {name!r}: choose one of {', '.join(sorted(CATEGORIES))}"
        ) from error
