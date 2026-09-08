"""What acts: `load_policy("pkg.mod:Name")`, and the reference policies the runner is checked with.

A policy is anything callable as `(observation) -> Sequence[float]`, instantiated once per run.
`load_policy` resolves a dotted spec, instantiates it and checks it is callable, and that is the
whole protocol -- there is no base class to inherit, because the thing under evaluation (Step 7's
AV3 model, behind the openpilot bridge) is not ours to make inherit from anything. One optional
half: a policy with a `bind(env)` method is handed each env the batch builds, before that env's
rows run. That is how a policy that reads the world directly -- the expert below, off the ego
vehicle; Step 7's, off the camera rig -- reaches it, since an observation is all the loop passes.

**Floor and ceiling.** The reference policies are CLI-only checks, never the thing under
evaluation. `ConstantPolicy` holds one action for the whole episode: the floor, what a run whose
results look like nothing was driving is compared against. `ExpertPolicy` wraps MetaDrive's
bundled PPO expert: the ceiling, what a good driver scores on this bank. A floor that scores
about the same as the ceiling means the runner is not feeding actions to the env, which is the
bug the pair exists to catch. `RaisingPolicy` raises on its first call, so the batch's promise
-- one scenario's error is recorded and the next scenario runs -- has a test.

Nothing here imports the simulator at import time; `ExpertPolicy` imports the expert when it is
constructed, which `load_policy` does before any env is built, so a machine without it refuses
the run before the simulator opens.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

import numpy as np

from scenariobank.handedness import DRIVE_SIDE_LEFT, drive_side

if TYPE_CHECKING:
    from scenariobank.runner import Actor

#: The width of the bundled expert's own observation: 19 state, 16 for four other vehicles and
#: 240 lidar points (`numpy_expert.py:56`, asserted there). `mirror_expert_observation` is
#: written against this layout and refuses any other width.
EXPERT_OBSERVATION_WIDTH = 275
#: Where the 240 lidar points start, and the first index of each other vehicle's four numbers.
_LIDAR_AT = 35
_OTHERS_AT = (19, 23, 27, 31)
#: The state and navigation entries that read as `1 - x` in the mirror: the heading against
#: the lane's lateral vector, the steering, the last steering, and for each of the two
#: checkpoints its lateral projection and its bend direction. `state_obs.py:30-150`,
#: `node_network_navigation.py:288-345`; which entries and why, in `mirror_expert_observation`.
_FLIPPED = (2, 4, 5, 10, 12, 15, 17)
#: The two five-number checkpoint blocks, and what one reads before the navigation has been
#: updated once: all zeros, which the expert's own `obs_correction` turns into `(0, 1, 0, 0, 0)`.
#: The placeholder means "no checkpoint yet" on either side, so it is left as it is.
_CHECKPOINTS_AT = (9, 14)
_CHECKPOINT_PLACEHOLDER = (0.0, 1.0, 0.0, 0.0, 0.0)

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


def bundled_expert() -> Callable[..., Any]:
    """MetaDrive's numpy PPO expert, `expert(vehicle, deterministic=...)`, imported on demand.

    The numpy one by name, not `metadrive.examples.ppo_expert.expert`: that package picks the
    torch expert whenever torch is importable, which the rig has and this machine does not, and
    a ceiling that is one arithmetic on one machine and another on the next is not one ceiling.
    The weights are the same file either way; only the numpy path is the same everywhere.
    """
    try:
        from metadrive.examples.ppo_expert.numpy_expert import expert
    except ImportError as error:
        raise PolicyError(
            f"ExpertPolicy needs MetaDrive's bundled PPO expert, which cannot be imported: {error}"
        ) from error
    return expert


def expert_weights() -> dict[str, np.ndarray]:
    """The bundled expert's weights, from the same file it loads them from. Six arrays."""
    try:
        from metadrive.examples.ppo_expert.numpy_expert import ckpt_path
    except ImportError as error:
        raise PolicyError(
            f"ExpertPolicy needs MetaDrive's bundled PPO expert, which cannot be imported: {error}"
        ) from error
    with np.load(ckpt_path) as archive:
        return {name: archive[name] for name in archive.files if "value" not in name}


def expert_forward(observation: np.ndarray, weights: dict[str, np.ndarray]) -> np.ndarray:
    """The expert's network, as `numpy_expert.py:66-73` writes it: the action mean, two numbers.

    Kept operation-for-operation the same as the original so a mirrored observation goes through
    the same arithmetic as an unmirrored one goes through inside `expert()`.
    """
    x = observation.reshape(1, -1)
    for layer in ("fc_1", "fc_2", "fc_out"):
        kernel = weights[f"default_policy/{layer}/kernel"]
        bias = weights[f"default_policy/{layer}/bias"]
        x = np.matmul(x, kernel) + bias
        if layer != "fc_out":
            x = np.tanh(x)
    mean, _log_std = np.split(x.reshape(-1), 2)
    return mean


def mirror_expert_observation(observation: np.ndarray) -> np.ndarray:
    """The expert's 275-wide observation, reflected about the vehicle's heading. An involution.

    The bank's maps are MetaDrive's mirrored about the x-axis (`handedness.py`), and the mirror
    is exact, so a left-side road *is* the right-side road the expert was trained on, seen in a
    mirror. Reflect what the expert sees and negate its steering and it drives the mirrored road
    exactly as it drives the original -- same step count, arrival for arrival.

    Which entries reflect is decided by how each is computed, not by whether it is lateral.
    `handedness` mirrors the *lane frames* -- `direction_lateral`, the arc direction -- so a
    number read off a lane's `local_coordinates` already comes out the same on both roads: the
    two road-border distances (`base_vehicle.py:527-536`) and the offset in the lane. Those are
    left alone. What is computed against a fixed chirality -- the vehicle's own frame, a fixed
    perpendicular, the sign of an arc -- comes out negated, and since each is normalised as
    `(x + 1) / 2` it reads `1 - x` here:

    - the heading against the lane's lateral vector (`heading_diff`, a cosine against a fixed
      perpendicular), the steering and the last steering (the mirrored driver steers the other
      way, and must see its own steering as the original);
    - per checkpoint, its lateral projection into the vehicle's frame and its bend direction
      (`(dir + 1) / 2`: clockwise 1, anticlockwise 0, and a straight lane 0.5, which the
      docstring in `state_obs.py` misreports as 0);
    - per other vehicle, its lateral position and lateral velocity, both in the vehicle's
      frame; a slot with no vehicle is four zeros (`lidar.py:130-136`) and stays four zeros;
    - the 240 lidar points, cast at `i * 2pi / 240` from the heading (`distance_detector.py:
      178-179`), reverse about the first: point `i` becomes point `240 - i`.

    Speeds, forward distances, bend radii, lane angles and the yaw rate (an unsigned magnitude)
    are the same from either side. Applied *after* the expert's own `obs_correction`, which is
    what its weights expect to see.

    **Measured, 2026-09-08.** The same row driven on both maps with mirrored actions tracks to
    the millimetre for 200 steps, 120 of them on arcs, and every entry above classifies as
    predicted on both. The first measurement did not: on an arc the lane frame's lateral axis
    came out inverted, because `handedness` mirrored an arc's sweep but not the sign of its
    lateral term. That was the mirror's to fix and it was fixed there (change 2), not here; the
    expert now drives the mirrored bank with the same step counts as the original.
    """
    obs = np.asarray(observation)
    if obs.shape != (EXPERT_OBSERVATION_WIDTH,):
        raise ValueError(
            f"the expert observes {EXPERT_OBSERVATION_WIDTH} numbers, not {list(obs.shape)}"
        )
    mirrored = obs.copy()
    placeholder = np.asarray(_CHECKPOINT_PLACEHOLDER)
    untouched = {
        base + offset
        for base in _CHECKPOINTS_AT
        if np.array_equal(obs[base : base + 5], placeholder)
        for offset in range(5)
    }
    for index in _FLIPPED:
        if index not in untouched:
            mirrored[index] = 1 - obs[index]
    for base in _OTHERS_AT:
        if obs[base : base + 4].any():
            mirrored[base + 1] = 1 - obs[base + 1]
            mirrored[base + 3] = 1 - obs[base + 3]
    lidar = obs[_LIDAR_AT:]
    mirrored[_LIDAR_AT + 1 :] = lidar[1:][::-1]
    return mirrored


class ExpertPolicy:
    """MetaDrive's bundled PPO expert, deterministic. The ceiling: what a good driver scores here.

    It holds the env it is bound to and ignores its `observation` argument. The expert observes
    the ego vehicle itself, through a 275-wide lidar observation of its own that it builds on
    first use (`numpy_expert.py:41-56`), so it takes the vehicle, not the bank's 19-wide state;
    `env.agent` is read at every call, because the agent is respawned at every reset.

    `deterministic=True` is the first reason this wrapper exists. The expert's default is
    `deterministic=False` (`numpy_expert.py:39`), and on that path the action is
    `np.random.normal(mean, std)` from the global numpy RNG (`:75`), which nothing seeds -- so
    two expert runs of one scenario would differ by construction, and Step 5's reproducibility
    diff would blame the runner.

    **The mirror is the second.** The expert was trained on MetaDrive's right-side roads and
    this bank's are mirrored to the left (`handedness.py`); measured, the expert as shipped
    arrives on nine of nine unmirrored roads and on none of the nine mirrored ones, leaving the
    road inside 24 steps. So on a left-side process (`handedness.drive_side()`, read when the
    policy is bound) it takes the expert's own observation, reflects it with
    `mirror_expert_observation`, runs the same network on it and negates the steering. On a
    right-side process it is the expert as shipped. The action is returned as the network gives
    it; the vehicle clips it to [-1, 1] itself (`base_vehicle.py:207`). What the mirror is
    exact for today, and what it is not, is in `mirror_expert_observation`.

    What the expert leaves behind is checked by the runner, not here: it swaps the vehicle's
    sensor config in and back out around each observation (`:58-60`), its own TODO admits the
    restore is incomplete, and `run_bank` fails a run whose observation shape moved.
    """

    def __init__(
        self,
        *,
        expert: Callable[..., Any] | None = None,
        weights: dict[str, np.ndarray] | None = None,
    ) -> None:
        self.expert = bundled_expert() if expert is None else expert
        self.weights = weights
        self.env: Any = None
        self.mirrored = False

    def bind(self, env: Any) -> None:
        """The env whose agent the expert drives. Called by the batch for each env it builds."""
        self.env = env
        self.mirrored = drive_side() == DRIVE_SIDE_LEFT
        if self.mirrored and self.weights is None:
            self.weights = expert_weights()

    def __call__(self, observation: Any) -> Sequence[float]:
        del observation
        if self.env is None:
            raise PolicyError(
                "ExpertPolicy is not bound to an env; the batch binds it after building one"
            )
        if not self.mirrored:
            return self.expert(self.env.agent, deterministic=True)
        _shipped, seen = self.expert(self.env.agent, deterministic=True, need_obs=True)
        reflected = mirror_expert_observation(np.asarray(seen).reshape(-1))
        mean = expert_forward(reflected, self.weights)
        return (-float(mean[0]), float(mean[1]))


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


__all__ = [
    "CONSTANT_ACTION",
    "EXPERT_OBSERVATION_WIDTH",
    "ConstantPolicy",
    "ExpertPolicy",
    "PolicyError",
    "RaisingPolicy",
    "bundled_expert",
    "expert_forward",
    "expert_weights",
    "load_policy",
    "mirror_expert_observation",
]
