"""`ObstacleManager`: the cones and barriers axes, as two counts on MetaDrive's own manager.

MetaDrive ships the placement already. `TrafficObjectManager` (`manager/object_manager.py`) is
`PRIORITY = 9`, draws every choice from `self.np_random`, publishes `accident_lanes` -- which
`PGTrafficManager` reads (`traffic_manager.py:253`) so traffic never spawns on top of an
obstacle field -- and lays out a cone corridor (`prohibit_scene`) or a barrier (`barrier_scene`)
from lane-frame arithmetic, which is what lets the same maths land on a mirrored road. What it
does not do is separate the two: stock MetaDrive drives both from one `accident_prob`, splits
at `PROHIBIT_SCENE_PROB` between the two scenes, and skips a block with probability
`1 - accident_prob`, so "three cone corridors and no barriers" is inexpressible. This subclass
overrides `reset()` to read two counts instead, `cones` corridors and `barriers` barriers, and
reuses the placement whole.

**Three facts carried in from the survey** (`options.py`, **Scenario options** in the plan):

* Only `Straight`, `Curve`, `InRampOnStraight` and `OutRampOnStraight` blocks take an obstacle
  (`object_manager.py:51-53`). On an `X`, `T` or `O` map there is no eligible block, so both
  counts place **nothing, with no error**. The result's `placed` is what makes that visible.
* The stock barrier branch spawns a broken-down *vehicle* half the time
  (`break_down_scene`). It is deliberately not used here: a run at `traffic=none` must have
  no cars on the road whatever the obstacle axes say, or the axes are not independent and
  Phase 4b cannot calibrate them one at a time.
* A corridor is `CONE_LONGITUDE` x (`2 * lat_num + longitude_num + 1`) metres long -- 24 m at
  a 3.5 m lane -- so its centre is drawn from the part of the lane that fits it, and a lane too
  short for one takes it at its midpoint.

The manager is registered under the stock name, `object_manager`, because that is the attribute
the traffic manager looks for. It imports the simulator at module scope: a manager is a
simulator object, and there is nothing here to run without one.
"""

from __future__ import annotations

from dataclasses import dataclass

from metadrive.component.pgblock.curve import Curve
from metadrive.component.pgblock.ramp import InRampOnStraight, OutRampOnStraight
from metadrive.component.pgblock.straight import Straight
from metadrive.component.road_network import Road
from metadrive.manager.object_manager import TrafficObjectManager

#: The block types an obstacle can land on. `object_manager.py:51-53`, verbatim.
ELIGIBLE_BLOCKS: tuple[type, ...] = (Straight, Curve, InRampOnStraight, OutRampOnStraight)

#: Clearance kept between a corridor's end and the end of its lane, in metres.
CORRIDOR_CLEARANCE = 2.0


@dataclass(frozen=True)
class Scene:
    """One placed obstacle scene, as the manager decided it. Read by the tests."""

    kind: str
    lane_index: tuple[str, str, int]
    longitude: float
    on_left: bool


class ObstacleManager(TrafficObjectManager):
    """`TrafficObjectManager` driven by two counts instead of one probability."""

    def __init__(self) -> None:
        super().__init__()
        self.cones = 0
        self.barriers = 0
        self.scenes: list[Scene] = []

    def before_reset(self) -> None:
        super().before_reset()
        config = self.engine.global_config
        self.cones = int(config["cones"])
        self.barriers = int(config["barriers"])

    def reset(self) -> None:
        """Place `cones` corridors, then `barriers` barriers, on the eligible blocks.

        Corridors first and barriers second, always, so the draw order -- and with it the
        layout at a seed -- does not depend on which axis was set. A map with no eligible block
        places nothing and says nothing; see the module docstring.
        """
        self.accident_lanes = []
        self.scenes = []
        blocks = [
            block for block in self.engine.current_map.blocks if type(block) in ELIGIBLE_BLOCKS
        ]
        if not blocks:
            return
        for _ in range(self.cones):
            self._corridor(blocks)
        for _ in range(self.barriers):
            self._barrier(blocks)

    def corridor_length(self, lane_width: float) -> float:
        """How long a cone corridor on a lane this wide is, in metres. `prohibit_scene`'s maths."""
        lat_num = int(lane_width / self.CONE_LATERAL)
        longitude_num = int(self.ACCIDENT_AREA_LEN / self.CONE_LONGITUDE)
        return (lat_num * 2 + longitude_num + 1) * self.CONE_LONGITUDE

    def _roads(self, block) -> tuple[Road, Road | None]:
        """The block's two candidate roads, the way the stock `reset()` names them."""
        road_1 = Road(block.pre_block_socket.positive_road.end_node, block.road_node(0, 0))
        if isinstance(block, Straight):
            return road_1, None
        road_2 = Road(block.road_node(0, 0), block.road_node(0, 1))
        return road_1, road_2

    def _pick(self, blocks):
        """A block, its road and whether the scene sits on the left: the stock draws, in order."""
        block = blocks[self.np_random.randint(len(blocks))]
        road_1, road_2 = self._roads(block)
        # A curve's corridor goes on its straight half, as stock; anywhere else it is a draw.
        road = road_2 if isinstance(block, Curve) else (road_1, road_2)[self.np_random.randint(2)]
        road = road_1 if road is None else road
        is_ramp = isinstance(block, InRampOnStraight | OutRampOnStraight)
        on_left = bool(self.np_random.rand() > 0.5 or (road is road_2 and is_ramp))
        return road, on_left

    def _corridor(self, blocks) -> None:
        road, on_left = self._pick(blocks)
        lanes = road.get_lanes(self.engine.current_map.road_network)
        lane = lanes[0 if on_left else -1]
        lane_width = self.engine.current_map.config[self.engine.current_map.LANE_WIDTH]
        half = self.corridor_length(lane_width) / 2
        low, high = half + CORRIDOR_CLEARANCE, lane.length - half - CORRIDOR_CLEARANCE
        longitude = low + self.np_random.rand() * (high - low) if high > low else lane.length / 2
        self.prohibit_scene(lane, longitude, lane_width, on_left)
        self.accident_lanes.append(lane)
        self.scenes.append(Scene("cones", tuple(lane.index), float(longitude), on_left))

    def _barrier(self, blocks) -> None:
        road, on_left = self._pick(blocks)
        lanes = road.get_lanes(self.engine.current_map.road_network)
        if len(lanes) == 1:
            index = -1
        else:
            index = self.np_random.randint(0, len(lanes) - 1) if on_left else -1
        lane = lanes[index]
        longitude = self.np_random.rand() * lane.length / 2 + lane.length / 2
        self.barrier_scene(lane, longitude)
        self.accident_lanes.append(lane)
        self.scenes.append(Scene("barriers", tuple(lane.index), float(longitude), on_left))


__all__ = ["CORRIDOR_CLEARANCE", "ELIGIBLE_BLOCKS", "ObstacleManager", "Scene"]
