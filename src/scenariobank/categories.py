"""The eleven categories, and the rule that fixes each one's exit.

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

#: The **default** seeds every category is built at. The same five for all eleven, so the three
#: intersection categories are the same junction driven three ways.
#:
#: A default, not a constant: `scenariobank generate --seeds` overrides it, per category if
#: wanted, and `scenariobank replace` swaps one scenario's seed in an existing bank. That
#: matters because **seed 4 is a poor draw for two categories** -- it builds a `curve` road 7%
#: from seed 0's and a `roundabout` indistinguishable from seed 0's. Kept as the default
#: anyway: 0-4 is the set every measurement in this repo was taken at, and which seeds a bank
#: uses is a decision for whoever builds it. `scenariobank seeds` ranks the alternatives.
SEEDS: tuple[int, ...] = (0, 1, 2, 3, 4)

@dataclass(frozen=True)
class BlockNeeds:
    """What a block wants in front of it before it will build -- as data, not only as prose.

    Three blocks of fifteen carry one, and they carry it for two different reasons. `f` and `F`
    do not build at all, at any seed, and say so in MetaDrive's own words. `P` builds perfectly
    well, but only behind a road already narrowed to one lane in each direction -- a *precondition
    on the sequence*, which MetaDrive states as a bare `assert` naming no remedy
    (`parking_lot.py:30`).

    Structured rather than a sentence because three separate readers need the rule, and three
    copies of a rule is three chances to disagree: `validate_block_seq` refuses on it, the
    refusal it raises quotes `text`, and the studio's road builder renders `text` and offers
    `insert` as a repair. `tests/unit/test_categories.py` asserts all three fields against the
    simulator, which is what keeps them honest.
    """

    #: What to tell a reader, in plain English. Markdown -- the studio runs it through the same
    #: `inline` every other caption uses, and a refusal quotes it with the markers stripped.
    text: str
    #: The same thing in one clause, for a flag table that repeats its cell in three places.
    #: Not derivable from `text` by truncation: the short form has to stay a whole sentence.
    short: str
    #: Block ids, at least one of which must appear *earlier* in the sequence for this block to
    #: have any chance of building. Empty means nothing in front of it can help.
    after_any: str = ""
    #: What to put in front of this block when nothing in `after_any` precedes it. Empty when
    #: there is no repair to offer, which is the same case as an empty `after_any`.
    insert: str = ""


@dataclass(frozen=True)
class Block:
    """One of the pieces a road is spelled from: the letter, MetaDrive's class, and what it is."""

    #: The character written into a block sequence -- the class's `ID` attribute.
    id: str
    #: The MetaDrive class registered under that ID. Asserted against the simulator by
    #: `tests/unit/test_categories.py`, so a renamed block fails a test rather than a palette.
    cls: str
    #: What the block is, in words. Ours, not MetaDrive's: the class names say `InRampOnStraight`
    #: and `Bidirection`, and a palette button has to say on-ramp and two-way road.
    label: str
    #: What the block needs in front of it, or `None` for the twelve that need nothing.
    needs: BlockNeeds | None = None


#: Both forks, measured at seeds 0-4. `MetaDrive` names them broken and they are broken at every
#: seed, so there is no repair to offer -- `after_any` and `insert` stay empty.
_FORK_IS_BROKEN = BlockNeeds(
    text="MetaDrive refuses to build this block at any seed \u2014 *\"Bug exists in this block, "
    "Recommend to use Ramp\"*. It is in the palette anyway, because that refusal is the "
    "simulator's to make and not the studio's to hide.",
    short="MetaDrive refuses to build it at any seed",
)

#: `ParkingLot` asserts `positive_lane_num == 1` (`parking_lot.py:30`) and a map starts at
#: `lane_num=3`, so a parking lot behind an unnarrowed road fails at every seed.
#:
#: Measured at seeds 0-4: every block passes the lane count through unchanged except `y`, which
#: narrows, and `Y`, which widens. One `y` reaches one lane at seeds 0, 1 and 4 but stops at two
#: on seeds 2 and 3 -- `Merge` drops `DiscreteSpace(min=1, max=2)` lanes floored at `max(1, ...)`
#: (`bottleneck.py:48-50`, `pg_space.py:317`) -- so `yP` builds at three seeds of five and `yyP`
#: at all five, which is why `insert` is two merges and not one.
_PARKING_LOT_NEEDS_ONE_LANE = BlockNeeds(
    text="A parking lot needs the road in front of it to be **one lane in each direction**, and "
    "a road starts at three. Only the lane merge `y` narrows a road: one merge drops one or two "
    "lanes depending on the seed, so two of them reach one lane at *every* seed. Only the lane "
    "split `Y` widens it again \u2014 every other block passes the count through.",
    short="needs a road already narrowed to one lane each way \u2014 put `yy` in front of it",
    after_any="y",
    insert="yy",
)


#: Every block ID `BLOCK_TYPE_DISTRIBUTION_V2` can produce (`blocks_prob_dist.py:22-41`), by the
#: `ID` attribute of each registered class rather than by the class name the dict is keyed on.
#:
#: `I` (`FirstPGBlock`) is deliberately absent: it is prepended to every map automatically and is
#: never written into a sequence. Held as a literal so this module stays importable without the
#: `sim` group; `tests/unit/test_categories.py` asserts it against MetaDrive so it cannot drift.
#:
#: In MetaDrive's own registration order, which is the order the studio's palette lays them out
#: in: the plain pieces first, the junctions, then the odd ones.
#:
#: **Twelve of the fifteen build on their own.** Measured at seeds 0-4: `f` and `F` -- both forks,
#: not just `f` -- refuse outright with "Bug exists in this block, Recommend to use Ramp", and `P`
#: refuses behind a three-lane road, which is every road until a merge narrows one. All three are
#: listed anyway: a palette that quietly dropped a block would be asserting something about the
#: simulator that only the simulator can say. What they carry instead is a `BlockNeeds`, so the
#: condition is stated *before* the click rather than as an assertion afterwards.
BLOCKS: tuple[Block, ...] = (
    Block("C", "Curve", "curve"),
    Block("S", "Straight", "straight"),
    Block("r", "InRampOnStraight", "on-ramp"),
    Block("R", "OutRampOnStraight", "off-ramp"),
    Block("X", "StdInterSection", "crossroads"),
    Block("T", "StdTInterSection", "T junction"),
    Block("O", "Roundabout", "roundabout"),
    Block("f", "InFork", "fork in", _FORK_IS_BROKEN),
    Block("F", "OutFork", "fork out", _FORK_IS_BROKEN),
    Block("y", "Merge", "lane merge"),
    Block("Y", "Split", "lane split"),
    Block("P", "ParkingLot", "parking lot", _PARKING_LOT_NEEDS_ONE_LANE),
    Block("$", "TollGate", "toll gate"),
    Block("B", "Bidirection", "two-way road"),
    Block("U", "StdInterSectionWithUTurn", "crossroads with U-turn"),
)

VALID_BLOCK_IDS: frozenset[str] = frozenset(block.id for block in BLOCKS)


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


def validate_block_ids(block_seq: str, *, category: str | None = None) -> None:
    """Raise `CategoryError` unless every character in `block_seq` is a registered block id.

    The spelling half of the check, on its own, because naming a road is not the same question
    as building one: `composed_name` wants a typo caught and has no opinion on whether the road
    lays out.
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


def validate_block_seq(block_seq: str, *, category: str | None = None) -> None:
    """Raise `CategoryError` unless this is a sequence MetaDrive could actually build.

    Worth doing at load time rather than at generation: an unknown ID surfaces as a failure deep
    inside `BIG`'s backtracking search, where it reads as "this seed could not be laid out"
    rather than as "this character is not a block". A block whose `BlockNeeds` the sequence
    cannot satisfy surfaces there too, and reads even worse -- as a bare `assert` about lane
    numbers, at every seed, which is what `unmet_need` exists to head off.
    """
    validate_block_ids(block_seq, category=category)
    unmet = unmet_need(block_seq)
    if unmet is not None:
        # The sentence already names the sequence, and `cli._ad_hoc_category` names a composed
        # road after its sequence -- so saying both would read "of 'SPS' for category 'SPS'".
        where = f" for category {category!r}" if category and category != block_seq else ""
        raise CategoryError(unmet.sentence(where))


@dataclass(frozen=True)
class UnmetNeed:
    """A block in a sequence whose `BlockNeeds` the sequence does not satisfy.

    Held as a value rather than raised directly because two callers want different things from
    it: `validate_block_seq` wants a `CategoryError`, and the studio wants the repair on its own
    so it can offer it as a suggestion before anything is pressed.
    """

    #: The offending block, and where in the sequence it sits (0-based).
    block: Block
    at: int
    #: The sequence as written, and the same sequence with `needs.insert` put in front of the
    #: offending block. Both, because every reader wants to show the second beside the first.
    block_seq: str
    repair: str

    def sentence(self, where: str = "") -> str:
        """Why this cannot build, and what to type instead. The one wording, for every reader."""
        return (
            f"{self.block.id!r} ({self.block.label}) at position {self.at + 1} of "
            f"{self.block_seq!r}{where} cannot build. "
            f"{plain_needs(self.block)} "
            f"Nothing in front of it does that, so try {self.repair!r} instead. "
            "No seed changes this."
        )


def unmet_need(block_seq: str) -> UnmetNeed | None:
    """The first block in `block_seq` whose `BlockNeeds` nothing in front of it satisfies.

    Only blocks that declare both an `after_any` and an `insert` are checked, which today means
    only `P`. The two forks declare neither: they fail *themselves*, and that judgement is
    MetaDrive's to make and to word, so nothing here pre-empts it. What is checked is a property
    of the *sequence* -- and catching those before `BIG`'s backtracking search turns them into
    "this seed could not be laid out" is the whole reason this module validates sequences at all.

    Deliberately not modelled: lane arithmetic. `yYP` narrows and then widens again and is
    refused by MetaDrive, not here. Answering that would mean reimplementing `Bottleneck`, and a
    second implementation of a simulator's geometry is a second thing to be wrong.
    """
    by_id = {block.id: block for block in BLOCKS}
    for at, char in enumerate(block_seq):
        needs = by_id[char].needs
        if needs is None or not needs.after_any or not needs.insert:
            continue
        if any(earlier in needs.after_any for earlier in block_seq[:at]):
            continue
        return UnmetNeed(
            block=by_id[char],
            at=at,
            block_seq=block_seq,
            repair=block_seq[:at] + needs.insert + block_seq[at:],
        )
    return None


def plain_needs(block: Block) -> str:
    """A block's `needs.text` with its markdown stripped, for a terminal that has no bold.

    The studio renders the same string through `inline`, so both readers are showing one
    sentence rather than two that have to be kept in step by hand.
    """
    if block.needs is None:
        return ""
    return block.needs.text.replace("**", "").replace("*", "").replace("`", "'")


#: The one block id that is not a letter. Spelled out rather than dropped, because a name with the
#: character removed would make `$S` and `S` the same category. No block is spelled with a
#: lower-case `t`, `o` or `l`, so the substitution cannot collide with a real sequence.
_SPELLED: dict[str, str] = {"$": "toll"}


def composed_name(block_seq: str, rule: ExitRule) -> str:
    """The category name a road composed by hand is filed under: the sequence, then the rule.

    Derived rather than typed, so the name **is** the road and the rule and cannot drift from
    what it describes: the same road composed twice, by two people, gets one category rather
    than two spellings of one. It is also why nothing has to be invented for a one-off.

    Case is kept: `r` is the on-ramp and `R` the off-ramp, and folding them together would file
    two different roads under one name. The result always matches the studio's name pattern, so
    a scenario id built from it stays servable as a URL segment.

    Only the ids are validated, not the sequence: this names a road, and `P` names one perfectly
    well whether or not the road in front of it lets it build. The build check belongs on the
    paths that build -- `Category`, `read_sockets`, `add`.
    """
    validate_block_ids(block_seq)
    spelled = "".join(_SPELLED.get(char, char) for char in block_seq)
    return f"{spelled}_{ExitRule(rule).value}"


#: **A seed does not always mean a different road.** Measured by `lane_geometry_digest` over
#: seeds 0-4: `X` produces **one** road, `T` produces **two**, and the other seven sequences
#: produce five each -- 38 distinct roads across the 55 scenarios. `StdInterSection` has a fixed
#: radius and the map pins `lane_num=3` and `lane_width=3.5`, so the junction has no seeded
#: degree of freedom left. Accepted deliberately: the five seeds of an intersection category vary
#: the *scene* -- traffic and hazard placement, which the option axes drive -- on a controlled
#: road. Phase 2 must therefore not assert 55 distinct roads. `scenariobank destinations`
#: re-measures it.
#:
#: **And 38 flatters the bank**, because it counts roads that are not *identical*. Measured by
#: `fingerprint.shape_gap` instead -- total lane length and map extent -- the closest pair of
#: **every** sequence is a near-duplicate: `CC` seeds 0 and 4 are 7% apart, `$S` and `YS` 4%,
#: `yS` 3%, `rS` and `RS` 2%, and `O`, `T` and `X` are not measurably apart at all. The four
#: sequences added at Step 9 are all straights of drawn length, so their seeds differ in how long
#: the road is and in nothing else -- a wider spread than `X` has, and a narrower one than a
#: turn. Only `CC` has real spread available (median pair gap 40% over seeds 0-25, against 12%
#: for `O` and under 14% for every `rS` pair); `X` and `T` have none by construction. **The
#: bank's variety is in the scene the Phase 4 options build, not in the road** -- already the
#: accepted position for `X`, and true of the bank as a whole. `scenariobank seeds` is how you
#: find a seed that would add more.
#:
#: Those five runs are still not identical: `random_spawn_lane_index` is left on (see
#: `config.base_config`), so the ego starts in lane 0, 1, 0, 1, 1 across seeds 0-4. Until Phase 4's
#: options arrive that is the *only* thing separating them, and `route_length` will not show it --
#: `navigation.total_length` is measured on a reference lane. The drawn lane is recorded per
#: scenario instead of being left implicit.
#:
#: Route lengths measured on this simulator at seeds 0-4, in metres, longest of the five, and
#: **measured on the mirrored, left-side-traffic map** (`scenariobank.handedness`):
#: intersection 122.5, t_junction 117.2, roundabout 268.5, curve 452.1, ramp_traffic_merge 275.0,
#: off_ramp_hold 260.0, lane_merge 188.6, lane_split 188.6, tollgate 168.6.
#: `max_steps` is `step_budget()` of those, with one exception: `tollgate` holds its own lanes to
#: 3 m/s (`tollgate.py:68`), half the speed the budget assumes, so its toll section is charged
#: for twice and the cap is `step_budget(route + toll)` at the worst seed -- 168.6 + 43.5 m, not
#: 168.6. Provisional until Phase 4b measures what a policy actually needs -- the house rule is
#: to re-measure a figure, never to quote one.
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
        Category(
            name="off_ramp_hold",
            block_seq="RS",
            exit_rule=ExitRule.ONLY,
            max_steps=660,
            description=(
                "Holds the through lane while an off-ramp leaves it to the left -- the mirror "
                "of `ramp_traffic_merge`, a lane departing rather than joining. The ego does "
                "not take the exit, and could not be asked to: `OutRampOnStraight` puts the "
                "ramp on a one-lane road of its own that dead-ends at `1R1_4_`, and that node "
                "is not a socket, so a route down the ramp is not expressible as a "
                "destination. The three through lanes survive the block intact."
            ),
        ),
        Category(
            name="lane_merge",
            block_seq="yS",
            exit_rule=ExitRule.ONLY,
            max_steps=480,
            description=(
                "The carriageway itself narrows: `Merge` takes the three lanes down to one on "
                "seeds 0, 1 and 4 and to two on seeds 2 and 3, and the lanes that end have to "
                "merge into the ones that do not. Not the same manoeuvre as "
                "`ramp_traffic_merge`, where the ego holds a through lane and somebody else "
                "joins -- here the ego's own lane is one of the ones that may run out."
            ),
        ),
        Category(
            name="lane_split",
            block_seq="YS",
            exit_rule=ExitRule.ONLY,
            max_steps=480,
            description=(
                "The carriageway widens: `Split` takes the three lanes up to five on seeds 0, "
                "1 and 4 and to four on seeds 2 and 3, and the ego holds its lane while they "
                "open beside it. Its route is the same length as `lane_merge`'s at every seed "
                "-- `Merge` and `Split` are one block drawn in either direction, and "
                "`total_length` is measured on the reference lane, which survives both. The "
                "road is not the same, and neither is the drive: what changes is how many "
                "lanes are alongside."
            ),
        ),
        Category(
            name="tollgate",
            block_seq="$S",
            exit_rule=ExitRule.ONLY,
            max_steps=540,
            description=(
                "A toll plaza on a straight. `TollGate` caps its own lanes at 3 m/s "
                "(`tollgate.py:68`) and parks a `TollGateBuilding` in every second lane, "
                "which on a three-lane road is the middle one -- so the block is both slower "
                "and partly blocked. The budget is charged for it: at 3 m/s the toll section "
                "costs twice the time its length earns at the reference speed, so `max_steps` "
                "is `step_budget(route + toll section)` at the worst seed rather than "
                "`step_budget(route)`."
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
