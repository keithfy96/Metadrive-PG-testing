"""`runner.py`: the one step loop, driven against a fake env so none of this needs a simulator.

What the loop owns and what these pin: the cap is enforced whether or not the env ends the
episode, the stride asks the actor once per `stride` steps and holds the answer between, and a
collision flag held high for thirty steps counts once. The last is the converter's complaint --
"reports one collision as thirty, and the number describes the frame rate" -- turned into a test.

The fake env is the smallest thing `run_episode` can be handed: `reset` and `step` with the
gymnasium five-tuple, an `action_space` with a shape, and a scripted `info` per step.
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np

from scenariobank.runner import COLLISION_FLAGS, count_rising_edges, run_episode


class FakeSpace:
    shape = (2,)


class FakeEnv:
    """Ends after `ends_at` steps if given one, else runs until the loop caps it."""

    def __init__(self, *, ends_at: int | None = None, infos: list[dict] | None = None):
        self.ends_at = ends_at
        self.infos = infos or []
        self.action_space = FakeSpace()
        self.seen: list[list[float]] = []
        self.reset_seed: int | None = None
        self.taken = 0

    def reset(self, *, seed: int):
        self.reset_seed = seed
        return np.zeros(19), {}

    def step(self, action):
        self.seen.append(list(action))
        self.taken += 1
        info = dict(self.infos[self.taken - 1]) if self.taken <= len(self.infos) else {}
        truncated = self.ends_at is not None and self.taken >= self.ends_at
        if truncated:
            info["max_step"] = True
        return np.zeros(19), 0.0, False, truncated, info


def zero(_observation):
    return (0.0, 0.0)


def test_the_cap_ends_an_episode_the_env_would_not():
    """`horizon` is the env's guard and this cap is the row's. When the env keeps going, the loop
    does not -- which is what turns a `horizon` that failed to take into a bounded run."""
    env = FakeEnv()
    drive = run_episode(env, seed=7, prepare=lambda _env: None, cap=25, stride=1, act=zero)
    assert drive.steps == 25
    assert env.taken == 25
    assert not drive.terminated and not drive.truncated


def test_the_env_ending_first_stops_the_loop_short_of_the_cap():
    env = FakeEnv(ends_at=10)
    drive = run_episode(env, seed=0, prepare=lambda _env: None, cap=100, stride=1, act=zero)
    assert drive.steps == 10
    assert drive.truncated is True
    assert drive.info["max_step"] is True


def test_the_reset_seed_and_the_prepare_result_are_what_the_drive_records():
    env = FakeEnv(ends_at=1)
    drive = run_episode(
        env, seed=28, prepare=lambda _env: "1T0_1_", cap=5, stride=1, act=zero
    )
    assert env.reset_seed == 28
    assert (drive.seed, drive.destination) == (28, "1T0_1_")
    assert drive.observation_shape == (19,)
    assert drive.action_shape == (2,)


def test_the_stride_asks_the_actor_once_per_stride_and_holds_the_answer_between():
    """Not a MetaDrive key. Five steps per decision issues a fifth of the actions and every step
    still receives one -- the previous decision, held."""
    asked: list[int] = []

    def counting(_observation):
        asked.append(1)
        return (0.1 * len(asked), 0.0)

    env = FakeEnv()
    drive = run_episode(env, seed=0, prepare=lambda _env: None, cap=23, stride=5, act=counting)
    assert drive.actions == len(asked) == 5
    assert drive.steps == 23
    assert env.seen[0] == env.seen[4] == [0.1, 0.0]
    assert env.seen[5] == [0.2, 0.0]


def test_a_collision_flag_held_high_for_thirty_steps_counts_once():
    """The rising edge, not the level. `crash_vehicle` is a per-step boolean that stays true for
    as long as the contact lasts, and thirty is what one contact reads as at the frame rate."""
    infos = [{"crash_vehicle": True} for _ in range(30)]
    env = FakeEnv(infos=infos)
    drive = run_episode(env, seed=0, prepare=lambda _env: None, cap=30, stride=1, act=zero)
    assert drive.collisions["vehicle"] == 1
    assert sum(drive.collisions.values()) == 1


def test_two_separate_contacts_count_twice_and_each_kind_is_counted_apart():
    infos = (
        [{"crash_vehicle": True}] * 3
        + [{}] * 2
        + [{"crash_vehicle": True, "crash_sidewalk": True}] * 4
        + [{"crash_sidewalk": True}] * 1
    )
    env = FakeEnv(infos=infos)
    drive = run_episode(env, seed=0, prepare=lambda _env: None, cap=len(infos), stride=1, act=zero)
    assert drive.collisions["vehicle"] == 2
    assert drive.collisions["sidewalk"] == 1
    assert drive.collisions["object"] == 0


def test_the_counter_alone_reads_only_the_named_flags():
    counts: dict[str, int] = {}
    previous: dict[str, bool] = {}
    count_rising_edges(counts, previous, {"crash": True, "crash_human": True, "max_step": True})
    count_rising_edges(counts, previous, {"crash_human": True})
    count_rising_edges(counts, previous, {})
    count_rising_edges(counts, previous, {"crash_human": True})
    assert counts == {"human": 2}
    assert {name for _, name in COLLISION_FLAGS} >= set(counts)


def test_every_collision_name_is_present_in_a_drive_even_at_zero():
    """A result that omits a count it did not see would read as "not measured" rather than
    "none", and those are different claims."""
    env = FakeEnv(ends_at=1)
    drive = run_episode(env, seed=0, prepare=lambda _env: None, cap=5, stride=1, act=zero)
    assert set(drive.collisions) == {name for _, name in COLLISION_FLAGS}
    assert all(count == 0 for count in drive.collisions.values())


def test_the_package_has_exactly_one_step_loop():
    """Phase 4's own done-when: `replay` and, later, `run` are callers of this loop, not owners
    of one. A second `env.step` in `src/` is a second runner."""
    call = re.compile(r"^\s*[^#\n]*=\s*env\.step\(", re.MULTILINE)
    sources = Path("src/scenariobank").rglob("*.py")
    sites = [path for path in sources if call.search(path.read_text())]
    assert sites == [Path("src/scenariobank/runner.py")]


def test_a_stop_ends_the_episode_between_two_steps_and_says_so():
    """Step 3's hook: read before each step, so a stop lands between steps and never inside
    one. Nothing else about the drive changes -- not capped, not truncated, seven steps taken."""
    env = FakeEnv()
    drive = run_episode(
        env,
        seed=0,
        prepare=lambda _env: None,
        cap=100,
        stride=1,
        act=zero,
        stop=lambda: env.taken >= 7,
    )
    assert (drive.steps, drive.stopped) == (7, True)
    assert env.taken == 7
    assert not drive.terminated and not drive.truncated


class TrafficCone:
    pass


class DefaultVehicle:
    pass


class FakeLayout:
    def layout_digest(self):
        return "abc123"


class FakeEngine:
    """What the loop reads off an env's engine: the objects, and an actor manager if any."""

    def __init__(self, objects, manager=None):
        self.objects = objects
        if manager is not None:
            self.vru_manager = manager

    def get_objects(self):
        return self.objects


def test_placed_and_the_actor_layout_are_read_off_the_engine_after_the_reset():
    """Step 4b: by class name, sorted; the digest from the actor manager when one is
    registered. An env with no engine -- every fake in these tests -- reads as nothing placed
    and no layout, rather than as an error."""
    env = FakeEnv(ends_at=1)
    env.engine = FakeEngine({"a": DefaultVehicle(), "b": TrafficCone(), "c": TrafficCone()})
    drive = run_episode(env, seed=0, prepare=lambda _env: None, cap=5, stride=1, act=zero)
    assert drive.placed == {"DefaultVehicle": 1, "TrafficCone": 2}
    assert drive.actor_layout_digest is None
    assert list(drive.placed) == sorted(drive.placed)
    env.engine = FakeEngine({"a": DefaultVehicle()}, manager=FakeLayout())
    drive = run_episode(env, seed=0, prepare=lambda _env: None, cap=5, stride=1, act=zero)
    assert (drive.placed, drive.actor_layout_digest) == ({"DefaultVehicle": 1}, "abc123")
    bare = run_episode(
        FakeEnv(ends_at=1), seed=0, prepare=lambda _env: None, cap=5, stride=1, act=zero
    )
    assert (bare.placed, bare.actor_layout_digest) == ({}, None)


def test_the_loop_sums_reward_and_cost_and_keeps_every_action_it_issued():
    class Rewarding(FakeEnv):
        def step(self, action):
            observation, _reward, terminated, truncated, info = super().step(action)
            info["cost"] = 0.5
            return observation, 2.0, terminated, truncated, info

    asked: list[int] = []

    def counting(_observation):
        asked.append(1)
        return (0.1 * len(asked), 0.0)

    env = Rewarding(ends_at=6)
    drive = run_episode(env, seed=0, prepare=lambda _env: None, cap=100, stride=3, act=counting)
    assert (drive.reward, drive.cost) == (12.0, 3.0)
    assert drive.issued_actions == [[0.1, 0.0], [0.2, 0.0]]
    assert drive.stopped is False
