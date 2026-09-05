"""Exit selection is pure, so most of it is testable with no simulator at all."""

import pytest

from scenariobank.categories import CATEGORIES, SEEDS, ExitRule
from scenariobank.doctor import has_simulator
from scenariobank.sockets import SocketError, SocketReading, select_exit

needs_sim = pytest.mark.skipif(
    not has_simulator(),
    reason="needs_sim: MetaDrive is not installed (uv sync --group sim)",
)


def reading(node, angle, *, is_entry=False, turn=None, entry_heading=0.0):
    """A socket at `angle` from the spawn. `turn` defaults to it, as on any unrotated road."""
    return SocketReading(
        index=f"1X-{node}",
        node=node,
        start_node=f"{node}_in",
        lane_count=3,
        angle_deg=angle,
        turn_deg=angle if turn is None else turn,
        is_entry=is_entry,
        entry_heading_deg=entry_heading,
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
    with pytest.raises(SocketError, match="no way out except the one the car drove in by"):
        select_exit([reading("behind", 180.0, is_entry=True)], ExitRule.ONLY)


def test_only_accepts_a_single_exit_block():
    assert select_exit([reading("end", 120.5)], ExitRule.ONLY).node == "end"


def test_only_refuses_to_guess_when_the_block_offers_a_choice():
    # In words a reader who has never opened MetaDrive can act on: what the road offers, what
    # the setting means, and which settings would work instead.
    with pytest.raises(SocketError, match="3 ways out"):
        select_exit(FOUR_WAY, ExitRule.ONLY)


def test_an_angle_rule_refuses_the_least_wrong_exit_rather_than_accepting_it():
    # The failure the plan's "two sockets of the same sign" check is aimed at: a junction that
    # is not shaped the way the category assumes should stop the build, not pick something.
    skewed = [reading("a", -20.0), reading("b", 10.0)]
    with pytest.raises(SocketError, match="nothing here turns left"):
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


@needs_sim
def test_the_spawn_lane_is_reproducible_and_untouched_by_the_option_axes():
    """The invariance Phase 2b rests on, for the one knob that is deliberately random.

    `random_spawn_lane_index` draws from the seeded RNG, so a scenario is only reproducible if
    that draw is a function of the seed alone. If MetaDrive ever changes how much randomness the
    traffic or object managers consume before the agent manager draws, this is what catches it.
    """
    from metadrive.envs.metadrive_env import MetaDriveEnv

    from scenariobank.config import base_config

    def lanes(**overrides):
        env = MetaDriveEnv(
            base_config(map="X", start_seed=0, num_scenarios=len(SEEDS), **overrides)
        )
        try:
            drawn = []
            for seed in SEEDS:
                env.reset(seed=seed)
                drawn.append(env.agent.lane_index[2])
            return drawn
        finally:
            env.close()

    baseline = lanes()
    assert len(set(baseline)) > 1, "a constant draw would make this test vacuous"
    assert lanes() == baseline, "not reproducible across env rebuilds"
    assert lanes(traffic_density=0.4) == baseline, "traffic moved the spawn lane"
    assert lanes(accident_prob=0.8) == baseline, "hazards moved the spawn lane"


@needs_sim
def test_the_spawn_lane_depends_on_the_seed_and_not_on_the_block_sequence():
    # Every map is `lane_num=3` and the draw is `randint(lane_num)`, so the sequence cannot
    # matter. `destinations.md` collapses its table to one row on the strength of this.
    from metadrive.envs.metadrive_env import MetaDriveEnv

    from scenariobank.config import base_config

    def lanes(block_seq):
        env = MetaDriveEnv(
            base_config(map=block_seq, start_seed=0, num_scenarios=len(SEEDS))
        )
        try:
            drawn = []
            for seed in SEEDS:
                env.reset(seed=seed)
                drawn.append(env.agent.lane_index[2])
            return drawn
        finally:
            env.close()

    assert lanes("X") == lanes("O")


@needs_sim
def test_every_surveyed_row_records_the_lane_the_ego_actually_spawned_in():
    from scenariobank.sockets import survey

    rows = survey(CATEGORIES["intersection_left"], SEEDS)
    lanes = [row["spawn_lane"] for row in rows]
    assert all(isinstance(lane, int) and 0 <= lane < 3 for lane in lanes)
    # The road is identical at all five seeds, so if this were constant too the five scenarios
    # would be one scenario repeated.
    assert len(set(lanes)) > 1


@needs_sim
def test_a_route_past_a_u_turn_reports_its_true_rotation_not_the_wrapped_one():
    """The regression test for the bug this pair of fields exists to fix.

    `curve` seed 0 sweeps +239.5 degrees. `wrap_to_pi` folds that to -120.5, which reads as a
    *right* turn of half the size. Both numbers are kept because both are wanted: the wrapped one
    is what `ExitRule` matches against, the unwrapped one is what a driver does.
    """
    from scenariobank.sockets import measure_route, resolve_destination

    category = CATEGORIES["curve"]
    exit_socket = resolve_destination(category, 0)
    measured = measure_route(category, 0, exit_socket.node)

    assert abs(measured.net_rotation_deg) > 180.0
    assert abs(exit_socket.angle_deg) <= 180.0
    assert measured.net_rotation_deg > 0 > exit_socket.angle_deg, "the fold flips the sign"


def test_the_turn_word_and_the_rules_read_the_junction_not_the_spawn():
    """The pure half of the `CSX` bug: two arms, only one of which is a left turn.

    Both sit +115.5 degrees from where the car set off, because a curve turned it that far before
    the junction. Measured from the junction one is straight ahead and the other is a left, and
    it is that number every rule matches. Reading `angle_deg` instead answered `left` with the
    arm the driver goes straight through.
    """
    from scenariobank.categories import ExitRule
    from scenariobank.sockets import _turn_word

    ahead = reading("ahead", 115.5, turn=0.0, entry_heading=115.5)
    leftward = reading("leftward", -154.5, turn=90.0, entry_heading=115.5)
    sockets = [ahead, leftward]

    assert _turn_word(ahead.turn_deg) == "straight"
    assert ahead.describe().endswith("straight")
    assert select_exit(sockets, ExitRule.STRAIGHT).node == "ahead"
    assert select_exit(sockets, ExitRule.LEFT).node == "leftward"
    assert select_exit(sockets, ExitRule.SHARPEST).node == "leftward"


@needs_sim
def test_a_road_that_curves_before_its_junction_still_reads_ninety_degree_turns():
    """The measured half of the same bug, on the road that exposed it.

    `CSX` seed 0 runs a curve into a crossroads, and the curve swings the car +115.5 degrees on
    the way. From the spawn its three arms read -154.5 / +115.5 / +25.5, from which `right` found
    nothing inside its tolerance and refused -- on a crossroads. From the junction they are the
    +90 / 0 / -90 a crossroads has, and all three rules resolve.
    """
    from scenariobank.categories import ExitRule
    from scenariobank.sockets import read_sockets

    readings = read_sockets("CSX", 0)
    assert readings[0].entry_heading_deg == pytest.approx(115.5, abs=1.0)
    assert sorted(round(one.turn_deg) for one in readings) == [-90, 0, 90]
    # The angles from the spawn are the ones that made the old reading refuse; they are kept.
    assert sorted(round(one.angle_deg) for one in readings) != [-90, 0, 90]

    chosen = {rule: select_exit(readings, rule).node for rule in
              (ExitRule.LEFT, ExitRule.RIGHT, ExitRule.STRAIGHT)}
    assert len(set(chosen.values())) == 3
    # And the one the card asked for: `left` is no longer the arm the drawing goes straight up.
    assert chosen[ExitRule.STRAIGHT] == "3X1_1_"
    assert chosen[ExitRule.LEFT] != "3X1_1_"


@needs_sim
def test_a_single_block_road_measures_the_same_turn_from_either_end():
    """The invariant that keeps the eleven shipped categories honest.

    Nothing rotates the car before a one-block road's junction, so the entry heading is zero and
    `turn_deg` is `angle_deg`. That is why moving the rules onto `turn_deg` left every checked-in
    destination byte-for-byte unchanged.
    """
    from scenariobank.sockets import read_sockets

    for readings in (read_sockets("X", 0), read_sockets("T", 0), read_sockets("O", 0)):
        assert readings[0].entry_heading_deg == pytest.approx(0.0, abs=0.5)
        assert all(one.turn_deg == pytest.approx(one.angle_deg, abs=0.5) for one in readings)


# ------------------------------------------- the words a failed build comes back in (step 10d)


def test_a_layout_failure_is_still_quoted_with_the_seed_that_produced_it():
    from scenariobank.sockets import explain_build_failure

    said = explain_build_failure(RuntimeError("Bug exists in this block"), "fS", 3)
    assert said == "seed 3 does not build for block sequence 'fS': Bug exists in this block"


def test_a_tail_is_appended_only_to_the_failures_the_seed_explains():
    """`bank._reset` adds a sentence about seeds not being substituted. It fits one case."""
    from scenariobank.sockets import explain_build_failure

    tail = ". The seed is not substituted."
    assert explain_build_failure(RuntimeError("nope"), "CC", 0, tail=tail).endswith(tail)
    assert not explain_build_failure(
        AssertionError("Lane number of previous block must be 1 in each direction"), "yP", 2,
        tail=tail,
    ).endswith(tail)


def test_the_parking_lot_assert_comes_back_as_the_condition_it_stands_for():
    """The raw assert names no block and no remedy, and reads as a bug rather than a rule."""
    from scenariobank.sockets import explain_build_failure

    said = explain_build_failure(
        AssertionError("Lane number of previous block must be 1 in each direction"), "yP", 2)
    assert "Lane number of previous block" not in said
    assert "parking lot" in said and "one lane in each direction" in said
    # The seed is still named: `yP` really does build at seeds 0, 1 and 4 and not at 2 and 3,
    # because one merge drops one or two lanes depending on the draw.
    assert "seed 2" in said
    assert "Two merges" in said
