"""`scenariobank import`: the copy, the refusals, and the schema that keeps the two banks apart.

Separate from `test_importing.py`, which tests the same module's other half. That file tests a
document -- pure rendering, no files -- and this one tests a command that writes 50 MB into a
directory. Different fixtures, different failure modes, and keeping them apart is what stops the
one test file from needing both a workspace on disk and a report built in memory.

The offline half builds a **fake workspace**: the files an import copies, beside a `WorkspaceReport`
that describes them. That is enough to exercise every refusal and the whole copy, so the parts of
this command that can go wrong are checked on a machine with no converter checkout and no
simulator. The live half is what proves the fake is shaped like the real thing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from scenariobank import importing
from scenariobank.bank import (
    MANIFEST_NAME,
    SCHEMA_VERSION,
    BankError,
    CategoryEntry,
    Manifest,
    OptionLevels,
    RealWorldEntry,
    ScenarioRow,
    SimulatorInfo,
    read_manifest,
    set_options,
    write_manifest,
)
from scenariobank.importing import DATASET_DIR, choose, import_workspace
from scenariobank.workspace import (
    DATASET_INDEX,
    Dataset,
    Entry,
    Provenance,
    Route,
    Scenario,
    Signals,
    WorkspaceError,
    WorkspaceReport,
)

WORKSPACES = Path(__file__).resolve().parents[2].parent / (
    "wingfin-osm-scenarionet-converter/workspaces"
)
needs_workspaces = pytest.mark.skipif(
    not WORKSPACES.is_dir(),
    reason=f"needs_workspaces: no converter checkout at {WORKSPACES}",
)

PROVENANCE = Provenance(
    generator_version="v1",
    generation_fingerprint="a" * 64,
    source_osm_sha256="b" * 64,
    reviewed_lane_model_sha256="c" * 64,
    stage_5_status="passed",
)


def scenario(name: str = "s", rate: float = 100.0, frames: int = 300) -> Scenario:
    return Scenario(
        scenario_id=f"osm-scenario_v1_{name}",
        file=f"sd_{name}.pkl",
        size_bytes=11,
        dataset="osm-scenario",
        coordinate="metadrive",
        sdc_id="ego",
        steps=frames,
        length=frames,
        step_hz=rate,
        duration_s=frames / rate,
        map_features=3,
        map_feature_types={"LANE_SURFACE_STREET": 3},
        tracks={"PEDESTRIAN": 2, "VEHICLE": 1},
        lights={"TRAFFIC_LIGHT": 1},
        route=Route(
            source="generated",
            name=name,
            start_lane="a",
            end_lane="b",
            lane_count=2,
            lane_changes=0,
            junction_movements=1,
            distance_m=395.11,
            speed_kph=50.0,
            slowest_kph=5.0,
            duration_s=frames / rate,
            driving_duration_s=frames / rate,
            waiting_s=0.0,
            stop_count=0,
        ),
        provenance=PROVENANCE,
    )


def report(path: Path, datasets: list[Dataset] | None = None, **overrides) -> WorkspaceReport:
    if datasets is None:
        datasets = [
            Dataset(
                name="scenarionet-100hz",
                scenarios=[scenario()],
                missing_files=[],
                map_image="stage-6-map-100hz.png",
            )
        ]
    fields = dict(
        report_version=1,
        name="junction-x",
        path=str(path),
        manifest_version=1,
        acquired_at="2026-08-15T00:00:00+00:00",
        driving_side="left",
        driving_side_source="explicit_cli",
        attribution="OpenStreetMap contributors",
        origin={"latitude": 3.0, "longitude": 101.0},
        bounds={"north": 3.1, "south": 3.0, "east": 101.1, "west": 101.0},
        stages={"stage_5": "passed"},
        provenance=PROVENANCE,
        tool_versions={"osmnx": "2.0.7"},
        artifacts={"source/map.osm": "d" * 64},
        signals=Signals(
            source="synthesised",
            version=1,
            cycle_seconds=60.0,
            time_step_s=0.01,
            phase_groups=3,
            signalled_lanes=8,
            lane_model_signals=1,
            note="OSM records only that a signal exists.",
        ),
        contents=[
            Entry(name="scenarionet-100hz", kind="dataset", files=3, size_bytes=1000,
                  checksummed=0),
            Entry(name="stage-6-map-100hz.png", kind="map_image", files=1, size_bytes=10,
                  checksummed=0),
            Entry(name="source", kind="directory", files=2, size_bytes=20, checksummed=1),
        ],
        last_conversion={"dataset_dir": "scenarionet-100hz", "step_hz": 100.0},
        datasets=datasets,
        warnings=[],
    )
    fields.update(overrides)
    return WorkspaceReport(**fields)


def workspace(tmp_path: Path, datasets: list[Dataset] | None = None, **overrides):
    """A directory holding the files an import copies, and the report that describes them.

    Written rather than mocked because the copy is the thing under test: a fake that returned
    paths without files would pass while `import` moved nothing.
    """
    found = report(tmp_path / "ws", datasets=datasets, **overrides)
    root = tmp_path / "ws"
    for dataset in found.datasets:
        directory = root / dataset.name
        directory.mkdir(parents=True, exist_ok=True)
        for name in (*DATASET_INDEX, *(one.file for one in dataset.scenarios)):
            (directory / name).write_bytes(b"not a pickle")
        if dataset.map_image:
            (root / dataset.map_image).write_bytes(b"not a png")
    (root / "source").mkdir(exist_ok=True)
    (root / "source" / "manifest.json").write_text(json.dumps({"version": 1}))
    (root / "reports").mkdir(exist_ok=True)
    (root / "reports" / "scenario-conversion-100hz.json").write_text("{}")
    return root, found


def imported(tmp_path: Path, monkeypatch, out: str = "bank", **kwargs) -> Manifest:
    root, found = workspace(tmp_path, **{k: v for k, v in kwargs.items() if k == "datasets"})
    for key, value in kwargs.items():
        if key != "datasets":
            found = found.model_copy(update={key: value})
    monkeypatch.setattr(importing, "read_workspace", lambda _path: found)
    return import_workspace(root, tmp_path / out)


def pg_bank(out: Path) -> Manifest:
    """The smallest thing `read_manifest` will accept as a generated bank."""
    out.mkdir(parents=True, exist_ok=True)
    manifest = Manifest(
        schema_version=SCHEMA_VERSION,
        bank_id="pg",
        created_utc="2026-09-07T00:00:00Z",
        metadrive=SimulatorInfo(edition=None, dist_version=None, commit=None, asset_version=None),
        base_config={},
        drive_side="left",
        categories={
            "curve": CategoryEntry(
                description="a curve",
                block_seq="C",
                exit_rule="only",
                max_steps=300,
                scenarios=[
                    ScenarioRow(
                        scenario_id="curve_0000",
                        seed=0,
                        destination="1C0_1_",
                        spawn_lane_index=0,
                        route_length_m=100.0,
                        net_rotation_deg=10.0,
                        turn_pairs="R",
                        thumbnail=None,
                    )
                ],
            )
        },
    )
    write_manifest(out, manifest)
    return manifest


# --- the copy ------------------------------------------------------------------------------------


def test_an_import_writes_the_files_a_runner_needs(tmp_path, monkeypatch):
    manifest = imported(tmp_path, monkeypatch)
    bank = tmp_path / "bank"
    for name in (*DATASET_INDEX, "sd_s.pkl"):
        assert (bank / DATASET_DIR / name).is_file(), name
    assert (bank / "source" / "manifest.json").is_file()
    assert (bank / "reports" / "scenario-conversion-100hz.json").is_file()
    assert (bank / "thumbs" / "junction-x.png").is_file()
    assert manifest.schema_version == "1.4"
    assert manifest.source == "osm-scenario"


def test_the_row_is_measured_off_the_recording(tmp_path, monkeypatch):
    manifest = imported(tmp_path, monkeypatch)
    entry = manifest.categories["junction-x"]
    row = entry.scenarios[0]
    assert (row.scenario_id, row.scenario_index) == ("junction-x_0000", 0)
    assert row.stored_id == "osm-scenario_v1_s"
    # The recording's own length is the cap: replay advances one frame per step.
    assert row.max_steps == 300 == entry.budget_for(row)
    assert row.route_length_m == 395.11
    assert row.tracks == {"PEDESTRIAN": 2, "VEHICLE": 1}
    assert entry.step_hz == 100.0


def test_the_provenance_travels_and_so_does_the_signal_caveat(tmp_path, monkeypatch):
    entry = imported(tmp_path, monkeypatch).categories["junction-x"]
    assert entry.provenance.generation_fingerprint == "a" * 64
    assert entry.attribution == "OpenStreetMap contributors"
    assert entry.artifacts == {"source/map.osm": "d" * 64}
    assert entry.signals and entry.signals.note.startswith("OSM records only")
    assert entry.copied == ["source/manifest.json", "reports/scenario-conversion-100hz.json"]


def test_a_workspace_with_no_picture_imports_without_one(tmp_path, monkeypatch):
    # `mosque-1` has no stage-6 map. A thumbnail is what a bank has, not what it needs.
    datasets = [
        Dataset(name="scenarionet-100hz", scenarios=[scenario()], missing_files=[], map_image=None)
    ]
    entry = imported(tmp_path, monkeypatch, datasets=datasets).categories["junction-x"]
    assert entry.scenarios[0].thumbnail is None
    assert not (tmp_path / "bank" / "thumbs").exists()


def test_a_re_import_does_not_leave_the_previous_conversion_behind(tmp_path, monkeypatch):
    """The failure this guards: two `sd_*.pkl` in one directory and a summary naming one."""
    imported(tmp_path, monkeypatch)
    assert (tmp_path / "bank" / DATASET_DIR / "sd_s.pkl").is_file()

    other = [
        Dataset(
            name="scenarionet-100hz",
            scenarios=[scenario("t")],
            missing_files=[],
            map_image="stage-6-map-100hz.png",
        )
    ]
    imported(tmp_path, monkeypatch, datasets=other)
    files = sorted(one.name for one in (tmp_path / "bank" / DATASET_DIR).iterdir())
    assert files == ["dataset_mapping.pkl", "dataset_summary.pkl", "sd_t.pkl"]


def test_importing_needs_no_simulator(tmp_path, monkeypatch):
    """MetaDrive did not build this bank and is not needed to write one.

    Snapshotted rather than asserted absolutely, because other tests in the same session import
    the simulator.
    """
    before = {name for name in sys.modules if name.startswith("metadrive")}
    manifest = imported(tmp_path, monkeypatch)
    assert {name for name in sys.modules if name.startswith("metadrive")} == before
    assert manifest.metadrive.commit is None
    assert manifest.base_config == {}


# --- the refusals ---------------------------------------------------------------------------------


def test_a_workspace_whose_stage_5_did_not_pass_is_refused(tmp_path, monkeypatch):
    failed = PROVENANCE.model_copy(update={"stage_5_status": "failed"})
    with pytest.raises(BankError, match="stage_5_status"):
        imported(tmp_path, monkeypatch, provenance=failed)


def test_a_right_side_workspace_is_refused_rather_than_mirrored(tmp_path, monkeypatch):
    with pytest.raises(BankError, match="drives on the 'right'"):
        imported(tmp_path, monkeypatch, driving_side="right")


def test_a_generated_bank_is_never_imported_over(tmp_path, monkeypatch):
    pg_bank(tmp_path / "bank")
    with pytest.raises(BankError, match="already holds a procedurally generated bank"):
        imported(tmp_path, monkeypatch)
    # And it is still there, untouched.
    assert read_manifest(tmp_path / "bank").source == "pg"


def test_a_rate_the_workspace_does_not_hold_names_the_ones_it_does(tmp_path, monkeypatch):
    datasets = [
        Dataset(name="scenarionet", scenarios=[scenario(rate=10.0)], missing_files=[],
                map_image=None)
    ]
    root, found = workspace(tmp_path, datasets=datasets)
    monkeypatch.setattr(importing, "read_workspace", lambda _path: found)
    with pytest.raises(WorkspaceError, match="no conversion at 100 Hz. It holds 10 Hz"):
        import_workspace(root, tmp_path / "bank")


def test_the_copier_and_the_checklist_have_to_agree(monkeypatch):
    """The other half of Step 2's guarantee: the page says what comes over, this brings it.

    A file the copier takes that the page does not call `partly copied` is a document that has
    stopped describing the code beside it.
    """
    monkeypatch.setattr(importing, "COPIED", ("actors/plan.json",))
    with pytest.raises(ValueError, match="importing.VERDICTS calls 'actors' 'dropped'"):
        importing._verify(
            Dataset(name="scenarionet", scenarios=[], missing_files=[], map_image=None)
        )


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("scenarionet-100hz", "reports/scenario-conversion-100hz.json"),
        ("scenarionet-10hz", "reports/scenario-conversion-10hz.json"),
        ("scenarionet", "reports/scenario-conversion.json"),
    ],
)
def test_the_report_copied_is_this_conversions_own(name, expected):
    assert importing._report_name(name) == expected


def test_the_rate_narrows_before_stage_6_breaks_the_tie(tmp_path):
    """Two conversions at one rate is not a contradiction: `junction-1` has exactly that."""
    datasets = [
        Dataset(name="scenarionet", scenarios=[scenario("old")], missing_files=[], map_image=None),
        Dataset(name="scenarionet-100hz", scenarios=[scenario("new")], missing_files=[],
                map_image=None),
        Dataset(name="scenarionet-10hz", scenarios=[scenario("slow", rate=10.0)],
                missing_files=[], map_image=None),
    ]
    found = report(tmp_path, datasets=datasets)
    assert choose(found, rate=100.0)[0].name == "scenarionet-100hz"
    assert choose(found, rate=10.0)[0].name == "scenarionet-10hz"


# --- schema 1.3 -----------------------------------------------------------------------------------


def test_a_bank_is_one_kind_or_the_other(tmp_path, monkeypatch):
    manifest = imported(tmp_path, monkeypatch)
    assert isinstance(manifest.categories["junction-x"], RealWorldEntry)
    with pytest.raises(ValidationError, match="are real-world categories"):
        Manifest.model_validate({**manifest.model_dump(), "source": "pg"})


def test_an_imported_bank_cannot_pin_option_levels(tmp_path, monkeypatch):
    """The six axes are contents of a recording here, so pinning one promises what nothing keeps."""
    manifest = imported(tmp_path, monkeypatch)
    raw = {**manifest.model_dump(), "options": OptionLevels(traffic="medium").model_dump()}
    with pytest.raises(ValidationError, match="pins option levels, and it cannot"):
        Manifest.model_validate(raw)


def test_a_1_2_manifest_reads_as_a_procedural_1_3_one(tmp_path):
    """The version moved because a 1.2 reader would refuse a category naming a dataset directory,
    not because anything on disk has to be rewritten."""
    pg_bank(tmp_path / "bank")
    raw = json.loads((tmp_path / "bank" / MANIFEST_NAME).read_text())
    del raw["source"]
    raw["schema_version"] = "1.2"
    manifest = Manifest.model_validate(raw)
    assert manifest.source == "pg"
    assert isinstance(manifest.categories["curve"], CategoryEntry)


def test_review_reads_an_imported_bank_and_reports_no_distinct_count(tmp_path, monkeypatch):
    """Step 3 refused this by name and Step 5 is where it learned what a recording's review is.

    The refusal existed so a review built on seeds would not report zeroes from computations that
    never ran. That constraint outlives the refusal: an imported bank holds one recording, so
    `distinct` is **absent** rather than equal to `total`.
    """
    from scenariobank.review import RealWorldReview, review

    report = review(imported(tmp_path, monkeypatch))
    assert report.source == "osm-scenario"
    assert report.total == 1
    assert report.distinct is None
    assert isinstance(report.categories[0], RealWorldReview)


def test_comparing_two_recordings_is_still_refused_by_name(tmp_path, monkeypatch):
    """`compare` keeps the guard `review` gave up: a bank holds one recording, so there is no
    second drive in it to compare the first against."""
    from scenariobank.review import compare

    manifest = imported(tmp_path, monkeypatch)
    row = manifest.categories["junction-x"].scenarios[0]
    with pytest.raises(BankError, match="built on seeds, block sequences and resolved exits"):
        compare(manifest, row.scenario_id, row.scenario_id)


def test_a_bank_row_carries_the_map_size_the_checklist_promises(tmp_path, monkeypatch):
    """Schema 1.4. `importing.SECTIONS` has said since Step 2 that a bank carries the map size;
    until 1.4 `_rows` passed neither field and nothing caught it, because `_verify` checks copied
    files and `_check` checks the reader. `_check_carried` closes that gap structurally and this
    closes it by value."""
    manifest = imported(tmp_path, monkeypatch)
    row = manifest.categories["junction-x"].scenarios[0]
    assert row.map_features == 3
    assert row.map_feature_types == {"LANE_SURFACE_STREET": 3}


def test_the_editing_commands_refuse_an_imported_bank(tmp_path, monkeypatch):
    """`replace`, `add`, `budget` and `options` are all built on a seed. This one has none."""
    imported(tmp_path, monkeypatch)
    with pytest.raises(BankError, match="`options` edits a procedurally generated bank"):
        set_options(tmp_path / "bank", {"traffic": "medium"})


# --- against a real workspace ---------------------------------------------------------------------


@needs_workspaces
def test_junction_1_imports_the_hundred_hertz_conversion(tmp_path):
    manifest = import_workspace(WORKSPACES / "junction-1", tmp_path / "bank")
    entry = manifest.categories["junction-1"]
    assert entry.step_hz == 100.0
    assert entry.scenarios[0].max_steps == 3782
    assert entry.scenarios[0].tracks == {
        "PEDESTRIAN": 101, "CYCLIST": 25, "TRAFFIC_BARRIER": 24, "VEHICLE": 1
    }
    assert entry.scenarios[0].lights == {"TRAFFIC_LIGHT": 8}
    # The whole triple, and the copied dataset is the 50 MB the checklist quotes.
    moved = sum(
        one.stat().st_size for one in (tmp_path / "bank" / DATASET_DIR).rglob("*") if one.is_file()
    )
    assert 49e6 < moved < 51e6
    assert read_manifest(tmp_path / "bank").categories["junction-1"].dataset_dir == DATASET_DIR


@needs_workspaces
def test_an_import_writes_nothing_into_the_workspace_it_read(tmp_path):
    workspace_dir = WORKSPACES / "junction-1"
    before = {
        one: one.stat().st_mtime_ns for one in workspace_dir.rglob("*") if one.is_file()
    }
    import_workspace(workspace_dir, tmp_path / "bank")
    after = {one: one.stat().st_mtime_ns for one in workspace_dir.rglob("*") if one.is_file()}
    assert before == after


@needs_workspaces
def test_a_ten_hertz_import_is_a_tenth_of_the_bytes(tmp_path):
    """The cost the rate flag buys, and the reason 100 Hz is still the default: the decision rate
    stays a per-run stride either way, and only the 10 Hz choice cannot be undone."""
    slow = import_workspace(WORKSPACES / "junction-1", tmp_path / "slow", rate=10.0)
    entry = slow.categories["junction-1"]
    assert (entry.step_hz, entry.scenarios[0].max_steps) == (10.0, 379)
    moved = sum(
        one.stat().st_size for one in (tmp_path / "slow" / DATASET_DIR).rglob("*") if one.is_file()
    )
    assert moved < 6e6
