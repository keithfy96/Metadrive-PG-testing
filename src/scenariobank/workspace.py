"""Reading a converter workspace: what a stored scenario is, before anything is imported.

A workspace is what `converter-scenarionet` leaves on disk for one place on earth --
`workspaces/junction-1/`. This module reads one and says what is in it. It writes nothing, builds
no environment, and needs neither the `sim` group nor the `web` one.

**R1 holds: read the files, do not import the converter.** The format read here is ScenarioNet's,
which is MetaDrive's -- a `dataset_summary.pkl` of plain dicts and one `sd_*.pkl` per scenario --
so the only thing needed to read it is `json`, `pickle` and `numpy`. Nothing from the converter is
imported, and neither is MetaDrive: `scenariobank workspace` answers on a machine that has no
simulator at all, which is the whole point of looking before importing.

**Three things the manifest cannot be trusted for**, all measured on the four workspaces at
`wingfin-osm-scenarionet-converter/workspaces/` on 2026-09-06 rather than assumed:

* **`stage_6` describes the last conversion, not the workspace.** `junction-1` holds three dataset
  directories -- `scenarionet/`, `scenarionet-100hz/`, `scenarionet-10hz/` -- and its manifest names
  only `scenarionet-100hz`. `scenarionet/` holds an older conversion of a different route (`test`,
  403.8 m) with nothing in it but the ego, and the manifest never mentions it. So the datasets are
  **discovered by walking the workspace**, and `stage_6` is reported as one conversion's record
  rather than as the index.
* **The rate is measured, not read.** `junction-1`'s `stage_6.step_hz` says `100.0`, and
  `scenarionet-10hz/` steps at 10 -- the same 37.8 s drive in 379 steps rather than 3782. The rate
  comes from `ts`, the timestamp array each scenario carries, so it describes the file it was taken
  from, and it is the difference between a 379-step budget and a 3782-step one.
* **`dataset_dir: null` does not mean there is no dataset.** `mosque-1`'s manifest says exactly
  that and `mosque-1/scenarionet/` holds a converted scenario anyway.

**The actor counts are not in the summary.** `dataset_summary.pkl` carries identity, provenance,
the route and the lane-model counts, but the tracks live in the scenario file -- and they are what
replaces the six option axes on this path, so a reader that skipped them would answer the wrong
question. `junction-1/scenarionet-10hz` holds 101 `PEDESTRIAN`, 25 `CYCLIST`, 24 `TRAFFIC_BARRIER`
and one `VEHICLE`, the ego, plus 8 `TRAFFIC_LIGHT` in `dynamic_map_states`; the same workspace's
`scenarionet/` holds the ego and nothing else. The file is ~1 MB and unpickles in about 0.02 s, so
it is read; a dataset of thousands rather than one would want a flag to skip it, and does not exist
yet.

**Pickles are read through a restricted unpickler.** This is the command you run on a workspace you
have not vetted -- somebody else's conversion, off a share -- and a pickle is code. All 21 pickles
in the four workspaces here name exactly one global between them, `numpy.array`, so the allowlist
can be tight enough to be worth having. MetaDrive will unpickle the same file with no
such guard when it runs it; that is an argument for looking first, not for looking carelessly.
"""

from __future__ import annotations

import json
import pickle
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

#: Dataset directories are `scenarionet`, `scenarionet-10hz`, `scenarionet-100hz`. Matched by
#: prefix rather than by an exact list because the rate suffix is the converter's to choose.
DATASET_PREFIX = "scenarionet"

#: ScenarioNet's index, both halves. `data_directory` points at a directory holding these two and
#: the `sd_*.pkl` files they name; a directory missing either is not a dataset, it is a leftover.
DATASET_INDEX = ("dataset_summary.pkl", "dataset_mapping.pkl")

#: The picture the converter draws of a converted map, which Phase 3 Step 3 brings over as the
#: thumbnail a real-world bank has instead of `figures.render_route`. `scenarionet-10hz` pairs with
#: `stage-6-map-10hz.png`; plain `scenarionet` with `stage-6-map.png`, which is the fallback too --
#: `mosque` has three dataset directories and two pictures.
MAP_IMAGE_STEM = "stage-6-map"

#: The stages a manifest records a status for, in order. Reported rather than judged, except for
#: stage 5 -- see `_warnings`, and Step 3, which refuses anything but `passed` at import.
STAGES = ("stage_1b", "stage_2", "stage_4", "stage_5", "stage_6")

#: Every global a workspace pickle may name. All of them build inert numpy data and none of them
#: can run anything else. Measured: across the four workspaces, all 21 pickles name only
#: `numpy.array`; the rest are here because a numpy that pickles arrays through `_reconstruct`
#: rather than `array` is a version difference, not a different file format.
ALLOWED_GLOBALS = frozenset(
    {
        ("numpy", "array"),
        ("numpy", "ndarray"),
        ("numpy", "dtype"),
        ("numpy", "bool_"),
        ("numpy", "float32"),
        ("numpy", "float64"),
        ("numpy", "int32"),
        ("numpy", "int64"),
        ("numpy.core.multiarray", "_reconstruct"),
        ("numpy._core.multiarray", "_reconstruct"),
        ("numpy.core.multiarray", "scalar"),
        ("numpy._core.multiarray", "scalar"),
    }
)


class WorkspaceError(RuntimeError):
    """Raised when a path is not a readable converter workspace."""


class _Unpickler(pickle.Unpickler):
    """A pickle reader that may build numpy arrays and nothing else.

    `find_class` is the only place an unpickler can be made to execute something, so refusing there
    is the whole guard. It raises `WorkspaceError` naming what was refused rather than
    `UnpicklingError`, because the answer to "this file wants `os.system`" is a sentence to a
    person, not a stack trace.
    """

    def find_class(self, module: str, name: str) -> Any:
        if (module, name) not in ALLOWED_GLOBALS:
            raise WorkspaceError(
                f"{self.path} names {module}.{name}, which a workspace pickle has no business "
                "naming. This file holds code, not scenario data; it was not read."
            )
        return super().find_class(module, name)


def read_pickle(path: Path) -> Any:
    """Load one workspace pickle through the restricted unpickler."""
    with path.open("rb") as handle:
        reader = _Unpickler(handle)
        reader.path = path
        try:
            return reader.load()
        except WorkspaceError:
            raise
        except Exception as error:  # noqa: BLE001 - pickle raises most of the builtin tree
            raise WorkspaceError(f"{path} is not readable as a pickle: {error}") from error


class Provenance(BaseModel):
    """The chain that says which lane model, reviewed by whom, this was built from."""

    model_config = ConfigDict(extra="forbid")

    generator_version: str | None
    generation_fingerprint: str | None
    source_osm_sha256: str | None
    reviewed_lane_model_sha256: str | None
    stage_5_status: str | None


class Route(BaseModel):
    """The ego's route, which is what `destination` is on the procedural side.

    `duration_s` is the one field a PG scenario has no equivalent of: the recorded drive took this
    long, so the step budget is **measured** rather than inherited from a category. That is Step 3's
    to use; this module only reports it.
    """

    model_config = ConfigDict(extra="forbid")

    source: str | None
    name: str | None
    start_lane: str | None
    end_lane: str | None
    lane_count: int
    lane_changes: int | None
    junction_movements: int | None
    distance_m: float | None
    speed_kph: float | None
    slowest_kph: float | None
    duration_s: float | None
    driving_duration_s: float | None
    waiting_s: float | None
    stop_count: int


class Scenario(BaseModel):
    """One stored scenario: what it is, how fast it was sampled, and who is in it."""

    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    file: str
    size_bytes: int
    dataset: str | None
    coordinate: str | None
    sdc_id: str | None
    #: `len(ts)`, cross-checked against the scenario file's own `length` -- a disagreement is a
    #: warning rather than an error, because the summary and the file are written by one pass and
    #: a difference means one of them was edited afterwards.
    steps: int
    length: int | None
    step_hz: float | None
    duration_s: float | None
    map_features: int | None
    #: The same features split by ScenarioNet type -- `LANE_SURFACE_STREET`,
    #: `ROAD_EDGE_BOUNDARY`, `ROAD_LINE_BROKEN_SINGLE_WHITE`. The total says how big the map is;
    #: the split says what kind of map it is, and a conversion that dropped its lane markings
    #: reads identically to one that dropped its road edges until they are counted apart.
    map_feature_types: dict[str, int]
    #: Track counts by ScenarioNet type: `VEHICLE`, `PEDESTRIAN`, `CYCLIST`, `TRAFFIC_BARRIER`.
    #: This is what replaces the six option axes -- contents of a recording rather than knobs.
    tracks: dict[str, int]
    lights: dict[str, int]
    route: Route | None
    provenance: Provenance


class Signals(BaseModel):
    """The traffic-light plan, and the sentence that has to travel with it.

    Read because it is a caveat about the data rather than about us. OSM records only that a signal
    exists -- it carries no cycle, no split and no offset -- so every number here was
    **synthesised** by the converter's stage 6 signal builder, and none of it was surveyed.
    `note` is the converter's own wording, carried verbatim rather than paraphrased: a result
    scored against these lights is scored against an invented plan, and the sentence saying so
    must survive into the bank.

    `lane_model_signals` is what the reviewed lane model declares and `phase_groups` is what stage 6
    built from it. They can disagree -- `mosque` declares four signals and built no phase groups at
    all -- and a recording with no light in it looks exactly like a junction with no lights.
    """

    model_config = ConfigDict(extra="forbid")

    source: str | None
    version: int | None
    cycle_seconds: float | None
    time_step_s: float | None
    phase_groups: int | None
    signalled_lanes: int | None
    lane_model_signals: int | None
    note: str | None


class Entry(BaseModel):
    """One top-level thing in the workspace, and what it weighs.

    An import copies a dataset directory and a thumbnail and leaves the rest behind, so this is the
    list that decision is made over -- measured by walking rather than assumed from a fixed list of
    names, because the day the converter starts writing a new directory is the day a fixed list
    stops mentioning it.

    `checksummed` is how many of the entry's files `source/manifest.json` records a sha256 for. A
    bank keeps the checksum rather than the bytes, so an entry with none cannot be recorded at all,
    only dropped -- and which entries those are is a measurement, not a guess. Measured on
    `junction-1`: `inspection/`, `lane-model/`, `normalized/`, `reports/`, `review/` and `source/`
    are recorded; `actors/`, `drives/`, `routes/`, `signals/`, `traffic/` and `review.json` are not.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    kind: Literal["dataset", "map_image", "directory", "file"]
    files: int
    size_bytes: int
    checksummed: int


class Dataset(BaseModel):
    """One ScenarioNet dataset directory inside a workspace."""

    model_config = ConfigDict(extra="forbid")

    name: str
    scenarios: list[Scenario]
    #: Named in `dataset_summary.pkl` but absent from disk. A dataset that is missing one of its
    #: scenario files is unimportable, and saying which one is missing is the whole answer.
    missing_files: list[str]
    map_image: str | None


class WorkspaceReport(BaseModel):
    """Everything `scenariobank workspace` read, as data. The text report renders this."""

    model_config = ConfigDict(extra="forbid")

    report_version: Literal[1]
    name: str
    path: str
    manifest_version: int | None
    acquired_at: str | None
    #: The one field that silently invalidates every result if it is wrong, which is why it is
    #: reported with its source: `explicit_cli` was chosen by a person, anything else was inferred.
    driving_side: str | None
    driving_side_source: str | None
    attribution: str | None
    origin: dict[str, float] | None
    bounds: dict[str, float] | None
    stages: dict[str, str | None]
    provenance: Provenance
    tool_versions: dict[str, str]
    #: `path` -> `sha256` for every artefact the manifest records one for. This is how a bank keeps
    #: the 3 MB of review HTML and the 1.5 MB lane model without keeping the bytes -- the same
    #: choice Phase 2 made about `base_config`: a record of the input, not the input.
    artifacts: dict[str, str]
    #: The traffic-light plan and its caveat, or `None` where nothing dynamic was built.
    signals: Signals | None
    #: Everything at the top level of the workspace, with its weight. What an import leaves behind
    #: is decided over this list.
    contents: list[Entry]
    #: What `stage_6` records about the conversion that ran last. Not an index of the datasets
    #: below -- see the module docstring.
    last_conversion: dict[str, Any]
    datasets: list[Dataset]
    warnings: list[str]


def _provenance(source: dict[str, Any]) -> Provenance:
    return Provenance(
        generator_version=source.get("generator_version"),
        generation_fingerprint=source.get("generation_fingerprint"),
        source_osm_sha256=source.get("source_osm_sha256"),
        reviewed_lane_model_sha256=source.get("reviewed_lane_model_sha256"),
        stage_5_status=source.get("stage_5_status"),
    )


def _manifest_provenance(manifest: dict[str, Any]) -> Provenance:
    """The same five fields the summary carries, assembled from the stages that own them.

    The summary's `metadata.provenance` is one block; the manifest keeps the same facts under the
    stage that produced each, so they are gathered here into the same shape. Reporting them in one
    shape is what makes a manifest that disagrees with its own dataset visible at a glance.
    """
    stage_4 = manifest.get("stage_4") or {}
    checksums = stage_4.get("input_checksums") or {}
    reviewed = (stage_4.get("artifacts") or {}).get("reviewed_lane_model") or {}
    return Provenance(
        generator_version=stage_4.get("generator_version"),
        generation_fingerprint=stage_4.get("generation_fingerprint"),
        source_osm_sha256=checksums.get("source_osm"),
        reviewed_lane_model_sha256=reviewed.get("sha256"),
        stage_5_status=(manifest.get("stage_5") or {}).get("status"),
    )


def _route(source: dict[str, Any] | None) -> Route | None:
    if not source:
        return None
    return Route(
        source=source.get("source"),
        name=str(source["name"]) if source.get("name") is not None else None,
        start_lane=source.get("start_lane"),
        end_lane=source.get("end_lane"),
        lane_count=len(source.get("lanes") or ()),
        lane_changes=source.get("lane_changes"),
        junction_movements=source.get("junction_movements"),
        distance_m=source.get("distance_m"),
        speed_kph=source.get("speed_kph"),
        slowest_kph=source.get("slowest_kph"),
        duration_s=source.get("duration_s"),
        driving_duration_s=source.get("driving_duration_s"),
        waiting_s=source.get("waiting_s"),
        stop_count=len(source.get("stops") or ()),
    )


def _rate(timestamps: Any) -> tuple[int, float | None, float | None]:
    """Steps, rate and wall-clock duration, taken from the scenario's own timestamp array.

    The median interval rather than the first: one duplicated or dropped timestamp would move a
    mean and cannot move a median, and this number is the difference between a 380-step budget and
    a 3800-step one.
    """
    import numpy as np

    if timestamps is None:
        return 0, None, None
    stamps = np.asarray(timestamps, dtype=float)
    if stamps.size < 2:
        return int(stamps.size), None, None
    interval = float(np.median(np.diff(stamps)))
    rate = round(1.0 / interval, 3) if interval > 0 else None
    return int(stamps.size), rate, round(float(stamps[-1] - stamps[0]), 3)


def _counts(entries: dict[str, Any] | None) -> dict[str, int]:
    """Track or light counts by type, commonest first, ties broken by name so JSON is stable."""
    tally: dict[str, int] = {}
    for entry in (entries or {}).values():
        kind = str(entry.get("type", "UNKNOWN"))
        tally[kind] = tally.get(kind, 0) + 1
    return dict(sorted(tally.items(), key=lambda item: (-item[1], item[0])))


def _map_image(root: Path, dataset_name: str) -> str | None:
    """The converter's picture of this conversion, by the naming convention it uses.

    `scenarionet-10hz` pairs with `stage-6-map-10hz.png`. Plain `scenarionet` has no suffix, and a
    suffixed directory whose own picture was never drawn falls back to the unsuffixed one -- which
    is what `mosque` needs, having three dataset directories and one picture.
    """
    suffix = dataset_name[len(DATASET_PREFIX) :].lstrip("-")
    candidates = [f"{MAP_IMAGE_STEM}-{suffix}.png"] if suffix else []
    candidates.append(f"{MAP_IMAGE_STEM}.png")
    for candidate in candidates:
        if (root / candidate).is_file():
            return candidate
    return None


def _scenario(directory: Path, file_name: str, meta: dict[str, Any]) -> Scenario:
    """One row of a dataset: its summary entry, plus the tracks only the file itself carries."""
    path = directory / file_name
    steps, rate, duration = _rate(meta.get("ts"))
    stored = read_pickle(path)
    return Scenario(
        scenario_id=str(meta.get("scenario_id") or stored.get("id") or file_name),
        file=file_name,
        size_bytes=path.stat().st_size,
        dataset=meta.get("dataset"),
        coordinate=meta.get("coordinate"),
        sdc_id=meta.get("sdc_id"),
        steps=steps,
        length=stored.get("length"),
        step_hz=rate,
        duration_s=duration,
        map_features=len(stored.get("map_features") or {}),
        map_feature_types=_counts(stored.get("map_features")),
        tracks=_counts(stored.get("tracks")),
        lights=_counts(stored.get("dynamic_map_states")),
        route=_route(meta.get("sdc_route")),
        provenance=_provenance(meta.get("provenance") or {}),
    )


def _dataset(root: Path, directory: Path) -> Dataset:
    """One dataset directory, read through its own index rather than by globbing.

    The summary names the scenario files and `dataset_mapping.pkl` says which subdirectory each
    sits in -- that is ScenarioNet's contract, and following it is what makes a merged dataset of
    several conversions readable here without a second code path.
    """
    summary = read_pickle(directory / DATASET_INDEX[0])
    mapping = read_pickle(directory / DATASET_INDEX[1])
    if not isinstance(summary, dict):
        raise WorkspaceError(f"{directory / DATASET_INDEX[0]} is not a dataset summary")

    scenarios, missing = [], []
    for file_name, meta in summary.items():
        relative = Path(mapping.get(file_name, "") or "") / file_name
        if not (directory / relative).is_file():
            missing.append(str(relative))
            continue
        scenarios.append(_scenario(directory, str(relative), meta))
    return Dataset(
        name=directory.name,
        scenarios=scenarios,
        missing_files=missing,
        map_image=_map_image(root, directory.name),
    )


def _signals(manifest: dict[str, Any]) -> Signals | None:
    """The light plan, from the two places stage 6 splits it across.

    `stage_6.signals` is the plan the signal builder synthesised; `stage_6.converted` is what came
    out the other end. Both are needed to tell "this junction has no lights" from "this junction has
    four signals and none of them was built".
    """
    stage_6 = manifest.get("stage_6") or {}
    converted = stage_6.get("converted") or {}
    plan = stage_6.get("signals") or {}
    if not plan and not converted.get("signals"):
        return None
    return Signals(
        source=plan.get("source"),
        version=plan.get("signals_version"),
        cycle_seconds=plan.get("cycle_seconds"),
        time_step_s=plan.get("time_step_s"),
        phase_groups=converted.get("phase_groups", len(plan.get("groups") or ()) or None),
        signalled_lanes=converted.get("signalled_lanes"),
        lane_model_signals=converted.get("signals"),
        note=plan.get("note"),
    )


def _artifacts(manifest: dict[str, Any]) -> dict[str, str]:
    """Every `{"path": ..., "sha256": ...}` the manifest records, flattened to one mapping.

    Walked rather than read from a fixed list of stages: the manifest keeps these triples under
    whichever stage produced the file, seven levels of nesting deep in places, and a reader that
    named the places would miss the next one the converter adds.
    """
    found: dict[str, str] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            path, digest = node.get("path"), node.get("sha256")
            if isinstance(path, str) and isinstance(digest, str):
                found[path] = digest
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(manifest)
    return dict(sorted(found.items()))


def _contents(root: Path, datasets: list[Dataset], artifacts: dict[str, str]) -> list[Entry]:
    """Every top-level entry, its weight, and how much of it the manifest has a checksum for."""
    dataset_names = {dataset.name for dataset in datasets}
    images = {dataset.map_image for dataset in datasets if dataset.map_image}
    entries = []
    for path in sorted(root.iterdir()):
        files = sorted(one for one in path.rglob("*") if one.is_file()) if path.is_dir() else [path]
        relative = {one.relative_to(root).as_posix() for one in files}
        entries.append(
            Entry(
                name=path.name,
                kind=(
                    "dataset"
                    if path.name in dataset_names
                    else "map_image"
                    if path.name in images
                    else "directory"
                    if path.is_dir()
                    else "file"
                ),
                files=len(files),
                size_bytes=sum(one.stat().st_size for one in files),
                checksummed=len(relative & set(artifacts)),
            )
        )
    return entries


def _warnings(report_parts: dict[str, Any]) -> list[str]:
    """What is worth saying out loud about this workspace, in the order it becomes a problem.

    Every one of these is a thing that reads as fine in the manifest and is not fine on disk, which
    is the only kind of finding a reader can contribute -- it builds nothing, so it cannot discover
    anything a run would have.
    """
    manifest: dict[str, Any] = report_parts["manifest"]
    datasets: list[Dataset] = report_parts["datasets"]
    provenance: Provenance = report_parts["provenance"]
    lines: list[str] = []

    if not datasets:
        lines.append(
            "no dataset directory: nothing here is importable yet, only the map it was built from"
        )
    if provenance.stage_5_status != "passed":
        lines.append(
            f"stage 5 is {provenance.stage_5_status!r}, not 'passed' -- import will refuse this"
        )
    if manifest.get("driving_side_source") not in {"explicit_cli", None}:
        lines.append(
            f"drive side {manifest.get('driving_side')!r} was "
            f"{manifest.get('driving_side_source')}, not stated by a person -- confirm it "
            "before anything is scored on this"
        )

    stage_6 = manifest.get("stage_6") or {}
    named = stage_6.get("dataset_dir")
    unrecorded = [one.name for one in datasets if one.name != named]
    if unrecorded:
        holds = "holds" if len(unrecorded) == 1 else "hold"
        listed = ", ".join(unrecorded)
        lines.append(
            f"stage_6 records {named!r} only; {listed} {holds} conversions the manifest does "
            "not describe"
            if named
            else f"stage_6 records no dataset directory, but {listed} {holds} converted scenarios"
        )

    for dataset in datasets:
        for name in dataset.missing_files:
            lines.append(f"{dataset.name}: {name} is named in the summary and is not on disk")
        if dataset.map_image is None:
            lines.append(
                f"{dataset.name}: no {MAP_IMAGE_STEM} picture -- a bank would have no thumbnail"
            )
        for scenario in dataset.scenarios:
            # Qualified by directory, not by scenario id: two dataset directories of one workspace
            # routinely hold the same scenario id at different rates -- `junction-1` has one in
            # `scenarionet/` and the same one in `scenarionet-100hz/` -- and an unqualified warning
            # about each would read as the same line printed twice.
            where = f"{dataset.name}/{scenario.scenario_id}"
            if scenario.length is not None and scenario.length != scenario.steps:
                lines.append(
                    f"{where}: the file says {scenario.length} steps and its "
                    f"timestamps say {scenario.steps}"
                )
            if scenario.provenance.generation_fingerprint != provenance.generation_fingerprint:
                lines.append(
                    f"{where}: built from lane model "
                    f"{(scenario.provenance.generation_fingerprint or '?')[:16]}, "
                    f"the manifest's is {(provenance.generation_fingerprint or '?')[:16]}"
                )
            others = {kind: n for kind, n in scenario.tracks.items() if kind != "VEHICLE"}
            if not others and not scenario.lights:
                lines.append(
                    f"{where}: the ego and nothing else -- no pedestrians, no "
                    "cyclists, no barriers, no lights were baked into this conversion"
                )
            if stage_6.get("step_hz") and scenario.step_hz and (
                abs(float(stage_6["step_hz"]) - scenario.step_hz) > 1e-6
            ):
                lines.append(
                    f"{where}: sampled at {scenario.step_hz:g} Hz, "
                    f"stage_6 says {float(stage_6['step_hz']):g} Hz"
                )
    return lines


def read_workspace(path: Path) -> WorkspaceReport:
    """Read one converter workspace. Writes nothing, builds nothing, imports no simulator."""
    root = Path(path)
    manifest_path = root / "source" / "manifest.json"
    if not manifest_path.is_file():
        raise WorkspaceError(
            f"{root} is not a converter workspace: no source/manifest.json. A workspace is one "
            "directory under the converter's workspaces/, e.g. .../workspaces/junction-1"
        )
    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as error:
        raise WorkspaceError(f"{manifest_path} is not readable as JSON: {error}") from error

    directories = sorted(
        entry
        for entry in root.iterdir()
        if entry.is_dir()
        and entry.name.startswith(DATASET_PREFIX)
        and all((entry / name).is_file() for name in DATASET_INDEX)
    )
    datasets = [_dataset(root, directory) for directory in directories]
    artifacts = _artifacts(manifest)
    provenance = _manifest_provenance(manifest)
    projection = ((manifest.get("stage_1b") or {}).get("projection") or {}).get("origin")
    stage_6 = manifest.get("stage_6") or {}

    return WorkspaceReport(
        report_version=1,
        name=root.name,
        path=str(root),
        manifest_version=manifest.get("manifest_version"),
        acquired_at=manifest.get("acquired_at"),
        driving_side=manifest.get("driving_side"),
        driving_side_source=manifest.get("driving_side_source"),
        attribution=manifest.get("attribution"),
        origin=(
            {"latitude": projection["latitude"], "longitude": projection["longitude"]}
            if projection
            else None
        ),
        bounds=(manifest.get("graph") or {}).get("bounds_wgs84"),
        stages={stage: (manifest.get(stage) or {}).get("status") for stage in STAGES},
        provenance=provenance,
        tool_versions={
            str(name): str(version)
            for name, version in sorted((manifest.get("tool_versions") or {}).items())
        },
        artifacts=artifacts,
        signals=_signals(manifest),
        contents=_contents(root, datasets, artifacts),
        last_conversion={
            "dataset_dir": stage_6.get("dataset_dir"),
            "step_hz": stage_6.get("step_hz"),
            "scenario_id": stage_6.get("scenario_id"),
            "map_features": stage_6.get("map_features"),
            "report": stage_6.get("report"),
        },
        datasets=datasets,
        warnings=_warnings(
            {"manifest": manifest, "datasets": datasets, "provenance": provenance}
        ),
    )


def _counts_line(counts: dict[str, int]) -> str:
    return ", ".join(f"{count} {kind}" for kind, count in counts.items()) or "none"


def format_report(report: WorkspaceReport) -> str:
    """Render the report as the terminal shows it: identity, then one block per dataset."""
    lines = [f"{report.name}  {report.path}"]
    lines.append(f"  drive side:   {report.driving_side} ({report.driving_side_source})")
    if report.origin:
        lines.append(
            f"  origin:       {report.origin['latitude']:.6f} N, "
            f"{report.origin['longitude']:.6f} E"
        )
    if report.bounds:
        bounds = report.bounds
        lines.append(
            f"  bounds:       {bounds['south']:.6f}..{bounds['north']:.6f} N, "
            f"{bounds['west']:.6f}..{bounds['east']:.6f} E"
        )
    lines.append(f"  attribution:  {report.attribution}")
    lines.append(
        "  stages:       "
        + ", ".join(f"{stage.replace('stage_', '')} {status}" for stage, status in
                    report.stages.items())
    )
    fingerprint = report.provenance.generation_fingerprint or "?"
    osm = report.provenance.source_osm_sha256 or "?"
    reviewed = report.provenance.reviewed_lane_model_sha256 or "?"
    lines.append(f"  lane model:   {fingerprint[:16]}  (osm {osm[:8]}, reviewed {reviewed[:8]})")
    lines.append(
        "  tools:        "
        + ", ".join(f"{name} {version}" for name, version in report.tool_versions.items())
    )
    if report.signals:
        signals = report.signals
        cycle = f"{signals.cycle_seconds:g} s cycle" if signals.cycle_seconds else "no cycle"
        lines.append(
            f"  signals:      {signals.lane_model_signals} in the lane model, "
            f"{signals.phase_groups} phase groups over {signals.signalled_lanes} lanes, "
            f"{cycle} ({signals.source})"
        )

    for dataset in report.datasets:
        picture = dataset.map_image or "no picture"
        lines.append(f"\n  {dataset.name}/  {len(dataset.scenarios)} scenario(s)  [{picture}]")
        for scenario in dataset.scenarios:
            rate = f"{scenario.step_hz:g} Hz" if scenario.step_hz else "rate unknown"
            duration = f"{scenario.duration_s:.1f} s" if scenario.duration_s else "?"
            lines.append(
                f"    {scenario.scenario_id}  {rate}  {scenario.steps} steps  {duration}"
                f"  {scenario.map_features} map features"
            )
            route = scenario.route
            if route:
                lines.append(
                    f"      route {route.name}: {route.start_lane} -> {route.end_lane}"
                    f"  {route.distance_m:.1f} m over {route.lane_count} lanes,"
                    f" {route.lane_changes} lane changes, {route.junction_movements} junction moves"
                )
                lines.append(
                    f"      drive {route.duration_s:.1f} s at up to {route.speed_kph:g} kph"
                    f" (slowest {route.slowest_kph:g}), waiting {route.waiting_s:g} s,"
                    f" {route.stop_count} stops"
                )
            lines.append(f"      actors: {_counts_line(scenario.tracks)}")
            lines.append(f"      lights: {_counts_line(scenario.lights)}")

    # No "no datasets" line here: `_warnings` already says it, and better -- it says what that
    # means for an import rather than only that the directory is absent.
    for warning in report.warnings:
        lines.append(f"  ! {warning}")
    return "\n".join(lines)


__all__ = [
    "ALLOWED_GLOBALS",
    "Dataset",
    "Entry",
    "Provenance",
    "Route",
    "Scenario",
    "Signals",
    "WorkspaceError",
    "WorkspaceReport",
    "format_report",
    "read_pickle",
    "read_workspace",
]
