"""The estimate: a median of what was delivered, narrowed to the rig and the difficulty when it
can be, and the calibration sweeps as the expert's bootstrap before anything was.

Built on the store test's helpers, so every sample here came through `ingest` from a tree the
way a delivered result would, and never from a hand-typed row.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scenariobank.calibration import EXPERT, Point, Sweep
from scenariobank.web.eta import (
    ENOUGH,
    LAST,
    WALL_TIMES,
    Estimate,
    Measurement,
    estimate,
    load_bootstrap,
    load_measurements,
)
from scenariobank.web.results import ResultsStore
from tests.unit.test_results_store import deliver, record, scored

AV3 = "scenariobank.av3:AV3Policy"


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "share" / "results"
    root.mkdir(parents=True)
    return root


@pytest.fixture
def store(tmp_path: Path, tree: Path) -> ResultsStore:
    return ResultsStore(tmp_path / ".studio" / "results.sqlite", tree)


def _job(tree, name, times, *, host=None, levels=None, policy=EXPERT, category="curve",
         when=None):
    rows = [
        scored(f"{category}_{index:04d}", category=category, wall_time_s=seconds)
        for index, seconds in enumerate(times)
    ]
    deliver(tree, name, record(name, rows, policy=policy, levels=levels), host=host)
    if when is not None:
        os.utime(tree / name, (when, when))


def _sweep(axis, categories, points, *, bank_id="curve"):
    return Sweep(
        schema_version=1,
        axis=axis,
        bank_id=bank_id,
        bank_path="banks/" + bank_id,
        categories=list(categories),
        scenarios=[f"{categories[0]}_0000"],
        policy=EXPERT,
        held={},
        measured_utc="2026-09-12T00:00:00Z",
        points=[
            Point(
                value=value, n=n, successes=n, success_rate=1.0, steps_mean=300.0,
                reward_mean=1.0, wall_time_s=wall, out="out/x",
            )
            for value, n, wall in points
        ],
    )


def test_nothing_delivered_and_not_the_expert_is_an_honest_none(store):
    answer = estimate(store, policy=AV3, categories={"curve": 5, "t_junction": 5})
    assert answer.seconds is None and answer.complete is False
    assert answer.missing == ["curve", "t_junction"] and answer.scenarios == 10
    assert "no scored run of " + AV3 in answer.per_category["curve"]["note"]
    assert "nothing measured stands in for one" in answer.per_category["curve"]["note"]


def test_the_median_of_the_newest_rows_and_not_the_mean(store, tree):
    # One degraded run is a 5x outlier: a mean would carry it for weeks, a median does not.
    _job(tree, "j1", [2.0, 2.0, 2.0, 2.0, 10.0])
    store.ingest()
    answer = estimate(store, policy=EXPERT, categories={"curve": 10})
    guess = answer.per_category["curve"]
    assert (guess["seconds_each"], guess["source"], guess["n"]) == (2.0, "rows", 5)
    assert guess["host"] is None and guess["levels_matched"] is False
    assert answer.seconds == 20.0 and answer.complete is True and answer.missing == []


def test_only_the_last_n_rows_count(store, tree):
    _job(tree, "old", [100.0] * 30, when=1_700_000_000)
    _job(tree, "new", [1.0] * LAST, when=1_700_100_000)
    store.ingest()
    guess = estimate(store, policy=EXPERT, categories={"curve": 1}).per_category["curve"]
    assert guess["seconds_each"] == 1.0 and guess["n"] == LAST


def test_narrowed_to_this_rig_at_these_levels_when_there_are_enough(store, tree):
    hard = {"traffic": "high"}
    _job(tree, "a-hard", [8.0] * ENOUGH, host="rig-a", levels=hard)
    _job(tree, "a-easy", [1.0] * ENOUGH, host="rig-a", levels={})
    _job(tree, "b-hard", [4.0] * ENOUGH, host="rig-b", levels=hard)
    store.ingest()

    def each(**kwargs):
        return estimate(store, policy=EXPERT, categories={"curve": 1}, **kwargs).per_category[
            "curve"
        ]

    this_rig_hard = each(host="rig-a", levels=hard)
    assert (this_rig_hard["seconds_each"], this_rig_hard["host"]) == (8.0, "rig-a")
    assert this_rig_hard["levels_matched"] is True and this_rig_hard["note"] is None
    # The other rig's number is the other rig's.
    assert each(host="rig-b", levels=hard)["seconds_each"] == 4.0
    # No rig named: over both rigs at this difficulty.
    both = each(levels=hard)
    assert both["seconds_each"] == 6.0 and both["host"] is None and both["levels_matched"]
    # No levels named: everything under this policy on this rig.
    assert each(host="rig-a")["seconds_each"] == pytest.approx(4.5)


def test_too_few_samples_in_the_narrow_subset_widens_and_says_so(store, tree):
    hard = {"traffic": "high"}
    _job(tree, "a-hard", [50.0], host="rig-a", levels=hard)     # one run: a median of one
    _job(tree, "a-easy", [1.0] * 5, host="rig-a", levels={})
    store.ingest()
    guess = estimate(
        store, policy=EXPERT, categories={"curve": 1}, host="rig-a", levels=hard
    ).per_category["curve"]
    # Widened to this rig at any levels rather than trusting the single hard run.
    assert guess["host"] == "rig-a" and guess["levels_matched"] is False
    assert guess["seconds_each"] == 1.0 and guess["note"] == "at other option levels"

    # A rig nobody has delivered from widens all the way, and the answer says the host is
    # not that rig's own.
    unknown = estimate(
        store, policy=EXPERT, categories={"curve": 1}, host="rig-c"
    ).per_category["curve"]
    assert unknown["host"] is None and unknown["source"] == "rows"


def test_the_last_subset_is_used_however_few_it_holds(store, tree):
    _job(tree, "only", [7.0], host="rig-a")
    store.ingest()
    guess = estimate(store, policy=EXPERT, categories={"curve": 1}).per_category["curve"]
    assert guess["seconds_each"] == 7.0 and guess["n"] == 1


def test_the_calibration_bootstraps_the_expert_and_nobody_else(store):
    records = [
        _sweep("traffic", ["curve"], [(0.0, 5, 5.0), (0.5, 5, 40.0)]),
        _sweep("cones", ["curve"], [(0, 5, 6.0), (4, 5, 7.0)]),
        _sweep("traffic", ["intersection_left", "t_junction"], [(0.0, 9, 9.0)],
               bank_id="t-junction-left-intersection"),
    ]
    none = estimate(store, policy=EXPERT, categories={"curve": 2}, records=records)
    guess = none.per_category["curve"]
    # Every axis at none: the slowest sweep's base, 6.0 / 5.
    assert guess == {
        "count": 2, "seconds_each": 1.2, "source": "calibration", "n": 2, "host": None,
        "levels_matched": True,
        "note": "the expert on the calibration machine, one axis at a time",
    }
    assert none.seconds == 2.4 and none.complete

    # Traffic high and cones medium: the slowest axis, not the sum -- 40/5 beats 7/5.
    hard = estimate(
        store, policy=EXPERT, categories={"curve": 1},
        levels={"traffic": "high", "cones": "medium"}, records=records,
    ).per_category["curve"]
    assert hard["seconds_each"] == 8.0 and hard["levels_matched"] is True

    # A level no sweep ran on this category leaves that axis out and says which.
    partial = estimate(
        store, policy=EXPERT, categories={"curve": 1}, levels={"cones": "high"},
        records=records,
    ).per_category["curve"]
    assert partial["seconds_each"] == 1.0 and partial["levels_matched"] is False
    assert partial["note"].endswith("; no sweep ran cones=high on curve")

    # A category a sweep covered among others: 9 s over 9 scenarios.
    t = estimate(store, policy=EXPERT, categories={"t_junction": 3}, records=records)
    assert t.per_category["t_junction"]["seconds_each"] == 1.0 and t.seconds == 3.0

    # A camera model is not the expert, and the sweeps say nothing about its speed.
    av3 = estimate(store, policy=AV3, categories={"curve": 1}, records=records)
    assert av3.seconds is None and av3.per_category["curve"]["source"] is None


def test_delivered_rows_replace_the_bootstrap_as_they_arrive(store, tree):
    records = [_sweep("traffic", ["curve"], [(0.0, 5, 5.0)])]
    before = estimate(store, policy=EXPERT, categories={"curve": 1}, records=records)
    assert before.per_category["curve"]["source"] == "calibration"
    _job(tree, "real", [30.0], host="sim")
    store.ingest()
    after = estimate(store, policy=EXPERT, categories={"curve": 1}, records=records)
    assert after.per_category["curve"] == {
        "count": 1, "seconds_each": 30.0, "source": "rows", "n": 1, "host": None,
        "levels_matched": False, "note": None,
    }


def test_a_partial_answer_is_a_floor_and_names_what_it_leaves_out(store, tree):
    _job(tree, "curves", [2.0, 4.0, 6.0], category="curve")
    store.ingest()
    answer = estimate(store, policy=EXPERT, categories={"curve": 5, "roundabout": 5})
    assert answer.seconds == 20.0 and answer.complete is False
    assert answer.missing == ["roundabout"]
    assert answer.per_category["roundabout"]["seconds_each"] is None
    assert isinstance(answer.as_dict(), dict) and answer.as_dict()["missing"] == ["roundabout"]
    assert isinstance(answer, Estimate)


def test_the_bootstrap_is_the_checked_in_records_and_a_missing_directory_is_no_bootstrap(
    tmp_path,
):
    from scenariobank.calibration import CALIBRATION_DIR

    records = load_bootstrap(Path.cwd() / CALIBRATION_DIR)
    assert {record.axis for record in records} >= {"traffic", "cones", "barriers"}
    assert load_bootstrap(tmp_path / "nowhere") == []
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "traffic.curve.json").write_text(json.dumps({"not": "a sweep"}))
    assert load_bootstrap(broken) == []


def _measured(policy, host, times, *, default=False):
    return Measurement(
        policy=policy, host=host, default=default, wall_time_s=list(times),
        scenario="t_junction_0000", simulated_s=32.0, measured_utc="2026-09-12",
        source="test",
    )


def test_the_measured_wall_times_stand_in_for_the_camera_model_per_rig(store):
    measurements = [
        _measured(AV3, "sim", [100.0, 102.2], default=True),
        _measured(AV3, "laptop", [912.5]),
    ]

    def each(**kwargs):
        return estimate(
            store, policy=AV3, categories={"curve": 35}, measurements=measurements, **kwargs
        )

    # The rig not yet known: the default entries, the rigs', and never the laptop's.
    unknown = each()
    guess = unknown.per_category["curve"]
    assert (guess["seconds_each"], guess["source"], guess["n"], guess["host"]) == (
        101.1, "measured", 2, None
    )
    assert unknown.seconds == pytest.approx(35 * 101.1) and unknown.complete
    assert guess["note"] == (
        "measured on t_junction_0000 on sim (2026-09-12); other roads scale with their length"
    )
    # This laptop's own figure when the laptop is the host, and the default when the host has
    # no figure of its own -- with `host` unset in the answer, so the reader knows it is not.
    assert each(host="laptop").per_category["curve"]["seconds_each"] == 912.5
    assert each(host="laptop").per_category["curve"]["host"] == "laptop"
    other = each(host="rig-b").per_category["curve"]
    assert other["seconds_each"] == 101.1 and other["host"] is None
    # Another policy has nothing measured.
    assert estimate(
        store, policy="x:Other", categories={"curve": 1}, measurements=measurements
    ).seconds is None
    # And the expert is bootstrapped by the sweeps, the measurements being second to them.
    both = estimate(
        store, policy=EXPERT, categories={"curve": 1},
        records=[_sweep("traffic", ["curve"], [(0.0, 5, 5.0)])],
        measurements=[_measured(EXPERT, "sim", [50.0], default=True)],
    ).per_category["curve"]
    assert both["source"] == "calibration" and both["seconds_each"] == 1.0


def test_delivered_rows_replace_the_measured_figure(store, tree):
    measurements = [_measured(AV3, "sim", [100.0], default=True)]
    _job(tree, "first-av3", [60.0, 70.0, 80.0], host="sim", policy=AV3)
    store.ingest()
    guess = estimate(
        store, policy=AV3, categories={"curve": 1}, host="sim", measurements=measurements
    ).per_category["curve"]
    assert guess == {
        "count": 1, "seconds_each": 70.0, "source": "rows", "n": 3, "host": "sim",
        "levels_matched": False, "note": None,
    }


def test_the_checked_in_wall_times_are_the_camera_model_on_the_rig_by_default(tmp_path):
    measured = load_measurements(Path.cwd() / WALL_TIMES)
    assert measured, "docs/reference/wall-times.json must load"
    defaults = [m for m in measured if m.default]
    assert defaults and all(m.policy == AV3 for m in defaults)
    assert all(m.host != "keith-82y7" for m in defaults), "a laptop is never the default"
    for entry in measured:
        assert entry.source and entry.measured_utc and entry.scenario
    # A missing or malformed file is no bootstrap rather than a failure.
    assert load_measurements(tmp_path / "nowhere.json") == []
    (tmp_path / "bad.json").write_text(json.dumps({"schema_version": 1, "measurements": [{}]}))
    assert load_measurements(tmp_path / "bad.json") == []
