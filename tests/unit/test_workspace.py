"""Reading a converter workspace, with no converter and no simulator anywhere near it.

Two halves, deliberately. Most tests **build a workspace by hand** in `tmp_path` -- a manifest,
a summary, a scenario -- so they run on any machine and can state the awkward cases the four real
workspaces happen not to contain today: a summary naming a file that is not there, a stage 5 that
did not pass, a pickle that names something other than numpy.

The rest read the real workspaces at `converter-scenarionet/workspaces/`, behind a named
`needs_workspaces` guard. They are the ones that would catch the format moving under us, and a
guard that skips silently would look exactly like a pass -- so the guard names itself and the
reason says where the checkout is expected to be.
"""

from __future__ import annotations

import json
import pickle
from pathlib import Path

import numpy as np
import pytest

from scenariobank.workspace import (
    ALLOWED_GLOBALS,
    WorkspaceError,
    format_report,
    read_pickle,
    read_workspace,
)

#: The converter checkout this repo reads workspaces out of: a sibling directory of this repo,
#: and not a dependency -- nothing here imports it. Resolved from this file rather than from the
#: working directory, so the guard means the same thing wherever pytest was started.
WORKSPACES = Path(__file__).resolve().parents[2].parent / (
    "wingfin-osm-scenarionet-converter/workspaces"
)

needs_workspaces = pytest.mark.skipif(
    not WORKSPACES.is_dir(),
    reason=f"needs_workspaces: no converter checkout at {WORKSPACES}",
)

FINGERPRINT = "57dcd345d17e5a862f9f38c97916fefe1799873c72b1e280407bb69b2e1da42c"


def manifest_dict(*, stage_5="passed", dataset_dir="scenarionet", step_hz=10.0):
    """A manifest with the fields the reader takes, and none of the ones it does not."""
    return {
        "manifest_version": 1,
        "acquired_at": "2026-08-15T17:34:30.959542+00:00",
        "attribution": "OpenStreetMap contributors",
        "driving_side": "left",
        "driving_side_source": "explicit_cli",
        "graph": {
            "bounds_wgs84": {
                "east": 101.6152512,
                "north": 3.1911378,
                "south": 3.1839982,
                "west": 101.6086997,
            }
        },
        "stage_1b": {
            "status": "passed",
            "projection": {"origin": {"latitude": 3.18589, "longitude": 101.61155}},
        },
        "stage_2": {"status": "passed"},
        "stage_4": {
            "status": "passed",
            "generator_version": "direct-osm-stage2-v24",
            "generation_fingerprint": FINGERPRINT,
            "input_checksums": {"source_osm": "4607fd46"},
            "artifacts": {"reviewed_lane_model": {"sha256": "fc205bca"}},
        },
        "stage_5": {"status": stage_5},
        "stage_6": {
            "status": "converted",
            "dataset_dir": dataset_dir,
            "step_hz": step_hz,
            "scenario_id": "junction-1",
            "map_features": 974,
            "report": "reports/scenario-conversion.json",
        },
        "tool_versions": {"converter": "0.1.0", "osmnx": "2.0.7"},
    }


def scenario_dict(*, steps=10, tracks=(("ego", "VEHICLE"),), lights=(), length=None):
    return {
        "id": "junction-1-abc-1",
        "version": "1.0",
        "length": steps if length is None else length,
        "tracks": {name: {"type": kind, "state": {}} for name, kind in tracks},
        "dynamic_map_states": {name: {"type": kind} for name, kind in lights},
        "map_features": {str(index): {} for index in range(974)},
        "metadata": {},
    }


def summary_entry(*, step_hz=10.0, steps=10, fingerprint=FINGERPRINT, route=True):
    entry = {
        "scenario_id": "junction-1-abc-1",
        "dataset": "osm-scenario",
        "coordinate": "metadrive",
        "sdc_id": "ego",
        "ts": np.arange(steps, dtype=float) / step_hz,
        "provenance": {
            "generator_version": "direct-osm-stage2-v24",
            "generation_fingerprint": fingerprint,
            "source_osm_sha256": "4607fd46",
            "reviewed_lane_model_sha256": "fc205bca",
            "stage_5_status": "passed",
        },
    }
    if route:
        entry["sdc_route"] = {
            "source": "generated",
            "name": "1",
            "start_lane": "aaaa",
            "end_lane": "bbbb",
            "lanes": ["aaaa", "cccc", "bbbb"],
            "lane_changes": 1,
            "junction_movements": 4,
            "distance_m": 403.75,
            "speed_kph": 50.0,
            "slowest_kph": 6.62,
            "duration_s": 36.95,
            "driving_duration_s": 36.95,
            "waiting_s": 0,
            "stops": [],
        }
    return entry


def build(
    tmp_path,
    *,
    name="junction-1",
    datasets=(("scenarionet", {}),),
    pictures=(),
    **manifest_kwargs,
):
    """Write a workspace to disk. `datasets` is `(directory, overrides)` per dataset."""
    root = tmp_path / name
    (root / "source").mkdir(parents=True)
    (root / "source" / "manifest.json").write_text(json.dumps(manifest_dict(**manifest_kwargs)))
    for picture in pictures:
        (root / picture).write_bytes(b"png")
    for directory, overrides in datasets:
        place = root / directory
        place.mkdir(parents=True, exist_ok=True)
        if overrides.get("index", True):
            file_name = "sd_one.pkl"
            entry = summary_entry(**overrides.get("summary", {}))
            (place / "dataset_summary.pkl").write_bytes(pickle.dumps({file_name: entry}))
            (place / "dataset_mapping.pkl").write_bytes(pickle.dumps({file_name: ""}))
            if overrides.get("file", True):
                stored = scenario_dict(**overrides.get("scenario", {}))
                (place / file_name).write_bytes(pickle.dumps(stored))
    return root


def test_a_directory_with_no_manifest_is_not_a_workspace(tmp_path):
    with pytest.raises(WorkspaceError) as raised:
        read_workspace(tmp_path)
    assert "source/manifest.json" in str(raised.value)


def test_a_manifest_that_is_not_json_says_so_rather_than_raising_a_decode_error(tmp_path):
    root = tmp_path / "broken"
    (root / "source").mkdir(parents=True)
    (root / "source" / "manifest.json").write_text("{not json")
    with pytest.raises(WorkspaceError) as raised:
        read_workspace(root)
    assert "not readable as JSON" in str(raised.value)


def test_identity_and_drive_side_come_off_the_manifest(tmp_path):
    report = read_workspace(build(tmp_path))
    assert (report.name, report.driving_side, report.driving_side_source) == (
        "junction-1",
        "left",
        "explicit_cli",
    )
    assert report.attribution == "OpenStreetMap contributors"
    assert report.origin == {"latitude": 3.18589, "longitude": 101.61155}
    assert report.provenance.generation_fingerprint == FINGERPRINT
    assert report.report_version == 1


def test_datasets_are_found_by_walking_the_workspace_not_read_out_of_stage_6(tmp_path):
    """The finding this reader exists for: `stage_6` records one conversion, not the workspace.

    `junction-1` really does hold three dataset directories while its manifest names one. A reader
    that trusted `stage_6` would report a third of what is there and would not say so.
    """
    root = build(
        tmp_path,
        datasets=(("scenarionet", {}), ("scenarionet-100hz", {}), ("scenarionet-10hz", {})),
        dataset_dir="scenarionet-10hz",
    )
    report = read_workspace(root)
    assert [one.name for one in report.datasets] == [
        "scenarionet",
        "scenarionet-100hz",
        "scenarionet-10hz",
    ]
    assert report.last_conversion["dataset_dir"] == "scenarionet-10hz"
    assert any("does not describe" in line for line in report.warnings)


def test_a_dataset_directory_named_in_no_manifest_at_all_is_still_read(tmp_path):
    report = read_workspace(build(tmp_path, dataset_dir=None, step_hz=None))
    assert [one.name for one in report.datasets] == ["scenarionet"]
    assert any("records no dataset directory" in line for line in report.warnings)


def test_a_directory_without_the_scenarionet_index_is_not_a_dataset(tmp_path):
    root = build(tmp_path, datasets=(("scenarionet-empty", {"index": False}),))
    assert read_workspace(root).datasets == []


@pytest.mark.parametrize(
    ("step_hz", "steps", "expected_hz"),
    [(10.0, 960, 10.0), (100.0, 3695, 100.0), (20.0, 40, 20.0)],
)
def test_the_rate_is_measured_from_the_timestamps_not_read_from_stage_6(
    tmp_path, step_hz, steps, expected_hz
):
    """`junction-1`'s manifest says 10 Hz for a workspace two thirds of which steps at 100."""
    root = build(
        tmp_path,
        datasets=(
            ("scenarionet", {"summary": {"step_hz": step_hz, "steps": steps},
                             "scenario": {"steps": steps}}),
        ),
        step_hz=10.0,
    )
    scenario = read_workspace(root).datasets[0].scenarios[0]
    assert scenario.step_hz == expected_hz
    assert scenario.steps == steps


def test_a_rate_that_disagrees_with_the_manifest_is_a_warning_not_a_correction(tmp_path):
    root = build(
        tmp_path,
        datasets=(("scenarionet", {"summary": {"step_hz": 100.0, "steps": 100},
                                   "scenario": {"steps": 100}}),),
        step_hz=10.0,
    )
    report = read_workspace(root)
    assert report.last_conversion["step_hz"] == 10.0
    assert report.datasets[0].scenarios[0].step_hz == 100.0
    assert any("stage_6 says 10 Hz" in line for line in report.warnings)


def test_actor_counts_come_from_the_scenario_file_because_the_summary_has_none(tmp_path):
    root = build(
        tmp_path,
        datasets=(
            (
                "scenarionet",
                {
                    "scenario": {
                        "tracks": (
                            ("ego", "VEHICLE"),
                            ("p1", "PEDESTRIAN"),
                            ("p2", "PEDESTRIAN"),
                            ("c1", "CYCLIST"),
                        ),
                        "lights": (("l1", "TRAFFIC_LIGHT"),),
                    }
                },
            ),
        ),
    )
    scenario = read_workspace(root).datasets[0].scenarios[0]
    # Commonest first, so the thing there is most of is the first thing read.
    assert list(scenario.tracks.items()) == [("PEDESTRIAN", 2), ("CYCLIST", 1), ("VEHICLE", 1)]
    assert scenario.lights == {"TRAFFIC_LIGHT": 1}
    assert scenario.map_features == 974


def test_a_recording_holding_only_the_ego_is_called_out(tmp_path):
    """What replaces the six option axes is the contents of the recording -- so an empty one
    matters. Every dataset in the four workspaces today is exactly this."""
    report = read_workspace(build(tmp_path))
    assert any("the ego and nothing else" in line for line in report.warnings)


def test_a_scenario_file_named_in_the_summary_and_missing_from_disk_is_reported(tmp_path):
    root = build(tmp_path, datasets=(("scenarionet", {"file": False}),))
    dataset = read_workspace(root).datasets[0]
    assert dataset.scenarios == []
    assert dataset.missing_files == ["sd_one.pkl"]


def test_the_route_is_read_as_the_measured_budget_a_pg_scenario_does_not_have(tmp_path):
    route = read_workspace(build(tmp_path)).datasets[0].scenarios[0].route
    assert (route.start_lane, route.end_lane, route.lane_count) == ("aaaa", "bbbb", 3)
    assert (route.distance_m, route.duration_s, route.stop_count) == (403.75, 36.95, 0)


def test_a_summary_with_no_route_reads_as_no_route_rather_than_raising(tmp_path):
    root = build(tmp_path, datasets=(("scenarionet", {"summary": {"route": False}}),))
    assert read_workspace(root).datasets[0].scenarios[0].route is None


@pytest.mark.parametrize(
    ("directory", "pictures", "expected"),
    [
        ("scenarionet-10hz", ("stage-6-map-10hz.png", "stage-6-map.png"), "stage-6-map-10hz.png"),
        ("scenarionet-10hz", ("stage-6-map.png",), "stage-6-map.png"),
        ("scenarionet", ("stage-6-map.png",), "stage-6-map.png"),
        ("scenarionet", (), None),
    ],
)
def test_the_map_picture_is_the_suffixed_one_then_the_plain_one(
    tmp_path, directory, pictures, expected
):
    root = build(tmp_path, datasets=((directory, {}),), pictures=pictures)
    assert read_workspace(root).datasets[0].map_image == expected


def test_a_stage_5_that_did_not_pass_is_a_warning_because_import_will_refuse_it(tmp_path):
    report = read_workspace(build(tmp_path, stage_5="failed"))
    assert any("not 'passed'" in line for line in report.warnings)


def test_a_scenario_built_from_a_different_lane_model_than_the_manifest_is_a_warning(tmp_path):
    root = build(
        tmp_path,
        datasets=(("scenarionet", {"summary": {"fingerprint": "ffff" * 16}}),),
    )
    assert any("built from lane model" in line for line in read_workspace(root).warnings)


def test_a_file_whose_length_disagrees_with_its_timestamps_is_a_warning(tmp_path):
    root = build(
        tmp_path,
        datasets=(("scenarionet", {"summary": {"steps": 10}, "scenario": {"length": 11}}),),
    )
    assert any("timestamps say 10" in line for line in read_workspace(root).warnings)


def test_a_workspace_with_no_dataset_says_nothing_is_importable_yet(tmp_path):
    report = read_workspace(build(tmp_path, datasets=()))
    assert report.datasets == []
    assert any("nothing here is importable yet" in line for line in report.warnings)


def test_warnings_name_the_directory_because_two_of_them_hold_the_same_scenario_id(tmp_path):
    root = build(
        tmp_path,
        datasets=(("scenarionet", {}), ("scenarionet-100hz", {})),
        dataset_dir="scenarionet",
    )
    ego_only = [line for line in read_workspace(root).warnings if "ego and nothing else" in line]
    assert len(ego_only) == 2
    assert {line.split("/")[0].strip() for line in ego_only} == {
        "scenarionet",
        "scenarionet-100hz",
    }


def test_a_pickle_naming_anything_but_numpy_is_refused_and_named(tmp_path):
    """This is the command you run on a workspace you did not make. A pickle is code."""
    import collections

    hostile = tmp_path / "hostile.pkl"
    hostile.write_bytes(pickle.dumps(collections.OrderedDict([("a", 1)])))
    with pytest.raises(WorkspaceError) as raised:
        read_pickle(hostile)
    assert "collections.OrderedDict" in str(raised.value)
    assert "it was not read" in str(raised.value)


def test_numpy_arrays_are_what_a_workspace_pickle_is_allowed_to_hold(tmp_path):
    path = tmp_path / "fine.pkl"
    path.write_bytes(pickle.dumps({"ts": np.arange(4, dtype=float)}))
    assert list(read_pickle(path)["ts"]) == [0.0, 1.0, 2.0, 3.0]
    assert ("numpy", "array") in ALLOWED_GLOBALS


def test_a_file_that_is_not_a_pickle_at_all_says_so(tmp_path):
    path = tmp_path / "nope.pkl"
    path.write_bytes(b"this is not a pickle")
    with pytest.raises(WorkspaceError) as raised:
        read_pickle(path)
    assert "not readable as a pickle" in str(raised.value)


def test_the_text_report_carries_the_identity_the_route_and_the_actors(tmp_path):
    text = format_report(read_workspace(build(tmp_path)))
    assert "drive side:   left (explicit_cli)" in text
    assert "10 Hz" in text
    assert "aaaa -> bbbb" in text
    assert "actors: 1 VEHICLE" in text
    assert "lights: none" in text


def test_reading_a_workspace_imports_no_simulator(tmp_path):
    """Asserted as *this call imports none*, not as "the process has none loaded" -- the rest of
    the suite loads MetaDrive, and a test that only passed when run alone would be worthless."""
    import sys

    before = {name for name in sys.modules if name.startswith("metadrive")}
    read_workspace(build(tmp_path))
    assert {name for name in sys.modules if name.startswith("metadrive")} == before


@needs_workspaces
@pytest.mark.parametrize("name", ["junction-1", "junction-1a", "mosque", "mosque-1"])
def test_every_real_workspace_reads(name):
    report = read_workspace(WORKSPACES / name)
    assert report.driving_side == "left"
    assert report.provenance.stage_5_status == "passed"
    assert format_report(report)


@needs_workspaces
def test_junction_1_holds_three_datasets_at_two_different_rates():
    """The measurement the module docstring rests on, asserted rather than quoted."""
    report = read_workspace(WORKSPACES / "junction-1")
    assert [one.name for one in report.datasets] == [
        "scenarionet",
        "scenarionet-100hz",
        "scenarionet-10hz",
    ]
    assert [one.scenarios[0].step_hz for one in report.datasets] == [100.0, 100.0, 10.0]
    assert report.last_conversion["dataset_dir"] == "scenarionet-100hz"


@needs_workspaces
def test_the_junction_1_checklist_is_what_the_files_say():
    """Phase 3 Step 2's checklist, asserted against the conversion it was written from.

    The same route reads identically at both rates and differs only in how finely it was sampled,
    which is the property the measured step budget rests on."""
    report = read_workspace(WORKSPACES / "junction-1")
    rates = {one.name: one.scenarios[0] for one in report.datasets}

    for name, steps in (("scenarionet-100hz", 3782), ("scenarionet-10hz", 379)):
        scenario = rates[name]
        assert scenario.tracks == {
            "PEDESTRIAN": 101,
            "CYCLIST": 25,
            "TRAFFIC_BARRIER": 24,
            "VEHICLE": 1,
        }
        assert scenario.lights == {"TRAFFIC_LIGHT": 8}
        assert scenario.map_features == 974
        assert scenario.steps == steps
        assert (scenario.route.distance_m, scenario.route.duration_s) == (395.11, 37.82)
        assert scenario.route.junction_movements == 14

    # And the third directory is an older conversion of a different route with nobody in it, which
    # is why `stage_6` cannot be the index of what a workspace holds.
    assert rates["scenarionet"].tracks == {"VEHICLE": 1}
    assert rates["scenarionet"].route.name == "test"


def run(path, *flags):
    """The command itself. Kept beside the workspace builder rather than in `test_cli.py`, which
    would have to import this module to build one -- and `tests/` is deliberately not a package."""
    from typer.testing import CliRunner

    from scenariobank import cli

    return CliRunner().invoke(cli.app, ["workspace", "--path", str(path), *flags])


def test_the_command_on_something_that_is_not_a_workspace_exits_one_and_says_what_one_is(tmp_path):
    result = run(tmp_path)
    assert result.exit_code == 1
    assert "workspace failed" in result.output
    assert "source/manifest.json" in result.output


def test_the_json_is_the_same_report_the_table_renders(tmp_path):
    root = build(tmp_path)
    table = run(root)
    emitted = run(root, "--json")
    assert (table.exit_code, emitted.exit_code) == (0, 0), table.output

    report = json.loads(emitted.stdout)
    assert report["report_version"] == 1
    assert report["driving_side"] == "left"
    assert [one["name"] for one in report["datasets"]] == ["scenarionet"]
    # Both renderings answer the one question this command exists for -- what is in here -- so a
    # scenario visible in one and not the other would be a reader with two different answers.
    assert report["datasets"][0]["scenarios"][0]["scenario_id"] in table.stdout
