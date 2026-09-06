"""Writing `docs/reference/importing.md` -- what must come over when a workspace becomes a bank.

Generated for the reason `commands.md` and `destinations.md` are: a checklist that is retyped goes
stale, and this one's subject is somebody else's file format. It carries a second guarantee neither
of those has, and it is the point of the step -- **every row of the checklist names a field of
`workspace.py`'s report, and every field of that report is on the checklist.** A field the reader
learns to read and the checklist forgets is an error here; so is a checklist row naming a field
nothing reads. `_check` raises rather than publishing either gap, the same way `docs.reference`
raises for a command in no group.

The values are measured from a real workspace on every render -- there is no column of numbers
typed into this module. That is the house rule (re-measure, never quote) and it is also the only
way the checklist can be trusted: it describes an import that has not been written yet, so the one
thing it can be right about today is what is actually on disk.

**What is left behind is measured too.** The plan's draft of this list named `bags/`, which no
workspace here has, and claimed the manifest carries a checksum for everything excluded, which it
does not: `actors/`, `drives/`, `routes/`, `signals/`, `traffic/` and `review.json` have no sha256
anywhere in `source/manifest.json`, so a bank cannot record them, only drop them. Both corrections
came out of walking the directory rather than reading the manifest, which is the same lesson Step 1
learned about `stage_6`.

This module imports no simulator and does not build an environment. It does need a converter
workspace to read, the way `destinations` needs MetaDrive: the page cannot be regenerated on a
machine that has neither, and `tests/unit/test_importing.py` skips its drift test by name there
while still checking the field coverage, which needs no workspace at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

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

#: The workspace the checked-in page is measured on, relative to the repo root: the sibling
#: checkout the plan means, not `~/Desktop/work/wingfin/converter-scenarionet`, which holds
#: workspaces of the same four names whose conversions carry nothing but the ego.
EXAMPLE_WORKSPACE = Path("../wingfin-osm-scenarionet-converter/workspaces/junction-1")

#: Where the checklist lives. Beside the other two generated references.
IMPORTING_DOC = Path("docs/reference/importing.md")

#: Field name -> the model whose fields it expands into. Containers are not rows of their own; a
#: row for `provenance` saying "the provenance" beside five rows saying which five would be noise.
#: They are declared so `_check` can tell a container from a field somebody forgot.
CONTAINERS: dict[str, str] = {
    "provenance": "provenance",
    "signals": "signals",
    "datasets": "dataset",
    "contents": "entry",
    "dataset.scenarios": "scenario",
    "scenario.route": "route",
    "scenario.provenance": "provenance",
}

#: The checklist itself: sections in reading order, each a title, a blurb, and the fields it
#: covers with why each must be brought over. Keys are `<model>.<field>`, unprefixed for the
#: report's own fields; `_value` resolves them against the measured example.
SECTIONS: tuple[tuple[str, str, dict[str, str]], ...] = (
    (
        "Identity and provenance",
        "From `source/manifest.json`, and mirrored in the summary's `metadata.provenance`. This "
        "is the half a procedural bank has no equivalent of: a PG scenario is a seed and a "
        "category, and can be rebuilt from them. This one cannot be rebuilt from anything, so "
        "what it was built from has to travel with it.",
        {
            "name": "the workspace's name, which is where the bank's id comes from",
            "manifest_version": "the shape the rest of this was read out of -- a bank that kept "
            "the fields and not the version could not be re-read against a converter that "
            "renamed them",
            "acquired_at": "when the OSM extract was taken. The junction may have been rebuilt "
            "since, and nothing else on disk would say so",
            "driving_side": "the one field that silently invalidates every result. It is why "
            "Phase 2 measures drive side from the map rather than trusting a flag",
            "driving_side_source": "`explicit_cli` was stated by a person; anything else was "
            "inferred, and an inferred left-hand junction is a result nobody can defend",
            "attribution": "a licence obligation. It must survive into a result, not only into "
            "the bank",
            "origin": "where on earth this is. A PG bank has no such thing, and without it the "
            "coordinates are metres from nowhere",
            "bounds": "the extract box. What lies outside it is not empty road, it is the edge "
            "of the data -- a route that leaves the box leaves the map",
            "stages": "which of the converter's five stages passed. Step 3 refuses an import "
            "whose stage 5 is anything but `passed`",
            "tool_versions": "the analogue of `manifest.metadrive`: which osmnx, pyproj and "
            "shapely drew this road",
            "artifacts": "`path` -> `sha256` for every file the manifest records one for. This "
            "is how the megabytes left behind are still recorded -- a record of the input, not "
            "the input, the same choice Phase 2 made about `base_config`",
            "last_conversion": "what `stage_6` says about the conversion that ran **last**. "
            "Carried as a record of one run, never as the index of what the workspace holds",
            "provenance.generator_version": "which build of the converter's stage 4 produced the "
            "lane model",
            "provenance.generation_fingerprint": "names the exact lane model this was built from. "
            "A scenario whose fingerprint differs from the manifest's was built from a model that "
            "has since been revised",
            "provenance.source_osm_sha256": "the OSM extract, byte for byte",
            "provenance.reviewed_lane_model_sha256": "the human-reviewed model out of stage 4 -- "
            "the point in the chain where a person, not a tool, said the road was right",
            "provenance.stage_5_status": "refuse anything but `passed` at import",
        },
    ),
    (
        "The dataset -- all three files, from one directory",
        "ScenarioNet needs the triple: `dataset_summary.pkl`, `dataset_mapping.pkl`, and each "
        "`sd_*.pkl` the summary names. `data_directory` points at this one directory and nothing "
        "else in the workspace is read at run time, so the import is a copy of a directory, not a "
        "reassembly. **A workspace can hold more than one**, and they are not views of one "
        "conversion -- see the table above.",
        {
            "dataset.name": "which directory this came from. It is not decoration: `scenarionet/` "
            "and `scenarionet-100hz/` in this workspace are different routes, converted three "
            "hours apart",
            "dataset.map_image": "the thumbnail. A real-world bank has no `figures.render_route` "
            "to draw one with, so the converter's picture is the picture",
            "scenario.scenario_id": "the row id in the bank",
            "scenario.file": "the `sd_*.pkl` itself -- the only file of the three that holds the "
            "drive",
            "scenario.size_bytes": "what it costs. At 100 Hz this is most of what an import moves",
            "scenario.dataset": "ScenarioNet's own name for the format, which Step 3 stores as "
            "`Manifest.source` -- `osm-scenario` rather than `pg`",
            "scenario.coordinate": "`metadrive` means the tracks are already in the simulator's "
            "frame and no transform is applied on our side",
            "scenario.sdc_id": "which track is the ego. Every other track is replayed around it",
            "scenario.steps": "how many recorded frames there are, counted from the timestamps",
            "scenario.length": "what the file says it has. A disagreement with `steps` means one "
            "of the two was edited after the conversion",
            "scenario.step_hz": "**measured from `ts`, not read from `stage_6`.** The runner sets "
            "`physics_world_step_size = 1 / step_hz`; replaying a 100 Hz recording at "
            "MetaDrive's default 0.1 s runs it at a tenth speed and raises nothing",
            "scenario.duration_s": "the wall-clock span of the recording, from its own timestamps",
        },
    ),
    (
        "The ego's route",
        "From `metadata.sdc_route`, equivalently `stage_6.routes[]`. This is what `destination` "
        "is on the procedural side -- except that here it was driven, so the numbers are "
        "measurements rather than a category's constants. `duration_s` is the one a PG scenario "
        "has no equivalent of: the step budget is **measured**, which is the single place a "
        "real-world bank is better off than a generated one.",
        {
            "route.source": "who chose this route -- the stage 6 route builder, or a person",
            "route.name": "the route's own name, which is the tail of the scenario id",
            "route.start_lane": "where the ego is placed. A lane id, not a spawn index",
            "route.end_lane": "where it is trying to get to: `destination`, in the converter's "
            "vocabulary",
            "route.lane_count": "how many lanes the route runs through",
            "route.lane_changes": "how many of those are changes rather than continuations",
            "route.junction_movements": "how many junctions are crossed. This is the difficulty "
            "of the drive in one number, and the reason this junction is worth having",
            "route.distance_m": "`route_length_m` in the bank, the same field a PG scenario "
            "records from `navigation.total_length`",
            "route.speed_kph": "the fastest the recording goes",
            "route.slowest_kph": "and the slowest, which is what a policy has to be willing to do",
            "route.duration_s": "how long the drive took. With the rate, this is the step budget",
            "route.driving_duration_s": "the same span with the waiting taken out",
            "route.waiting_s": "how much of it was spent stopped. A scenario that is one third "
            "red light needs a budget that knows it",
            "route.stop_count": "how many times it stopped",
        },
    ),
    (
        "What replaces the six option axes",
        "They are not knobs on this path. The traffic, the pedestrians and the barriers were "
        "baked into the recording when it was converted, and the bank records what is in it "
        "rather than pretending it can set it. `resolve_options()` has nothing to resolve here, "
        "and Phase 4's env construction takes a different branch for the same reason.",
        {
            "scenario.tracks": "everyone who was recorded, by ScenarioNet type. One `VEHICLE` is "
            "the ego; the rest are replayed",
            "scenario.lights": "the traffic lights, from `dynamic_map_states`. Each carries a "
            "state for every recorded step",
            "scenario.map_features": "the map itself, as a feature count. It is what makes a "
            "cold reset here slower than generating a PG road, not faster",
            "scenario.map_feature_types": "the same features split by kind. A conversion that "
            "lost its lane markings reads identically to one that lost its road edges until "
            "these are counted apart",
        },
    ),
    (
        "The traffic lights, and the sentence that travels with them",
        "Carried **verbatim**, because it is a caveat about the data and not about us.",
        {
            "signals.source": "not surveyed and not observed -- invented from the single fact "
            "OSM records, which is that a signal is there",
            "signals.version": "which version of the signal builder invented this plan",
            "signals.cycle_seconds": "the full cycle it repeats on",
            "signals.time_step_s": "the resolution the plan was laid out at",
            "signals.phase_groups": "how many groups the lanes were divided into",
            "signals.signalled_lanes": "how many lanes actually got a light",
            "signals.lane_model_signals": "how many the reviewed lane model declared. It can "
            "exceed the groups built from it -- `mosque` declares four signals and built none",
            "signals.note": "the converter's own sentence about all of the above, quoted below "
            "rather than summarised",
        },
    ),
)

#: Read, and deliberately not carried into a bank. Keyed for the same reason the rows above are:
#: a field nobody can point at a reason for is a field somebody forgot.
LEFT: dict[str, str] = {
    "report_version": "the reader's output shape, which says nothing about the workspace",
    "path": "where the workspace happened to sit on the machine that read it. A bank that "
    "recorded this would be recording somebody's laptop",
    "warnings": "a reading of the workspace rather than part of it. At import they stop being "
    "warnings: Step 3 refuses, it does not caution",
    "dataset.missing_files": "a reading too -- what the summary names and the disk does not "
    "have. There is nothing to carry; the import refuses",
    "entry.name": "the top-level entries are measured to decide what an import leaves behind, "
    "and none of the measurement travels: the bank keeps the checksum, not this",
    "entry.kind": "same -- whether it is a dataset, a thumbnail or neither is a question asked "
    "at import time and answered once",
    "entry.files": "same",
    "entry.size_bytes": "same. It is what the table above weighs an import against",
    "entry.checksummed": "same. How much of an entry the manifest can record is what decides "
    "whether leaving it behind loses anything",
}

#: What happens to each top-level entry of a workspace that is not a dataset directory or a
#: thumbnail. Hand-written, because it is a decision rather than a measurement -- and keyed, so a
#: converter that starts writing a new directory makes this page raise rather than quietly omit it.
#: The sizes and the checksum counts beside each are measured.
VERDICTS: dict[str, tuple[str, str]] = {
    "source": (
        "partly copied",
        "`manifest.json` is the provenance and is copied whole. `map.osm` is 100 kB of input "
        "recorded by checksum",
    ),
    "reports": (
        "partly copied",
        "`scenario-conversion-<rate>.json` comes over -- it is this conversion's own report. The "
        "other sixteen are stage reports and are recorded by checksum",
    ),
    "inspection": (
        "recorded",
        "about 3 MB of review HTML. Written to be read by a person during conversion, and the "
        "conversion is over",
    ),
    "lane-model": (
        "recorded",
        "3 MB of JSON. The bank keeps its sha256, which is what `generation_fingerprint` is "
        "already checked against",
    ),
    "normalized": ("recorded", "the projected road network the lane model was built from"),
    "review": ("recorded", "the applied review decisions, and the reviewed OSM"),
    "review.json": (
        "dropped",
        "the review session's state. The manifest records no sha256 for it, so there is nothing "
        "to keep but the bytes",
    ),
    "review.partial.json": ("dropped", "an unfinished review session. Same, and less finished"),
    "actors": (
        "dropped",
        "the actor plan stage 6 built the recording from. What it produced is in `tracks`; the "
        "plan itself has no checksum in the manifest",
    ),
    "drives": (
        "dropped",
        "recorded drives from somebody's rig. Not this scenario, and not checksummed",
    ),
    "routes": ("dropped", "the route plan. What it produced is `sdc_route`"),
    "signals": ("dropped", "the signal plan. What it produced is in `dynamic_map_states`"),
    "traffic": ("dropped", "the traffic plan. What it produced is in `tracks`"),
}

#: Cells that would otherwise render as a wall: a mapping of 28 checksums, or a nested dict.
_FORMATS: dict[str, Any] = {
    "artifacts": lambda value: f"{len(value)} paths",
    "origin": lambda value: f"{value['latitude']:.5f} N, {value['longitude']:.5f} E",
    "bounds": lambda value: (
        f"{value['south']:.4f}..{value['north']:.4f} N, {value['west']:.4f}..{value['east']:.4f} E"
    ),
    "signals.note": lambda value: "quoted below",
}

HEADER = """# Importing a converter workspace

Generated by `scenariobank importing`. Do not edit by hand.

Every row below names a field of the reader in `workspace.py`, and every field of that reader is on
one of these lists -- the generator raises rather than publishing a checklist and a reader that
disagree. Every value is measured from a real workspace at render time; nothing here is typed in.

**What this is for.** A scenario somebody already generated and saved has no seed and no category,
so nothing about it can be rebuilt. Everything that makes it trustworthy -- which road, which lane
model, reviewed by whom, which side of the road, whose licence -- exists only as a fact recorded at
conversion time. This is the list of those facts, and it is what `scenariobank import` must carry
into a bank for a result scored on the scenario to mean anything afterwards.
"""


def _check() -> None:
    """Raise unless the checklist and the reader cover exactly the same fields.

    The guarantee this module exists for. A field added to `workspace.py` with no row here is a
    thing the reader learned to read and the checklist never mentions; a row here naming a field
    the reader does not have is a promise nothing keeps.
    """
    models = {
        "": WorkspaceReport,
        "provenance": Provenance,
        "signals": Signals,
        "dataset": Dataset,
        "scenario": Scenario,
        "route": Route,
        "entry": Entry,
    }
    known = {
        f"{prefix}.{name}" if prefix else name
        for prefix, model in models.items()
        for name in model.model_fields
    }
    listed: list[str] = [key for _, _, fields in SECTIONS for key in fields]
    listed += list(LEFT) + list(CONTAINERS)
    if len(listed) != len(set(listed)):
        duplicates = sorted({key for key in listed if listed.count(key) > 1})
        raise ValueError(f"importing lists {duplicates} more than once")

    missing = sorted(known - set(listed))
    unknown = sorted(set(listed) - known)
    if missing or unknown:
        raise ValueError(
            f"the checklist and workspace.py disagree: {missing} are read and not on the "
            f"checklist, {unknown} are on the checklist and not read. Every field the reader "
            "has must be either brought over, or listed in LEFT with the reason it is not."
        )


def choose(report: WorkspaceReport) -> tuple[Dataset, Scenario]:
    """The dataset and scenario the value column is measured from.

    The one `stage_6` says ran last, at the highest rate it holds -- which is what Step 3's
    `--rate 100` default would import. Not the first directory found: `junction-1`'s directories
    sort with an older ego-only conversion first, and a checklist measured on that one would
    describe an empty road.
    """
    named = (report.last_conversion or {}).get("dataset_dir")
    pairs = [
        (dataset, scenario) for dataset in report.datasets for scenario in dataset.scenarios
    ]
    if not pairs:
        raise WorkspaceError(
            f"{report.name} holds no converted scenario, so there is nothing to measure a "
            "checklist against. Point --path at a workspace that has been through stage 6."
        )
    return max(pairs, key=lambda pair: (pair[0].name == named, pair[1].step_hz or 0, pair[0].name))


def _resolve(key: str, report: WorkspaceReport, dataset: Dataset, scenario: Scenario) -> Any:
    """One checklist key's measured value, by the model its prefix names."""
    prefix, _, name = key.rpartition(".")
    subject: Any = {
        "": report,
        "provenance": report.provenance,
        "signals": report.signals,
        "dataset": dataset,
        "scenario": scenario,
        "route": scenario.route,
    }[prefix]
    return getattr(subject, name, None) if subject is not None else None


def _cell(key: str, value: Any) -> str:
    """One measured value, as a table cell. Long hashes are cut; nothing else is."""
    if key in _FORMATS:
        return _FORMATS[key](value) if value is not None else "none"
    if value is None:
        return "none"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, dict):
        if not value:
            return "none"
        # A tally reads "101 PEDESTRIAN"; anything else is a mapping and reads "stage_5 passed".
        # One rule for both put the version before the tool and the status before the stage.
        if all(isinstance(one, int) and not isinstance(one, bool) for one in value.values()):
            return ", ".join(f"{count} {name}" for name, count in value.items())
        return ", ".join(f"{name} {one}" for name, one in value.items())
    if isinstance(value, list):
        return f"{len(value)}"
    text = str(value)
    if len(text) == 64 and all(character in "0123456789abcdef" for character in text):
        return f"`{text[:16]}...`"
    return f"`{text}`"


def _datasets_table(report: WorkspaceReport) -> list[str]:
    """What this workspace actually holds, which is the question the checklist opens on."""
    lines = [
        "## What the example workspace holds",
        "",
        "One conversion per row, found by walking the workspace rather than by reading `stage_6`,",
        "which records only the run that happened last. A workspace with more than one of these is",
        "holding **different conversions**, not different views of one -- compare the routes.",
        "",
        "| directory | scenario | rate | steps | route | distance | recorded besides the ego "
        "| on disk |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for dataset in report.datasets:
        for scenario in dataset.scenarios:
            route = scenario.route
            others = sum(
                count for kind, count in scenario.tracks.items() if kind != "VEHICLE"
            ) + sum(scenario.lights.values())
            lines.append(
                f"| `{dataset.name}/` | `{scenario.scenario_id}` "
                f"| {scenario.step_hz:g} Hz | {scenario.steps} "
                f"| `{route.name if route else 'none'}` "
                f"| {route.distance_m:.1f} m | {others} besides the ego "
                f"| {scenario.size_bytes / 1e6:.1f} MB |"
            )
    lines.append("")
    return lines


def _section(
    title: str,
    blurb: str,
    fields: dict[str, str],
    report: WorkspaceReport,
    dataset: Dataset,
    scenario: Scenario,
) -> list[str]:
    lines = [f"## {title}", "", blurb, "", f"| field | `{report.name}` | why it must come |",
             "|---|---|---|"]
    for key, why in fields.items():
        value = _cell(key, _resolve(key, report, dataset, scenario))
        lines.append(f"| `{key}` | {value} | {why} |")
    lines.append("")
    return lines


def _left_behind(report: WorkspaceReport, dataset: Dataset) -> list[str]:
    """What an import does not copy, weighed -- and whether leaving it loses anything.

    Weighed against **one** dataset directory, not all of them. `junction-1` holds three and an
    import takes one; a total that added them up would say an import costs 57 MB when it costs 50.

    Raises on an entry with no verdict. That is deliberate: the alternative is a page that quietly
    stops mentioning a directory the converter started writing, which is the exact failure this
    document is generated to avoid.
    """
    unkeyed = sorted(
        entry.name
        for entry in report.contents
        if entry.kind not in {"dataset", "map_image"} and entry.name not in VERDICTS
    )
    if unkeyed:
        raise ValueError(
            f"{report.name} holds {unkeyed}, which importing.VERDICTS does not decide about. "
            "Every top-level entry needs a verdict: copied, recorded, or dropped."
        )

    lines = [
        "## What is left behind",
        "",
        "Measured by walking the workspace, not read off a list of names. **copied** goes into the",
        "bank; **recorded** means the manifest carries a sha256 and the bank keeps that instead of",
        "the bytes -- a record of the input, not the input; **dropped** means the manifest carries",
        "no checksum either, so there is nothing to keep. That last group is a finding rather than",
        "a choice: a bank cannot record what nothing hashed.",
        "",
        "| entry | on disk | files | checksummed | verdict | why |",
        "|---|---|---|---|---|---|",
    ]
    copied = 0
    for entry in sorted(report.contents, key=lambda one: one.name):
        if entry.kind == "dataset":
            verdict, why = (
                ("copied", "the dataset itself -- all three files, and the only directory "
                 "`data_directory` is pointed at")
                if entry.name == dataset.name
                else ("left", "another conversion of this junction. An import takes one, and "
                      "the rate is what decides which")
            )
        elif entry.kind == "map_image":
            verdict, why = (
                ("copied", "the thumbnail this bank has instead of a drawn route")
                if entry.name == dataset.map_image
                else ("left", "the picture belonging to a conversion this import is not taking")
            )
        else:
            verdict, why = VERDICTS[entry.name]
        if verdict == "copied":
            copied += entry.size_bytes
        lines.append(
            f"| `{entry.name}` | {entry.size_bytes / 1e6:.2f} MB | {entry.files} "
            f"| {entry.checksummed} | {verdict} | {why} |"
        )

    total = sum(entry.size_bytes for entry in report.contents)
    lines += [
        "",
        f"**An import of `{dataset.name}` moves {copied / 1e6:.1f} MB** of the "
        f"{total / 1e6:.1f} MB this workspace",
        "occupies, plus `manifest.json` and one conversion report. Almost all of it is a single",
        "`sd_*.pkl`, and almost all of *that* is the sampling rate: the same drive at 10 Hz is a",
        "tenth the size. That is the one decision an import cannot take back -- `step_hz` is fixed",
        "when the file is written, while a decision interval is a stride the runner picks on every",
        "run.",
        "",
    ]
    return lines


def _not_carried() -> list[str]:
    lines = [
        "## Read, and deliberately not carried",
        "",
        "Listed rather than omitted, so that every field of the reader is accounted for one way or",
        "the other. Nothing here is a gap; each is a reading of the workspace rather than a part",
        "of it.",
        "",
        "| field | why not |",
        "|---|---|",
    ]
    lines += [f"| `{key}` | {why} |" for key, why in LEFT.items()]
    lines.append("")
    return lines


def render(report: WorkspaceReport) -> str:
    """Render the checklist against one measured workspace."""
    _check()
    dataset, scenario = choose(report)

    lines = [HEADER, ""]
    lines += [
        f"Measured on **`{report.name}`** at `{report.path}`, acquired `{report.acquired_at}`.",
        f"The value column is `{dataset.name}/{scenario.scenario_id}` -- the conversion `stage_6`",
        f"records as the last to run, at the highest rate it holds ({scenario.step_hz:g} Hz),",
        "which is what an import takes by default.",
        "",
    ]
    lines += _datasets_table(report)
    for title, blurb, fields in SECTIONS:
        lines += _section(title, blurb, fields, report, dataset, scenario)
        if title.startswith("The traffic lights") and report.signals and report.signals.note:
            lines += [f"> {report.signals.note}", ""]
    lines += _left_behind(report, dataset)
    lines += _not_carried()
    return "\n".join(lines).rstrip() + "\n"


def write(path: Path = IMPORTING_DOC, workspace: Path = EXAMPLE_WORKSPACE) -> Path:
    """Read the example workspace and write the checklist."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(read_workspace(workspace)))
    return path


__all__ = [
    "EXAMPLE_WORKSPACE",
    "IMPORTING_DOC",
    "LEFT",
    "SECTIONS",
    "VERDICTS",
    "choose",
    "render",
    "write",
]
