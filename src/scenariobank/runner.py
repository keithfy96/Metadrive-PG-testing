"""The step loop. One of them, for both bank kinds, and every caller goes through it.

`replay.drive` owned this loop from Phase 3 Step 6, and it refused procedural banks precisely so
it would not become a second runner. Step 2 moves the loop here and turns `replay` into a caller
of it, so `grep -n "env.step(" src/` returns one site. Everything kind-specific -- which env, which
seed, what `prepare` does -- is settled in `env.py` before this module is reached; what arrives
here is an env that has been built, a seed, a prepare step, a cap and something that acts.

**The cap is enforced here, on top of `horizon`.** `horizon` is an env-level guard and one env
carries one of them, but a row's budget is per row (`entry.budget_for(row)`: its own `max_steps`
if it declares one, else its entry's). So the loop counts, and stops at the cap whether or not
the env has said so -- which is also what turns a `horizon` that failed to take into a bounded
run rather than a silent 1000-step one. Belt and braces, on both kinds, identically.

**Collisions are counted on the rising edge.** `crash_vehicle` and its siblings are per-step
booleans (`base_vehicle.py:43-45`, set at `:788-794`), and `contact_results` is a set of *type
names* (`:61`, `:798`), so "once per vehicle per episode" needs an identity MetaDrive does not
hand over. Counting each flag's low-to-high transitions is the cheap correct thing, and it is what
the converter's complaint -- a per-step count "reports one collision as thirty, and the number
describes the frame rate" -- is actually about. Per-vehicle identity via the `contactTest` nodes
is a Step 3 check to attempt, not a promise made here.

**The decision rate is a stride in this loop**, never a MetaDrive key: the same action is handed
to `env.step` until the next decision is due, so a slower rate changes how many actions are
issued and never how long the episode is.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

#: The `info` flags a collision count is read off, as `info` key -> the name the count is kept
#: under. In the order `metadrive_env.py:138-142` writes them.
COLLISION_FLAGS: tuple[tuple[str, str], ...] = (
    ("crash_vehicle", "vehicle"),
    ("crash_object", "object"),
    ("crash_building", "building"),
    ("crash_human", "human"),
    ("crash_sidewalk", "sidewalk"),
)

#: What acts: the observation in, an action out. A policy, once Step 4 builds one; until then a
#: closure over a constant action.
Actor = Callable[[Any], Sequence[float]]


@dataclass
class Drive:
    """One episode, measured. What the loop knows and nothing it does not.

    A dataclass rather than a model because it is not a record anyone reads back -- `replay`
    turns it into an `Episode`, and Step 3 will turn it into a result. `info` is the env's last
    `info` dict whole, so the caller chooses which keys matter.
    """

    seed: int
    #: Where the route ends, read back off the env by `prepare`. `None` on a recorded row.
    destination: str | None
    steps: int
    actions: int
    terminated: bool
    truncated: bool
    info: dict[str, Any]
    #: Rising-edge counts, by `COLLISION_FLAGS` name.
    collisions: dict[str, int] = field(default_factory=dict)
    observation_shape: tuple[int, ...] | None = None
    observation_shape_end: tuple[int, ...] | None = None
    action_shape: tuple[int, ...] | None = None
    seconds: float = 0.0


def shape_of(value: Any) -> tuple[int, ...] | None:
    """The shape of an observation or a space, or `None` if it has none."""
    shape = getattr(value, "shape", None)
    return tuple(int(n) for n in shape) if shape else None


def count_rising_edges(counts: dict[str, int], previous: dict[str, bool], info: dict) -> None:
    """Advance the collision counts by one step's `info`. A flag held high counts once."""
    for key, name in COLLISION_FLAGS:
        now = bool(info.get(key))
        if now and not previous.get(name, False):
            counts[name] = counts.get(name, 0) + 1
        previous[name] = now


def run_episode(
    env: Any,
    *,
    seed: int,
    prepare: Callable[[Any], str | None],
    cap: int,
    stride: int,
    act: Actor,
) -> Drive:
    """Reset onto `seed`, `prepare`, then step until the env ends the episode or `cap` is hit.

    `prepare` is `env.py`'s per-row step with the row already bound; it is called after the reset
    and its return is the drive's `destination`. `act` is asked once per `stride` steps and its
    answer held between. The caller owns the env, including `close()`.
    """
    observation, _ = env.reset(seed=seed)
    destination = prepare(env)
    at_reset = shape_of(observation)
    action_shape = shape_of(env.action_space)
    counts: dict[str, int] = {name: 0 for _, name in COLLISION_FLAGS}
    previous: dict[str, bool] = {}
    taken = 0
    issued = 0
    info: dict[str, Any] = {}
    terminated = truncated = False
    action: list[float] = []
    started = time.perf_counter()
    while taken < cap:
        # The stride, and the whole of the decision rate: the same action is handed to
        # `env.step` until the next decision is due.
        if taken % stride == 0:
            action = [float(value) for value in act(observation)]
            issued += 1
        observation, _reward, terminated, truncated, info = env.step(action)
        taken += 1
        count_rising_edges(counts, previous, info)
        if terminated or truncated:
            break
    seconds = time.perf_counter() - started
    return Drive(
        seed=seed,
        destination=destination,
        steps=taken,
        actions=issued,
        terminated=bool(terminated),
        truncated=bool(truncated),
        info=dict(info),
        collisions=counts,
        observation_shape=at_reset,
        observation_shape_end=shape_of(observation),
        action_shape=action_shape,
        seconds=seconds,
    )


__all__ = ["COLLISION_FLAGS", "Actor", "Drive", "count_rising_edges", "run_episode", "shape_of"]
