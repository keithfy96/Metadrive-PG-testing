"""`env.py`: the seam between a manifest entry and an env, both kinds.

Two halves, the way `test_replay.py` is split. The **config** half needs MetaDrive importable
(`base_config` names `StateObservation`) but builds no env, and pins the three keys that silently
do nothing when they are wrong: `horizon` is the entry's `max_steps` and not `base_config`'s
1000, `num_scenarios` is `num_scenarios_for(seeds)` and not `len(seeds)`, `start_seed` is the
smallest seed. The **live** half drives the procedural banks in `banks/` through `replay.drive`,
which is the first time this package steps a PG road, and checks the things only a drive can:
every seed resets, the route ends where the manifest says, the episode ends at the entry's cap
and not at 1000, a row's own budget caps it shorter, and the observation is 19 wide at both ends.

The live tests name real banks and skip when a bank is absent, the way `test_replay.py` skips
without `banks/junction-1`. They are cheap -- a `T` road is 320 steps at under half a millisecond
each -- so nothing here is module-scoped.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scenariobank.bank import (
    CategoryEntry,
    Manifest,
    RealWorldEntry,
    RealWorldRow,
    ScenarioRow,
    SimulatorInfo,
    num_scenarios_for,
    read_manifest,
)
from scenariobank.config import OBSERVATION_SHAPE, SCENARIO_OBSERVATION_SHAPE
from scenariobank.doctor import has_simulator
from scenariobank.env import (
    DEFAULT_DECISION_REPEAT,
    DEFAULT_PHYSICS_STEP_S,
    build_config,
    expected_shape,
    replay_config,
    seed_for,
    step_hz_for,
)
from scenariobank.options import resolve_options
from scenariobank.replay import BUDGETED, MAX_STEP_PHRASE, drive
from scenariobank.workspace import Provenance

needs_sim = pytest.mark.skipif(
    not has_simulator(),
    reason="needs_sim: MetaDrive is not installed (uv sync --group sim)",
)

T_JUNCTION = Path("banks/t-junction")
LEFT = Path("banks/t-junction-left-intersection")
CURVE = Path("banks/curve")
JUNCTION = Path("banks/junction-1")


def needs_bank(bank: Path):
    return pytest.mark.skipif(
        not (bank / "manifest.json").exists(), reason=f"needs the bank at {bank}"
    )


def row(index: int = 0, *, steps: int = 400) -> RealWorldRow:
    """One recorded row, the same shape `test_replay.row` builds."""
    return RealWorldRow(
        scenario_id=f"junction-x_{index:04d}",
        scenario_index=index,
        file=f"sd_osm-scenario_v1_junction-x-{index}.pkl",
        stored_id=f"junction-x-{index}",
        max_steps=steps,
        route_length_m=395.1,
        duration_s=steps / 100.0,
        tracks={"VEHICLE": 3},
        lights={"TRAFFIC_LIGHT": 8},
        route=None,
        map_features=974,
        map_feature_types={"LANE_SURFACE_STREET": 434},
        thumbnail=None,
    )


def recorded_entry(rows: list[RealWorldRow]) -> RealWorldEntry:
    return RealWorldEntry(
        description="a junction, converted",
        dataset_dir="dataset",
        step_hz=100.0,
        origin=None,
        attribution="(c) OpenStreetMap contributors",
        provenance=Provenance(
            generator_version="1.0.0",
            generation_fingerprint="abc123",
            source_osm_sha256=None,
            reviewed_lane_model_sha256=None,
            stage_5_status="reviewed",
        ),
        tool_versions={"osmnx": "1.9.3"},
        artifacts={},
        copied=[],
        signals=None,
        max_steps=max(one.max_steps for one in rows),
        scenarios=rows,
    )


def imported(rows: list[RealWorldRow] | None = None) -> Manifest:
    rows = rows if rows is not None else [row()]
    return Manifest(
        schema_version="1.4",
        bank_id="junction-x",
        created_utc="2026-09-01T00:00:00Z",
        source="osm-scenario",
        metadrive=SimulatorInfo(edition=None, dist_version=None, commit=None, asset_version=None),
        base_config={},
        drive_side="left",
        categories={"junction-x": recorded_entry(rows)},
    )


def pg_row(seed: int, index: int, destination: str = "1T0_1_", **overrides) -> ScenarioRow:
    return ScenarioRow(
        scenario_id=f"t_junction_{index:04d}",
        seed=seed,
        destination=destination,
        spawn_lane_index=0,
        route_length_m=111.7,
        net_rotation_deg=0.0,
        turn_pairs="",
        thumbnail=None,
        **overrides,
    )


def pg_entry(seeds: list[int], *, max_steps: int = 320) -> CategoryEntry:
    """A `T` category over `seeds`, in the order given."""
    return CategoryEntry(
        description="a T junction",
        block_seq="T",
        exit_rule="straight",
        max_steps=max_steps,
        scenarios=[pg_row(seed, index) for index, seed in enumerate(seeds)],
    )


def pg_manifest(entry: CategoryEntry, **options) -> Manifest:
    return Manifest(
        schema_version="1.4",
        bank_id="tj",
        created_utc="2026-09-01T00:00:00Z",
        metadrive=SimulatorInfo(edition=None, dist_version=None, commit=None, asset_version=None),
        base_config={},
        drive_side="left",
        options=options,
        categories={"t_junction": entry},
    )


# --- offline: the seam's arithmetic --------------------------------------------------------


def test_the_seed_handed_to_reset_is_the_rows_seed_or_its_index():
    """Both bound an index in `base_env.py:926`, which is why one `num_scenarios_for` sizes both."""
    assert seed_for(pg_row(28, 1)) == 28
    assert seed_for(row(3)) == 3


def test_the_expected_width_is_the_config_constant_for_the_kind():
    assert expected_shape(pg_entry([0])) == OBSERVATION_SHAPE == (19,)
    assert expected_shape(recorded_entry([row()])) == SCENARIO_OBSERVATION_SHAPE == (31,)


def test_a_road_steps_at_metadrives_default_rate_and_a_recording_at_its_own():
    """10 Hz is 0.02 s x 5, the two keys `base_config` does not touch; the test below pins that it
    still does not, so this number stays the number the env runs at."""
    assert step_hz_for(pg_entry([0])) == 1 / (DEFAULT_PHYSICS_STEP_S * DEFAULT_DECISION_REPEAT)
    assert step_hz_for(pg_entry([0])) == 10.0
    assert step_hz_for(recorded_entry([row()])) == 100.0


# --- config: the three keys that silently do nothing when wrong ----------------------------


@needs_sim
def test_a_procedural_entrys_horizon_is_its_own_max_steps_and_not_the_base_configs_1000():
    """`horizon` is the config key (`metadrive_env.py:58`); `max_step` is a `TerminationState`
    field and setting it does nothing. `t_junction` is 320 and `CCS_only` 1320, and until this
    was wired both ran to 1000."""
    entry = pg_entry([0, 2, 3, 4], max_steps=320)
    config = build_config(Path("."), entry, resolve_options(pg_manifest(entry)))
    assert config["horizon"] == 320
    assert config["map"] == "T"


@needs_sim
def test_num_scenarios_bounds_an_index_so_a_gapped_seed_list_is_sized_by_its_span():
    """`banks/curve` holds seeds `[30, 1, 2, 3, 22]`: five scenarios, `num_scenarios` 30."""
    entry = pg_entry([30, 1, 2, 3, 22])
    config = build_config(Path("."), entry, resolve_options(pg_manifest(entry)))
    assert config["start_seed"] == 1
    assert config["num_scenarios"] == num_scenarios_for([30, 1, 2, 3, 22]) == 30


@needs_sim
def test_the_traffic_axis_is_the_one_option_knob_the_config_reads():
    """The pinned level's number, from `LEVELS`, and nothing else moves. The other four numeric
    axes are counts for the managers Step 4b builds and no config key exists for them yet."""
    entry = pg_entry([0])
    config = build_config(Path("."), entry, resolve_options(pg_manifest(entry, traffic="low")))
    assert config["traffic_density"] == 0.05
    assert config["random_traffic"] is False
    unpinned = build_config(Path("."), entry, resolve_options(pg_manifest(entry)))
    assert unpinned["traffic_density"] == 0.0


@needs_sim
def test_a_procedural_config_leaves_the_step_rate_at_metadrives_default():
    """What makes `step_hz_for`'s 10 Hz true. If either key is ever set here, that function has
    to read it back rather than assume."""
    entry = pg_entry([0])
    config = build_config(Path("."), entry, resolve_options(pg_manifest(entry)))
    assert "physics_world_step_size" not in config
    assert "decision_repeat" not in config


@needs_sim
def test_a_recorded_entry_of_one_row_builds_replay_configs_dict(tmp_path):
    """The Phase 3 Step 6 config is the per-row form of the same dict; for the one-row entries
    every bank here holds, the two are identical."""
    manifest = imported([row(3, steps=400)])
    entry = manifest.categories["junction-x"]
    assert isinstance(entry, RealWorldEntry)
    config = build_config(tmp_path, entry, resolve_options(manifest))
    assert config == replay_config(tmp_path, entry, entry.scenarios[0])
    assert (config["start_scenario_index"], config["num_scenarios"]) == (3, 1)
    assert config["horizon"] == 400


@needs_sim
def test_a_recorded_entry_of_many_rows_is_sized_like_a_procedural_one(tmp_path):
    """Same `num_scenarios_for` on both kinds: the recording indices bound an index too."""
    manifest = imported([row(0, steps=100), row(4, steps=400)])
    entry = manifest.categories["junction-x"]
    config = build_config(tmp_path, entry, resolve_options(manifest))
    assert (config["start_scenario_index"], config["num_scenarios"]) == (0, 5)
    assert config["horizon"] == entry.max_steps == 400


@needs_sim
def test_options_resolved_for_the_wrong_kind_are_refused_at_the_seam():
    """The resolver and the builder are handed the same manifest by every caller; this is the
    check for the day one of them is not."""
    entry = pg_entry([0])
    with pytest.raises(ValueError, match="'recorded' bank cannot build a procedural env"):
        build_config(Path("."), entry, resolve_options(imported()))


# --- live: the first procedural drives -----------------------------------------------------


@needs_sim
@needs_bank(T_JUNCTION)
def test_a_road_ends_at_its_entrys_own_cap_and_not_at_1000():
    """The verify block's first two claims: `t_junction` at 320, `CCS_only` at 1320, both by
    `max_step` -- the env said so, the loop did not have to."""
    manifest = read_manifest(T_JUNCTION)
    short = drive(T_JUNCTION, manifest, scenario="t_junction_0000")
    long = drive(T_JUNCTION, manifest, scenario="CCS_only_0000")
    assert (short.steps, short.budget) == (320, 320)
    assert (long.steps, long.budget) == (1320, 1320)
    for episode in (short, long):
        assert episode.kind == "pg"
        assert episode.flags["max_step"] is True
        assert episode.ended_by == MAX_STEP_PHRASE["pg"]
        assert episode.stored_id is None and episode.scenario_index is None


@needs_sim
@needs_bank(T_JUNCTION)
def test_a_rows_own_budget_caps_it_below_the_entrys_horizon(tmp_path):
    """`horizon` is the entry's and one env carries one of them, so a row's own `max_steps` is the
    loop's to enforce. Made on a scratch copy with `set_max_steps`, and the sibling row is driven
    after it to show the cap was the row's and not the env's."""
    import shutil

    from scenariobank.bank import set_max_steps

    scratch = tmp_path / "scratch-tj"
    shutil.copytree(T_JUNCTION, scratch)
    set_max_steps(scratch, "t_junction_0000", 100)
    manifest = read_manifest(scratch)
    capped = drive(scratch, manifest, scenario="t_junction_0000")
    sibling = drive(scratch, manifest, scenario="t_junction_0002")
    assert (capped.steps, capped.budget) == (100, 100)
    assert capped.flags["max_step"] is False
    assert capped.ended_by == BUDGETED
    assert (sibling.steps, sibling.budget) == (320, 320)


@needs_sim
@needs_bank(LEFT)
def test_the_route_ends_where_the_manifest_row_says_on_rows_whose_destinations_differ():
    """`StdTInterSection` exposes a different arm per seed, so `set_route` after the reset is the
    only way one env per entry can hold both. Read back off the navigation, not echoed."""
    manifest = read_manifest(LEFT)
    rows = {
        row.scenario_id: row for row in manifest.categories["t_junction"].scenarios
    }
    differing = [
        one for one in rows.values() if one.destination != rows["t_junction_0000"].destination
    ]
    assert differing, "the bank no longer holds two destinations in one category"
    for chosen in (rows["t_junction_0000"], differing[0]):
        episode = drive(LEFT, manifest, scenario=chosen.scenario_id)
        assert episode.seed == chosen.seed
        assert episode.destination == chosen.destination


@needs_sim
@needs_bank(CURVE)
def test_every_seed_in_a_gapped_seed_list_resets_and_observes_19_at_both_ends():
    """`banks/curve` is `[30, 1, 2, 3, 22]`: the first seed is the largest, which is exactly the
    case `len(seeds)` as `num_scenarios` fails on. Capped short, because five full `CC` drives
    prove nothing the first hundred steps of each do not."""
    manifest = read_manifest(CURVE)
    entry = manifest.categories["curve"]
    for chosen in entry.scenarios:
        episode = drive(CURVE, manifest, scenario=chosen.scenario_id, steps=100)
        assert episode.seed == chosen.seed
        assert episode.observation_shape == OBSERVATION_SHAPE
        assert episode.observation_shape_end == OBSERVATION_SHAPE
        assert episode.action_shape == (2,)


@needs_sim
@needs_bank(CURVE)
def test_a_pinned_traffic_level_spawns_traffic_without_hitting_a_stationary_ego():
    """`banks/curve` pins traffic=low, so this is the first drive with cars in it. Under a zero
    action nothing should be hit -- and the collision counts say so, which is the rising-edge
    counter's first live reading."""
    manifest = read_manifest(CURVE)
    assert manifest.options.traffic == "low"
    episode = drive(CURVE, manifest)
    assert episode.collisions == {
        "vehicle": 0, "object": 0, "building": 0, "human": 0, "sidewalk": 0
    }
    assert episode.ended_by == MAX_STEP_PHRASE["pg"]


@needs_sim
@needs_bank(JUNCTION)
def test_the_recorded_drive_is_unchanged_by_the_seam():
    """Phase 3 Step 6's numbers, through the new loop: 3782 frames, 31 wide."""
    episode = drive(JUNCTION, read_manifest(JUNCTION), steps=50)
    assert episode.kind == "recorded"
    assert episode.seed is None and episode.destination is None
    assert episode.budget == 3782
    assert episode.observation_shape == SCENARIO_OBSERVATION_SHAPE
    assert episode.step_hz == 100.0
