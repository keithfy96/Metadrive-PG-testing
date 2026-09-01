"""Route figures: the visual confirmation that a category turns the way it claims to.

Deliberately not MetaDrive's top-down renderer. That renderer draws the road and the agents but
has no notion of a route (`top_down_renderer.py` has no navigation pass), and the whole question
here is which way the *route* goes. So this draws the road network from lane centrelines and
overlays the pinned route on top of it, which also means it works headless with no display, no
window, and no image buffer.
"""

from __future__ import annotations

from pathlib import Path

from scenariobank.categories import Category
from scenariobank.sockets import resolve_destination, route_rotation

#: How many points each lane centreline is sampled at. Enough for a roundabout's arcs to look
#: like arcs rather than chords.
SAMPLES_PER_LANE = 40


class FigureError(RuntimeError):
    """Raised when a route figure cannot be drawn."""


def _lane_points(lane, samples: int = SAMPLES_PER_LANE):
    import numpy as np

    return np.array([lane.position(s, 0) for s in np.linspace(0, lane.length, samples)])


def draw_route(category: Category, seed: int, out_path: Path) -> dict:
    """Render `category` at `seed` to a PNG, returning what was drawn.

    Builds the env, pins the destination through `vehicle_config` and hands the live env to
    `render_route`. That is the honest shape for a command answering one question about one
    scenario; `bank.generate`, which already holds a reset env, calls `render_route` directly.
    """
    from metadrive.envs.metadrive_env import MetaDriveEnv

    from scenariobank.config import base_config

    exit_socket = resolve_destination(category, seed)
    env = MetaDriveEnv(
        base_config(
            map=category.block_seq,
            start_seed=seed,
            num_scenarios=1,
            vehicle_config={"destination": exit_socket.node},
        )
    )
    try:
        env.reset(seed=seed)
        return render_route(env, category=category, seed=seed, exit_socket=exit_socket,
                            out_path=out_path)
    finally:
        env.close()


def render_route(
    env,
    *,
    category: Category,
    seed: int,
    exit_socket,
    out_path: Path,
    net_rotation: float | None = None,
) -> dict:
    """Draw the route of an env that has **already** reset and had its destination pinned.

    The route is taken from the navigation module rather than reconstructed here, so the picture
    shows the route the runner will actually drive and a mismatch between the rule and the road
    shows up as a picture rather than as a number.

    **This is what makes a bank thumbnail readable.** A map alone cannot answer the question
    anyone actually asks of it -- which way does this go -- because it does not say which end the
    ego starts at. `curve` seeds 2 and 3 start at the top of their frame and bend downward, and
    read from the wrong end they are indistinguishable from the seeds that turn the other way.
    The spawn arrow is the fix.

    `net_rotation` is accepted rather than always computed: `generate` needs the number for the
    manifest anyway, and `route_rotation` should run once per scenario rather than twice.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    road_map = env.engine.current_map
    network = road_map.road_network
    checkpoints = list(env.agent.navigation.checkpoints)
    route_pairs = set(zip(checkpoints[:-1], checkpoints[1:], strict=True))
    spawn = np.asarray(env.agent.position, dtype=float)
    heading = float(env.agent.heading_theta)
    total_length = float(env.agent.navigation.total_length)
    # The title reports rotation along the route, not `exit_socket.angle_deg`. The latter is
    # `wrap_to_pi`'d, so a `curve` seed that sweeps past a U-turn would be labelled with half
    # its rotation and the opposite sign -- and the picture would visibly disagree with it.
    if net_rotation is None:
        net_rotation, _ = route_rotation(env)

    figure, axes = plt.subplots(figsize=(8, 8))
    for start, tos in network.graph.items():
        for end, lanes in tos.items():
            on_route = (start, end) in route_pairs
            for lane in lanes:
                points = _lane_points(lane)
                axes.plot(
                    points[:, 0],
                    points[:, 1],
                    color="#d64545" if on_route else "#c8c8c8",
                    linewidth=3.0 if on_route else 1.0,
                    zorder=3 if on_route else 1,
                    solid_capstyle="round",
                )

    arrow = 12.0
    axes.arrow(
        spawn[0],
        spawn[1],
        arrow * np.cos(heading),
        arrow * np.sin(heading),
        width=1.2,
        color="#2b6cb0",
        zorder=5,
        length_includes_head=True,
    )
    end_lane = network.graph[checkpoints[-2]][checkpoints[-1]][-1]
    end_point = end_lane.position(end_lane.length, 0)
    axes.plot(*end_point, marker="*", markersize=22, color="#2f855a", zorder=5)

    axes.set_aspect("equal")
    axes.axis("off")
    axes.set_title(
        f"{category.name}  seed {seed}\n"
        f"{category.exit_rule.value} -> {exit_socket.node} "
        f"({net_rotation:+.1f} deg, {total_length:.0f} m)",
        fontsize=11,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(out_path, dpi=110, bbox_inches="tight")
    plt.close(figure)

    return {
        "category": category.name,
        "seed": seed,
        "destination": exit_socket.node,
        "angle_deg": exit_socket.angle_deg,
        "net_rotation_deg": round(net_rotation, 2),
        "route_length_m": round(total_length, 1),
        "path": str(out_path),
    }
