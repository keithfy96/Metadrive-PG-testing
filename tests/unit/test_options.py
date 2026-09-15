"""`resolve_options`: names in, numerics out, with every refusal named.

Nothing here needs a simulator. The one test that touches a bank on disk reads `banks/curve`'s
pinned block and skips if that bank is not checked out; every other test builds the same block
in memory, so the resolver's behaviour is pinned even on a machine with no bank at all.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from scenariobank.bank import Manifest, OptionLevels, read_manifest
from scenariobank.handedness import DRIVE_SIDE_LEFT
from scenariobank.options import (
    AXES,
    LABELS,
    LEVEL_NAMES,
    LEVELS,
    NUMERIC_AXES,
    PHASE_8_LIGHTS,
    REPLAY_FLAGS,
    TIERS,
    TRAFFIC_FLOOR,
    OptionError,
    ResolvedOptions,
    describe,
    nearest_level,
    options_for,
    resolve_options,
)

CURVE_BANK = Path("banks/curve")

needs_bank = pytest.mark.skipif(
    not (CURVE_BANK / "manifest.json").exists(),
    reason=f"needs the generated bank at {CURVE_BANK}",
)

#: What `banks/curve` pins, copied here so the in-memory tests read the same block the on-disk
#: one does.
CURVE_PINNED = {"traffic": "low", "cones": "medium", "barriers": "high", "pedestrians": "low"}


def _manifest(source: str = "pg", **pinned: str) -> Manifest:
    return Manifest(
        schema_version="1.4",
        bank_id="test",
        created_utc="2026-09-07T00:00:00Z",
        source=source,
        metadrive={"edition": None, "dist_version": None, "commit": None, "asset_version": None},
        base_config={},
        drive_side=DRIVE_SIDE_LEFT,
        options=OptionLevels(**pinned),
        categories={},
    )


def _curve() -> Manifest:
    return _manifest(**CURVE_PINNED)


# --- the tables -------------------------------------------------------------------------------


def test_levels_covers_every_axis_and_every_level_and_nothing_else():
    assert tuple(LEVELS) == AXES
    for axis in AXES:
        assert tuple(LEVELS[axis]) == LEVEL_NAMES, axis


def test_traffic_none_is_exactly_zero_and_every_other_traffic_level_clears_the_floor():
    # `traffic_manager.py:65-67` returns before placing anything below 1e-2. A "low" written
    # under that would be a "none" that every result called "low".
    assert LEVELS["traffic"]["none"] == 0.0
    for name in LEVEL_NAMES[1:]:
        assert LEVELS["traffic"][name] >= TRAFFIC_FLOOR, name


@pytest.mark.parametrize("axis", [axis for axis in NUMERIC_AXES if axis != "traffic"])
def test_every_count_axis_starts_at_zero_and_never_decreases(axis):
    numbers = [LEVELS[axis][name] for name in LEVEL_NAMES]
    assert numbers[0] == 0
    assert numbers == sorted(numbers)
    assert all(isinstance(number, int) for number in numbers)


def test_every_tier_names_all_six_axes_and_none_of_them_touches_lights_yet():
    for tier, block in TIERS.items():
        assert tuple(block) == AXES, tier
        assert block["lights"] == "none", f"{tier}: lights is Phase 8"
    assert set(PHASE_8_LIGHTS) <= set(TIERS)


def test_tiers_are_ordered_easy_to_hard_on_every_axis():
    for axis in AXES:
        ranks = [LEVEL_NAMES.index(TIERS[tier][axis]) for tier in ("easy", "medium", "hard")]
        assert ranks == sorted(ranks), axis


# --- resolving a procedural bank -------------------------------------------------------------


def test_no_flags_resolves_to_exactly_what_the_bank_pins():
    resolved = resolve_options(_curve())
    assert resolved.kind == "pg"
    assert resolved.tier is None
    assert resolved.levels == {**dict.fromkeys(AXES, "none"), **CURVE_PINNED}
    assert set(resolved.origin.values()) == {"manifest"}
    assert resolved.raw == {} and resolved.replay == {}


def test_every_record_carries_the_six_names_and_the_six_numerics():
    resolved = resolve_options(_curve())
    assert tuple(resolved.levels) == AXES
    assert tuple(resolved.values) == AXES
    assert tuple(resolved.origin) == AXES
    for axis in AXES:
        assert resolved.values[axis] == LEVELS[axis][resolved.levels[axis]]


def test_tier_hard_expands_to_six_names_and_overrides_the_pin():
    resolved = resolve_options(_curve(), tier="hard")
    assert resolved.tier == "hard"
    assert [resolved.levels[axis] for axis in AXES] == [
        "high", "medium", "medium", "medium", "low", "none"
    ]
    assert set(resolved.origin.values()) == {"tier"}


def test_an_explicit_level_beats_the_tier_on_its_axis_only():
    resolved = resolve_options(_curve(), tier="hard", levels={"traffic": "low"})
    assert resolved.levels["traffic"] == "low"
    assert resolved.origin["traffic"] == "flag"
    for axis in AXES:
        if axis != "traffic":
            assert resolved.levels[axis] == TIERS["hard"][axis], axis
            assert resolved.origin[axis] == "tier"


def test_an_explicit_level_without_a_tier_leaves_the_other_axes_pinned():
    resolved = resolve_options(_curve(), levels={"cones": "none"})
    assert resolved.levels["cones"] == "none" and resolved.origin["cones"] == "flag"
    assert resolved.levels["barriers"] == "high" and resolved.origin["barriers"] == "manifest"


def test_a_raw_value_is_the_number_the_env_gets_and_the_nearest_name_is_recorded_beside_it():
    resolved = resolve_options(_curve(), raw={"traffic": 0.25})
    assert resolved.values["traffic"] == 0.25
    assert resolved.levels["traffic"] == "medium"  # 0.05 from medium (0.3), 0.15 from low (0.1)
    assert resolved.origin["traffic"] == "raw"
    assert resolved.raw == {"traffic": 0.25}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.0, "none"),
        (0.27, "medium"),  # 0.03 from medium (0.3), 0.17 from low (0.1)
        (0.20, "low"),  # exactly between low and medium: the weaker wins
        (0.9, "high"),
    ],
)
def test_nearest_level_rounds_a_raw_traffic_density_to_the_closest_name(value, expected):
    assert nearest_level("traffic", value) == expected


def test_a_raw_count_resolves_the_same_way():
    resolved = resolve_options(_curve(), raw={"cones": 3})
    assert resolved.levels["cones"] == "medium"  # 1 from medium (4), 2 from low (1)
    assert resolved.values["cones"] == 3
    assert resolve_options(_curve(), raw={"cones": 5}).levels["cones"] == "medium"  # tie: weaker


# --- refusals -----------------------------------------------------------------------------------


def test_lights_above_none_is_refused_by_name_until_phase_8():
    with pytest.raises(OptionError, match="Phase 8"):
        resolve_options(_curve(), levels={"lights": "low"})


def test_a_bank_that_pinned_lights_is_refused_and_told_how_to_unpin_it():
    # `scenariobank options --lights medium` accepts the level today; the resolver is where it
    # stops, and the sentence has to say how to get past it.
    with pytest.raises(OptionError, match="Phase 8.*--lights none"):
        resolve_options(_manifest(lights="medium"))


def test_lights_at_none_is_not_refused_from_any_origin():
    assert resolve_options(_manifest(lights="none"), tier="hard").levels["lights"] == "none"
    assert resolve_options(_curve(), levels={"lights": "none"}).levels["lights"] == "none"


def test_a_raw_traffic_density_in_the_manager_dead_zone_is_refused_rather_than_run_as_none():
    with pytest.raises(OptionError, match="traffic_manager.py:65-67"):
        resolve_options(_curve(), raw={"traffic": 0.005})
    # The two edges of the gap are fine: exactly zero is `none`, the floor itself places traffic.
    assert resolve_options(_curve(), raw={"traffic": 0.0}).levels["traffic"] == "none"
    assert resolve_options(_curve(), raw={"traffic": TRAFFIC_FLOOR}).values["traffic"] == 0.01


@pytest.mark.parametrize(
    ("raw", "why"),
    [
        ({"traffic": -0.1}, "negative"),
        ({"cones": 2.5}, "not a whole number"),
        ({"lights": 1}, "no number behind its levels"),
        ({"weather": 1}, "not an option axis"),
    ],
)
def test_a_raw_value_that_cannot_be_placed_is_refused_by_name(raw, why):
    with pytest.raises(OptionError, match=why):
        resolve_options(_curve(), raw=raw)


def test_the_same_axis_as_both_a_level_and_a_raw_value_is_two_answers_and_refused():
    with pytest.raises(OptionError, match="traffic.*both"):
        resolve_options(_curve(), levels={"traffic": "low"}, raw={"traffic": 0.05})


@pytest.mark.parametrize(
    ("kwargs", "why"),
    [
        ({"tier": "brutal"}, "not a tier"),
        ({"levels": {"weather": "high"}}, "not an option axis"),
        ({"levels": {"traffic": "enormous"}}, "not a level for traffic"),
    ],
)
def test_an_unknown_tier_axis_or_level_is_refused_by_name(kwargs, why):
    with pytest.raises(OptionError, match=why):
        resolve_options(_curve(), **kwargs)


# --- a recorded bank ----------------------------------------------------------------------------


def test_a_recorded_bank_resolves_to_the_three_replay_switches_and_no_axes():
    resolved = resolve_options(_manifest(source="osm-scenario"))
    assert resolved.kind == "recorded"
    assert resolved.replay == REPLAY_FLAGS
    assert resolved.levels == {} and resolved.values == {} and resolved.origin == {}


@pytest.mark.parametrize(
    "kwargs", [{"tier": "easy"}, {"levels": {"traffic": "low"}}, {"raw": {"traffic": 0.05}}]
)
def test_a_recorded_bank_refuses_any_axis_because_a_recording_has_no_knobs(kwargs):
    with pytest.raises(OptionError, match="no option axes"):
        resolve_options(_manifest(source="osm-scenario"), **kwargs)


def test_replay_config_and_the_record_read_the_same_replay_switches():
    # One dict in `options.py`; `env.replay_config` spreads it and the resolver copies it. A test
    # on the source rather than on a built env, so it holds on a machine without the simulator.
    from scenariobank import env

    assert env.REPLAY_FLAGS is REPLAY_FLAGS
    assert "**REPLAY_FLAGS" in Path(env.__file__).read_text()


# --- the record and the seam --------------------------------------------------------------------


def test_the_record_round_trips_and_forbids_a_field_nobody_declared():
    resolved = resolve_options(_curve(), tier="medium", raw={"cyclists": 3})
    again = ResolvedOptions.model_validate(resolved.model_dump())
    assert again == resolved
    with pytest.raises(ValidationError):
        ResolvedOptions.model_validate({**resolved.model_dump(), "weather": "wet"})


def test_options_for_is_the_seam_and_hands_back_the_banks_options_unchanged():
    resolved = resolve_options(_curve(), tier="easy")
    assert options_for(resolved, entry=object(), row=object()) is resolved


@needs_bank
def test_the_real_curve_bank_resolves_to_the_block_it_pins():
    manifest = read_manifest(CURVE_BANK)
    resolved = resolve_options(manifest)
    assert resolved.levels == manifest.options.model_dump()
    assert {axis: resolved.levels[axis] for axis in CURVE_PINNED} == CURVE_PINNED


def test_describe_is_the_six_axes_as_data_in_form_order():
    schema = describe()
    assert schema["schema_version"] == 1
    assert [axis["name"] for axis in schema["axes"]] == list(AXES)
    assert schema["level_names"] == list(LEVEL_NAMES)
    assert schema["tiers"] == TIERS
    for axis in schema["axes"]:
        assert axis["label"] == LABELS[axis["name"]]
        # The numbers are the resolver's own table, not a copy of it.
        assert axis["levels"] == LEVELS[axis["name"]]
        assert axis["numeric"] == (axis["name"] in NUMERIC_AXES)
        assert (axis["raw"] is None) == (not axis["numeric"])
    by_name = {axis["name"]: axis for axis in schema["axes"]}
    assert by_name["traffic"]["raw"] == {"minimum": 0, "integer": False, "floor": TRAFFIC_FLOOR}
    assert by_name["cones"]["raw"] == {"minimum": 0, "integer": True, "floor": None}
    # Every level runs today on five axes; lights offers none alone and says why.
    for name in NUMERIC_AXES:
        assert by_name[name]["choices"] == list(LEVEL_NAMES) and by_name[name]["restricted"] is None
    assert by_name["lights"]["choices"] == ["none"]
    assert "Phase 8" in by_name["lights"]["restricted"]


def test_every_axis_has_a_label_and_the_labels_are_the_flags_help():
    assert set(LABELS) == set(AXES)
    from typer.main import get_command

    from scenariobank.cli import app

    options = {p.name: p for p in get_command(app).commands["options"].params}
    run = {p.name: p for p in get_command(app).commands["run"].params}
    for axis in AXES:
        assert options[axis].help.startswith(LABELS[axis])
        assert run[axis].help.startswith(LABELS[axis])
