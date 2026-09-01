"""The bank: what `generate` writes, and the two things about it that are easy to get wrong."""

import json

import pytest

from scenariobank.bank import (
    MANIFEST_NAME,
    BankError,
    Manifest,
    describe_config,
    generate,
    num_scenarios_for,
    read_manifest,
    replace_scenario,
    scenario_id,
    write_manifest,
)
from scenariobank.categories import CATEGORIES, SEEDS
from scenariobank.doctor import has_simulator
from scenariobank.handedness import DRIVE_SIDE_LEFT

needs_sim = pytest.mark.skipif(
    not has_simulator(),
    reason="needs_sim: MetaDrive is not installed (uv sync --group sim)",
)


def test_scenario_id_is_the_position_in_the_category_not_the_seed():
    assert scenario_id("curve", 0) == "curve_0000"
    assert scenario_id("curve", 12) == "curve_0012"


@pytest.mark.parametrize(
    ("seeds", "expected"),
    [((0, 1, 2, 3, 4), 5), ((0,), 1), ((3, 4), 2), ((0, 9), 10), ((7, 2), 6)],
)
def test_num_scenarios_sizes_a_range_and_not_a_count(seeds, expected):
    # `num_scenarios` reads as a count and bounds an *index*: `base_env.py:926` asserts
    # `start_index <= seed < start_index + num_scenarios`. Sizing it `len(seeds)` is right only
    # while the seeds are contiguous, so the two disagree exactly where it would start failing.
    assert num_scenarios_for(seeds) == expected
    assert num_scenarios_for((0, 9)) != len((0, 9))


def test_describe_config_records_the_observation_class_rather_than_dropping_it():
    # `agent_observation` is a class, and it is the key that pins the observation to Box(19,).
    # A manifest that silently omitted it would look complete and describe a different bank.
    described = describe_config({"agent_observation": Manifest, "nested": {"n": 1}, "on": True})
    assert described["agent_observation"] == "scenariobank.bank.Manifest"
    assert described == {
        "agent_observation": "scenariobank.bank.Manifest",
        "nested": {"n": 1},
        "on": True,
    }


def test_manifest_rejects_an_unknown_field():
    # `extra="forbid"` is what makes the schema version mean something: a field the writer added
    # and the reader does not know about is a failure, not a silently ignored key.
    raw = json.loads(_minimal_manifest().model_dump_json())
    raw["map_id"] = "deadbeef"
    with pytest.raises(ValueError):
        Manifest.model_validate(raw)


def test_read_manifest_says_what_a_missing_manifest_means(tmp_path):
    with pytest.raises(BankError, match="generation writes it last"):
        read_manifest(tmp_path)


def test_write_manifest_round_trips(tmp_path):
    write_manifest(tmp_path, _minimal_manifest())
    assert read_manifest(tmp_path) == _minimal_manifest()
    assert not list(tmp_path.glob("*.tmp"))


def _minimal_manifest() -> Manifest:
    return Manifest(
        schema_version="1.0",
        bank_id="test",
        created_utc="2026-08-31T00:00:00Z",
        metadrive={"edition": None, "dist_version": None, "commit": None, "asset_version": None},
        base_config={},
        drive_side=DRIVE_SIDE_LEFT,
        categories={},
    )


@needs_sim
def test_generate_writes_a_bank_the_runner_can_read(tmp_path):
    # Two categories on one block sequence, two seeds: enough to exercise the shared reset and
    # the per-seed loop without paying for all 35.
    manifest = generate(
        tmp_path,
        bank_id="test-bank",
        category_names=["intersection_left", "intersection_right"],
        seeds=(0, 1),
    )

    assert read_manifest(tmp_path) == manifest
    assert (tmp_path / MANIFEST_NAME).exists()
    assert manifest.drive_side == DRIVE_SIDE_LEFT
    assert sorted(manifest.categories) == ["intersection_left", "intersection_right"]

    left = manifest.categories["intersection_left"]
    assert left.max_steps == CATEGORIES["intersection_left"].max_steps
    assert [row.seed for row in left.scenarios] == [0, 1]
    assert [row.scenario_id for row in left.scenarios] == [
        "intersection_left_0000",
        "intersection_left_0001",
    ]
    # The rule resolved, not a hardcoded node: left and right must reach different exits.
    assert {row.destination for row in left.scenarios} == {"1X0_1_"}
    assert {row.destination for row in manifest.categories["intersection_right"].scenarios} == {
        "1X2_1_"
    }

    for row in left.scenarios:
        assert row.route_length_m > 0
        assert (tmp_path / row.thumbnail).stat().st_size > 0


@needs_sim
def test_the_two_x_categories_share_a_seed_reset_and_so_share_a_spawn_lane(tmp_path):
    # The three `X` categories are one junction driven three ways, so one reset serves them all.
    # If that ever stops being true the spawn lanes diverge, and the five seeds of an `X`
    # category stop being comparable -- the spawn lane is the only thing separating them.
    manifest = generate(
        tmp_path,
        bank_id="test-bank",
        category_names=["intersection_left", "intersection_straight"],
        seeds=SEEDS,
        thumbnails=False,
    )
    lanes = {
        name: [row.spawn_lane_index for row in entry.scenarios]
        for name, entry in manifest.categories.items()
    }
    assert lanes["intersection_left"] == lanes["intersection_straight"]
    assert lanes["intersection_left"] == [0, 1, 0, 1, 1]
    assert all(row.thumbnail is None for row in manifest.categories["intersection_left"].scenarios)


@needs_sim
def test_two_categories_on_one_road_get_different_thumbnails(tmp_path):
    # The bug this is here for: thumbnails used to be of the *map*, so the three `X` categories
    # at one seed wrote three byte-identical PNGs, and a `curve` thumbnail gave no way to tell a
    # right turn from a left one -- read from the wrong end of the road they look the same. A
    # thumbnail now draws the route and the spawn, so two routes on one road cannot collide.
    manifest = generate(
        tmp_path,
        bank_id="test-bank",
        category_names=["intersection_left", "intersection_right"],
        seeds=(0,),
    )
    images = {
        name: (tmp_path / entry.scenarios[0].thumbnail).read_bytes()
        for name, entry in manifest.categories.items()
    }
    assert len(set(images.values())) == 2, "same road, same picture -- the route is not drawn"

    # And the direction is in the data too, not only in the picture. Positive is left.
    turns = {
        name: entry.scenarios[0].net_rotation_deg for name, entry in manifest.categories.items()
    }
    assert turns["intersection_left"] == pytest.approx(90.0, abs=1.0)
    assert turns["intersection_right"] == pytest.approx(-90.0, abs=1.0)


@needs_sim
def test_curve_seeds_cover_all_four_turn_direction_pairs(tmp_path):
    # `CC` draws each block's direction independently, so what a seed picks is a *pair*. Seeds
    # 0-4 reach all four of LL, LR, RR, RL -- with five seeds over four pairs one must repeat,
    # and it is LL. Recorded per scenario so the bank itself says which way a curve turns;
    # `net_rotation_deg` alone cannot, since a gentle left and a left-right both read low.
    manifest = generate(
        tmp_path, bank_id="test-bank", category_names=["curve"], seeds=SEEDS, thumbnails=False
    )
    pairs = [row.turn_pairs for row in manifest.categories["curve"].scenarios]
    assert pairs == ["LL", "LR", "RR", "RL", "LL"]
    assert set(pairs) == {"LL", "LR", "RR", "RL"}


@needs_sim
def test_per_category_seeds_override_the_shared_list(tmp_path):
    # Two categories in one run on different seeds. They sit on different roads here, but the
    # grouping is keyed on (block_seq, seeds) precisely so that two categories sharing a road
    # can still be built at different seeds without sharing a reset.
    manifest = generate(
        tmp_path,
        bank_id="test-bank",
        category_names=["curve", "roundabout"],
        seeds=(0, 1),
        category_seeds={"curve": (0, 22)},
        thumbnails=False,
    )
    assert [row.seed for row in manifest.categories["curve"].scenarios] == [0, 22]
    assert [row.seed for row in manifest.categories["roundabout"].scenarios] == [0, 1]


def test_a_seed_override_for_an_unknown_category_is_refused(tmp_path):
    # Pure: the name is checked before any simulator work, so a typo costs nothing.
    with pytest.raises(BankError, match="unknown categor"):
        generate(
            tmp_path,
            bank_id="test-bank",
            category_names=["curve"],
            category_seeds={"curv": (0, 1)},
        )


@needs_sim
def test_replace_swaps_a_scenario_without_changing_the_bank(tmp_path):
    # The correction loop. `curve` seed 4 draws a road 7% from seed 0's -- near enough that the
    # two thumbnails are the same picture -- so it is the scenario worth swapping out.
    generate(tmp_path, bank_id="test-bank", category_names=["curve"], seeds=(0, 4))
    before = read_manifest(tmp_path)
    old_row = before.categories["curve"].scenarios[1]
    old_image = (tmp_path / old_row.thumbnail).read_bytes()

    row = replace_scenario(tmp_path, "curve_0001", 22)
    after = read_manifest(tmp_path)

    # Same size, same id, same slot, same thumbnail path -- only the seed and its measurements.
    assert len(after.categories["curve"].scenarios) == 2
    assert after.categories["curve"].scenarios[1] == row
    assert row.scenario_id == "curve_0001" == old_row.scenario_id
    assert row.thumbnail == old_row.thumbnail
    assert row.seed == 22 and old_row.seed == 4
    assert row.turn_pairs == "LR" and old_row.turn_pairs == "LL"
    assert (tmp_path / row.thumbnail).read_bytes() != old_image


@needs_sim
def test_replace_without_thumbnails_deletes_the_stale_one(tmp_path):
    # The old PNG is a picture of the seed that was just replaced. Keeping it would leave the
    # manifest pointing at an image that contradicts it, which is worse than pointing at nothing.
    generate(tmp_path, bank_id="test-bank", category_names=["curve"], seeds=(0, 4))
    stale = tmp_path / read_manifest(tmp_path).categories["curve"].scenarios[1].thumbnail
    assert stale.exists()

    row = replace_scenario(tmp_path, "curve_0001", 22, thumbnails=False)

    assert row.thumbnail is None
    assert not stale.exists()
    assert read_manifest(tmp_path).categories["curve"].scenarios[1].thumbnail is None


@needs_sim
def test_replace_refuses_a_seed_the_category_already_uses(tmp_path):
    generate(tmp_path, bank_id="test-bank", category_names=["curve"], seeds=(0, 4),
             thumbnails=False)
    with pytest.raises(BankError, match="already used by curve"):
        replace_scenario(tmp_path, "curve_0001", 0)


def test_replace_names_the_scenarios_that_do_exist(tmp_path):
    write_manifest(tmp_path, _minimal_manifest())
    with pytest.raises(BankError, match="no scenario 'curve_0004'"):
        replace_scenario(tmp_path, "curve_0004", 22)


@needs_sim
def test_generate_refuses_duplicate_seeds(tmp_path):
    with pytest.raises(BankError, match="duplicate seeds"):
        generate(tmp_path, bank_id="test-bank", category_names=["curve"], seeds=(0, 0))
