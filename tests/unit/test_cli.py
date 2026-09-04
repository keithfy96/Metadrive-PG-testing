"""The CLI's pure parts: argument parsing, and the two renderings of a seed ranking.

A bad seed list should fail on the flag rather than deep inside a map build, and `--json` and the
aligned table should never disagree about which seed is worth swapping to -- the studio reads one
of them and a person reads the other.
"""

import json
import re

import pytest
import typer

from scenariobank.categories import SEEDS, get_category
from scenariobank.cli import _ad_hoc_category, _parse_scan, _parse_seed_options, _parse_seeds
from scenariobank.options import AXES
from scenariobank.sockets import SocketReading

#: The four-way junction as this simulator actually builds it, mirroring `test_sockets.FOUR_WAY`.
#: Kept here too so the rule table is testable without a simulator, which is what it is.
FOUR_WAY_READINGS = [
    SocketReading(index=f"1X-socket{n}", node=node, start_node="1X0_0_", lane_count=3,
                  angle_deg=angle, turn_deg=angle, is_entry=False)
    for n, (node, angle) in enumerate([("left", 90.0), ("straight", 0.0), ("right", -90.0)])
]


def test_a_bare_seed_list_becomes_the_shared_default():
    shared, overrides = _parse_seed_options(["0,1,2"])
    assert shared == (0, 1, 2)
    assert overrides == {}


def test_no_seed_flag_falls_back_to_the_bank_seeds():
    assert _parse_seed_options(None) == (SEEDS, {})
    assert _parse_seed_options([]) == (SEEDS, {})


def test_a_named_list_overrides_one_category_and_leaves_the_rest():
    shared, overrides = _parse_seed_options(["0,1,2,3,4", "curve=0,1,2,3,22"])
    assert shared == (0, 1, 2, 3, 4)
    assert overrides == {"curve": (0, 1, 2, 3, 22)}


def test_a_later_flag_wins_over_an_earlier_one():
    # So a shared list can be given first and then contradicted, in either order.
    shared, overrides = _parse_seed_options(["0,1", "curve=5,6", "curve=7,8", "2,3"])
    assert shared == (2, 3)
    assert overrides == {"curve": (7, 8)}


@pytest.mark.parametrize("bad", ["0,x,2", "curve=0,x"])
def test_a_non_integer_seed_fails_on_the_flag(bad):
    # Not deep inside a map build, where it would read as "this seed could not be laid out".
    with pytest.raises(typer.BadParameter, match="seeds must be integers"):
        _parse_seed_options([bad])


def test_scan_accepts_an_inclusive_range_or_a_list():
    assert _parse_scan("0-4") == (0, 1, 2, 3, 4)
    assert _parse_scan("0,7,22") == (0, 7, 22)


def test_an_ad_hoc_category_needs_a_rule_and_validates_the_sequence():
    assert _ad_hoc_category("CC", "only").block_seq == "CC"
    with pytest.raises(typer.BadParameter, match="--block-seq needs --rule"):
        _ad_hoc_category("CC", None)
    with pytest.raises(typer.BadParameter, match="unknown rule"):
        _ad_hoc_category("CC", "sideways")


def test_parse_seeds_names_the_flag_it_failed_on():
    with pytest.raises(typer.BadParameter) as error:
        _parse_seeds("nope", hint="--keep")
    assert error.value.param_hint == "--keep"


def _ranking():
    """Four seeds as `variety.scan` returns them: kept first, then candidates most distinct first.

    Hand-built rather than scanned. What is under test is that the two renderings agree, and a
    real scan would need a simulator and thirty-one resets to say the same thing.
    """
    from scenariobank.variety import SeedReading

    def one(seed, *, gap, length, kept=False):
        return SeedReading(
            seed=seed, is_kept=kept, destination="1C0_1_", route_length_m=length,
            net_rotation_deg=-88.0, turn_pairs="LL", gap=gap,
            nearest_kept=None if kept else 0,
        )

    return [
        one(0, gap=None, length=452.0, kept=True),
        one(1, gap=None, length=418.0, kept=True),
        one(22, gap=0.41, length=311.0),
        one(7, gap=0.34, length=280.0),
        # Under `variety.NEAR_DUPLICATE`: a second picture of seed 0, and both renderings must
        # say so.
        one(4, gap=0.07, length=420.0),
    ]


def _seeds_run(monkeypatch, *flags):
    from click.testing import CliRunner

    from scenariobank import cli, variety

    monkeypatch.setattr(cli, "has_simulator", lambda: True)
    monkeypatch.setattr(variety, "scan", lambda *args, **kwargs: _ranking())
    result = CliRunner().invoke(
        typer.main.get_command(cli.app),
        ["seeds", "--category", "curve", "--keep", "0,1", "--scan", "0-22", *flags],
    )
    assert result.exit_code == 0, result.output
    return result.stdout


def test_the_json_and_the_table_rank_the_same_seeds(monkeypatch):
    """`--json` and the aligned table are two renderings of one list, and the order is the answer.

    The studio reads the JSON and a person reads the table. If the two could rank differently,
    the seed the page recommends would not be the seed the command recommends.
    """
    printed = _seeds_run(monkeypatch)
    document = json.loads(_seeds_run(monkeypatch, "--json"))

    from_table = [int(seed) for seed in re.findall(r"^\s*seed (\d+)", printed, re.M)]
    assert from_table == [row["seed"] for row in document["scan"]]
    assert from_table == [0, 1, 22, 7, 4]
    # The road a ranking was measured on travels with it: a replacement is built on the road the
    # *manifest* records, and the two have to be the same one.
    assert document["block_seq"] == get_category("curve").block_seq
    assert document["keep"] == [0, 1]


def test_both_renderings_flag_the_same_near_duplicates(monkeypatch):
    printed = _seeds_run(monkeypatch)
    document = json.loads(_seeds_run(monkeypatch, "--json"))

    flagged = {int(seed) for seed in re.findall(r"^\s*seed (\d+).*near-duplicate", printed, re.M)}
    assert flagged == {row["seed"] for row in document["scan"] if row["near_duplicate"]}
    # Not empty, or the assertion above would pass on a rule nobody applied.
    assert flagged == {4}


def test_a_kept_seed_is_never_a_near_duplicate_of_itself():
    """`gap` is `None` for a kept seed, and `None` is not "close to zero, so a duplicate"."""
    from scenariobank.variety import SeedReading

    kept = SeedReading(
        seed=0, is_kept=True, destination="1C0_1_", route_length_m=452.0,
        net_rotation_deg=-88.0, turn_pairs="LL", gap=None, nearest_kept=None,
    )
    assert kept.is_near_duplicate is False
    assert kept.as_dict()["near_duplicate"] is False


def _bank_for_options(tmp_path):
    """A written bank with one curve row. No simulator, and no thumbnail is needed: `options`
    reads the manifest and writes the manifest."""
    from scenariobank.bank import CategoryEntry, Manifest, ScenarioRow, write_manifest
    from scenariobank.handedness import DRIVE_SIDE_LEFT

    write_manifest(
        tmp_path,
        Manifest(
            schema_version="1.1",
            bank_id="b",
            created_utc="2026-09-04T00:00:00Z",
            metadrive={
                "edition": None, "dist_version": None, "commit": None, "asset_version": None,
            },
            base_config={"traffic_density": 0.0},
            drive_side=DRIVE_SIDE_LEFT,
            categories={
                "curve": CategoryEntry(
                    description="two curves",
                    block_seq="CC",
                    exit_rule="only",
                    max_steps=1200,
                    scenarios=[
                        ScenarioRow(
                            scenario_id="curve_0000",
                            seed=0,
                            destination="2C0_1_",
                            spawn_lane_index=0,
                            route_length_m=340.0,
                            net_rotation_deg=90.0,
                            turn_pairs="LL",
                            thumbnail="thumbs/curve_0000.png",
                        )
                    ],
                )
            },
        ),
    )
    return tmp_path


def _options_run(tmp_path, *flags):
    from click.testing import CliRunner

    from scenariobank import cli

    return CliRunner().invoke(
        typer.main.get_command(cli.app),
        ["options", "--bank", str(tmp_path), *flags],
    )


def test_options_show_reads_the_levels_without_changing_them(tmp_path):
    _bank_for_options(tmp_path)
    result = _options_run(tmp_path, "--show")

    assert result.exit_code == 0, result.output
    listed = [line.split() for line in result.stdout.splitlines() if line.startswith("  ")]
    assert listed == [[axis, "none"] for axis in AXES], "six axes, all at the floor"
    assert "pins no option levels" in result.stdout
    assert "pinned in" not in result.stdout, "--show writes nothing, so it names no manifest"


def test_options_pins_the_axes_it_is_given_and_says_nothing_was_rebuilt(tmp_path):
    _bank_for_options(tmp_path)
    result = _options_run(tmp_path, "--traffic", "medium", "--pedestrians", "low")

    assert result.exit_code == 0, result.output
    assert "runs at traffic=medium, pedestrians=low, everything else none" in result.stdout
    assert "nothing was rebuilt" in result.stdout

    read_back = _options_run(tmp_path, "--show")
    assert "traffic=medium" in read_back.stdout


def test_options_needs_something_to_do(tmp_path):
    # Naming no axis and not asking to read is a command with no effect, which is more likely a
    # flag that was typed wrong than a thing anyone meant.
    _bank_for_options(tmp_path)
    assert _options_run(tmp_path).exit_code != 0
    assert _options_run(tmp_path, "--show", "--traffic", "low").exit_code != 0


def test_a_level_the_bank_would_refuse_leaves_the_command_at_exit_one(tmp_path):
    _bank_for_options(tmp_path)
    result = _options_run(tmp_path, "--traffic", "enormous")
    assert result.exit_code == 1
    assert "options failed" in result.output


def test_every_exit_rule_is_reported_including_the_ones_that_find_nothing():
    """The refusal is the useful half of the answer often enough to be a row rather than a gap.

    The table and the studio's exit reader are both drawn from this, so a rule that cannot be
    satisfied says why in the command's own words instead of quietly disappearing.
    """
    from scenariobank.categories import ExitRule
    from scenariobank.cli import _rule_outcomes

    outcomes = _rule_outcomes(FOUR_WAY_READINGS)
    # A list, in `ExitRule`'s own order: `json.dumps(sort_keys=True)` would alphabetise a dict,
    # and the studio would then print these in a different order from the terminal.
    assert [one["rule"] for one in outcomes] == [rule.value for rule in ExitRule]
    by_rule = {one["rule"]: one for one in outcomes}
    assert by_rule["left"] == {
        "rule": "left",
        "node": "left",
        "angle_deg": 90.0,
        "turn_deg": 90.0,
        "refused": None,
    }
    assert by_rule["sharpest"]["node"] in {"left", "right"}
    # A crossroads has three ways out, and `only` says so rather than reading as absent. The
    # refusal is shown verbatim on the card, so it names the settings that would work instead.
    assert by_rule["only"]["node"] is None
    assert "3 ways out" in by_rule["only"]["refused"]
    assert "left, right, straight or sharpest" in by_rule["only"]["refused"]


def test_the_sockets_document_names_the_road_it_measured():
    """`--json` emits one document about *this road at this seed*, not a bare list of sockets.

    Two screens read it -- the edit panel's exit dropdown and the studio's exit reader -- and the
    second needs the rules, which a list of sockets has nowhere to carry.
    """
    from typer.testing import CliRunner

    from scenariobank.cli import app

    result = CliRunner().invoke(app, ["sockets", "--block-seq", "X", "--seed", "0", "--json"])
    if result.exit_code != 0:
        pytest.skip("needs_sim: MetaDrive is not installed (uv sync --group sim)")
    document = json.loads(result.stdout[result.stdout.index("{"):])
    assert document["block_seq"] == "X"
    assert document["seed"] == 0
    # The crossroads as this simulator builds it: three arms, one each way.
    assert sorted(round(one["angle_deg"]) for one in document["sockets"]) == [-90, 0, 90]
    # And no entry among them. A block's sockets are the connections it offers onward; the arm
    # it was driven in through belongs to the block before it, so nothing built from the fifteen
    # blocks ever marks one.
    assert not any(one["is_entry"] for one in document["sockets"])
    # Nothing turns the car before a one-block road's junction, so the two angles agree. They
    # part company only on a composed road, and it is `turn_deg` the rules match.
    assert all(one["entry_heading_deg"] == 0 for one in document["sockets"])
    assert all(one["turn_deg"] == one["angle_deg"] for one in document["sockets"])
    assert [one["rule"] for one in document["rules"]] == ["only", "left", "right", "straight",
                                                          "sharpest"]
    left = next(one for one in document["rules"] if one["rule"] == "left")
    assert left["angle_deg"] == 90.0
