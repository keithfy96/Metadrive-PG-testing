"""The bank: what `generate` writes, and the two things about it that are easy to get wrong."""

import json

import pytest

from scenariobank.bank import (
    MANIFEST_NAME,
    READABLE_VERSIONS,
    SCHEMA_VERSION,
    BankError,
    CategoryEntry,
    Manifest,
    ScenarioRow,
    _exit_intent,
    _next_index,
    add_scenario,
    describe_config,
    generate,
    num_scenarios_for,
    read_manifest,
    remove_scenario,
    replace_scenario,
    scenario_id,
    set_max_steps,
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
    assert read_manifest(tmp_path) == _minimal_manifest().model_copy(
        update={"schema_version": SCHEMA_VERSION}
    )
    assert not list(tmp_path.glob("*.tmp"))


def test_a_manifest_is_stamped_with_the_shape_the_writer_wrote(tmp_path):
    """A 1.0 bank edited by this build comes back 1.1, and both versions still read.

    The version describes the shape of the file, not the history of the bank. This build can write
    a row that declares its own exit or its own budget, and a 1.0 reader forbids extra keys -- so
    a file it has touched has to say 1.1 whether or not this particular edit used one.
    """
    write_manifest(tmp_path, _minimal_manifest())
    assert read_manifest(tmp_path).schema_version == SCHEMA_VERSION
    assert "1.0" in READABLE_VERSIONS and SCHEMA_VERSION in READABLE_VERSIONS


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


# --------------------------------------------- editing one item: schema 1.1's three overrides


def _edited_row(index: int, seed: int, **over) -> ScenarioRow:
    """One plausible row, so the edits below can be tested without building a road."""
    return ScenarioRow(
        scenario_id=scenario_id("curve", index),
        seed=seed,
        destination="1C0_1_",
        spawn_lane_index=0,
        route_length_m=400.0,
        net_rotation_deg=12.0,
        turn_pairs="LL",
        thumbnail=f"thumbs/curve_{index:04d}.png",
        **over,
    )


def _bank_on_disk(tmp_path, count: int = 5) -> Manifest:
    """A written bank with `count` curve rows and a picture for each. No simulator anywhere."""
    rows = [_edited_row(index, index) for index in range(count)]
    manifest = _minimal_manifest().model_copy(
        update={
            "categories": {
                "curve": CategoryEntry(
                    description="two curves",
                    block_seq="CC",
                    exit_rule="only",
                    max_steps=1200,
                    scenarios=rows,
                )
            }
        }
    )
    (tmp_path / "thumbs").mkdir(exist_ok=True)
    for row in rows:
        (tmp_path / row.thumbnail).write_bytes(b"not really a png")
    write_manifest(tmp_path, manifest)
    return manifest


def test_removing_a_scenario_leaves_a_gap_rather_than_renumbering(tmp_path):
    """The decision the whole edit step turns on.

    An id is how a run refers to a scenario and how a result is keyed, so renumbering `curve_0003`
    down because `curve_0002` went would change the id of a scenario nobody touched. The gap
    costs nothing: `_locate` searches by id, and the next id is read off the highest rather than
    counted, so the empty position is never re-issued.
    """
    _bank_on_disk(tmp_path)
    gone = remove_scenario(tmp_path, "curve_0002")
    after = read_manifest(tmp_path)

    assert gone.scenario_id == "curve_0002"
    assert [row.scenario_id for row in after.categories["curve"].scenarios] == [
        "curve_0000", "curve_0001", "curve_0003", "curve_0004",
    ]
    # The picture goes with the row: a thumbnail of a scenario that is not in the manifest is
    # wrong rather than merely stale.
    assert not (tmp_path / "thumbs" / "curve_0002.png").exists()
    assert (tmp_path / "thumbs" / "curve_0003.png").exists()
    # And the position is not handed out again. Five rows minus one is four; the next id is 5.
    assert scenario_id("curve", _next_index(after.categories["curve"])) == "curve_0005"


def test_removing_a_categorys_last_scenario_removes_the_category(tmp_path):
    _bank_on_disk(tmp_path, count=1)
    manifest = read_manifest(tmp_path)
    manifest.categories["roundabout"] = CategoryEntry(
        description="a roundabout", block_seq="O", exit_rule="only", max_steps=1200,
        scenarios=[_edited_row(0, 0).model_copy(update={"scenario_id": "roundabout_0000",
                                                        "thumbnail": None})],
    )
    write_manifest(tmp_path, manifest)

    said = []
    remove_scenario(tmp_path, "curve_0000", progress=said.append)

    assert list(read_manifest(tmp_path).categories) == ["roundabout"]
    assert any("the category went with it" in line for line in said)


def test_the_last_scenario_in_a_bank_cannot_be_removed(tmp_path):
    # An empty bank is a manifest describing nothing. `generate` is how a new bank is made, and
    # deleting the directory is how an old one goes.
    _bank_on_disk(tmp_path, count=1)
    with pytest.raises(BankError, match="only scenario in this bank"):
        remove_scenario(tmp_path, "curve_0000")
    assert read_manifest(tmp_path).categories["curve"].scenarios


def test_a_scenario_can_carry_its_own_step_budget_without_a_rebuild(tmp_path):
    """`max_steps` is declared, not measured, which is why this edit builds nothing.

    Everything else on a row is read off a road. A budget is a cap somebody chose, so choosing a
    different one is an edit to the manifest -- and `budget_for` is the one place the override is
    resolved, so the review and a rebuild read the same number.
    """
    _bank_on_disk(tmp_path, count=2)
    said = []
    row = set_max_steps(tmp_path, "curve_0001", 200, progress=said.append)
    entry = read_manifest(tmp_path).categories["curve"]

    assert row.max_steps == 200
    assert entry.budget_for(entry.scenarios[1]) == 200
    assert entry.budget_for(entry.scenarios[0]) == 1200, "the others still follow the category"
    # A 400 m route earns 1000 steps, so 200 is a cap that ends the episode short. Written
    # anyway -- it is a decision, not an error -- but never silently.
    assert any("its own cap of 200" in line for line in said)

    cleared = set_max_steps(tmp_path, "curve_0001", None)
    assert cleared.max_steps is None
    assert read_manifest(tmp_path).categories["curve"].budget_for(cleared) == 1200


def test_a_budget_that_ends_the_episode_before_it_begins_is_refused(tmp_path):
    _bank_on_disk(tmp_path, count=2)
    with pytest.raises(BankError, match="before it began"):
        set_max_steps(tmp_path, "curve_0001", 0)


def test_a_pinned_exit_does_not_outlive_the_seed_it_was_pinned_at(tmp_path):
    """An exit node names an arm of one seed's road, so it cannot be carried to another.

    `StdTInterSection` offers right-and-straight on seeds 0, 1 and 4 and left-and-straight on 2
    and 3. Carrying a pin across a seed change would fail the rebuild with a message about a node
    nobody typed, so the pin is dropped and the category's rule resolves the new road's exit.
    """
    entry = _bank_on_disk(tmp_path).categories["curve"]
    pinned = _edited_row(0, 0, exit_node="1C0_1_")
    said = []

    kept = _exit_intent(entry, pinned, 0, exit_rule=None, destination=None, inherit=False,
                        say=said.append)
    assert kept == (None, "1C0_1_") and not said

    moved = _exit_intent(entry, pinned, 22, exit_rule=None, destination=None, inherit=False,
                         say=said.append)
    assert moved == (None, None)
    assert any("arm of seed 0's road" in line for line in said)


def test_an_exit_is_either_resolved_or_named_but_not_both(tmp_path):
    entry = _bank_on_disk(tmp_path).categories["curve"]
    row = _edited_row(0, 0)
    with pytest.raises(BankError, match="not both"):
        _exit_intent(entry, row, 0, exit_rule="left", destination="1X0_1_", inherit=False,
                     say=lambda _message: None)
    with pytest.raises(BankError, match="unknown exit rule"):
        _exit_intent(entry, row, 0, exit_rule="leftish", destination=None, inherit=False,
                     say=lambda _message: None)


@needs_sim
def test_add_numbers_past_the_highest_id_even_over_a_gap(tmp_path):
    # The pair that has to agree: removing leaves `curve_0000` empty, and adding must not walk
    # back into it. Two rows, one removed, one added -- the new id is 2, not 0.
    generate(tmp_path, bank_id="test-bank", category_names=["curve"], seeds=(0, 1),
             thumbnails=False)
    remove_scenario(tmp_path, "curve_0000")

    row = add_scenario(tmp_path, "curve", 22, thumbnails=False)
    after = read_manifest(tmp_path)

    assert row.scenario_id == "curve_0002"
    assert [one.scenario_id for one in after.categories["curve"].scenarios] == [
        "curve_0001", "curve_0002",
    ]
    assert [one.seed for one in after.categories["curve"].scenarios] == [1, 22]
    with pytest.raises(BankError, match="already used by curve"):
        add_scenario(tmp_path, "curve", 22, thumbnails=False)


@needs_sim
def test_a_scenario_can_be_pointed_at_another_exit_without_changing_its_seed(tmp_path):
    """The other half of an edit: same draw, different destination.

    `intersection_left` and `intersection_right` are the same junction with different rules, so
    the row rebuilt at rule `right` must land where the `right` category lands -- and record the
    rule it was built from, because a rebuild months later has to resolve the same way.
    """
    generate(tmp_path, bank_id="test-bank", category_names=["intersection_left"], seeds=(0,),
             thumbnails=False)
    before = read_manifest(tmp_path).categories["intersection_left"].scenarios[0]

    row = replace_scenario(tmp_path, "intersection_left_0000", exit_rule="right",
                           thumbnails=False)
    entry = read_manifest(tmp_path).categories["intersection_left"]

    assert row.seed == before.seed, "no seed given means the seed it already has"
    assert row.destination == "1X2_1_" != before.destination
    assert row.exit_rule == "right" and entry.rule_for(row) == "right"
    # The category itself did not move. An overridden row keeps its category name, and the
    # entry stays the declared intent for every row that has none of its own.
    assert entry.exit_rule == "left"

    pinned = replace_scenario(tmp_path, "intersection_left_0000", destination=before.destination,
                              thumbnails=False)
    assert pinned.exit_node == before.destination and pinned.exit_rule is None
    assert pinned.destination == before.destination

    back = replace_scenario(tmp_path, "intersection_left_0000", inherit_exit=True,
                            thumbnails=False)
    assert back.exit_rule is None and back.exit_node is None
    assert back.destination == before.destination


@needs_sim
def test_a_pinned_exit_the_road_does_not_offer_is_refused_by_name(tmp_path):
    generate(tmp_path, bank_id="test-bank", category_names=["intersection_left"], seeds=(0,),
             thumbnails=False)
    with pytest.raises(BankError, match="has no exit '1T0_1_'"):
        replace_scenario(tmp_path, "intersection_left_0000", destination="1T0_1_",
                         thumbnails=False)


def test_a_1_0_manifest_still_opens_and_overrides_nothing(tmp_path):
    """The banks already on disk, read by the build that can write overrides.

    1.1 only *added* optional fields, so a 1.0 row is a 1.1 row that overrides nothing. The
    version moved because the reverse does not hold: `extra="forbid"` means a 1.0 reader refuses
    a row that declares its own budget.
    """
    raw = json.loads(_bank_on_disk(tmp_path).model_dump_json())
    raw["schema_version"] = "1.0"
    for row in raw["categories"]["curve"]["scenarios"]:
        for key in ("exit_rule", "exit_node", "max_steps"):
            del row[key]
    (tmp_path / MANIFEST_NAME).write_text(json.dumps(raw))

    entry = read_manifest(tmp_path).categories["curve"]

    assert read_manifest(tmp_path).schema_version == "1.0"
    assert all(row.max_steps is None and row.exit_rule is None for row in entry.scenarios)
    assert entry.budget_for(entry.scenarios[0]) == entry.max_steps
    assert entry.rule_for(entry.scenarios[0]) == entry.exit_rule
