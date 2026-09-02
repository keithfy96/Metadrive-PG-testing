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
    NEAR_DUPLICATE,
    NEAR_DUPLICATE_VERDICT,
    SAME_DRIVE,
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
