"""Reading a block's exit sockets, and turning a category's rule into a destination node.

This is a discovery tool that became a generation-time dependency. The draft classified turns
after the fact -- reset, measure the angle between spawn heading and final-lane heading, keep the
seeds that came out above +30 degrees. That is gone. Here the angle is measured for every socket
*before* anything drives, the category's rule picks one, and the chosen node is pinned so that
`auto_assign_task` never chooses the route.

Pinned two ways, for the same effect. `measure_route` and `figures.draw_route` set
`vehicle_config["destination"]` at construction, which is the honest thing for a command that
builds one env to answer one question. `bank.generate` instead resets first and calls
`navigation.set_route` afterwards, because it resets once per seed and shares that reset across
every category on the same road -- and `auto_assign_task` draws its throwaway destination from a
*fresh* generator (`get_np_random(random_seed)`), not from a manager's stream, so letting it run
and overriding it afterwards perturbs nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from scenariobank.categories import (
    TARGET_ANGLE,
    Category,
    CategoryError,
    ExitRule,
    validate_block_seq,
)

#: Heading samples per route lane when integrating rotation. Dense enough that a single step
#: never approaches pi even on the tightest arc MetaDrive builds (radius 25 m), which is what
#: lets `route_rotation` sum wrapped deltas without losing a full turn.
_ROTATION_SAMPLES = 20


class SocketError(RuntimeError):
    """Raised when a block does not offer the exit a category asked for."""


@dataclass(frozen=True)
class SocketReading:
    """One exit of the destination block, as measured from where the ego spawns."""

    index: str
    node: str
    start_node: str
    lane_count: int
    angle_deg: float
    #: True when this socket is the one the ego arrives through. `auto_assign_task` excludes it
    #: from its random draw, and so does every rule here.
    is_entry: bool

    def describe(self) -> str:
        turn = "entry" if self.is_entry else _turn_word(self.angle_deg)
        return f"{self.index:<16} {self.node:<12} {self.angle_deg:+7.1f}  {turn}"


def _turn_word(angle_deg: float) -> str:
    if abs(angle_deg) < 30:
        return "straight"
    return "left" if angle_deg > 0 else "right"


def read_sockets(block_seq: str, seed: int) -> list[SocketReading]:
    """Build one env, reset it once, and measure every exit of the final block.

    The angle is the final lane's heading at its far end, minus the ego's spawn heading, wrapped
    into (-pi, pi]. `heading_theta_at` does **not** wrap -- a circular lane can run past +/-pi --
    so the wrap is load-bearing rather than defensive. Heading is counter-clockwise-positive
    (`straight_lane.py:56`), which makes positive angles left turns.
    """
    validate_block_seq(block_seq)

    from metadrive.envs.metadrive_env import MetaDriveEnv

    from scenariobank.config import base_config

    env = MetaDriveEnv(base_config(map=block_seq, start_seed=seed, num_scenarios=1))
    try:
        env.reset(seed=seed)
        return read_sockets_from_env(env)
    finally:
        env.close()


def read_sockets_from_env(env) -> list[SocketReading]:
    """Measure the exits of the map an env has **already** been reset into.

    Split out of `read_sockets` for `bank.generate`, which resets once per scenario and cannot
    afford a second env per category. The measurement is identical; only the ownership of the
    env differs.

    Safe to call after `navigation.set_route` has been pointed somewhere else: `set_route`
    rebuilds `current_road` from `checkpoints[0]`, which is always the spawn node, so `is_entry`
    reads the same before and after.
    """
    import numpy as np
    from metadrive.utils.math import wrap_to_pi

    road_map = env.engine.current_map
    spawn_heading = env.agent.heading_theta
    spawn_node = env.agent.navigation.current_road.start_node

    readings = []
    for socket in road_map.blocks[-1].get_socket_list():
        road = socket.positive_road
        lanes = road.get_lanes(road_map.road_network)
        lane = lanes[-1]
        angle = np.degrees(wrap_to_pi(lane.heading_theta_at(lane.length) - spawn_heading))
        readings.append(
            SocketReading(
                index=str(socket.index),
                node=road.end_node,
                start_node=road.start_node,
                lane_count=len(lanes),
                angle_deg=round(float(angle), 2),
                is_entry=socket.is_socket_node(spawn_node),
            )
        )
    return readings


def select_exit(readings: list[SocketReading], rule: ExitRule) -> SocketReading:
    """Apply a category's rule to a block's sockets. Pure: no simulator, no seed."""
    usable = [reading for reading in readings if not reading.is_entry]
    if not usable:
        raise SocketError("the destination block offers no exit other than the one driven in by")

    if rule is ExitRule.ONLY:
        if len(usable) != 1:
            raise SocketError(
                f"ExitRule.ONLY needs a single-exit block, but this one offers "
                f"{len(usable)}: {[reading.node for reading in usable]}. Use an angle rule."
            )
        return usable[0]

    if rule is ExitRule.SHARPEST:
        return max(usable, key=lambda reading: abs(reading.angle_deg))

    target = TARGET_ANGLE[rule]
    chosen = min(usable, key=lambda reading: abs(reading.angle_deg - target))
    # A rule that lands 60 degrees from what it asked for did not find its exit; it found the
    # least-wrong one. That is the failure the plan's "two sockets with the same sign" check is
    # aimed at, and it is cheaper to catch here than to discover in a thumbnail.
    if abs(chosen.angle_deg - target) > 45.0:
        raise SocketError(
            f"no exit near {target:+.0f} degrees: closest is {chosen.node} at "
            f"{chosen.angle_deg:+.1f}. This block is not shaped the way the category assumes."
        )
    return chosen


def resolve_destination(category: Category, seed: int) -> SocketReading:
    """Return the exit `category` drives to at `seed`. One env build, one reset."""
    try:
        return select_exit(read_sockets(category.block_seq, seed), category.exit_rule)
    except SocketError as error:
        raise SocketError(f"{category.name} at seed {seed}: {error}") from error


@dataclass(frozen=True)
class RouteMeasurement:
    """Everything one reset of the pinned route can tell us. One env build, three facts."""

    #: `navigation.total_length`. Measured on a reference lane, so it is **blind to
    #: `spawn_lane`** -- the five `X` seeds all read 111.70 while starting in two different lanes.
    length_m: float
    #: The lane the ego actually spawned in, drawn by `random_spawn_lane_index`. See
    #: `config.base_config`: this is the only thing that differs between the five `X` seeds.
    spawn_lane: int
    #: Total rotation **along the driven route**, unwrapped, in degrees. Not the same as
    #: `SocketReading.angle_deg`, which is a `wrap_to_pi` of the *final heading* -- correct for
    #: choosing an exit, wrong for describing one. `curve` seed 0 sweeps +239.5 deg and its
    #: wrapped final heading reads -120.5.
    net_rotation_deg: float
    #: One character per `Curve` block the route passes, `L` or `R`, in order. `""` for a
    #: sequence with no curves. This is what makes `CC`'s four-way direction coverage checkable.
    turn_pairs: str


def route_rotation(env) -> tuple[float, list[tuple[str, float]]]:
    """Integrate heading along the pinned route, and attribute the rotation to blocks.

    Summing **wrapped deltas** between closely spaced samples recovers the unwrapped total: a
    240 degree sweep comes out as +239.5 rather than folding to -120.5. Sampling is dense enough
    that no single step approaches pi, which is what makes the wrap safe rather than lossy.

    Rotation is measured from the geometry rather than read off `Parameter.dir`, so it describes
    the map as *built* -- `handedness.install` mirrors it, and a parameter-based reading would
    silently label every turn backwards.
    """
    import numpy as np
    from metadrive.utils.math import wrap_to_pi

    road_map = env.engine.current_map
    network = road_map.road_network
    checkpoints = list(env.agent.navigation.checkpoints)

    per_block: dict[int, float] = {}
    theta = float(env.agent.heading_theta)
    for start, end in zip(checkpoints[:-1], checkpoints[1:], strict=True):
        lane = network.graph[start][end][-1]
        owner = next(
            (
                index
                for index, block in enumerate(road_map.blocks)
                if end in block.block_network.graph.get(start, {})
            ),
            None,
        )
        for step in range(1, _ROTATION_SAMPLES + 1):
            sampled = float(lane.heading_theta_at(lane.length * step / _ROTATION_SAMPLES))
            per_block[owner] = per_block.get(owner, 0.0) + float(wrap_to_pi(sampled - theta))
            theta = sampled

    rotations = [
        (road_map.blocks[index].ID, float(np.degrees(radians)))
        for index, radians in sorted(per_block.items(), key=lambda item: (item[0] is None, item[0]))
        if index is not None
    ]
    total = float(np.degrees(sum(per_block.values())))
    return total, rotations


def turn_pairs(rotations: list[tuple[str, float]]) -> str:
    """Reduce per-block rotations to the `Curve` blocks' directions. Pure: no simulator."""
    return "".join(
        "L" if degrees > 0 else "R" for block_id, degrees in rotations if block_id == "C"
    )


def measure_route(category: Category, seed: int, destination: str) -> RouteMeasurement:
    """Build the env that drives the pinned route, reset once, and measure it.

    Separate from `read_sockets` on purpose: this one proves the destination is *reachable*,
    which naming a node does not. `set_route` runs a shortest path and would raise here rather
    than at run time. The spawn lane and the rotation are read from the same reset, so they
    cost nothing beyond the build that was already happening.
    """
    from metadrive.envs.metadrive_env import MetaDriveEnv

    from scenariobank.config import base_config

    env = MetaDriveEnv(
        base_config(
            map=category.block_seq,
            start_seed=seed,
            num_scenarios=1,
            vehicle_config={"destination": destination},
        )
    )
    try:
        env.reset(seed=seed)
        rotation, rotations = route_rotation(env)
        return RouteMeasurement(
            length_m=float(env.agent.navigation.total_length),
            spawn_lane=int(env.agent.lane_index[2]),
            net_rotation_deg=round(rotation, 2),
            turn_pairs=turn_pairs(rotations),
        )
    finally:
        env.close()


def route_length(category: Category, seed: int, destination: str) -> float:
    """Return the length of the pinned route, in metres. Thin wrapper over `measure_route`."""
    return measure_route(category, seed, destination).length_m


def survey(category: Category, seeds: tuple[int, ...]) -> list[dict[str, Any]]:
    """Resolve and measure `category` at every seed. This is what writes destinations.md."""
    rows = []
    for seed in seeds:
        exit_socket = resolve_destination(category, seed)
        measured = measure_route(category, seed, exit_socket.node)
        rows.append(
            {
                "seed": seed,
                "destination": exit_socket.node,
                "angle_deg": exit_socket.angle_deg,
                # The turn word comes from the *route*, not from the socket. They disagree
                # whenever a route sweeps past 180 degrees -- `curve` seeds 0 and 4 do.
                "turn": _turn_word(measured.net_rotation_deg),
                "net_rotation_deg": measured.net_rotation_deg,
                "turn_pairs": measured.turn_pairs,
                "spawn_lane": measured.spawn_lane,
                "route_length_m": round(measured.length_m, 1),
            }
        )
    return rows


__all__ = [
    "CategoryError",
    "RouteMeasurement",
    "SocketError",
    "SocketReading",
    "measure_route",
    "read_sockets",
    "read_sockets_from_env",
    "route_rotation",
    "resolve_destination",
    "route_length",
    "select_exit",
    "survey",
    "turn_pairs",
]
