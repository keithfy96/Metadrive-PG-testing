"""Argument parsing, which is pure and worth pinning: a bad seed list should fail on the flag."""

import pytest
import typer

from scenariobank.categories import SEEDS
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
