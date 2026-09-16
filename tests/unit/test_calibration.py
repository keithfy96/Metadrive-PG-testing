"""Phase 4b's sweep, checked without a simulator, and the gate that `LEVELS` was measured.

The measured page and its JSON records are checked in the way `destinations.md` is. What is
testable here is the fold from `Results` to a `Point`, the four-value pick, the refusals that
come before any env is built, and the two drift checks that make "done when" mechanical: the
checked-in page is the render of the checked-in records, and every number in `LEVELS` for a
swept axis is a value some record ran.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scenariobank import calibration
from scenariobank.calibration import (
    CALIBRATION_DIR,
    CALIBRATION_DOC,
    SWEEP_SCHEMA_VERSION,
    CalibrationError,
    Point,
    Sweep,
    check_values,
    held_levels,
    job_for,
    levels_match,
    load_records,
    point_for,
    render,
    suggest,
    sweep,
    write,
)
from scenariobank.cli import _parse_values
from scenariobank.options import AXES, LEVEL_NAMES, LEVELS, NUMERIC_AXES, nearest_level
from scenariobank.results import (
    RESULTS_SCHEMA_VERSION,
    BankInfo,
    EnvInfo,
    Results,
    ScenarioResult,
    summarize,
)

LEFT = Path("banks/t-junction-left-intersection")


def needs_bank(bank: Path):
    return pytest.mark.skipif(
        not (bank / "manifest.json").exists(), reason=f"needs the bank at {bank}"
    )


def _row(
    scenario_id: str, *, success: bool, reason: str | None, steps: int, **collisions: int
) -> ScenarioResult:
    counts = {"vehicle": 0, "object": 0, "building": 0, "human": 0, "sidewalk": 0}
    counts.update(collisions)
    return ScenarioResult(
        scenario_id=scenario_id,
        category="t_junction",
        seed=0,
        status="ok",
        success=success,
        failure_reason=reason,
        steps=steps,
        actions=steps,
        reward=10.0 if success else -5.0,
        cost=0.0,
        wall_time_s=1.5,
        collisions=counts,
        placed={"DefaultVehicle": 1, "Pedestrian": 2},
    )


def _results(rows: list[ScenarioResult]) -> Results:
    return Results(
        schema_version=RESULTS_SCHEMA_VERSION,
        started_utc="2026-09-13T00:00:00Z",
        finished_utc="2026-09-13T00:00:10Z",
        bank=BankInfo(path="banks/x", id="x", source="pg", schema_version="1.3"),
        policy=calibration.EXPERT,
        options={"kind": "pg"},
        env=EnvInfo(
            observation_shape_before=(19,),
            observation_shape_after=(19,),
            step_hz=10.0,
            decision_hz=None,
            stride=1,
        ),
        results=rows,
        summary=summarize(rows),
    )


def _point(value: float, rate: float, n: int = 5) -> Point:
    successes = round(rate * n)
    return Point(
        value=value,
        n=n,
        successes=successes,
        success_rate=successes / n,
        steps_mean=100.0,
        reward_mean=1.0,
        wall_time_s=1.0,
        out="out",
    )


# --- the fold -----------------------------------------------------------------------------------


def test_a_point_sums_collisions_and_placed_over_the_rows_and_means_the_rest(tmp_path):
    rows = [
        _row("a", success=True, reason=None, steps=100),
        _row("b", success=False, reason="crash_vehicle", steps=50, vehicle=1),
        _row("c", success=False, reason="crash_vehicle", steps=60, vehicle=2, human=1),
    ]
    point = point_for(0.25, _results(rows), tmp_path)
    assert (point.n, point.successes, point.success_rate) == (3, 1, pytest.approx(1 / 3))
    assert point.by_failure_reason == {"crash_vehicle": 2}
    assert point.collisions == {"building": 0, "human": 1, "object": 0, "sidewalk": 0, "vehicle": 3}
    assert point.placed == {"DefaultVehicle": 3, "Pedestrian": 6}
    assert point.steps_mean == 70.0
    assert point.wall_time_s == 4.5
    assert point.out == str(tmp_path)


# --- the pick ---------------------------------------------------------------------------------


def test_suggest_spreads_four_values_across_the_drop():
    rates = [(0, 1.0), (0.05, 0.8), (0.1, 0.6), (0.2, 0.4), (0.3, 0.2), (0.4, 0.2)]
    points = [_point(v, r) for v, r in rates]
    # A third and two thirds of the way down from 1.0 to 0.2 are 0.73 and 0.47: the nearest
    # rates are 0.8 (at 0.05) and 0.4 (at 0.2), so the four read 1.0, 0.8, 0.4, 0.2.
    assert suggest(points) == {"none": 0.0, "low": 0.05, "medium": 0.2, "high": 0.3}


def test_suggest_takes_the_smallest_value_at_the_floor_and_keeps_the_picks_in_order():
    points = [_point(v, r) for v, r in [(0, 1.0), (1, 0.6), (2, 0.6), (3, 0.0), (4, 0.0)]]
    assert suggest(points) == {"none": 0.0, "low": 1.0, "medium": 2.0, "high": 3.0}


@pytest.mark.parametrize(
    "rates",
    [
        [(0, 1.0), (1, 1.0), (2, 1.0), (3, 1.0)],  # nothing moves: cones on an X
        [(0, 1.0), (1, 0.0), (2, 0.0), (3, 0.0)],  # one value between 0 and the floor
        [(0, 1.0), (1, 0.5), (2, 0.0)],  # fewer than four values
        [(1, 1.0), (2, 0.5), (3, 0.2), (4, 0.0)],  # no 0
    ],
)
def test_suggest_says_none_when_the_sweep_cannot_be_spread(rates):
    assert suggest([_point(v, r) for v, r in rates]) is None


# --- the refusals, before any env -----------------------------------------------------------


@needs_bank(LEFT)
def test_bad_values_are_refused_before_any_run(tmp_path):
    from scenariobank.bank import read_manifest

    manifest = read_manifest(LEFT)
    with pytest.raises(CalibrationError, match="traffic=0.005.*below 0.01"):
        check_values(manifest, "traffic", [0, 0.005])
    with pytest.raises(CalibrationError, match="cones=1.5.*whole number"):
        check_values(manifest, "cones", [0, 1.5])
    with pytest.raises(CalibrationError, match="lights.*schedule"):
        check_values(manifest, "lights", [0, 1])
    with pytest.raises(CalibrationError, match="repeats"):
        check_values(manifest, "traffic", [0, 0.1, 0.1])
    with pytest.raises(CalibrationError, match="no values"):
        check_values(manifest, "traffic", [])

    def never(*_args, **_kwargs):
        raise AssertionError("a bad value reached the runner")

    with pytest.raises(CalibrationError):
        sweep(LEFT, "traffic", [0, 0.005], out=tmp_path, run=never)
    with pytest.raises(CalibrationError, match="no category named nope"):
        sweep(LEFT, "traffic", [0, 0.1], categories=["nope"], out=tmp_path, run=never)


@needs_bank(LEFT)
def test_every_other_axis_is_held_at_none_and_the_swept_one_travels_raw():
    from scenariobank.bank import read_manifest

    manifest = read_manifest(LEFT)
    job = job_for(manifest, LEFT, "pedestrians", 3, scenarios=["intersection_left_0000"])
    assert job.options.tier is None
    assert job.options.levels == held_levels("pedestrians")
    assert set(job.options.levels) == set(AXES) - {"pedestrians"}
    assert set(job.options.levels.values()) == {"none"}
    assert job.options.raw == {"pedestrians": 3.0}
    assert job.bank.id == manifest.bank_id
    assert job.policy == calibration.EXPERT


@needs_bank(LEFT)
def test_a_sweep_runs_once_per_value_in_order_and_reports_as_it_goes(tmp_path, monkeypatch):
    monkeypatch.setattr(calibration, "_utc_now", lambda: "2026-09-13T00:00:00Z")
    seen: list[tuple[float, Path]] = []

    def fake_run(job, out, *, progress):
        seen.append((job.options.raw["traffic"], out))
        progress(f"row {job.scenarios[0]}")
        rows = [_row(sid, success=job.options.raw["traffic"] < 0.2, reason=None, steps=10)
                for sid in job.scenarios]
        return _results(rows)

    said: list[str] = []
    record = sweep(
        LEFT, "traffic", [0, 0.1, 0.2], categories=["t_junction"],
        out=tmp_path, progress=said.append, run=fake_run,
    )
    assert [value for value, _ in seen] == [0.0, 0.1, 0.2]
    assert [out for _, out in seen] == [
        tmp_path / "traffic" / "t-junction-left-intersection" / f"traffic={v}"
        for v in ("0", "0.1", "0.2")
    ]
    assert record.categories == ["t_junction"]
    assert len(record.scenarios) == 4 and all(s.startswith("t_junction") for s in record.scenarios)
    assert [p.success_rate for p in record.points] == [1.0, 1.0, 0.0]
    assert record.held == held_levels("traffic")
    assert record.schema_version == SWEEP_SCHEMA_VERSION
    assert said[0].startswith("[1/3] traffic=0  4 scenarios -> ")
    assert "row t_junction_0000" in said
    assert any(line.strip().startswith("traffic=0.2  success 0/4 = 0.00") for line in said)


# --- the record and the page -----------------------------------------------------------------


def _sweep(axis: str, bank_id: str, points: list[Point]) -> Sweep:
    return Sweep(
        schema_version=SWEEP_SCHEMA_VERSION,
        axis=axis,
        bank_id=bank_id,
        bank_path=f"banks/{bank_id}",
        categories=["c"],
        scenarios=["c_0000"],
        policy=calibration.EXPERT,
        held=held_levels(axis),
        measured_utc="2026-09-13T00:00:00Z",
        metadrive_commit="85e5dadc6c7436d324348f6e3d8f8e680c06b4db",
        points=points,
    )


def test_write_records_the_sweep_and_renders_the_page_from_every_record(tmp_path):
    records = tmp_path / "records"
    page = tmp_path / "page.md"
    first = _sweep(
        "cones", "curve", [_point(0, 1.0), _point(1, 0.8), _point(3, 0.4), _point(6, 0.0)]
    )
    second = _sweep(
        "traffic",
        "left",
        [_point(v, r) for v, r in [(0, 1.0), (0.05, 0.8), (0.15, 0.4), (0.35, 0.0)]],
    )
    path, doc = write(records, page, first)
    assert path == records / "cones.curve.json"
    assert json.loads(path.read_text())["axis"] == "cones"
    write(records, page, second)
    loaded = load_records(records)
    assert [(r.axis, r.bank_id) for r in loaded] == [("traffic", "left"), ("cones", "curve")]
    text = doc.read_text()
    assert text == render(loaded)
    assert text.index("## traffic") < text.index("## cones"), "axes in AXES order"
    assert "### cones on `curve`" in text
    assert f"| 3 | `{nearest_level('cones', 3)}` | 2/5 | 0.40 |" in text, "looked up at render"
    assert "Spread the rate suggests: `none` 0, `low` 1, `medium` 3, `high` 6." in text


def test_a_record_that_is_not_a_sweep_is_named(tmp_path):
    (tmp_path / "traffic.x.json").write_text("{}")
    with pytest.raises(CalibrationError, match="traffic.x.json is not a sweep record"):
        load_records(tmp_path)


def test_levels_match_names_the_level_whose_number_no_sweep_ran():
    table = [float(LEVELS["cones"][name]) for name in LEVEL_NAMES]
    swept = _sweep("cones", "curve", [_point(v, 1.0 - i / 3) for i, v in enumerate(table)])
    assert levels_match([swept]) == {}
    off = _sweep(
        "cones", "curve", [_point(0, 1.0), _point(77, 0.0), _point(99, 0.0), _point(111, 0.0)]
    )
    assert levels_match([off]) == {"cones": ["low", "medium", "high"]}
    assert levels_match([]) == {}


def test_render_only_rewrites_the_page_from_the_records_and_drives_nothing(tmp_path):
    from typer.testing import CliRunner

    from scenariobank.cli import app

    records = tmp_path / "records"
    page = tmp_path / "page.md"
    write(records, page, _sweep("cones", "curve", [_point(0, 1.0), _point(6, 0.0)]))
    page.write_text("stale")
    result = CliRunner().invoke(
        app, ["calibrate", "--render-only", "--record", str(records), "--doc", str(page)]
    )
    assert result.exit_code == 0, result.output
    assert page.read_text() == render(load_records(records))
    assert "reference written" in result.output and "1 records" in result.output
    assert "options.LEVELS['cones'] has" in result.output, "1 and 4 were never swept here"
    refused = CliRunner().invoke(app, ["calibrate", "--render-only", "--axis", "cones"])
    assert refused.exit_code != 0 and "drop --axis" in refused.output
    missing = CliRunner().invoke(app, ["calibrate", "--axis", "cones", "--values", "0,1"])
    assert missing.exit_code != 0 and "--bank is required" in missing.output


def test_parse_values_takes_comma_lists_and_repeats():
    import typer

    assert _parse_values(["0,0.05", "0.1", " 0.2, "]) == [0.0, 0.05, 0.1, 0.2]
    with pytest.raises(typer.BadParameter, match="numbers"):
        _parse_values(["0,x"])


# --- the gate: the checked-in page and table were measured ------------------------------------


def test_the_checked_in_page_is_the_render_of_the_checked_in_records():
    records = load_records(CALIBRATION_DIR) if CALIBRATION_DIR.exists() else []
    assert CALIBRATION_DOC.read_text() == render(records), (
        f"{CALIBRATION_DOC} is out of date with {CALIBRATION_DIR}; run a sweep, or "
        "re-render it"
    )


def test_every_level_of_a_swept_axis_is_a_value_some_sweep_ran():
    # Phase 4b's "done when": the numbers in options.py came from the tables, not from a guess.
    records = load_records(CALIBRATION_DIR) if CALIBRATION_DIR.exists() else []
    assert levels_match(records) == {}


def test_every_numeric_axis_has_been_swept():
    records = load_records(CALIBRATION_DIR) if CALIBRATION_DIR.exists() else []
    assert {record.axis for record in records} >= set(NUMERIC_AXES)


def test_every_swept_axis_separates_its_levels_somewhere():
    # Four levels whose success rates sit apart, on at least one bank per axis: read off the
    # record, the rates from none to high never rise, and high is below none. Never-rise rather
    # than strictly-fall because of barriers: one barrier scene already holds the expert to
    # 0.20 on `curve` and no count above it can fall further than 0.00, so that axis has one
    # real step in it and the page says so. A flat axis -- high scoring what none scores -- is
    # a broken calibration, not a calibrated one, and fails here.
    records = load_records(CALIBRATION_DIR) if CALIBRATION_DIR.exists() else []
    for axis in NUMERIC_AXES:
        separated = []
        for record in records:
            if record.axis != axis:
                continue
            by_value = {point.value: point.success_rate for point in record.points}
            rates = [by_value.get(float(LEVELS[axis][name])) for name in LEVEL_NAMES]
            if None in rates:
                continue
            never_rise = all(a >= b for a, b in zip(rates, rates[1:], strict=False))
            separated.append(never_rise and rates[-1] < rates[0])
        assert any(separated), f"{axis}: no record separates the four levels"
