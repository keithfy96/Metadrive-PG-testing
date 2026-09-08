"""`load_policy`, the reference policies, and the expert's mirror.

The protocol first -- a spec resolves to something callable, and every way it cannot is refused
by name -- then `ExpertPolicy` against a fake expert: it refuses to act unbound, reads the agent
at every call, passes `deterministic=True` every time, and on a left-side process reflects what
the expert saw and steers the other way. The mirror itself is pinned as arithmetic: an
involution that touches exactly the entries that reflect and nothing else.

Two tests at the end need the simulator and the bank at `LIVE_BANK`: that the expert this
package binds is the numpy one whatever torch says, and that two expert runs of one scenario
are one run -- the same action digest, the same step count, the observation 19 wide at both
ends -- while the floor is a different one.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import numpy as np
import pytest

import scenariobank.policies as policies_module
from scenariobank.doctor import has_simulator
from scenariobank.handedness import DRIVE_SIDE_LEFT, DRIVE_SIDE_RIGHT
from scenariobank.policies import (
    CONSTANT_ACTION,
    EXPERT_OBSERVATION_WIDTH,
    ConstantPolicy,
    ExpertPolicy,
    PolicyError,
    RaisingPolicy,
    expert_forward,
    load_policy,
    mirror_expert_observation,
)
from scenariobank.results import JOB_SCHEMA_VERSION, Job, JobBank
from scenariobank.runner import run_bank

needs_sim = pytest.mark.skipif(
    not has_simulator(),
    reason="needs_sim: MetaDrive is not installed (uv sync --group sim)",
)

#: The procedural bank the live half drives one row of.
LIVE_BANK = Path("banks/t-junction-left-intersection")

needs_bank = pytest.mark.skipif(
    not (LIVE_BANK / "manifest.json").exists(),
    reason=f"needs the bank at {LIVE_BANK}",
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


# --- the expert, against a fake ---------------------------------------------------------------


class FakeExpert:
    """Records every call; answers a fixed mean and, when asked, a fixed observation."""

    def __init__(self, observation=None):
        self.calls: list[tuple] = []
        self.observation = observation
        self.mean = np.array([0.25, 0.5])

    def __call__(self, vehicle, deterministic=False, need_obs=False):
        self.calls.append((vehicle, deterministic, need_obs))
        return (self.mean, self.observation) if need_obs else self.mean


class FakeAgentEnv:
    def __init__(self, agent):
        self.agent = agent


def tiny_weights(seed: int = 3) -> dict[str, np.ndarray]:
    """Weights in the expert's layer names and a shape `expert_forward` accepts, small."""
    rng = np.random.default_rng(seed)
    shapes = {"fc_1": (EXPERT_OBSERVATION_WIDTH, 8), "fc_2": (8, 8), "fc_out": (8, 4)}
    weights = {}
    for layer, shape in shapes.items():
        weights[f"default_policy/{layer}/kernel"] = rng.normal(size=shape) * 0.1
        weights[f"default_policy/{layer}/bias"] = rng.normal(size=shape[1]) * 0.1
    return weights


def test_the_expert_refuses_to_act_unbound_then_reads_the_agent_deterministically(monkeypatch):
    monkeypatch.setattr(policies_module, "drive_side", lambda: DRIVE_SIDE_RIGHT)
    fake = FakeExpert()
    policy = ExpertPolicy(expert=fake)
    with pytest.raises(PolicyError, match="not bound"):
        policy(np.zeros(19))
    env = FakeAgentEnv("car-1")
    policy.bind(env)
    assert list(policy(np.zeros(19))) == [0.25, 0.5]
    env.agent = "car-2"
    policy(None)
    assert [call[0] for call in fake.calls] == ["car-1", "car-2"], "the agent, at every call"
    assert all(call[1] is True for call in fake.calls), "deterministic=True, every time"
    assert policy.mirrored is False


def test_on_a_left_side_process_the_expert_sees_the_mirror_and_steers_the_other_way(monkeypatch):
    monkeypatch.setattr(policies_module, "drive_side", lambda: DRIVE_SIDE_LEFT)
    seen = np.linspace(0.05, 0.95, EXPERT_OBSERVATION_WIDTH)
    fake = FakeExpert(observation=seen.reshape(1, -1))
    weights = tiny_weights()
    policy = ExpertPolicy(expert=fake, weights=weights)
    policy.bind(FakeAgentEnv("car"))
    assert policy.mirrored is True
    steering, throttle = policy(None)
    mean = expert_forward(mirror_expert_observation(seen), weights)
    assert (steering, throttle) == (-float(mean[0]), float(mean[1]))
    assert fake.calls == [("car", True, True)], "the expert's own observation, deterministic"
    assert steering != -0.25, "the shipped mean is not what a mirrored process returns"


def test_the_mirror_is_an_involution_that_touches_only_what_reflects():
    obs = np.random.default_rng(7).random(EXPERT_OBSERVATION_WIDTH)
    mirrored = mirror_expert_observation(obs)
    assert np.allclose(mirror_expert_observation(mirrored), obs)
    flipped = {2, 4, 5, 10, 12, 15, 17} | {19 + 4 * k + 1 for k in range(4)}
    flipped |= {19 + 4 * k + 3 for k in range(4)}
    for index in range(35):
        expected = 1 - obs[index] if index in flipped else obs[index]
        assert mirrored[index] == pytest.approx(expected), index
    assert mirrored[35] == obs[35], "the laser along the heading is its own mirror"
    assert np.array_equal(mirrored[36:], obs[36:][::-1])


def test_an_empty_vehicle_slot_and_the_checkpoint_placeholder_stay_as_they_are():
    obs = np.zeros(EXPERT_OBSERVATION_WIDTH)
    obs[10] = obs[15] = 1.0  # what `obs_correction` makes of a navigation not yet updated
    obs[23:27] = [0.5, 0.25, 0.5, 0.75]  # one vehicle, in the second slot
    mirrored = mirror_expert_observation(obs)
    assert np.array_equal(mirrored[9:19], obs[9:19]), "the placeholder is not a lateral"
    assert mirrored[19:23].tolist() == [0.0] * 4 and mirrored[27:35].tolist() == [0.0] * 8
    assert mirrored[23:27].tolist() == [0.5, 0.75, 0.5, 0.25]


def test_the_mirror_refuses_any_other_width():
    with pytest.raises(ValueError, match="275"):
        mirror_expert_observation(np.zeros(19))


# --- live: the expert this package binds ------------------------------------------------------


@needs_sim
def test_the_expert_bound_here_is_the_numpy_one_whatever_torch_says():
    policy = load_policy("scenariobank.policies:ExpertPolicy")
    assert policy.expert.__module__ == "metadrive.examples.ppo_expert.numpy_expert"


@needs_sim
@needs_bank
def test_two_expert_runs_of_one_scenario_are_one_run_and_the_floor_is_another(tmp_path):
    job = Job(
        schema_version=JOB_SCHEMA_VERSION,
        bank=JobBank(path=str(LIVE_BANK)),
        scenarios=["intersection_left_0000"],
        policy="scenariobank.policies:ExpertPolicy",
    )
    first = run_bank(job, tmp_path / "first")
    second = run_bank(job, tmp_path / "second")
    assert first.results[0].actions_digest == second.results[0].actions_digest
    assert first.results[0].steps == second.results[0].steps > 0
    assert first.env.observation_shape_before == first.env.observation_shape_after == (19,)
    floor = job.model_copy(update={"policy": "scenariobank.policies:ConstantPolicy"})
    assert run_bank(floor, tmp_path / "floor").results[0].actions_digest != (
        first.results[0].actions_digest
    )
