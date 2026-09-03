"""A category is a claim about a road and a turn. Both halves are testable."""

import pytest

from scenariobank.categories import (
    BLOCKS,
    CATEGORIES,
    SEEDS,
    VALID_BLOCK_IDS,
    CategoryError,
    ExitRule,
    composed_name,
    get_category,
    step_budget,
    validate_block_seq,
)
from scenariobank.doctor import has_simulator

needs_sim = pytest.mark.skipif(
    not has_simulator(),
    reason="needs_sim: MetaDrive is not installed (uv sync --group sim)",
)


@pytest.mark.parametrize("block_seq", ["X", "T", "O", "CC", "rS", "SSS", "C$B"])
def test_a_sequence_of_registered_block_ids_validates(block_seq):
    validate_block_seq(block_seq)


@pytest.mark.parametrize("block_seq", ["Z", "XZ", "x", "cc", "X ", "1"])
def test_a_sequence_containing_an_unregistered_id_is_rejected(block_seq):
    with pytest.raises(CategoryError, match="unknown block id"):
        validate_block_seq(block_seq)


def test_the_first_block_is_not_writable_in_a_sequence():
    # `I` is prepended by the generator. Writing it would silently mean a second one.
    with pytest.raises(CategoryError, match="unknown block id"):
        validate_block_seq("IX")


def test_an_empty_sequence_is_rejected_rather_than_producing_a_bare_first_block():
    with pytest.raises(CategoryError, match="empty block sequence"):
        validate_block_seq("")


def test_the_rejection_message_names_the_category_it_came_from():
    with pytest.raises(CategoryError, match="for category 'my_category'"):
        validate_block_seq("Z", category="my_category")


def test_every_shipped_category_has_a_buildable_block_sequence():
    for name, category in CATEGORIES.items():
        validate_block_seq(category.block_seq, category=name)


def test_every_shipped_category_is_keyed_on_its_own_name():
    assert all(name == category.name for name, category in CATEGORIES.items())


def test_the_bank_is_eleven_categories_at_five_seeds():
    assert len(CATEGORIES) == 11
    assert len(SEEDS) == 5


def test_the_three_intersection_categories_share_one_road_and_differ_only_in_rule():
    intersections = [c for name, c in CATEGORIES.items() if name.startswith("intersection_")]
    assert {c.block_seq for c in intersections} == {"X"}
    assert len({c.exit_rule for c in intersections}) == 3


def test_a_composed_road_is_named_after_the_road_and_the_rule():
    """The name has to be derivable, because that is what makes it not an invented name.

    Composed twice by two people, `CCX` at `sharpest` has to be one category rather than two
    spellings of one -- so the name is a function of the road and the rule and of nothing else.
    """
    assert composed_name("CCX", ExitRule.SHARPEST) == "CCX_sharpest"
    assert composed_name("CCX", "sharpest") == "CCX_sharpest"
    # Case carries meaning: `r` is the on-ramp and `R` the off-ramp. Folding them together would
    # file two different roads under one name.
    assert composed_name("rS", ExitRule.ONLY) != composed_name("RS", ExitRule.ONLY)
    with pytest.raises(CategoryError, match="unknown block id"):
        composed_name("Z", ExitRule.ONLY)


def test_the_toll_gate_is_spelled_out_rather_than_dropped_from_a_name():
    # `$` is the one id that is not a letter, and a name is a directory-safe key. Removing it
    # would make `$S` and `S` the same category.
    assert composed_name("$S", ExitRule.ONLY) == "tollS_only"
    assert composed_name("$S", ExitRule.ONLY) != composed_name("S", ExitRule.ONLY)


def test_every_composed_name_is_one_the_studio_can_serve():
    """A scenario id becomes a URL segment and a PNG filename, so the name has to survive both."""
    from scenariobank.web.api import _NAME

    for block in BLOCKS:
        for rule in ExitRule:
            name = composed_name(block.id, rule)
            assert _NAME.match(name), name
            assert _NAME.match(f"{name}_0000")
    # And it can never collide with a shipped category: those are words, these end in `_<rule>`
    # after a run of block ids.
    assert not any(name in CATEGORIES for name in
                   (composed_name(b.id, r) for b in BLOCKS for r in ExitRule))


def test_asking_for_a_category_that_does_not_exist_lists_the_ones_that_do():
    with pytest.raises(CategoryError, match="intersection_left"):
        get_category("intersection_diagonal")


@pytest.mark.parametrize(
    ("length_m", "expected"),
    [(0.0, 0), (10.0, 40), (117.2, 300), (122.5, 320), (452.1, 1140)],
)
def test_the_step_budget_rounds_up_so_a_route_is_never_short_of_its_own_estimate(
    length_m, expected
):
    assert step_budget(length_m) == expected


def test_every_category_budget_covers_the_longest_route_it_was_measured_at():
    # The measured longest route per category, from docs/reference/destinations.md.
    longest = {
        "intersection_left": 111.7,
        "intersection_right": 117.2,
        "intersection_straight": 122.5,
        "t_junction": 117.2,
        "roundabout": 268.5,
        "curve": 452.1,
        "ramp_traffic_merge": 275.0,
        "off_ramp_hold": 260.0,
        "lane_merge": 188.6,
        "lane_split": 188.6,
        "tollgate": 168.6,
    }
    assert set(longest) == set(CATEGORIES), "a new category needs its measured route here"
    for name, category in CATEGORIES.items():
        assert category.max_steps >= step_budget(longest[name]), name


def test_the_tollgate_budget_pays_for_the_speed_limit_the_block_imposes():
    """`step_budget` assumes 6 m/s everywhere. The `$` block does not allow it.

    `TollGate._add_building_and_speed_limit` calls `lane.set_speed_limit(3)` on every lane it
    lays, so the toll section costs twice the time its length earns. The cap is the only one in
    `CATEGORIES` that is deliberately above `step_budget(longest route)`, and this is the
    arithmetic that says by how much -- 168.6 m of route and 43.5 m of toll at the worst seed.
    """
    assert CATEGORIES["tollgate"].max_steps == step_budget(168.6 + 43.5)
    assert CATEGORIES["tollgate"].max_steps > step_budget(168.6)


def test_the_two_bottleneck_categories_are_one_block_drawn_each_way():
    # `Merge` and `Split` measure the same route -- `total_length` is read off the reference
    # lane, which survives both -- so the pair is only two scenarios because the roads differ.
    # Asserted so that a future edit cannot quietly collapse them into one category.
    merge, split = CATEGORIES["lane_merge"], CATEGORIES["lane_split"]
    assert (merge.block_seq, split.block_seq) == ("yS", "YS")
    assert merge.max_steps == split.max_steps


def test_the_four_step_9_categories_all_take_the_only_exit_their_road_offers():
    # Each is a straight carriageway with one downstream socket. A rule that has to choose
    # between arms would be a claim their roads cannot support.
    added = ["off_ramp_hold", "lane_merge", "lane_split", "tollgate"]
    assert {CATEGORIES[name].exit_rule for name in added} == {ExitRule.ONLY}


@needs_sim
def test_the_valid_block_ids_are_exactly_what_this_metadrive_can_build():
    # The literal in `categories.py` exists so the module imports without MetaDrive. This is
    # what stops it drifting from the simulator it describes.
    from metadrive.component.algorithm.blocks_prob_dist import PGBlockDistConfig
    from metadrive.utils.registry import get_metadrive_class

    registered = {get_metadrive_class(name).ID for name in PGBlockDistConfig.all_blocks("v2")}
    assert registered == VALID_BLOCK_IDS


@needs_sim
def test_every_rule_a_category_uses_is_one_the_selector_implements():
    from scenariobank.sockets import select_exit  # noqa: F401

    assert {c.exit_rule for c in CATEGORIES.values()} <= set(ExitRule)


def test_turn_pairs_reads_only_the_curve_blocks_and_in_order():
    # Pure, so it runs without the simulator. `I` and `S` contribute no turn; the sign of each
    # `C` block's rotation is the letter.
    from scenariobank.sockets import turn_pairs

    assert turn_pairs([("I", 0.0), ("C", 115.5), ("C", -50.5)]) == "LR"
    assert turn_pairs([("I", 0.0), ("C", -69.5), ("C", -71.4)]) == "RR"
    assert turn_pairs([("I", 0.0), ("r", 0.0), ("S", 0.0)]) == ""


@needs_sim
def test_the_curve_seeds_cover_every_combination_of_two_turn_directions():
    """`CC` is two independent draws, so the seeds must reach all four pairs -- not just two
    lefts and two rights, but left-then-right and right-then-left as well.

    This holds today by luck rather than by construction: the seeds are fixed at 0-4 and the
    parameters come from MetaDrive's own RNG. That is exactly why it is asserted. A simulator
    bump that shifts the draw would quietly collapse the coverage to three combinations, and
    every road would still build, every route would still resolve, and nothing else would fail.
    """
    from scenariobank.sockets import measure_route, resolve_destination

    category = get_category("curve")
    signatures = {
        measure_route(category, seed, resolve_destination(category, seed).node).turn_pairs
        for seed in SEEDS
    }
    assert signatures == {"LL", "LR", "RL", "RR"}


# ------------------------------------------------------------------ the blocks table (step 10)


def test_the_valid_ids_are_the_blocks_table_and_every_block_says_what_it_is():
    # The palette is laid out from this table and `validate_block_seq` checks against it, so the
    # two cannot disagree about which letters are roads. Fifteen, each named in words.
    assert len(BLOCKS) == 15
    assert len({block.id for block in BLOCKS}) == len(BLOCKS)
    assert {block.id for block in BLOCKS} == VALID_BLOCK_IDS
    assert all(block.cls and block.label for block in BLOCKS)


@needs_sim
def test_every_block_names_the_class_metadrive_registers_under_its_id():
    # The literal exists so the module imports without MetaDrive. This is what stops the class
    # column drifting from the simulator it describes -- and the order is MetaDrive's own, so the
    # palette lays the blocks out the way `blocks_prob_dist.py` lists them.
    from metadrive.component.algorithm.blocks_prob_dist import PGBlockDistConfig
    from metadrive.utils.registry import get_metadrive_class

    registered = [get_metadrive_class(name) for name in PGBlockDistConfig.all_blocks("v2")]
    assert [(block.id, block.cls) for block in BLOCKS] == [
        (cls.ID, cls.__name__) for cls in registered
    ]
