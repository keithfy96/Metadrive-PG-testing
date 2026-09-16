"""`TrafficLightManager`: the Lights axis, as lights at every junction on a per-seed clock.

MetaDrive ships the light and not the manager. `BaseTrafficLight`
(`component/traffic_light/base_traffic_light.py`) is a lane-wide invisible wall with four
rendered models: on green the wall's collide mask is `AllOff` and nothing detects it, on red
and yellow it is `InvisibleWall`, so the ego's contact test (`base_vehicle.py:_state_check`)
sets `vehicle.red_light` **only** when the ego crosses the line on red -- the violation flag was
already correct, and written by nothing. The IDM policy every traffic car drives with sees the
wall as a stopped object ahead (`idm_policy.py:347`) and brakes for it, so cross traffic waits at
red without a give-way rule being taught. What was missing is the thing that puts a light on a
procedural road and changes its colour: the only manager MetaDrive has, `ScenarioLightManager`,
replays a recorded tape and needs `data_manager`, which a PG run does not have.

**Where a light goes.** Every `InterSection` block (`X`, `T` and the `Std` variants -- an
`isinstance` test, so a new junction kind is covered) has one road in from the block before it
(`pre_block_socket.positive_road`, the arm the ego arrives on) and one road in per remaining
socket (`socket.negative_road`; a `T` has already removed its missing arm's roads and socket).
One light per lane of each road in, at the **stop line** -- `STOP_LINE_SETBACK` metres before
the lane ends and the junction begins -- and not at the stock `PLACE_LONGITUDE = 5`, which is
five metres *into* the lane from its start, in the block before the junction. The heading is
taken at the same longitude, so a light on a curved approach faces the way the lane does there.

**The phase model, ported from the converter's `tools/signal_control.py`.** One `cycle_s` for
the junction; per group, `green_s`, `yellow_s` and an `offset_s` at which its green starts;
**red is the remainder**, so the three can never fail to add up. Two groups per junction: the
ego's arm and the arm opposite it (`major`), and the cross arms (`minor`), told apart by the
heading of each road at the stop line. `LEVELS["lights"]` names the cycle and the major green
-- the ego's own green, which is what a level reads as: `high` is green ten seconds in thirty.
The minor road's green is what the cycle has left after both yellows, and the two groups are
never green together by construction (`plan` refuses a level that would make them).

**One offset per episode, applied to every group**, drawn from `self.np_random`, which
`BaseEngine.seed` re-seeds from the episode seed before every reset the way it does every
manager's. Randomising groups separately would put crossing movements green at once (the
converter's own finding); drawing from a private generator, as the converter does, would make
the colour at a given step vary between runs of one seed, and here a seed is a promise. The
clock is `episode_step` times the engine's step -- `physics_world_step_size x decision_repeat`
off the config, never a configured rate; the converter's note: *"reading the tape's rate here
was right only by coincidence. Do not put it back."*

**Destroyed and respawned every episode**, with `force_destroy=True`. `engine.clear_objects`
otherwise recycles an object into a pool and `spawn_object` hands it back with
`obj.reset(**kwargs)`, and `engine._object_clean_check` asserts only on `BaseVehicle` and
`TrafficObject` -- so a light kept across a reset would survive with `self.lane` pointing into
a map that no longer exists, and nothing would say so.

A state-vector policy cannot perceive these lights: the 19-number observation has no colour in
it. A camera policy sees the rendered model natively. That is the axis's scope, and it is why
the bundled expert running a red is this module's pass condition rather than a defect.

Imports the simulator at module scope, like `obstacles.py` and `actors.py`: a manager is a
simulator object, and there is nothing here to run without one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from metadrive.component.pgblock.intersection import InterSection
from metadrive.component.traffic_light.base_traffic_light import BaseTrafficLight
from metadrive.manager.base_manager import BaseManager

#: How long every yellow lasts. Not a level: the converter's plans use one figure for every
#: group, and a yellow that varied with difficulty would move a rule of the road, not a knob.
YELLOW_S = 3.0

#: Metres before the end of an approach lane the stop line sits. Inside the lane, so the wall
#: is on the approach and not in the junction box, and far enough in that a car stopped at it
#: is clear of the crossing traffic.
STOP_LINE_SETBACK = 1.0

#: An arm whose heading is within this of the ego's arm, or of its reverse, is on the same
#: road through the junction. The PG junctions are square, so cross arms sit near 90 degrees.
SAME_ROAD_RAD = math.radians(30)

GREEN, YELLOW, RED = "green", "yellow", "red"


class LightsError(ValueError):
    """A schedule that cannot be built. Says which number and why."""


@dataclass(frozen=True)
class Phase:
    """One group's timing: when its green starts within the cycle, and how long each colour is."""

    cycle_s: float
    green_s: float
    yellow_s: float
    offset_s: float

    @property
    def red_s(self) -> float:
        return self.cycle_s - self.green_s - self.yellow_s

    def colour(self, seconds: float) -> str:
        """The colour this group shows `seconds` into the episode. Green, yellow, then red."""
        into = (seconds - self.offset_s) % self.cycle_s
        if into < self.green_s:
            return GREEN
        if into < self.green_s + self.yellow_s:
            return YELLOW
        return RED


def plan(cycle_s: float, green_s: float, yellow_s: float = YELLOW_S) -> tuple[Phase, Phase]:
    """The two groups of one junction: `(major, minor)`.

    The major group is green from the top of the cycle for `green_s`; the minor group's green
    starts when the major's yellow ends and runs until its own yellow must start for the cycle
    to close. Red is what is left on each, so a plan either fits or is refused here.
    """
    if cycle_s <= 0 or green_s <= 0 or yellow_s <= 0:
        raise LightsError(
            f"a lights schedule needs positive numbers: cycle {cycle_s}, green {green_s}, "
            f"yellow {yellow_s}"
        )
    minor_green = cycle_s - green_s - 2 * yellow_s
    if minor_green <= 0:
        raise LightsError(
            f"green {green_s} s plus two yellows of {yellow_s} s leaves the cross road "
            f"{minor_green:.1f} s of green in a {cycle_s} s cycle; the two roads would be green "
            "together"
        )
    major = Phase(cycle_s=cycle_s, green_s=green_s, yellow_s=yellow_s, offset_s=0.0)
    minor = Phase(
        cycle_s=cycle_s, green_s=minor_green, yellow_s=yellow_s, offset_s=green_s + yellow_s
    )
    return major, minor


def same_road(heading: float, other: float) -> bool:
    """Whether two arms are the one road through a junction: parallel, either way round."""
    difference = (heading - other) % math.pi
    return min(difference, math.pi - difference) < SAME_ROAD_RAD


class PGTrafficLight(BaseTrafficLight):
    """A light at a longitude along its lane, facing the way the lane does there."""

    def __init__(self, lane, longitude: float, **kwargs: Any) -> None:
        super().__init__(lane, position=lane.position(longitude, 0), **kwargs)
        self.longitude = longitude
        self.set_heading_theta(lane.heading_theta_at(longitude))

    def set_status(self, status: str) -> None:
        if status == GREEN:
            self.set_green()
        elif status == YELLOW:
            self.set_yellow()
        elif status == RED:
            self.set_red()
        else:
            raise LightsError(f"{status!r} is not a colour")


@dataclass(frozen=True)
class Placed:
    """One light: where it is and which group drives it. What the layout tests read."""

    block: str
    group: str
    lane_index: tuple
    position: tuple[float, float]
    heading: float


class TrafficLightManager(BaseManager):
    """Spawns a light at every approach lane of every junction; cycles them from one clock."""

    #: After the map (0), the traffic (10) and the actors (11): the lights need the map and
    #: nothing needs the lights at reset.
    PRIORITY = 12

    def __init__(self) -> None:
        super().__init__()
        self.cycle_s = 0.0
        self.green_s = 0.0
        self.step_s = 0.0
        self.episode_offset_s = 0.0
        self.phases: tuple[Phase, Phase] | None = None
        #: Light id -> the phase that drives it.
        self._driven: dict[str, Phase] = {}
        self._placed: list[Placed] = []

    def before_reset(self) -> None:
        # Destroyed, never recycled: see the module docstring on `_object_clean_check`.
        self.clear_objects(list(self.spawned_objects), force_destroy=True)
        self.spawned_objects = {}
        self._driven = {}
        self._placed = []
        config = self.engine.global_config
        self.cycle_s = float(config["lights_cycle_s"])
        self.green_s = float(config["lights_green_s"])
        self.step_s = float(config["physics_world_step_size"]) * int(config["decision_repeat"])
        self.phases = plan(self.cycle_s, self.green_s)

    def reset(self) -> None:
        """One light per approach lane of every junction, then the episode's offset."""
        assert self.phases is not None
        major, minor = self.phases
        network = self.engine.current_map.road_network
        for block in self.engine.current_map.blocks:
            if not isinstance(block, InterSection):
                continue
            arms = [block.pre_block_socket.positive_road] + [
                socket.negative_road for socket in block.get_socket_list()
            ]
            ego_heading: float | None = None
            for road in arms:
                lanes = road.get_lanes(network)
                if not lanes:
                    continue
                heading = lanes[0].heading_theta_at(lanes[0].length - STOP_LINE_SETBACK)
                if ego_heading is None:
                    ego_heading = heading
                group = "major" if same_road(heading, ego_heading) else "minor"
                phase = major if group == "major" else minor
                for lane in lanes:
                    longitude = max(lane.length - STOP_LINE_SETBACK, 0.0)
                    light = self.spawn_object(PGTrafficLight, lane=lane, longitude=longitude)
                    self._driven[light.id] = phase
                    self._placed.append(
                        Placed(
                            block=block.id,
                            group=group,
                            lane_index=tuple(lane.index),
                            position=(float(light.position[0]), float(light.position[1])),
                            heading=float(lane.heading_theta_at(longitude)),
                        )
                    )
        # One draw, after the layout, applied to every group: the layout is the map's and the
        # offset is this episode's, and a test pins that stepping consumes nothing further.
        self.episode_offset_s = float(self.np_random.rand()) * self.cycle_s
        self._show(0)

    def before_step(self, *args: Any, **kwargs: Any) -> dict:
        """The colour for the step about to be simulated. `episode_step` was just advanced."""
        self._show(self.episode_step)
        return {}

    def _show(self, step: int) -> None:
        seconds = step * self.step_s + self.episode_offset_s
        for light_id, phase in self._driven.items():
            light = self.spawned_objects[light_id]
            colour = phase.colour(seconds)
            if getattr(light, "shown", None) != colour:
                light.set_status(colour)
                light.shown = colour

    def colour_at(self, step: int, group: str) -> str:
        """What `group` shows at `step`, for a reader without a light to look at."""
        assert self.phases is not None
        phase = self.phases[0] if group == "major" else self.phases[1]
        return phase.colour(step * self.step_s + self.episode_offset_s)

    def placed(self) -> list[Placed]:
        return list(self._placed)

    def get_state(self) -> dict:
        state = super().get_state()
        state["episode_offset_s"] = self.episode_offset_s
        return state


__all__ = [
    "GREEN",
    "RED",
    "SAME_ROAD_RAD",
    "STOP_LINE_SETBACK",
    "YELLOW",
    "YELLOW_S",
    "LightsError",
    "PGTrafficLight",
    "Phase",
    "Placed",
    "TrafficLightManager",
    "plan",
    "same_road",
]
