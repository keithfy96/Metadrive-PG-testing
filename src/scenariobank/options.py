"""The six option axes a bank pins, by name.

**Names only.** A bank stores a *level* -- `traffic=medium` -- and a run resolves that level to a
number the simulator understands. The numbers are Phase 4b's, measured rather than chosen, and they
live in a `LEVELS` table this module grows later; putting them here now would pin a guess into a
file whose whole purpose is to outlive one.

The split matters because the two halves change at different rates and for different reasons. A
level name is written into a manifest and read back months later, so it is schema and moves only
with a version bump. The number behind it is a calibration: it changes whenever the measurement is
redone, and no bank on disk has to be touched when it does.

**What the simulator already provides**, surveyed at the pinned MetaDrive commit `85e5dadc`, because
it decides how much of this is ours to write:

* `traffic` is `PGTrafficManager` -- `traffic_density`, `traffic_mode`, IDM policies, with a
  `< 1e-2` short-circuit at `traffic_manager.py:65-67`.
* `cones` and `barriers` are both `TrafficObjectManager` (`manager/object_manager.py`), driven by
  one knob, `accident_prob`. It splits internally at `PROHIBIT_SCENE_PROB = 0.67` between a cone
  corridor and a barrier/breakdown scene, so **stock MetaDrive offers one knob for two of these
  axes**. They are kept as two names anyway, because the names are schema and splitting them later
  would cost another version bump; Phase 4 subclasses that manager and overrides `reset()` to pick
  `prohibit_scene` (cones) or `barrier_scene` (barriers) per axis, reusing the placement maths
  whole. Two facts to carry into that: `break_down_scene` spawns a *vehicle*, so cones above `none`
  puts cars on the road even at `traffic=none`; and accidents are placed only on `Straight`,
  `Curve`, `InRampOnStraight` and `OutRampOnStraight` blocks, which is why `X`, `T` and `O` roads
  quietly ignore the axis.
* `pedestrians` and `cyclists` are half provided. MetaDrive ships the objects -- `Pedestrian` and
  `Cyclist`, with physics bodies, models and `set_velocity` -- but nothing that decides where they
  walk on a procedurally generated map: `policy/` holds no pedestrian policy, and the only manager
  that spawns them replays a logged trajectory from a recorded dataset, which a PG road does not
  have. Phase 4's `actors.py` is therefore the only placement code in this project that is ours.
* `lights` is Phase 8.
"""

from __future__ import annotations

from typing import Literal

#: The six axes, in the order the studio's form shows them. The order is part of the reading: the
#: three that change what is *on* the road come before the two that change who is *beside* it.
AXES: tuple[str, ...] = ("traffic", "cones", "barriers", "pedestrians", "cyclists", "lights")

#: Every level, weakest first. `none` is the floor rather than an absence, which is what lets
#: "the axis was never set" and "the axis is at zero" be the same state -- the same reasoning as
#: `config.base_config` naming `traffic_density` at `0.0` instead of omitting it.
LEVEL_NAMES: tuple[str, ...] = ("none", "low", "medium", "high")

Level = Literal["none", "low", "medium", "high"]

__all__ = ["AXES", "LEVEL_NAMES", "Level"]
