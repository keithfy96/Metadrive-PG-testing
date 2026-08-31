"""Exit selection is pure, so most of it is testable with no simulator at all."""

import pytest

from scenariobank.categories import CATEGORIES, SEEDS, ExitRule
from scenariobank.doctor import has_simulator
from scenariobank.sockets import SocketError, SocketReading, select_exit

needs_sim = pytest.mark.skipif(
    not has_simulator(),
    reason="needs_sim: MetaDrive is not installed (uv sync --group sim)",
)


def reading(node, angle, *, is_entry=False):
    return SocketReading(
        index=f"1X-{node}",
        node=node,
        start_node=f"{node}_in",
        lane_count=3,
        angle_deg=angle,
        is_entry=is_entry,
    )


# The four-way junction as this simulator actually builds it.
FOUR_WAY = [reading("right", -90.0), reading("straight", 0.0), reading("left", 90.0)]


@pytest.mark.parametrize(
    ("rule", "expected"),
    [(ExitRule.LEFT, "left"), (ExitRule.RIGHT, "right"), (ExitRule.STRAIGHT, "straight")],
)
def test_each_angle_rule_picks_the_exit_it_names(rule, expected):
    assert select_exit(FOUR_WAY, rule).node == expected


@pytest.mark.parametrize("arms", [["right", "straight"], ["straight", "left"]])
def test_sharpest_takes_the_turn_whichever_arm_the_seed_offered(arms):
    # This is the T junction: seeds 0, 1 and 4 offer right-and-straight, seeds 2 and 3 offer
    # left-and-straight. A hardcoded node would be unresolvable on two of the five.
    sockets = [r for r in FOUR_WAY if r.node in arms]
    assert abs(select_exit(sockets, ExitRule.SHARPEST).angle_deg) == 90.0


def test_the_socket_driven_in_by_is_never_a_destination():
    # `auto_assign_task` excludes it too; a route back the way you came is not a scenario.
    sockets = [reading("behind", 180.0, is_entry=True), reading("ahead", 0.0)]
    assert select_exit(sockets, ExitRule.SHARPEST).node == "ahead"


def test_a_block_whose_only_socket_is_the_entry_fails_loudly():
    with pytest.raises(SocketError, match="no exit other than"):
        select_exit([reading("behind", 180.0, is_entry=True)], ExitRule.ONLY)


def test_only_accepts_a_single_exit_block():
    assert select_exit([reading("end", 120.5)], ExitRule.ONLY).node == "end"


def test_only_refuses_to_guess_when_the_block_offers_a_choice():
    with pytest.raises(SocketError, match="single-exit block"):
        select_exit(FOUR_WAY, ExitRule.ONLY)


def test_an_angle_rule_refuses_the_least_wrong_exit_rather_than_accepting_it():
    # The failure the plan's "two sockets of the same sign" check is aimed at: a junction that
    # is not shaped the way the category assumes should stop the build, not pick something.
    skewed = [reading("a", -20.0), reading("b", 10.0)]
    with pytest.raises(SocketError, match="not shaped the way"):
        select_exit(skewed, ExitRule.LEFT)


def test_an_exit_within_forty_five_degrees_of_the_target_is_still_accepted():
    assert select_exit([reading("wide", 60.0)], ExitRule.LEFT).node == "wide"


@needs_sim
@pytest.mark.parametrize("category_name", sorted(CATEGORIES))
def test_every_category_resolves_a_reachable_destination_at_every_seed(category_name):
    from scenariobank.sockets import resolve_destination, route_length

    category = CATEGORIES[category_name]
    for seed in SEEDS:
        exit_socket = resolve_destination(category, seed)
        # Naming a node does not make it reachable; running the shortest path does.
        assert route_length(category, seed, exit_socket.node) > 0.0


@needs_sim
def test_the_three_intersection_variants_leave_by_three_different_exits():
    from scenariobank.sockets import read_sockets

    readings = read_sockets("X", 0)
    chosen = {
        rule: select_exit(readings, rule).node
        for rule in (ExitRule.LEFT, ExitRule.RIGHT, ExitRule.STRAIGHT)
    }
    assert len(set(chosen.values())) == 3


@needs_sim
@pytest.mark.parametrize("seed", SEEDS)
def test_the_t_junction_turns_on_every_seed_even_though_the_direction_changes(seed):
    from scenariobank.sockets import resolve_destination

    chosen = resolve_destination(CATEGORIES["t_junction"], seed)
    assert abs(chosen.angle_deg) > 45.0
