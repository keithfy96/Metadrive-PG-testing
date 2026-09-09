"""Phase 4 Step 5, the gate: two runs of one job are one run, and the options do something.

The first claim failed when it was first measured, on `banks/curve` at `hard`, and it failed
twice over. A row scored differently after other rows than alone in the same env -- 339 steps by
itself, 218 after one row, 300 after three -- and the same row alone scored differently in two
processes whose environment blocks differed in size, because MetaDrive's lidar hands the IDM
policy a `set` of objects ordered by address and a cone corridor ties on longitude. `env.py`
pins the order and `runner.py` builds one env per row; the two live tests here are those two
measurements, kept as tests, and the third is the second claim: `hard` below `easy` on the bank
where cones and barriers bite, and only slower on the `X` bank where nothing can be placed and
the expert still arrives.

The two carriers are pinned separately because they were found separately: the sort key is
pure and offline, the lidar every env gets is checked once against the simulator, and the row
comparison runs the row alone, in company, and in another process with a different
environment block -- the one thing that moved it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from scenariobank.doctor import has_simulator
from scenariobank.env import object_order
from scenariobank.results import JOB_SCHEMA_VERSION, Job, JobBank, JobOptions, Results
from scenariobank.runner import run_bank

needs_sim = pytest.mark.skipif(
    not has_simulator(),
    reason="needs_sim: MetaDrive is not installed (uv sync --group sim)",
)

CURVE = Path("banks/curve")
LEFT = Path("banks/t-junction-left-intersection")
EXPERT = "scenariobank.policies:ExpertPolicy"


def needs_bank(bank: Path):
    return pytest.mark.skipif(
        not (bank / "manifest.json").exists(), reason=f"needs the bank at {bank}"
    )


def job_for(bank: Path, scenarios: list[str], tier: str) -> Job:
    return Job(
        schema_version=JOB_SCHEMA_VERSION,
        bank=JobBank(path=str(bank)),
        scenarios=scenarios,
        options=JobOptions(tier=tier),
        policy=EXPERT,
    )


def rows(report: Results) -> dict[str, dict]:
    """Every row, whole, minus the one field that is a clock."""
    return {r.scenario_id: r.model_dump(exclude={"wall_time_s"}) for r in report.results}


# --- offline: the order ------------------------------------------------------------------------


class Thing:
    def __init__(self, x: float, y: float, heading: float = 0.0) -> None:
        self.position = (x, y)
        self.heading_theta = heading


class Cone(Thing):
    pass


class Barrier(Thing):
    pass


def test_object_order_is_class_then_position_then_heading():
    things = [Cone(2, 0), Barrier(9, 9), Cone(1, 5), Cone(1, 2, heading=1.0), Cone(1, 2)]
    ordered = sorted(things, key=object_order)
    assert [(type(t).__name__, *t.position, t.heading_theta) for t in ordered] == [
        ("Barrier", 9, 9, 0.0),
        ("Cone", 1, 2, 0.0),
        ("Cone", 1, 2, 1.0),
        ("Cone", 1, 5, 0.0),
        ("Cone", 2, 0, 0.0),
    ]


# --- live ----------------------------------------------------------------------------------------


@needs_sim
@needs_bank(CURVE)
def test_the_lidar_every_env_gets_reports_its_objects_as_a_sorted_list():
    from scenariobank.bank import read_manifest
    from scenariobank.env import build_env, pinned_lidar_class, seed_for
    from scenariobank.options import resolve_options

    manifest = read_manifest(CURVE)
    name, entry = next(iter(manifest.categories.items()))
    row = entry.scenarios[0]
    env, prepare = build_env(CURVE, entry, resolve_options(manifest, tier="hard"))
    try:
        env.reset(seed=seed_for(row))
        prepare(env, row)
        lidar = env.engine.get_sensor("lidar")
        assert type(lidar) is pinned_lidar_class()
        assert {"lidar", "side_detector", "lane_line_detector"} <= set(env.engine.sensors)
        objects = lidar.get_surrounding_objects(env.agent)
        assert isinstance(objects, list) and objects, "a hard row has company at the spawn"
        assert objects == sorted(objects, key=object_order)
        vehicles = lidar.get_surrounding_vehicles(objects)
        assert isinstance(vehicles, list) and vehicles == sorted(vehicles, key=object_order)
    finally:
        env.close()


@needs_sim
@needs_bank(CURVE)
def test_a_row_scores_the_same_alone_in_company_and_in_another_process(tmp_path):
    """`curve_0003` at `hard`: the row that moved both ways before the fix."""
    pair = run_bank(job_for(CURVE, ["curve_0002", "curve_0003"], "hard"), tmp_path / "pair")
    alone = run_bank(job_for(CURVE, ["curve_0003"], "hard"), tmp_path / "alone")
    assert rows(pair)["curve_0003"] == rows(alone)["curve_0003"]
    assert alone.results[0].failure_reason is not None, "a row the traffic actually reaches"
    placed = alone.results[0].placed
    assert placed["TrafficCone"] and placed["TrafficBarrier"] and placed["Pedestrian"]

    # The same pair in another process, whose environment block is another size: what moved
    # the row alone by nine steps before the lidar's order was pinned.
    subprocess.run(
        [
            sys.executable, "-m", "scenariobank", "run",
            "--bank", str(CURVE), "--scenarios", "curve_0002,curve_0003", "--tier", "hard",
            "--policy", EXPERT, "--out", str(tmp_path / "other"),
        ],
        env={**os.environ, "PYTHONHASHSEED": "8"},
        check=True,
        capture_output=True,
    )
    # `--tier hard` on the command line is a subdirectory of `--out`.
    other = Results.model_validate_json(
        (tmp_path / "other" / "hard" / "results.json").read_text()
    )
    assert rows(other) == rows(pair)
    volatile = {"started_utc", "finished_utc", "results"}
    assert other.model_dump(exclude=volatile) == pair.model_dump(exclude=volatile)


@needs_sim
@needs_bank(CURVE)
@needs_bank(LEFT)
def test_hard_is_below_easy_where_the_axes_bite_and_only_slower_where_they_cannot(tmp_path):
    curve = {
        tier: run_bank(job_for(CURVE, ["curve_0004"], tier), tmp_path / f"curve-{tier}").results[0]
        for tier in ("easy", "hard")
    }
    assert curve["easy"].success and not curve["hard"].success
    assert curve["hard"].placed["TrafficCone"] and curve["hard"].placed["TrafficBarrier"]
    assert "TrafficCone" not in curve["easy"].placed

    left = {
        tier: run_bank(
            job_for(LEFT, ["intersection_left_0000"], tier), tmp_path / f"left-{tier}"
        ).results[0]
        for tier in ("easy", "hard")
    }
    assert left["easy"].success and left["hard"].success, "the expert arrives on an X at hard"
    assert left["hard"].steps > left["easy"].steps
    assert left["hard"].placed["Pedestrian"] and left["hard"].placed["Cyclist"]
    assert "TrafficCone" not in left["hard"].placed, "nothing is placed on an X"
