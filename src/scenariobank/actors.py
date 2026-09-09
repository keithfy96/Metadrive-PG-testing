"""`VRUManager`: pedestrians and cyclists on a procedural road. The one placement code that is ours.

MetaDrive ships the objects -- `Pedestrian` and `Cyclist` under `component/traffic_participants/`,
with physics bodies, models and `set_velocity` -- and nothing that decides where they go on a PG
map: `policy/` holds no pedestrian policy, and the only manager that spawns them replays a
logged track from a recorded dataset. So this manager decides, and because it is ours the
reproducibility of two of the six axes rests on it rather than on the simulator. Two things
follow, both tested in `test_actors.py`: two envs at one seed and one level lay the actors out
identically, and stepping the episode consumes none of the manager's randomness.

**Every draw happens in `reset()`, from `self.np_random`**, which `BaseEngine.seed()` re-seeds
from the episode seed before every reset the way it does every manager's. `after_step()` only
compares positions. The layout is drawn off the **map**, never off the ego's route: the route
is pinned *after* the reset (`env._prepare_procedural`, `navigation.set_route`), so at reset it
is not yet known, and a person crossing a road does not know where one particular car is going
anyway. Every positive road of every block after the first is a candidate; the first block is
where the ego spawns.

**What an actor does.** No walking behaviour ships: constant speed in a straight line between
two fixed endpoints, turning round at each, no avoidance.

* A **pedestrian** crosses the road. At a longitude drawn along a road it walks from just inside
  one outer kerb to just inside the other -- across the road's own lanes and the same number
  the other way, the opposing carriageway where there is one -- and back.
* A **cyclist** rides the outer lane near its kerb, between two longitudes `LANE_MARGIN` in
  from the lane's ends, and back. It is re-aimed every step at a point `LOOK_AHEAD` further
  along on the same lateral, which is what keeps it on an arc. Cyclists drawn onto the same
  lane get disjoint stretches of it, `SEGMENT_GAP` apart: measured, two on one stretch ride
  head-on into each other and spend the episode stalled, which is a collision the layout
  made rather than one the ego did.

Both stay on the road surface: the sidewalk is a rigid body, and an actor placed on it is an
actor the physics pushes somewhere the layout did not say.

**The push goes to the physics body, not through `set_velocity`.** Measured 2026-09-08: any
write to an actor's transform -- `set_heading_theta`, `standup`, and so the stock
`set_velocity`, which calls `standup` -- costs the next physics substep, so an actor pushed
that way every step moves `(decision_repeat - 1) / decision_repeat` of its speed: 0.096 m per
step at 1.2 m/s and MetaDrive's five substeps, and nothing at all at one. Writing the body's
linear velocity directly costs nothing. So `_push` does that, and rewrites the heading only
when it has turned by more than `HEADING_TOLERANCE`: at a turnaround, and every few steps on
an arc.

**An actor is not driven while a vehicle is touching it.** Found 2026-09-09 in the first film
(Phase 4 Step 5b): traffic vehicles sitting inside a bend, off the road. Not the mirror --
traffic alone holds its centreline to 0.02 m over 300 steps -- and not the obstacles, which
move nothing. Every vehicle that left its lane carried `crash_human`: it had hit a pedestrian or
a cyclist, and `_push` kept writing the actor's velocity every step regardless, so a 70 kg body
pinned against a car shoved it with an impulse the body never had to earn -- an unstoppable
object, knocking two to four of forty cars per row off the road. So `after_step` asks the
physics world, before every push, whether a vehicle is in contact with the actor
(`contactTest` on its body, the way the lidar finds its neighbours; a vehicle's chassis node is
named `MetaDriveType.VEHICLE`), and skips the push while one is: for those steps the actor is
a 70 kg body and the car moves it, not the other way round. Once the car has passed, the actor
walks or rides on. Left struck for good it lay in the lane, and the expert -- which does brake
for what its lidar sees -- sat behind it to the step cap. Measured with the gate: no vehicle
leaves its lane on any row. The names of the actors ever touched are in `struck`, in the order
it first happened. Traffic does not brake for a person -- IDM keeps its distance only from
objects with a `lane`, and a participant has none -- which is stock behaviour and is not
changed here.

**Every actor carries the lane it is on, because the traffic reads it.** `IDMPolicy.act`
(`policy/idm_policy.py:236-260`) hands every object its lidar sees to
`FrontBackObjects.get_find_front_back_objs`, which reads `obj.lane` -- and wraps the whole
thing in a bare `except` whose fallback is *no front object*. MetaDrive's participants have no
`lane` attribute, so a traffic car with a pedestrian or a cyclist anywhere in its 50 m radius
raised, fell back, and drove blind into the car ahead: with actors alone on `curve` at
`traffic=high`, thirty-one of forty cars carried `crash_vehicle` by the time the expert
crashed, and none with obstacles alone. So `reset` and `after_step` set `actor.lane` to the
lane of its road nearest to it -- the one under a crossing pedestrian, the outer lane under a
cyclist -- and the traffic then treats an actor as it treats a car on its lane: it brakes for
it, and changes lane around it. Measured after: no traffic crash and no car off the road on
that row at `hard`; with actors alone, eleven crashes in 222 steps, which is what stock
MetaDrive produces on its own map at this density with no actor at all.

**The result carries the layout as a digest**, `layout_digest()`: `fingerprint.sha256_hex` over
the sorted `layout()` lines -- kind, lane, spawn point, both endpoints, to the millimetre. A
sibling of `lane_geometry_digest`, measured on the run rather than stored in the bank, so
"the actors were the same" is checkable months later.

Imports the simulator at module scope: a manager is a simulator object.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from metadrive.component.traffic_participants.cyclist import Cyclist
from metadrive.component.traffic_participants.pedestrian import Pedestrian
from metadrive.manager.base_manager import BaseManager
from metadrive.type import MetaDriveType
from panda3d.core import LVector3

from scenariobank.fingerprint import sha256_hex

#: Walking speed, in m/s. `Pedestrian.SPEED_LIST[-1]`, the gait the faster walk animation is
#: keyed to; the slower one is a shuffle a car never meets.
PEDESTRIAN_SPEED = 1.2

#: Riding speed, in m/s. A steady urban pace; a third of the expert's cruising speed.
CYCLIST_SPEED = 4.0

#: How far inside an outer kerb a crossing turns round, in metres.
KERB_INSET = 0.5

#: How far in from the outer lane edge a cyclist rides, in metres. Wider than the bike's
#: 0.4 m body so the body stays inside the lane line.
CYCLIST_KERB_OFFSET = 0.8

#: Clearance kept from each end of a lane by every patrol, in metres. A lane shorter than twice
#: this takes no actor.
LANE_MARGIN = 5.0

#: How far ahead along its lane a cyclist aims, in metres.
LOOK_AHEAD = 2.0

#: The gap between two cyclists' stretches of one lane, in metres. More than a bike's length.
SEGMENT_GAP = 3.0

#: How far an actor's heading may drift from where it is going before it is rewritten, in
#: radians. About six degrees; see the module docstring on what a rewrite costs.
HEADING_TOLERANCE = 0.1


@dataclass(frozen=True)
class Patrol:
    """One actor's route: where it was put and the two points it walks or rides between.

    `start` and `end` are world points. A pedestrian's are the two kerbs at one longitude; a
    cyclist's are the two ends of its run on one lateral, and `lane_index` plus `lateral` say
    which lane it follows between them. `forward` is the way it set off: toward `end`.
    """

    kind: str
    lane_index: tuple[str, str, int]
    spawn: tuple[float, float]
    start: tuple[float, float]
    end: tuple[float, float]
    forward: bool
    #: Cyclists only: the lateral it holds and the longitudes it turns at.
    lateral: float = 0.0
    longitudes: tuple[float, float] = (0.0, 0.0)

    def line(self) -> str:
        """The patrol as one text line, to the millimetre. What `layout_digest` hashes."""
        points = (self.spawn, self.start, self.end)
        where = "|".join(f"{x:.3f},{y:.3f}" for x, y in points)
        return f"{self.kind}|{'/'.join(map(str, self.lane_index))}|{where}"


def _outward_sign(lanes, longitude: float) -> float:
    """Which lateral sign stacks the lanes outward from lane 0. `+1` on a one-lane road.

    Measured off the lanes rather than assumed, because `handedness` mirrors the lateral axis
    and the same code has to lay actors out on both markets.
    """
    if len(lanes) == 1:
        return 1.0
    _, lateral = lanes[0].local_coordinates(lanes[-1].position(longitude, 0))
    return 1.0 if lateral > 0 else -1.0


class VRUManager(BaseManager):
    """Spawns pedestrians and cyclists in `reset()`, walks and rides them in `after_step()`."""

    #: After the map (0), the obstacles (9) and the traffic (10). It reads the map alone.
    PRIORITY = 11

    def __init__(self) -> None:
        super().__init__()
        self.pedestrians = 0
        self.cyclists = 0
        self.patrols: dict[str, Patrol] = {}
        self._toward_end: dict[str, bool] = {}
        #: Actors a vehicle has touched, in the order it first happened. For the record.
        self.struck: list[str] = []

    def before_reset(self) -> None:
        super().before_reset()
        config = self.engine.global_config
        self.pedestrians = int(config["pedestrians"])
        self.cyclists = int(config["cyclists"])
        self.patrols = {}
        self._toward_end = {}
        self.struck = []

    def reset(self) -> None:
        """Every draw of the episode: pedestrians first, then cyclists, on the candidate roads."""
        roads = [
            lanes
            for block in self.engine.current_map.blocks[1:]
            for lanes in block.block_network.get_positive_lanes()
            if lanes[0].length > 2 * LANE_MARGIN
        ]
        if not roads:
            return
        for _ in range(self.pedestrians):
            self._spawn_crossing(roads)
        # Every cyclist's draws first, then the stretches: the split of a shared lane depends
        # on how many landed on it, and must not move the draws of the ones after.
        rides = []
        for _ in range(self.cyclists):
            lanes = roads[self.np_random.randint(len(roads))]
            rides.append((lanes, self.np_random.rand(), self.np_random.rand()))
        for lanes, stretch, at, forward in _stretches(rides):
            self._spawn_rider(lanes, stretch, at, forward)

    def after_step(self, *args, **kwargs) -> dict:
        """Aim and push every actor no vehicle is touching. Nothing random."""
        del args, kwargs
        for name, patrol in self.patrols.items():
            actor = self.spawned_objects[name]
            self._place_on_lane(actor, patrol)
            if self._touched_by_a_vehicle(actor):
                if name not in self.struck:
                    self.struck.append(name)
                continue
            if patrol.kind == "pedestrian":
                self._walk(name, actor, patrol)
            else:
                self._ride(name, actor, patrol)
        return {}

    def _place_on_lane(self, actor, patrol: Patrol) -> None:
        """`actor.lane`: the lane of its road it is nearest to, which the traffic reads."""
        start, end, _ = patrol.lane_index
        lanes = self.engine.current_map.road_network.graph[start][end]
        position = actor.position[:2]
        actor.lane = min(lanes, key=lambda lane: abs(lane.local_coordinates(position)[1]))

    def _touched_by_a_vehicle(self, actor) -> bool:
        """Is a vehicle in contact with `actor` right now? Asked of the physics world."""
        world = self.engine.physics_world.dynamic_world
        for contact in world.contactTest(actor.body, True).getContacts():
            for node in (contact.getNode0(), contact.getNode1()):
                if node is not actor.body and node.getName() == MetaDriveType.VEHICLE:
                    return True
        return False

    def layout(self) -> list[str]:
        """Every patrol as a line, sorted. Empty when nothing was placed."""
        return sorted(patrol.line() for patrol in self.patrols.values())

    def layout_digest(self) -> str:
        """`sha256_hex` over `layout()`."""
        return sha256_hex("\n".join(self.layout()))

    # --- placement -----------------------------------------------------------------------

    def _spawn_crossing(self, roads) -> None:
        lanes = roads[self.np_random.randint(len(roads))]
        lane, count, width = lanes[0], len(lanes), lanes[0].width
        longitude = LANE_MARGIN + self.np_random.rand() * (lane.length - 2 * LANE_MARGIN)
        sign = _outward_sign(lanes, longitude)
        # Lane 0's centre is half a lane from the centreline. Out to this road's outer kerb one
        # way; across the centreline and the same number of lanes to the other kerb the other.
        outer = sign * (count * width - width / 2 - KERB_INSET)
        inner = -sign * (count * width + width / 2 - KERB_INSET)
        start = tuple(float(v) for v in lane.position(longitude, outer))
        end = tuple(float(v) for v in lane.position(longitude, inner))
        forward = bool(self.np_random.rand() > 0.5)
        spawn = start if forward else end
        heading = _heading(spawn, end if forward else start)
        actor = self.spawn_object(Pedestrian, position=list(spawn), heading_theta=heading)
        self._keep(actor, Patrol("pedestrian", tuple(lane.index), spawn, start, end, forward))

    def _spawn_rider(self, lanes, longitudes: tuple[float, float], at: float, forward: bool):
        lane = lanes[-1]
        sign = _outward_sign(lanes, lane.length / 2)
        lateral = sign * (lane.width / 2 - CYCLIST_KERB_OFFSET)
        spawn = tuple(float(v) for v in lane.position(at, lateral))
        start = tuple(float(v) for v in lane.position(longitudes[0], lateral))
        end = tuple(float(v) for v in lane.position(longitudes[1], lateral))
        heading = lane.heading_theta_at(at) + (0.0 if forward else math.pi)
        actor = self.spawn_object(Cyclist, position=list(spawn), heading_theta=heading)
        patrol = Patrol(
            "cyclist", tuple(lane.index), spawn, start, end, forward,
            lateral=float(lateral), longitudes=longitudes,
        )
        self._keep(actor, patrol)

    def _keep(self, actor, patrol: Patrol) -> None:
        self.patrols[actor.name] = patrol
        self._toward_end[actor.name] = patrol.forward
        self._place_on_lane(actor, patrol)

    # --- motion --------------------------------------------------------------------------

    def _walk(self, name: str, actor, patrol: Patrol) -> None:
        toward_end = self._toward_end[name]
        target = patrol.end if toward_end else patrol.start
        position = np.asarray(actor.position[:2], dtype=float)
        along = np.subtract(target, patrol.start if toward_end else patrol.end)
        if np.dot(np.subtract(target, position), along) <= 0:
            toward_end = self._toward_end[name] = not toward_end
            target = patrol.end if toward_end else patrol.start
        _push(actor, _heading(position, target), PEDESTRIAN_SPEED)

    def _ride(self, name: str, actor, patrol: Patrol) -> None:
        lane = self.engine.current_map.road_network.get_lane(patrol.lane_index)
        toward_end = self._toward_end[name]
        longitude, _ = lane.local_coordinates(actor.position[:2])
        low, high = patrol.longitudes
        if toward_end and longitude >= high:
            toward_end = self._toward_end[name] = False
        elif not toward_end and longitude <= low:
            toward_end = self._toward_end[name] = True
        ahead = longitude + (LOOK_AHEAD if toward_end else -LOOK_AHEAD)
        aim = lane.position(min(max(ahead, low), high), patrol.lateral)
        _push(actor, _heading(actor.position[:2], aim), CYCLIST_SPEED)


def _stretches(rides):
    """Each ride's stretch of its lane: the whole run, or an equal share of it when shared.

    `rides` are `(lanes, where, which_way)` draws in draw order; the result keeps that order and
    turns each into `(lanes, (low, high), at, forward)`, `at` placed by `where` inside its own
    stretch. Two rides on one lane never overlap.
    """
    sharing: dict[tuple, list[int]] = {}
    for k, (lanes, _, _) in enumerate(rides):
        sharing.setdefault(tuple(lanes[-1].index), []).append(k)
    out = []
    for k, (lanes, where, which_way) in enumerate(rides):
        group = sharing[tuple(lanes[-1].index)]
        low, high = LANE_MARGIN, lanes[-1].length - LANE_MARGIN
        share = (high - low - (len(group) - 1) * SEGMENT_GAP) / len(group)
        start = low + group.index(k) * (share + SEGMENT_GAP)
        stretch = (start, start + share)
        out.append((lanes, stretch, start + where * share, bool(which_way > 0.5)))
    return out


def _heading(here, there) -> float:
    return float(math.atan2(there[1] - here[1], there[0] - here[0]))


def _push(actor, heading: float, speed: float) -> None:
    """Move along `heading` at `speed`, in the world frame; face it once it has turned enough."""
    turned = (heading - actor.heading_theta + math.pi) % (2 * math.pi) - math.pi
    if abs(turned) > HEADING_TOLERANCE:
        actor.set_heading_theta(heading)
        actor.standup()
    upward = actor.body.getLinearVelocity()[2]
    actor.body.setLinearVelocity(
        LVector3(speed * math.cos(heading), speed * math.sin(heading), upward)
    )


__all__ = [
    "CYCLIST_KERB_OFFSET",
    "CYCLIST_SPEED",
    "HEADING_TOLERANCE",
    "KERB_INSET",
    "LANE_MARGIN",
    "LOOK_AHEAD",
    "PEDESTRIAN_SPEED",
    "SEGMENT_GAP",
    "Patrol",
    "VRUManager",
]
