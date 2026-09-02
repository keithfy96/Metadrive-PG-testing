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
