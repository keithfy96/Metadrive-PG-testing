"""`load_policy` and the two diagnostic policies. Nothing here needs a simulator.

Step 4 adds `ExpertPolicy` and the determinism check to this file; what is pinned now is the
protocol -- a spec resolves to something callable, and every way it cannot is refused by name.
"""

from __future__ import annotations

import sys
import types

import pytest

from scenariobank.policies import (
    CONSTANT_ACTION,
    ConstantPolicy,
    PolicyError,
    RaisingPolicy,
    load_policy,
)


class NotCallable:
    """Instantiates fine and cannot act. What a wrong class name looks like."""


class TakesCheckpoint:
    """What Step 7's policy will look like from here: a constructor that wants a path."""

    def __init__(self, *, checkpoint_path: str) -> None:
        self.checkpoint_path = checkpoint_path

    def __call__(self, observation):
        del observation
        return (0.0, 0.0)


@pytest.fixture(autouse=True)
def fake_policies(monkeypatch):
    """The two classes above as an importable module, `fake_policies`, for the length of a test.

    `tests/` is not a package -- there is no `__init__.py`, by house convention -- so a spec cannot
    name this file. A module built by hand and put in `sys.modules` is what one looks like.
    """
    module = types.ModuleType("fake_policies")
    module.NotCallable = NotCallable
    module.TakesCheckpoint = TakesCheckpoint
    monkeypatch.setitem(sys.modules, "fake_policies", module)


def test_a_spec_resolves_instantiates_and_returns_the_callable():
    policy = load_policy("scenariobank.policies:ConstantPolicy")
    assert isinstance(policy, ConstantPolicy)
    assert policy(None) == CONSTANT_ACTION
    assert len(policy(None)) == 2, "one steering and one throttle, the env's action shape"


@pytest.mark.parametrize(
    ("spec", "phrase"),
    [
        ("scenariobank.policies", "the form is `pkg.mod:Name`"),
        (":ConstantPolicy", "the form is `pkg.mod:Name`"),
        ("nowhere.at.all:Thing", "cannot import 'nowhere.at.all'"),
        ("scenariobank.policies:Nope", "has no 'Nope'"),
        ("scenariobank.policies:CONSTANT_ACTION", "not a class or a factory"),
        ("fake_policies:NotCallable", "cannot be called as a policy"),
    ],
)
def test_every_way_a_spec_can_fail_is_refused_by_name(spec, phrase):
    with pytest.raises(PolicyError, match=phrase):
        load_policy(spec)


def test_a_checkpoint_is_handed_to_the_constructor_only_when_given():
    loaded = load_policy("fake_policies:TakesCheckpoint", checkpoint_path="w.pt")
    assert loaded.checkpoint_path == "w.pt"
    with pytest.raises(TypeError):
        load_policy("fake_policies:TakesCheckpoint")
    assert isinstance(load_policy("scenariobank.policies:ConstantPolicy"), ConstantPolicy)


def test_the_constant_policy_holds_whatever_it_was_given():
    assert ConstantPolicy((0.25, -1))(None) == (0.25, -1.0)


def test_the_raising_policy_raises_and_says_it_meant_to():
    with pytest.raises(RuntimeError, match="as it is for"):
        RaisingPolicy()(None)
