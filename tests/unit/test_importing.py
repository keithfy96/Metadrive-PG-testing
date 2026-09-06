"""The import checklist is generated, so what is tested is that it cannot go quietly wrong.

Two halves, and they need different things. The **coverage** half -- every field of the reader is
on the checklist and every row of the checklist names a field -- needs no workspace and no
simulator, so it runs everywhere and is what actually holds the two files together. The **drift**
half compares the checked-in page against a real conversion and is skipped by name where the
converter checkout is not beside this one, the way `destinations` cannot be re-measured without
MetaDrive.

The fixtures here build `WorkspaceReport` objects directly rather than writing pickles. Nothing in
this module reads a workspace except the live tests; the rendering is a function of the report, and
testing it through the file format would be testing `workspace.py` a second time.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scenariobank import importing
from scenariobank.importing import (
    EXAMPLE_WORKSPACE,
    IMPORTING_DOC,
    LEFT,
    SECTIONS,
    choose,
    render,
)
from scenariobank.workspace import (
    Dataset,
    Entry,
    Provenance,
    Route,
    Scenario,
    Signals,
    WorkspaceError,
    WorkspaceReport,
    read_workspace,
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


def scenario(name: str = "s", rate: float = 100.0) -> Scenario:
    return Scenario(
        scenario_id=name,
        file=f"sd_{name}.pkl",
        size_bytes=1000,
        dataset="osm-scenario",
        coordinate="metadrive",
        sdc_id="ego",
        steps=100,
        length=100,
        step_hz=rate,
        duration_s=1.0,
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
            distance_m=10.0,
            speed_kph=50.0,
            slowest_kph=5.0,
            duration_s=1.0,
            driving_duration_s=1.0,
            waiting_s=0.0,
            stop_count=0,
        ),
        provenance=PROVENANCE,
    )


def report(
    datasets: list[Dataset] | None = None, contents: list[Entry] | None = None
) -> WorkspaceReport:
    if datasets is None:
        datasets = [
            Dataset(
                name="scenarionet-100hz",
                scenarios=[scenario()],
                missing_files=[],
                map_image="stage-6-map-100hz.png",
            )
        ]
    if contents is None:
        contents = [
            Entry(name=one.name, kind="dataset", files=3, size_bytes=1000, checksummed=3)
            for one in datasets
        ] + [
            Entry(
                name="stage-6-map-100hz.png",
                kind="map_image",
                files=1,
                size_bytes=10,
                checksummed=0,
            ),
            Entry(name="source", kind="directory", files=2, size_bytes=20, checksummed=1),
        ]
    return WorkspaceReport(
        report_version=1,
        name="w",
        path="/tmp/w",
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
        contents=contents,
        last_conversion={"dataset_dir": "scenarionet-100hz", "step_hz": 100.0},
        datasets=datasets,
        warnings=[],
    )


# --- the coverage guarantee, which is the whole point and needs no workspace -------------------


def test_the_checklist_and_the_reader_cover_the_same_fields():
    # If this fails, `workspace.py` grew or lost a field and the checklist did not follow.
    importing._check()


def test_a_field_the_reader_has_and_the_checklist_forgets_is_an_error(monkeypatch):
    # The failure this module exists to make loud: a reader that learns something the checklist
    # never mentions, which is exactly how the two would drift apart while `import` is written.
    monkeypatch.setattr(importing, "LEFT", {k: v for k, v in LEFT.items() if k != "path"})
    with pytest.raises(ValueError, match="are read and not on the checklist"):
        importing._check()


def test_a_row_naming_a_field_nothing_reads_is_an_error(monkeypatch):
    monkeypatch.setattr(importing, "LEFT", {**LEFT, "invented": "a promise nothing keeps"})
    with pytest.raises(ValueError, match="are on the checklist and not read"):
        importing._check()


def test_a_field_listed_twice_is_an_error(monkeypatch):
    monkeypatch.setattr(importing, "LEFT", {**LEFT, "driving_side": "already carried above"})
    with pytest.raises(ValueError, match="more than once"):
        importing._check()


def test_every_why_is_a_sentence_rather_than_a_placeholder():
    # A blank cell is the failure mode of a hand-maintained table; a one-word one is the same
    # failure wearing a hat.
    for _, _, fields in SECTIONS:
        for key, why in fields.items():
            assert len(why.split()) >= 4, f"{key} has no reason beside it"


# --- rendering ---------------------------------------------------------------------------------


def test_an_unkeyed_top_level_entry_raises_rather_than_being_dropped():
    # The day the converter starts writing a new directory, this page must say so rather than
    # quietly stop mentioning it -- the same failure the whole document is generated to avoid.
    contents = [Entry(name="bags", kind="directory", files=1, size_bytes=1, checksummed=0)]
    with pytest.raises(ValueError, match="VERDICTS does not decide"):
        render(report(contents=contents))


def test_a_workspace_with_no_conversion_says_so():
    with pytest.raises(WorkspaceError, match="holds no converted scenario"):
        render(report(datasets=[]))


def test_the_note_is_quoted_verbatim_rather_than_summarised():
    # It is a caveat about the data, and paraphrasing a caveat is how it stops being one.
    assert "> OSM records only that a signal exists." in render(report())


def test_rendering_is_stable():
    one = report()
    assert render(one) == render(one)


def test_only_the_dataset_an_import_takes_is_weighed_as_copied():
    """Three dataset directories are not three times the cost. An import takes one."""
    datasets = [
        Dataset(name="scenarionet", scenarios=[scenario("old", 100.0)], missing_files=[],
                map_image="stage-6-map.png"),
        Dataset(name="scenarionet-100hz", scenarios=[scenario("new", 100.0)], missing_files=[],
                map_image="stage-6-map-100hz.png"),
    ]
    contents = [
        Entry(name="scenarionet", kind="dataset", files=3, size_bytes=1_000_000, checksummed=0),
        Entry(name="scenarionet-100hz", kind="dataset", files=3, size_bytes=50_000_000,
              checksummed=3),
        Entry(name="stage-6-map.png", kind="map_image", files=1, size_bytes=0, checksummed=0),
        Entry(name="stage-6-map-100hz.png", kind="map_image", files=1, size_bytes=0,
              checksummed=0),
    ]
    rendered = render(report(datasets=datasets, contents=contents))
    assert "An import of `scenarionet-100hz` moves 50.0 MB" in rendered
    assert "another conversion of this junction" in rendered


def test_the_conversion_stage_6_names_is_the_one_measured():
    """Not the first directory found: `junction-1` sorts an older ego-only conversion first, and
    a checklist measured on that one would describe an empty road."""
    datasets = [
        Dataset(name="scenarionet", scenarios=[scenario("old", 100.0)], missing_files=[],
                map_image=None),
        Dataset(name="scenarionet-100hz", scenarios=[scenario("new", 100.0)], missing_files=[],
                map_image=None),
        Dataset(name="scenarionet-10hz", scenarios=[scenario("new", 10.0)], missing_files=[],
                map_image=None),
    ]
    dataset, one = choose(report(datasets=datasets, contents=[]))
    assert (dataset.name, one.scenario_id) == ("scenarionet-100hz", "new")


def test_the_highest_rate_wins_when_stage_6_names_nothing():
    datasets = [
        Dataset(name="scenarionet-10hz", scenarios=[scenario("a", 10.0)], missing_files=[],
                map_image=None),
        Dataset(name="scenarionet-100hz", scenarios=[scenario("b", 100.0)], missing_files=[],
                map_image=None),
    ]
    one = report(datasets=datasets, contents=[])
    one.last_conversion = {}
    assert choose(one)[0].name == "scenarionet-100hz"


def test_writing_it_needs_no_simulator(tmp_path, monkeypatch):
    """A checklist about stored scenarios that could only be written where MetaDrive is installed
    would be a reference nobody can regenerate. Snapshotted rather than asserted absolutely,
    because other tests in the same session import the simulator."""
    import sys

    before = {name for name in sys.modules if name.startswith("metadrive")}
    monkeypatch.setattr(importing, "read_workspace", lambda _path: report())
    importing.write(tmp_path / "importing.md", tmp_path)
    assert {name for name in sys.modules if name.startswith("metadrive")} == before


# --- the checked-in page, against a real conversion ---------------------------------------------


@needs_workspaces
def test_the_checked_in_page_matches_the_workspace_it_was_measured_on():
    # The drift guard, on the other axis: the page and the conversion it describes.
    assert IMPORTING_DOC.read_text() == render(read_workspace(EXAMPLE_WORKSPACE)), (
        f"{IMPORTING_DOC} is out of date. Run: uv run scenariobank importing"
    )


@needs_workspaces
def test_junction_1_is_measured_on_the_hundred_hertz_conversion():
    dataset, one = choose(read_workspace(EXAMPLE_WORKSPACE))
    assert dataset.name == "scenarionet-100hz"
    assert one.tracks == {"PEDESTRIAN": 101, "CYCLIST": 25, "TRAFFIC_BARRIER": 24, "VEHICLE": 1}


@needs_workspaces
def test_the_page_says_what_cannot_be_recorded_at_all():
    """`drives/`, `traffic/` and the rest carry no sha256 anywhere in the manifest, so leaving
    them behind loses them. That is measured, not decided -- and it is the correction the plan's
    hand-written draft of this list needed."""
    page = IMPORTING_DOC.read_text()
    for name in ("drives", "traffic", "routes", "signals", "actors", "review.json"):
        assert f"| `{name}` |" in page
    assert "a bank cannot record what nothing hashed" in page


@needs_workspaces
@pytest.mark.parametrize("name", ["junction-1", "mosque", "mosque-1"])
def test_every_converted_workspace_renders(name):
    # Not junction-1a: it has no conversion, and saying so is a different test.
    assert render(read_workspace(WORKSPACES / name)).startswith("# Importing a converter")


def run(*flags: str):
    """The command as a person runs it. Lives here beside the module it exercises, because
    `tests/` is deliberately not a package and a helper cannot be imported across files."""
    from typer.testing import CliRunner

    from scenariobank import cli

    return CliRunner().invoke(cli.app, ["importing", *flags])


@needs_workspaces
def test_the_command_writes_the_page_where_it_is_told_to(tmp_path):
    # No --path: the default is the workspace the checked-in page is measured on, and the page
    # records where it was read from, so a run against the same workspace by another path would
    # differ in that one line and in nothing else.
    out = tmp_path / "importing.md"
    result = run("--out", str(out))
    assert result.exit_code == 0, result.output
    assert out.read_text() == IMPORTING_DOC.read_text()


@needs_workspaces
def test_the_command_exits_one_on_a_workspace_with_nothing_converted(tmp_path):
    result = run("--out", str(tmp_path / "x.md"), "--path", str(WORKSPACES / "junction-1a"))
    assert result.exit_code == 1
    assert "importing failed" in result.output
    assert "no converted scenario" in result.output
