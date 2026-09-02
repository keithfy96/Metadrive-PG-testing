"""Reviewing a bank, with no simulator anywhere near it.

Every test here builds rows by hand and calls pure functions. That is the property under test as
much as the numbers are: a review that needed an environment could not run in CI, could not run on
a machine with no MetaDrive, and could not answer while you were looking at the page.

The numbers are taken from real banks rather than invented, so a change to the measure has to
argue with the roads that motivated it.
"""

from __future__ import annotations

import pytest

from scenariobank.bank import CategoryEntry, Manifest, ScenarioRow
from scenariobank.handedness import DRIVE_SIDE_LEFT
from scenariobank.review import (
    DISTINCT,
    IDENTICAL,
    INCOMPARABLE,
    NEAR_DUPLICATE,
    NEAR_DUPLICATE_VERDICT,
    SAME_DRIVE,
    compare,
    find,
    gap,
    review,
    review_category,
    verdict,
)


def row(name, seed, *, dest="2C0_1_", lane=0, length=340.0, rotation=90.0, pairs=""):
    return ScenarioRow(
        scenario_id=name,
        seed=seed,
        destination=dest,
        spawn_lane_index=lane,
        route_length_m=length,
        net_rotation_deg=rotation,
        turn_pairs=pairs,
        thumbnail=f"thumbs/{name}.png",
    )


def entry(rows, *, block_seq="CC", rule="only", max_steps=1200, description="test"):
    return CategoryEntry(
        description=description,
        block_seq=block_seq,
        exit_rule=rule,
        max_steps=max_steps,
        scenarios=rows,
    )


# ------------------------------------------------------------------ the measure


def test_two_rows_measuring_the_same_are_identical():
    left = row("a", 0, lane=1)
    right = row("b", 3, lane=1)
    assert gap(left, right) == 0.0
    assert verdict(left, right) == IDENTICAL


def test_the_same_drive_from_a_different_lane_is_not_a_duplicate():
    """The weakest real difference, and the only one the `X` categories have.

    A policy meets lane 0 and lane 1 differently, so these are two scenarios -- but a bank built
    entirely on this distinction is one road wearing five names, and the verdict has to be able to
    say that without calling them identical.
    """
    left = row("a", 0, lane=0)
    right = row("b", 1, lane=1)
    assert gap(left, right) == 0.0
    assert verdict(left, right) == SAME_DRIVE


def test_curve_seeds_0_and_4_are_the_near_duplicate_the_threshold_was_set_for():
    # The real rows out of `banks/curve`: 452.06 m at +239.5 deg against 419.95 m at +225.45,
    # both LL. Visibly the same corner, which is what `NEAR_DUPLICATE` exists to catch.
    left = row("curve_0000", 0, length=452.06, rotation=239.5, pairs="LL")
    right = row("curve_0004", 4, length=419.95, rotation=225.45, pairs="LL")
    assert gap(left, right) == pytest.approx(0.071, abs=0.001)
    assert verdict(left, right) == NEAR_DUPLICATE_VERDICT


def test_mirrored_turn_pairs_are_distinct_however_close_the_lengths():
    """The case that makes the measure multi-field rather than a length comparison.

    `curve` seeds 1 and 3 are 5% apart in route length -- closer than seeds 0 and 4, which *are*
    near-duplicates -- but one drives left-then-right and the other right-then-left. A measure
    built on length would have flagged the wrong pair and missed the right one.
    """
    left = row("curve_0001", 1, length=340.13, rotation=81.3, pairs="LR")
    right = row("curve_0003", 3, length=323.13, rotation=67.5, pairs="RL")
    assert abs(left.route_length_m - right.route_length_m) / left.route_length_m < 0.06
    assert gap(left, right) == 1.0
    assert verdict(left, right) == DISTINCT


def test_a_different_exit_is_a_different_drive():
    left = row("a", 0, dest="1T0_1_", rotation=90.0)
    right = row("b", 2, dest="1T2_1_", rotation=-90.0)
    assert gap(left, right) == 1.0


def test_rotation_is_measured_against_a_full_turn_not_relatively():
    """A relative difference on an angle breaks near zero: +1 against -1 would read as 200%."""
    left = row("a", 0, rotation=1.0)
    right = row("b", 1, rotation=-1.0)
    assert gap(left, right) == pytest.approx(2.0 / 360.0)
    assert verdict(left, right) == NEAR_DUPLICATE_VERDICT


def test_a_long_way_round_is_distinct_by_rotation_alone():
    # Same length, same exit, same pairs, but one sweeps past a U-turn and the other does not.
    left = row("a", 0, rotation=20.0)
    right = row("b", 1, rotation=200.0)
    assert gap(left, right) == pytest.approx(0.5)
    assert gap(left, right) > NEAR_DUPLICATE


# ------------------------------------------------------------- the category report


def test_identical_rows_collapse_and_the_count_is_the_headline():
    # `banks/t-junction-left-intersection`, `intersection_left`: five seeds, two scenarios.
    rows = [
        row(f"intersection_left_000{i}", i, dest="1X0_1_", lane=lane, length=111.7, rotation=90.0)
        for i, lane in enumerate((0, 1, 0, 1, 1))
    ]
    report = review_category("intersection_left", entry(rows, block_seq="X", rule="left",
                                                        max_steps=320))

    assert (report.duplicates.total, report.duplicates.distinct) == (5, 2)
    assert report.duplicates.identical_groups == [
        ["intersection_left_0000", "intersection_left_0002"],
        ["intersection_left_0001", "intersection_left_0003", "intersection_left_0004"],
    ]
    # One route shared by all five, so one sentence rather than the ten pairs it decomposes into.
    assert report.duplicates.same_drive_groups == [[r.scenario_id for r in rows]]


def test_an_x_junction_says_the_lane_count_is_the_ceiling():
    """The sentence that turns a number into an instruction.

    "2 distinct of 5" invites you to swap seeds. It would not help: the road does not vary with the
    seed, so the lane count is the ceiling however many you build, and the report has to say so.
    """
    rows = [
        row(f"s{i}", i, dest="1X0_1_", lane=lane, length=111.7, rotation=90.0)
        for i, lane in enumerate((0, 1, 0, 1, 1))
    ]
    report = review_category("intersection_left", entry(rows, block_seq="X", max_steps=320))
    joined = " ".join(report.warnings)
    assert "2 distinct" in joined
    assert "do not vary with the seed" in joined
    assert "2 lane(s) is the ceiling" in joined


def test_a_category_that_really_does_vary_says_nothing_about_a_ceiling():
    # `t_junction` reaches four of five: two exits times two lanes. The ceiling sentence would be
    # wrong here, so the guard is on `distinct <= lanes` rather than on the block sequence alone.
    rows = [
        row("t0", 0, dest="1T0_1_", lane=0, length=111.7, rotation=90.0),
        row("t1", 1, dest="1T0_1_", lane=1, length=111.7, rotation=90.0),
        row("t2", 2, dest="1T2_1_", lane=0, length=117.2, rotation=-90.0),
        row("t3", 3, dest="1T2_1_", lane=1, length=117.2, rotation=-90.0),
        row("t4", 4, dest="1T0_1_", lane=1, length=111.7, rotation=90.0),
    ]
    report = review_category("t_junction", entry(rows, block_seq="T", rule="sharpest",
                                                 max_steps=320))
    assert report.duplicates.distinct == 4
    assert report.duplicates.identical_groups == [["t1", "t4"]]
    assert "ceiling" not in " ".join(report.warnings)
    assert "1 of them repeats" in " ".join(report.warnings)


def test_coverage_names_the_turn_pairs_that_are_missing():
    rows = [
        row("c0", 0, pairs="LL", length=452.1, rotation=239.5),
        row("c1", 1, pairs="LR", length=340.1, rotation=81.3),
    ]
    report = review_category("curve", entry(rows))
    assert report.coverage.turn_pairs_present == ["LL", "LR"]
    assert report.coverage.turn_pairs_missing == ["RL", "RR"]
    assert "no scenario drives RL, RR" in " ".join(report.warnings)


def test_a_sequence_with_no_curves_is_not_missing_turn_pairs():
    """`X` and `T` have no `Curve` blocks, so the question does not arise.

    Reporting all four as absent would be a warning about something that cannot be fixed.
    """
    rows = [row("x0", 0, dest="1X0_1_", pairs=""), row("x1", 1, dest="1X0_1_", lane=1, pairs="")]
    report = review_category("intersection_left", entry(rows, block_seq="X", max_steps=320))
    assert report.coverage.turn_pairs_present == []
    assert report.coverage.turn_pairs_missing == []
    assert not any("no scenario drives" in line for line in report.warnings)


def test_a_route_earning_more_steps_than_the_cap_is_the_first_warning():
    """The one statistic that is about correctness rather than variety.

    Over the cap means a policy runs out of steps before reaching the destination -- a scenario
    that cannot be passed. It is ordered first because everything else here is about quality.
    """
    # `curve`'s real cap. 200 m earns well under it; 4000 m earns several times it.
    rows = [row("c0", 0, length=200.0, pairs="LL"), row("c1", 1, length=4000.0, pairs="LR")]
    report = review_category("curve", entry(rows, max_steps=1200))

    assert report.budget.over_budget == ["c1"]
    assert report.budget.worst_scenario == "c1"
    assert report.budget.worst_earned > 1200
    assert "cap of 1200" in report.warnings[0]
    assert "run out of steps" in report.warnings[0]


def test_spread_flags_a_route_that_sweeps_past_a_u_turn():
    rows = [
        row("c0", 0, length=452.1, rotation=239.5, pairs="LL"),
        row("c1", 1, length=221.1, rotation=-140.8, pairs="RR"),
    ]
    report = review_category("curve", entry(rows))
    assert report.spread.past_a_u_turn == ["c0"]
    assert report.spread.route_length_min_m == 221.1
    assert report.spread.route_length_max_m == 452.1
    assert report.spread.route_length_median_m == pytest.approx(336.6)


def test_every_scenario_in_one_lane_is_worth_saying():
    rows = [row("a", 0, lane=2, pairs="LL"), row("b", 1, lane=2, length=200.0, pairs="RR")]
    report = review_category("curve", entry(rows))
    assert "every scenario spawns in lane 2" in " ".join(report.warnings)


def test_a_single_scenario_has_no_closest_pair():
    report = review_category("curve", entry([row("only", 0)]))
    assert report.duplicates.closest is None
    assert report.duplicates.distinct == 1


def test_an_empty_category_is_a_report_rather_than_a_crash():
    report = review_category("curve", entry([]))
    assert report.duplicates.total == 0
    assert report.warnings == ["curve holds no scenarios."]


# ------------------------------------------------------------------- the bank


def test_a_bank_totals_its_categories_without_comparing_across_them():
    """Two categories differ by declaration -- different road, different exit rule.

    A gap between them would be a number with nothing behind it, so the bank total is a sum of the
    per-category counts and never a comparison.
    """
    manifest = Manifest(
        schema_version="1.0",
        bank_id="mixed",
        created_utc="2026-09-02T00:00:00Z",
        metadrive={"edition": None, "dist_version": None, "commit": None, "asset_version": None},
        base_config={},
        drive_side=DRIVE_SIDE_LEFT,
        categories={
            "curve": entry(
                [row("c0", 0, pairs="LL"), row("c1", 1, length=200.0, pairs="RR")],
            ),
            "intersection_left": entry(
                [
                    row("x0", 0, dest="1X0_1_", lane=0, length=111.7),
                    row("x1", 1, dest="1X0_1_", lane=0, length=111.7),
                ],
                block_seq="X",
                rule="left",
                max_steps=320,
            ),
        },
    )
    report = review(manifest)

    assert report.bank_id == "mixed"
    assert (report.total, report.distinct) == (4, 3)
    assert [one.category for one in report.categories] == ["curve", "intersection_left"]


def test_reviewing_a_bank_never_imports_the_simulator(tmp_path):
    """Checked in a subprocess, because `sys.modules` is shared by the whole test session.

    This is the property the module is built around, not a nicety: a review that needed an
    environment could not run in CI, could not run on a machine with no MetaDrive, and could not
    answer while you were looking at the page. It is easy to lose to one convenient import, and
    nothing else here would notice.
    """
    import subprocess
    import sys

    from scenariobank.bank import write_manifest

    write_manifest(
        tmp_path,
        Manifest(
            schema_version="1.0",
            bank_id="b",
            created_utc="2026-09-02T00:00:00Z",
            metadrive={
                "edition": None, "dist_version": None, "commit": None, "asset_version": None,
            },
            base_config={},
            drive_side=DRIVE_SIDE_LEFT,
            categories={"curve": entry([row("c0", 0, pairs="LL")])},
        ),
    )
    done = subprocess.run(
        [
            sys.executable, "-c",
            "import sys;"
            "from scenariobank.bank import read_manifest;"
            "from scenariobank.review import review;"
            f"review(read_manifest({str(tmp_path)!r}));"
            "print([m for m in sys.modules if m.startswith('metadrive')])",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert done.stdout.strip() == "[]"


# ------------------------------------------------------- two scenarios, side by side


def two_categories():
    """A bank holding the two `curve` seeds that are near-duplicates and one `X` scenario."""
    return Manifest(
        schema_version="1.0",
        bank_id="pairs",
        created_utc="2026-09-02T00:00:00Z",
        metadrive={"edition": None, "dist_version": None, "commit": None, "asset_version": None},
        base_config={},
        drive_side=DRIVE_SIDE_LEFT,
        categories={
            "curve": entry(
                [
                    row("curve_0000", 0, length=452.06, rotation=239.5, pairs="LL"),
                    row("curve_0004", 4, length=419.95, rotation=225.45, pairs="LL"),
                    # Seeds 1 and 3: 5% apart in length and mirrored, the pair a length-only
                    # measure would have called the near-duplicate.
                    row("curve_0001", 1, length=336.9, rotation=81.0, pairs="LR"),
                    row("curve_0003", 3, length=354.7, rotation=-81.0, pairs="RL"),
                ],
            ),
            "intersection_left": entry(
                [row("intersection_left_0000", 0, dest="1X0_1_", lane=1, length=111.7)],
                block_seq="X",
                rule="left",
                max_steps=320,
            ),
        },
    )


def test_a_scenario_is_found_by_its_id_rather_than_by_parsing_it():
    """The id starts with its category by convention, and a convention is not a lookup."""
    name, found_entry, found_row = find(two_categories(), "curve_0004")
    assert (name, found_entry.block_seq, found_row.seed) == ("curve", "CC", 4)
    assert find(two_categories(), "curve_0099") is None


def test_the_comparison_carries_the_reviews_own_verdict_and_gap():
    answer = compare(two_categories(), "curve_0000", "curve_0004")
    assert answer.category == "curve"
    assert answer.verdict == NEAR_DUPLICATE_VERDICT
    assert answer.gap == pytest.approx(0.071, abs=0.001)
    assert "same picture" in answer.summary


def test_the_mirrored_pair_says_which_field_made_it_100_percent():
    """The sentence has to name the reason, or `100% apart` on two 5%-apart routes reads as a bug.

    This is the pair that motivated the multi-field measure: `curve` seeds 1 and 3 are closer in
    length than the real near-duplicates and drive opposite shapes.
    """
    answer = compare(two_categories(), "curve_0001", "curve_0003")
    assert answer.verdict == DISTINCT
    assert answer.gap == 1.0
    assert "LR" in answer.summary and "RL" in answer.summary
    apart = {one.field: one for one in answer.fields}
    assert apart["turn pairs"].same is False
    # And the fields still report the truth the verdict flattened: they *are* close in length.
    assert apart["route length"].apart.endswith("5%")


def test_two_categories_are_compared_without_being_scored():
    """Answered rather than refused. Clicking two cards is a fair thing to do, and "there is no
    number here, and here is why" beats an error a person cannot act on."""
    answer = compare(two_categories(), "curve_0000", "intersection_left_0000")
    assert answer.verdict == INCOMPARABLE
    assert answer.gap is None
    assert answer.category is None
    assert "declaration" in answer.summary
    fields = [one.field for one in answer.fields]
    # The three that are the category itself appear only when they differ.
    assert fields[:3] == ["scenario type", "road", "exit rule"]
    assert [one for one in answer.fields if one.field == "road"][0].same is False


def test_one_category_hides_the_rows_that_would_always_agree():
    fields = [one.field for one in compare(two_categories(), "curve_0000", "curve_0004").fields]
    assert "road" not in fields and "scenario type" not in fields
    assert fields[0] == "seed"


def test_the_step_budget_is_reported_per_scenario_against_its_cap():
    answer = compare(two_categories(), "curve_0000", "curve_0004")
    budget = [one for one in answer.fields if one.field == "step budget"][0]
    # 452 m earns more than 420 m does, and both are inside `curve`'s cap of 1200.
    assert budget.left == "1140 / 1200"
    assert budget.right == "1060 / 1200"


def test_the_review_reports_what_each_route_earns_so_the_page_need_not_round():
    one = review_category("curve", two_categories().categories["curve"])
    assert one.budget.earned["curve_0000"] == 1140
    assert one.budget.worst_earned == max(one.budget.earned.values())


def test_a_scenario_the_bank_does_not_hold_is_a_lookup_failure():
    with pytest.raises(LookupError, match="curve_0099"):
        compare(two_categories(), "curve_0000", "curve_0099")


def test_a_scenario_compared_with_itself_is_refused():
    """Not a comparison, and answering `0% apart, identical` would be a true statement that
    misleads: it reads as two scenarios that match."""
    with pytest.raises(ValueError, match="itself"):
        compare(two_categories(), "curve_0000", "curve_0000")


def test_identical_rows_say_that_no_seed_change_will_separate_them():
    manifest = two_categories()
    manifest.categories["intersection_left"].scenarios.append(
        row("intersection_left_0002", 2, dest="1X0_1_", lane=1, length=111.7),
    )
    answer = compare(manifest, "intersection_left_0000", "intersection_left_0002")
    assert answer.verdict == IDENTICAL
    assert answer.gap == 0.0
    assert all(one.same for one in answer.fields if one.field != "seed")


def test_a_lane_only_pair_names_both_lanes():
    manifest = two_categories()
    manifest.categories["intersection_left"].scenarios.append(
        row("intersection_left_0001", 1, dest="1X0_1_", lane=0, length=111.7),
    )
    answer = compare(manifest, "intersection_left_0000", "intersection_left_0001")
    assert answer.verdict == SAME_DRIVE
    assert "lane 1" in answer.summary and "lane 0" in answer.summary
