"""What acts: `load_policy("pkg.mod:Name")`, and the diagnostic policies the runner is checked with.

A policy is anything callable as `(observation) -> Sequence[float]`, instantiated once per run.
`load_policy` resolves a dotted spec, instantiates it and checks it is callable, and that is the
whole protocol -- there is no base class to inherit, because the thing under evaluation (Step 7's
AV3 model, behind the openpilot bridge) is not ours to make inherit from anything.

The two shipped here are floor checks, never the thing under evaluation. `ConstantPolicy` holds
one action for the whole episode, which is what a run whose results look like nothing was driving
is compared against. `RaisingPolicy` raises on its first call, and exists so the batch's promise
-- one scenario's error is recorded and the next scenario runs -- has a test. Step 4 adds
`ExpertPolicy`, the ceiling, around MetaDrive's bundled PPO expert.

Nothing here imports the simulator.
"""

from __future__ import annotations

import importlib
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from scenariobank.runner import Actor

#: Neither steering nor throttle: the action `replay` drives with, and `ConstantPolicy`'s default.
#: Enough to measure an episode's length and what ends it; a floor that goes nowhere.
CONSTANT_ACTION: tuple[float, float] = (0.0, 0.0)


class PolicyError(RuntimeError):
    """A spec that does not name a callable. Always says which part failed."""


class ConstantPolicy:
    """One action, every decision. The floor: a run this cannot be told apart from drove nothing."""

    def __init__(self, action: Sequence[float] = CONSTANT_ACTION) -> None:
        self.action = tuple(float(value) for value in action)

    def __call__(self, observation: Any) -> Sequence[float]:
        del observation
        return self.action


class RaisingPolicy:
    """Raises on every call. For proving that the batch records an error and carries on."""

    def __call__(self, observation: Any) -> Sequence[float]:
        del observation
        raise RuntimeError("RaisingPolicy raised, as it is for")


def load_policy(spec: str, *, checkpoint_path: str | None = None) -> Actor:
    """Import `pkg.mod`, take `Name`, instantiate it, and check the instance is callable.

    `checkpoint_path` is passed to the constructor by keyword only when it is set, so the two
    diagnostic policies, which take none, are constructed as `Name()`. An importable module with
    no such name, a name that is not a class or factory, and an instance that cannot be called
    are each refused with the part of the spec that failed.
    """
    module_name, colon, class_name = spec.partition(":")
    if not colon or not module_name or not class_name:
        raise PolicyError(f"{spec!r} is not a policy spec; the form is `pkg.mod:Name`")
    try:
        module = importlib.import_module(module_name)
    except ImportError as error:
        raise PolicyError(f"cannot import {module_name!r} for policy {spec!r}: {error}") from error
    try:
        factory = getattr(module, class_name)
    except AttributeError as error:
        raise PolicyError(f"{module_name!r} has no {class_name!r} (policy {spec!r})") from error
    if not callable(factory):
        raise PolicyError(f"{spec!r} is not a class or a factory; it cannot be instantiated")
    policy = factory(checkpoint_path=checkpoint_path) if checkpoint_path else factory()
    if not callable(policy):
        raise PolicyError(f"{spec!r} instantiated to something that cannot be called as a policy")
    return policy


__all__ = ["CONSTANT_ACTION", "ConstantPolicy", "PolicyError", "RaisingPolicy", "load_policy"]
