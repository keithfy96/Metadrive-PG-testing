"""`scenariobank replay`: the first tests in this repo that ever step an environment.

Everything before this stopped at `reset`. `doctor` builds an env to read its spaces and closes
it; `sockets`, `destinations` and `figures` reset one to measure a road. Nothing ran a loop, which
is why Phase 3 could import a recording, review it and still not know whether it drives.

Split the way the rest of the suite is. The **offline** half is every refusal and every piece of
arithmetic -- which bank is wrong, which scenario id does not exist, what stride a decision rate
means -- and it runs on a machine with no MetaDrive. The **`needs_sim`** half drives
`banks/junction-1` for real and pins the three numbers that were claims until Step 6 measured
them: the episode is 3782 steps, the observation is 31 wide at both ends, and the stride moves the
action count without moving the episode length.

The live half is expensive on purpose -- a full replay is about 10 s -- so it runs the whole
recording exactly twice, once at every step and once at 20 Hz, and checks the slower rates against
a capped run. Two full replays are what the length-invariance claim costs; three would buy nothing.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from scenariobank.bank import (
    BankError,
    CategoryEntry,
    Manifest,
    RealWorldEntry,
    RealWorldRow,
    ScenarioRow,
    SimulatorInfo,
    read_manifest,
)
from scenariobank.config import SCENARIO_OBSERVATION_SHAPE
from scenariobank.doctor import has_simulator
from scenariobank.replay import (
    CAPPED,
    IDLE_ACTION,
    drive,
    format_episode,
    recorded,
    replay_config,
    select,
    stride_for,
)
from scenariobank.workspace import Provenance

needs_sim = pytest.mark.skipif(
    not has_simulator(),
    reason="needs_sim: MetaDrive is not installed (uv sync --group sim)",
)

#: The imported bank the live half drives. Skipped rather than failed when it is absent, the way
#: the import tests skip without the converter checkout: it is 50 MB of copied pickles and not
#: every checkout of this repo will have run an import.
LIVE_BANK = Path("banks/junction-1")

needs_bank = pytest.mark.skipif(
    not (LIVE_BANK / "manifest.json").exists(),
    reason=f"needs the imported bank at {LIVE_BANK}",
)

#: What `banks/junction-1` measured on 2026-09-07, and what Step 6 exists to have measured.
FRAMES = 3782
STEP_HZ = 100.0


def row(index: int = 0, *, steps: int = 400) -> RealWorldRow:
    """One recorded row, with only the fields `replay` reads filled in meaningfully."""
    return RealWorldRow(
        scenario_id=f"junction-x_{index:04d}",
        scenario_index=index,
        file=f"sd_osm-scenario_v1_junction-x-{index}.pkl",
        stored_id=f"junction-x-{index}",
        max_steps=steps,
        route_length_m=395.1,
        duration_s=steps / STEP_HZ,
        tracks={"VEHICLE": 3},
        lights={"TRAFFIC_LIGHT": 8},
        route=None,
        map_features=974,
        map_feature_types={"LANE_SURFACE_STREET": 434},
        thumbnail=None,
    )


def entry(rows: list[RealWorldRow]) -> RealWorldEntry:
    """The imported entry those rows sit in."""
    return RealWorldEntry(
        description="a junction, converted",
        dataset_dir="dataset",
        step_hz=STEP_HZ,
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
    """A whole imported bank, in memory. No files: nothing offline here opens one."""
    rows = rows if rows is not None else [row()]
    return Manifest(
        schema_version="1.4",
        bank_id="junction-x",
        created_utc="2026-09-01T00:00:00Z",
        source="osm-scenario",
        metadrive=SimulatorInfo(
            edition=None, dist_version=None, commit=None, asset_version=None
        ),
        base_config={},
        drive_side="left",
        categories={"junction-x": entry(rows)},
    )


def procedural() -> Manifest:
    """A PG bank, which `replay` refuses."""
    return Manifest(
        schema_version="1.4",
        bank_id="curve",
        created_utc="2026-09-01T00:00:00Z",
        metadrive=SimulatorInfo(
            edition=None, dist_version=None, commit=None, asset_version=None
        ),
        base_config={},
        drive_side="left",
        categories={
            "curve": CategoryEntry(
                description="a curve",
                block_seq="CC",
                exit_rule="straight",
                max_steps=500,
                scenarios=[
                    ScenarioRow(
                        scenario_id="curve_0000",
                        seed=0,
                        destination="1C0_1_",
                        spawn_lane_index=0,
                        route_length_m=100.0,
                        net_rotation_deg=0.0,
                        turn_pairs="straight-straight",
                        thumbnail=None,
                    )
                ],
            )
        },
    )


# --- offline: the refusals ---------------------------------------------------------------


def test_a_procedural_bank_is_refused_by_name_and_not_half_driven():
    """The mirror of `review`'s old refusal, and there for the same reason.

    A PG scenario needs `MetaDriveEnv` and a route set per row -- a different env with a different
    setup. A `replay` that quietly did half of it would be the second step loop Phase 4 is written
    to avoid.
    """
    with pytest.raises(BankError, match="procedural bank"):
        recorded(procedural())


def test_an_imported_bank_is_not_refused():
    recorded(imported())


def test_refusing_needs_no_simulator():
    """`drive` guards before it imports MetaDrive, so a wrong bank is answered off the manifest.

    Worth a test rather than a comment: the natural way to write the function puts the import
    first, and the cost of that is a refusal that only works on a machine that did not need one.
    """
    import scenariobank.replay as replay

    source = Path(replay.__file__).read_text()
    body = source[source.index("def drive("):]
    assert body.index("recorded(manifest)") < body.index("from metadrive")


# --- offline: choosing a row -------------------------------------------------------------


def test_the_first_recording_is_the_default_because_a_bank_holds_one():
    name, _entry, chosen = select(imported())
    assert (name, chosen.scenario_id) == ("junction-x", "junction-x_0000")


def test_a_recording_can_be_named():
    _name, _entry, chosen = select(imported([row(0), row(1)]), "junction-x_0001")
    assert chosen.scenario_index == 1


def test_an_unknown_scenario_id_says_so_rather_than_driving_the_first():
    with pytest.raises(BankError, match="no scenario named 'junction-x_0009'"):
        select(imported(), "junction-x_0009")


# --- offline: the stride ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("decision_hz", "expected"), [(None, 1), (100.0, 1), (20.0, 5), (10.0, 10), (5.0, 20)]
)
def test_the_decision_rate_is_a_stride_over_a_100_hz_recording(decision_hz, expected):
    """Replay advances one recorded frame per `env.step`, so `decision_repeat` is pinned at 1 and
    the only place a slower decision rate can live is the loop's own counter."""
    assert stride_for(STEP_HZ, decision_hz) == expected


def test_deciding_faster_than_the_recording_was_sampled_is_refused():
    with pytest.raises(ValueError, match="faster than the recording"):
        stride_for(STEP_HZ, 200.0)


@pytest.mark.parametrize("bad", [0.0, -5.0])
def test_a_decision_rate_must_be_positive(bad):
    with pytest.raises(ValueError, match="must be positive"):
        stride_for(STEP_HZ, bad)


# --- offline: the config ------------------------------------------------------------------


@needs_sim
def test_the_config_replays_at_the_rate_the_recording_was_sampled_at(tmp_path):
    """MetaDrive's default 0.02 x 5 is 10 Hz. Opening a 100 Hz recording at it replays every actor
    at a tenth of its speed while nothing raises, which is why this is pinned and tested."""
    config = replay_config(tmp_path, entry([row()]), row())
    assert config["physics_world_step_size"] == 1 / STEP_HZ
    assert config["decision_repeat"] == 1


@needs_sim
def test_the_horizon_is_the_recordings_own_length():
    """Measured, not inherited. With `horizon` at `BaseEnv`'s `None` default the env replays past
    the last recorded frame indefinitely -- so the cap is the row's own `budget_for`."""
    config = replay_config(Path("."), entry([row(steps=400)]), row(steps=400))
    assert config["horizon"] == 400


@needs_sim
def test_the_dataset_directory_is_made_absolute(tmp_path):
    """Stored relative because a bank is copied and mounted; resolved because MetaDrive reads it
    against the process's working directory rather than the bank's."""
    config = replay_config(tmp_path, entry([row()]), row())
    assert config["data_directory"] == str((tmp_path / "dataset").resolve())


@needs_sim
def test_one_scenario_is_opened_at_the_rows_own_index(tmp_path):
    config = replay_config(tmp_path, entry([row(0), row(3)]), row(3))
    assert (config["start_scenario_index"], config["num_scenarios"]) == (3, 1)


@needs_sim
def test_the_observation_rig_is_the_banks_own_and_not_metadrives_default(tmp_path):
    """A recording and a PG scenario are perceived through the same sensors even though they end
    up different widths -- so these come from `config.py` rather than being restated in
    `replay_config`."""
    from metadrive.obs.state_obs import StateObservation

    from scenariobank.config import SENSOR_CONFIG

    config = replay_config(tmp_path, entry([row()]), row())
    assert config["agent_observation"] is StateObservation
    assert config["vehicle_config"] == SENSOR_CONFIG


@needs_sim
def test_the_recordings_contents_are_replayed_as_recorded(tmp_path):
    """Traffic, lights and the rest are what Step 5's review reports about the bank. Turning any
    of them off here would make the drive disagree with the description."""
    config = replay_config(tmp_path, entry([row()]), row())
    assert config["no_traffic"] is False
    assert config["no_light"] is False
    assert config["reactive_traffic"] is False


@needs_sim
def test_the_config_is_not_the_pg_one(tmp_path):
    """`base_config` pins `start_seed` and `random_spawn_lane_index` for a generated road and
    installs the left-hand mirror over MetaDrive's PG lane geometry. A stored map has no generated
    geometry to mirror and already drives on whichever side it was recorded on."""
    config = replay_config(tmp_path, entry([row()]), row())
    assert "start_seed" not in config
    assert "random_spawn_lane_index" not in config


# --- live: the drive ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def full_run():
    """One full replay of `banks/junction-1`, shared by every test that reads the same drive.

    Module-scoped because it costs about 10 s. Every assertion below is about the same episode,
    so running it once is not a shortcut -- it is the correct scope for the claim.
    """
    return drive(LIVE_BANK, read_manifest(LIVE_BANK))


@needs_sim
@needs_bank
def test_a_recording_ends_when_it_runs_out_and_not_before(full_run):
    """The claim Step 6 exists to check. It ends at the recording's own length, and it ends
    because `horizon` says so -- `max_step` is what MetaDrive calls that."""
    assert full_run.steps == FRAMES
    assert full_run.budget == FRAMES
    assert full_run.flags["max_step"] is True
    assert full_run.truncated is True
    assert full_run.ended_by == "ran out of recording"


@needs_sim
@needs_bank
def test_nothing_crashed_or_arrived_under_a_zero_action(full_run):
    """A zero action goes nowhere, which is the point: the episode is measured, not driven. Route
    completion is a couple of percent and every crash flag is clear."""
    assert full_run.flags["arrive_dest"] is False
    assert not any(value for key, value in full_run.flags.items() if key.startswith("crash"))
    assert full_run.route_completion is not None
    assert 0.0 < full_run.route_completion < 0.1


@needs_sim
@needs_bank
def test_a_stored_scenario_observes_31_scalars_and_keeps_doing_so(full_run):
    """Not the 19 a PG bank produces. Same sensors both times -- the whole 12-wide difference is
    `TrajectoryNavigation`'s 22 scalars against `NodeNetworkNavigation`'s 10.

    Asserted at both ends of the episode rather than only at reset, because a width that changed
    mid-episode is exactly the failure a single reading cannot see, and no policy could be handed
    an observation that did that.
    """
    assert full_run.observation_shape == SCENARIO_OBSERVATION_SHAPE
    assert full_run.observation_shape_end == SCENARIO_OBSERVATION_SHAPE
    assert full_run.action_shape == (2,)


@needs_sim
@needs_bank
def test_at_every_step_one_action_is_issued_per_frame(full_run):
    assert (full_run.stride, full_run.actions) == (1, FRAMES)
    assert full_run.decision_hz is None


@needs_sim
@needs_bank
def test_a_slower_decision_rate_changes_the_actions_and_not_the_episode():
    """The second full replay, and the one that costs what it costs for a reason.

    Both halves of the claim need the whole recording: that 20 Hz issues a fifth of the actions is
    visible in the first hundred steps, but that it does not shorten the episode is only visible at
    the end of it.
    """
    at_20 = drive(LIVE_BANK, read_manifest(LIVE_BANK), decision_hz=20.0)
    assert at_20.stride == 5
    assert at_20.steps == FRAMES
    assert at_20.actions == math.ceil(FRAMES / 5) == 757


@needs_sim
@needs_bank
@pytest.mark.parametrize(("decision_hz", "stride"), [(10.0, 10), (5.0, 20)])
def test_the_slower_rates_stride_correctly_too(decision_hz, stride):
    """Capped short, because the length-invariance half is already proven at 20 Hz and re-proving
    it twice more would cost 20 s to learn nothing new. What is checked here is the stride acting:
    one action per `stride` steps, counted over a hundred of them."""
    short = drive(LIVE_BANK, read_manifest(LIVE_BANK), decision_hz=decision_hz, steps=100)
    assert short.stride == stride
    assert short.steps == 100
    assert short.actions == math.ceil(100 / stride)
    assert short.ended_by == CAPPED


@needs_sim
@needs_bank
def test_a_capped_run_says_it_was_capped_rather_than_that_the_recording_ran_out():
    """`--steps` is for checking the round trip without paying for the whole recording, and a
    report that called that "ran out of recording" would be lying about a 50-step run."""
    short = drive(LIVE_BANK, read_manifest(LIVE_BANK), steps=50)
    assert (short.steps, short.budget) == (50, FRAMES)
    assert short.ended_by == CAPPED
    assert short.flags["max_step"] is False


# --- the printed report -------------------------------------------------------------------


@needs_sim
@needs_bank
def test_the_printed_report_names_the_recording_and_what_ended_it(full_run):
    text = format_episode(full_run)
    assert full_run.stored_id in text
    assert "ran out of recording" in text
    assert "unchanged across the episode" in text
    assert "(31,)" in text


def test_a_changed_observation_width_is_called_out_rather_than_left_to_be_noticed():
    """Built by hand because it cannot be produced: the width has never changed mid-episode here.
    The warning exists for the day it does, and an unreachable branch that was never rendered
    would be a warning nobody could trust."""
    from scenariobank.replay import Episode

    episode = Episode(
        bank_id="junction-x",
        category="junction-x",
        scenario_id="junction-x_0000",
        stored_id="junction-x-0",
        scenario_index=0,
        step_hz=STEP_HZ,
        decision_hz=None,
        stride=1,
        budget=400,
        steps=400,
        actions=400,
        terminated=False,
        truncated=True,
        ended_by="ran out of recording",
        flags={"max_step": True},
        route_completion=0.02,
        observation_shape=(31,),
        observation_shape_end=(41,),
        action_shape=(2,),
        seconds=1.0,
        ms_per_step=2.5,
    )
    text = format_episode(episode)
    assert "CHANGED to (41,)" in text
    assert "no policy can be handed" in text


def test_the_idle_action_is_neither_steering_nor_throttle():
    """Named rather than written as a literal in the loop, so "no policy ran" is one thing on the
    record rather than a pair of zeroes someone has to recognise."""
    assert IDLE_ACTION == (0.0, 0.0)


# --- the command --------------------------------------------------------------------------


def run_cli(*argv):
    from typer.testing import CliRunner

    from scenariobank.cli import app

    return CliRunner().invoke(app, ["replay", *argv])


def test_the_command_refuses_a_procedural_bank_with_the_sentence(tmp_path):
    from scenariobank.bank import write_manifest

    write_manifest(tmp_path, procedural())
    result = run_cli("--bank", str(tmp_path))
    assert result.exit_code == 1
    assert "procedural bank" in result.output


def test_the_command_refuses_an_impossible_decision_rate_before_building_anything(tmp_path):
    """Off the manifest, so it costs nothing and works with no simulator installed."""
    from scenariobank.bank import write_manifest

    write_manifest(tmp_path, imported())
    result = run_cli("--bank", str(tmp_path), "--decision-hz", "200")
    assert result.exit_code == 1
    assert "faster than the recording" in result.output


def test_the_command_says_which_bank_when_there_is_no_manifest(tmp_path):
    result = run_cli("--bank", str(tmp_path))
    assert result.exit_code == 1
    assert "manifest.json" in result.output


@needs_sim
@needs_bank
def test_the_command_emits_json_that_round_trips_into_the_model():
    """`--json` is the shape a later step reads, so it is checked against the model rather than
    against a string. `extra="forbid"` means a field added and not printed fails here."""
    import json

    from scenariobank.replay import Episode

    result = run_cli("--bank", str(LIVE_BANK), "--steps", "20", "--json")
    assert result.exit_code == 0
    episode = Episode.model_validate(json.loads(result.output))
    assert episode.steps == 20
    assert episode.observation_shape == SCENARIO_OBSERVATION_SHAPE
