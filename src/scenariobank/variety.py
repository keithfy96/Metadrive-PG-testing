"""How different are two roads, and which seeds would give you the most different ones.

This module exists because a hash could not answer the question that was being asked of it.
`fingerprint.lane_geometry_digest` decides whether two roads are *the same*, and
`docs/reference/destinations.md` counted its equality classes and called the result "distinct
roads". That reported `curve` seeds 0 and 4 as two roads when their first blocks differ by 2%
and their routes by 7% -- close enough that the two thumbnails are the same picture -- and
reported `roundabout` seeds 0 and 4 as two roads when nothing measurable separates them.

So a seed is not automatically a scenario. `scan` measures how much a candidate seed would
actually add to a set you are keeping, and `closest_pair` reports the least distinct pair a
sequence already has. Both are built on `fingerprint.shape_gap`, which is deliberately coarse.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from scenariobank.categories import Category
from scenariobank.sockets import (
    SocketError,
    read_sockets_from_env,
    route_rotation,
    select_exit,
    turn_pairs,
)


@dataclass(frozen=True)
class SeedReading:
    """One seed, measured: what it drives like, and how unlike the kept seeds it is."""

    seed: int
    #: True when this seed is one of the ones being kept, so it is a baseline, not a candidate.
    is_kept: bool
    destination: str
    route_length_m: float
    net_rotation_deg: float
    turn_pairs: str
    #: Worst relative difference from the *nearest* kept seed's road. `None` for a kept seed.
    #: Small means this seed would add little: 0.07 is `curve` seed 4 against seed 0.
    gap: float | None
    nearest_kept: int | None

    def describe(self) -> str:
        gap = (
            "kept"
            if self.gap is None
            else f"{self.gap * 100:4.0f}% from seed {self.nearest_kept}"
        )
        pairs = f" {self.turn_pairs}" if self.turn_pairs else ""
        return (
            f"seed {self.seed:<4}{self.route_length_m:7.1f} m  {self.net_rotation_deg:+7.1f} deg"
            f"{pairs:<5}  -> {self.destination:<9} {gap}"
        )


def scan(
    category: Category,
    keep: Sequence[int],
    candidates: Sequence[int],
    *,
    progress: Callable[[str], None] | None = None,
) -> list[SeedReading]:
    """Measure every seed in `keep` and `candidates`, and rank the candidates by distinctness.

    One env for the whole scan, one reset per seed. The env is sized over the full seed *range*
    with `num_scenarios_for`, because `num_scenarios` bounds an index rather than counting
    scenarios -- a scan of `0-30` is 31 wide however few seeds are asked for.

    Kept seeds come back too, unranked, so the output shows what the candidates are being
    compared against rather than asking you to hold it in your head.
    """
    from metadrive.envs.metadrive_env import MetaDriveEnv

    from scenariobank.bank import num_scenarios_for
    from scenariobank.config import base_config
    from scenariobank.fingerprint import road_shape, shape_gap

    keep = tuple(dict.fromkeys(keep))
    every = tuple(dict.fromkeys((*keep, *candidates)))
    if not every:
        raise SocketError("nothing to scan: give at least one seed")
    say = progress or (lambda _message: None)

    shapes: dict[int, object] = {}
    measured: dict[int, tuple[str, float, float, str]] = {}
    env = MetaDriveEnv(
        base_config(
            map=category.block_seq,
            start_seed=min(every),
            num_scenarios=num_scenarios_for(every),
        )
    )
    try:
        for done, seed in enumerate(sorted(every), start=1):
            env.reset(seed=seed)
            chosen = select_exit(read_sockets_from_env(env), category.exit_rule)
            env.agent.navigation.set_route(env.agent.lane_index, chosen.node)
            rotation, rotations = route_rotation(env)
            shapes[seed] = road_shape(env.engine.current_map)
            measured[seed] = (
                chosen.node,
                round(float(env.agent.navigation.total_length), 2),
                round(rotation, 2),
                turn_pairs(rotations),
            )
            say(f"[{done}/{len(every)}] seed {seed}")
    finally:
        env.close()

    readings = []
    for seed in every:
        node, length, rotation, pairs = measured[seed]
        gap = nearest = None
        if seed not in keep and keep:
            nearest = min(keep, key=lambda k: shape_gap(shapes[seed], shapes[k]))
            gap = shape_gap(shapes[seed], shapes[nearest])
        readings.append(
            SeedReading(
                seed=seed,
                is_kept=seed in keep,
                destination=node,
                route_length_m=length,
                net_rotation_deg=rotation,
                turn_pairs=pairs,
                gap=gap,
                nearest_kept=nearest,
            )
        )
    kept = [r for r in readings if r.is_kept]
    ranked = sorted(
        (r for r in readings if not r.is_kept), key=lambda r: -(r.gap or 0.0)
    )
    return kept + ranked


def closest_pair(shapes: dict[int, object]) -> tuple[int, int, float] | None:
    """Return the two most alike seeds and how far apart they are, or `None` for fewer than two.

    This is what the distinct-roads table was missing: it counted how many roads were *not
    identical* and never said how close the closest two were.
    """
    import itertools

    from scenariobank.fingerprint import shape_gap

    pairs = [
        (shape_gap(shapes[a], shapes[b]), a, b)
        for a, b in itertools.combinations(sorted(shapes), 2)
    ]
    if not pairs:
        return None
    gap, a, b = min(pairs)
    return a, b, gap


#: Below this, two seeds of one sequence are reported as near-duplicates rather than as two
#: roads. Chosen from measurement, not taste: `curve` seeds 0 and 4 sit at 0.07 and are visibly
#: the same corner, while the next-closest `curve` pair is at 0.34 and is visibly not.
NEAR_DUPLICATE = 0.10


__all__ = ["NEAR_DUPLICATE", "SeedReading", "closest_pair", "scan"]
