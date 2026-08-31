"""Reading a block's exit sockets, and turning a category's rule into a destination node.

This is a discovery tool that became a generation-time dependency. The draft classified turns
after the fact -- reset, measure the angle between spawn heading and final-lane heading, keep the
seeds that came out above +30 degrees. That is gone. Here the angle is measured for every socket
*before* anything drives, the category's rule picks one, and the chosen node is written into
`vehicle_config["destination"]` so `auto_assign_task` never runs.
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

    import numpy as np
    from metadrive.envs.metadrive_env import MetaDriveEnv
    from metadrive.utils.math import wrap_to_pi

    from scenariobank.config import base_config

    env = MetaDriveEnv(base_config(map=block_seq, start_seed=seed, num_scenarios=1))
    try:
        env.reset(seed=seed)
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
    finally:
        env.close()


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


def route_length(category: Category, seed: int, destination: str) -> float:
    """Return the length of the pinned route, in metres, by building the env that drives it.

    Separate from `read_sockets` on purpose: this one proves the destination is *reachable*,
    which naming a node does not. `set_route` runs a shortest path and would raise here rather
    than at run time.
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
        return float(env.agent.navigation.total_length)
    finally:
        env.close()


def survey(category: Category, seeds: tuple[int, ...]) -> list[dict[str, Any]]:
    """Resolve and measure `category` at every seed. This is what writes destinations.md."""
    rows = []
    for seed in seeds:
        exit_socket = resolve_destination(category, seed)
        rows.append(
            {
                "seed": seed,
                "destination": exit_socket.node,
                "angle_deg": exit_socket.angle_deg,
                "turn": _turn_word(exit_socket.angle_deg),
                "route_length_m": round(route_length(category, seed, exit_socket.node), 1),
            }
        )
    return rows


__all__ = [
    "CategoryError",
    "SocketError",
    "SocketReading",
    "read_sockets",
    "resolve_destination",
    "route_length",
    "select_exit",
    "survey",
]
