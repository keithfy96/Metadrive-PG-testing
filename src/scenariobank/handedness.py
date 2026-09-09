"""Left-side traffic: the whole PG map, mirrored about the x-axis.

MetaDrive drives on the **right**. That is not a setting -- there is no `drive_side`,
`traffic_side` or `left_hand` anywhere in the package -- it is baked into the geometry layer,
in the sign of `StraightLane.direction_lateral` and in the `clockwise` flags the block classes
pass to `create_bend_straight`. This bank is for a left-side market, so every map it produces
must be the mirror image of the map MetaDrive would have built.

**The mirror is exact, and that is the point.** Reflecting about `y = 0` is an isometry, so the
mirrored road is the *same road*: same node names, same lane count, same lane lengths, same
curve radii, same block topology -- only the handedness differs. `tests/unit/test_handedness.py`
asserts exactly that, lane by lane, for every block sequence the bank uses. Anything less than
exact would mean the mirror is deforming roads rather than reflecting them, and every route
length, step budget and difficulty claim in the bank would be measuring a different road than
the one MetaDrive validated.

Four sign changes produce it, and no others:

1. **`StraightLane.direction_lateral` is negated.** MetaDrive defines it as `[dy, -dx]`, a -90
   degree rotation, so positive lateral is the vehicle's *right*. Negating it makes positive
   lateral the vehicle's *left*. Every lane in a PG map is placed by a `position(lon, lat)`
   call on the previous lane, so this one flip walks the whole map to the other side of the
   centreline -- including the opposing carriageway, the lane lines, and the sidewalks, because
   they are all derived from lane frames rather than placed independently.

2. **Every `CircularLane` traverses the other way, and keeps its lateral axis.** `clockwise`
   is inverted at construction, which inverts `end_phase` (the arc sweeps the other way),
   `direction`, and therefore `heading_theta_at`. A right-hand bend becomes a left-hand bend.
   This is what makes roundabouts circulate clockwise, which is what a left-side market expects.
   But `direction` also carries the sign of the lateral term in `position` and
   `local_coordinates` (`circular_lane.py:57-61`, `:130`), so inverting the sweep alone hands
   every arc a lateral axis pointing the *other* way from a straight lane's -- the centreline
   mirrors exactly and everything placed off it (lane lines, sidewalks, a vehicle's offset in
   the lane, a navigation checkpoint) lands on the wrong side. So the mirrored class negates
   the lateral in both methods as well, and the axis is the true mirror on arcs as on
   straights. Found 2026-09-08 by driving the same row on both maps and comparing the
   observation entry by entry; the exactness test now samples off the centreline too.

3. **The three `is_clockwise()` sites that do lateral arithmetic are inverted to compensate.**
   `create_pg_block_utils` uses `is_clockwise()` for two different jobs: which way the arc goes
   (correctly inverted by change 2) and which side of an arc the *sibling* lanes of a
   multi-lane road sit on. The second job is lateral, so it has to flip a second time -- the two
   flips cancel and the sibling radii come out where the reflection needs them. Miss this and
   the arcs still mirror, but every multi-lane curve, intersection and roundabout gets its lane
   radii offset the wrong way: the map is *nearly* right, lane counts and node names all match,
   and only the lane lengths give it away. That failure was found by the exactness test, not by
   looking at a picture, which is why the test asserts geometry rather than plausibility.

4. **The IDM policy's lateral term is negated back.** *(Found 2026-09-09, from the first film,
   Phase 4 Step 5b.)* Changes 1 and 2 make a positive lateral coordinate mean *the vehicle's
   left* on every lane, where stock MetaDrive means its right. The expert is fine with that,
   because its observation is mirrored back before the network sees it (Phase 4 Step 4a). The
   traffic is not: `IDMPolicy.steering_control` (`policy/idm_policy.py:294-302`) feeds
   `-lat` to its lateral PID and drives unmirrored steering, so on the mirrored map an offset
   from the centreline was pushed *outward*. The heading loop (gains 1.7 / 3.5) is far stronger
   than the lateral one (0.3 / 0.05), so a car holding its lane heading looked fine -- 0.02 m
   off centre over 300 steps behind an idle ego -- and every car that changed lanes or was
   nudged drifted to the kerb: eight of thirty-five mounted the sidewalk in 146 steps with the
   expert driving, none in the same run on stock MetaDrive. With `lat` negated back, none do,
   and the lane deviation matches stock (2.4 % of vehicle-steps more than a metre off centre,
   against 4.0 % stock and 13 % before). `TrajectoryIDMPolicy` keeps its own copy of the
   method and is not touched: it drives `reactive_traffic` on a `ScenarioEnv`, whose lanes this
   mirror never sees.

Changes 3 and 4 are applied by rewriting the functions from their own source text rather than by
keeping a forked copy of them here. `_mirror_block_utils` asserts on the exact lines it expects
to find and raises `HandednessError` if they are not there. That is deliberate: this package
pins MetaDrive by commit precisely so that its internals cannot move under us, and a patch that
silently stopped applying -- leaving a right-side-traffic bank that still claims to be
left-side -- is the worst failure available here. It fails loudly at import instead.
"""

from __future__ import annotations

import inspect
from typing import Any

#: Which side of the road traffic keeps to. The bank is built entirely on `LEFT`; `RIGHT` is
#: MetaDrive's own convention and is named only so that reports have something to say when the
#: mirror is not installed.
DRIVE_SIDE_LEFT = "left"
DRIVE_SIDE_RIGHT = "right"

#: The exact lines `_mirror_block_utils` rewrites, and what it rewrites them to. Keyed by the
#: number of occurrences expected, so a MetaDrive change that adds a fourth call site is an
#: error rather than a partial patch.
_LATERAL_SITES: tuple[tuple[str, str, int], ...] = (
    (
        "new_lane_clockwise = True if lane.is_clockwise() else False",
        "new_lane_clockwise = False if lane.is_clockwise() else True",
        1,
    ),
    (
        "new_lane_clockwise = False if reference_lane.is_clockwise() else True",
        "new_lane_clockwise = True if reference_lane.is_clockwise() else False",
        2,
    ),
)

#: Every module that imported the patched names directly (`from ... import CreateRoadFrom`) and
#: therefore holds its own reference that has to be rebound.
_BLOCK_MODULES: tuple[str, ...] = (
    "first_block",
    "straight",
    "curve",
    "ramp",
    "roundabout",
    "fork",
    "bottleneck",
    "intersection",
    "std_intersection",
    "t_intersection",
    "std_t_intersection",
    "parking_lot",
    "tollgate",
    "bidirection",
)

_PATCHED_NAMES = ("create_bend_straight", "CreateRoadFrom", "CreateAdverseRoad")

#: Change 4's one line, in `IDMPolicy.steering_control`, and what it becomes.
_IDM_LATERAL_SITE = (
    "steering += self.lateral_pid.get_result(-lat)",
    "steering += self.lateral_pid.get_result(lat)",
)

_installed = False


class HandednessError(RuntimeError):
    """Raised when the mirror cannot be applied to this MetaDrive."""


def drive_side() -> str:
    """Which side of the road this process's maps put traffic on."""
    return DRIVE_SIDE_LEFT if _installed else DRIVE_SIDE_RIGHT


def install() -> None:
    """Mirror MetaDrive's PG geometry layer. Idempotent; must run before any map is built.

    Called from `config.base_config`, which is the one function every env-building path in this
    package goes through. A config function with a side effect is a smell, but the alternative
    is an invariant that holds only when a caller remembers to opt in -- and a scenario built
    right-side-traffic by accident is indistinguishable from a correct one until someone looks
    at a picture.
    """
    global _installed
    if _installed:
        return

    from metadrive.component.lane.circular_lane import CircularLane
    from metadrive.component.lane.straight_lane import StraightLane
    from metadrive.component.pgblock import create_pg_block_utils as utils
    from metadrive.policy import idm_policy

    _mirror_straight_lanes(StraightLane)
    _mirror_circular_lanes(CircularLane)
    patched = _mirror_block_utils(utils)
    _rebind(patched)
    _mirror_idm_lateral(idm_policy)
    _installed = True


def _mirror_straight_lanes(straight_lane: type) -> None:
    """Change 1: positive lateral means the vehicle's left, not its right."""
    import numpy as np

    def relateral(lane: Any) -> None:
        lane.direction_lateral = np.array([-lane.direction[1], lane.direction[0]])

    original_init = straight_lane.__init__
    original_update = straight_lane.update_properties

    def __init__(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        relateral(self)

    def update_properties(self: Any) -> None:
        original_update(self)
        relateral(self)

    straight_lane.__init__ = __init__
    straight_lane.update_properties = update_properties


def _mirror_circular_lanes(circular_lane: type) -> None:
    """Change 2: every arc sweeps the other way, and its lateral axis stays a true mirror.

    The sweep is inverted through `clockwise`. That also inverts `direction`, which the class
    uses as the sign of the lateral term, so `position` and `local_coordinates` are wrapped to
    negate the lateral back -- otherwise `position(lon, +w/2)` on a mirrored arc is the mirror
    of `position(lon, -w/2)` on the original, while on a mirrored straight it is the mirror of
    `position(lon, +w/2)`. `polygon` samples both edges and needs no wrapping.
    """
    original_init = circular_lane.__init__
    original_position = circular_lane.position
    original_local_coordinates = circular_lane.local_coordinates

    def __init__(
        self: Any,
        center: Any,
        radius: float,
        start_phase: float,
        angle: float,
        clockwise: bool = True,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        original_init(self, center, radius, start_phase, angle, not clockwise, *args, **kwargs)

    def position(self: Any, longitudinal: float, lateral: float) -> Any:
        return original_position(self, longitudinal, -lateral)

    def local_coordinates(self: Any, position: Any) -> tuple[float, float]:
        longitudinal, lateral = original_local_coordinates(self, position)
        return longitudinal, -lateral

    circular_lane.__init__ = __init__
    circular_lane.position = position
    circular_lane.local_coordinates = local_coordinates


def _mirror_block_utils(utils: Any) -> dict[str, Any]:
    """Change 3, plus the one perpendicular choice `create_bend_straight` makes by index.

    Returns the patched callables so `_rebind` can push them into the block modules that
    imported them by name.
    """
    source = inspect.getsource(utils)
    for original, mirrored, count in _LATERAL_SITES:
        found = source.count(original)
        if found != count:
            raise HandednessError(
                f"cannot mirror MetaDrive: expected {count} occurrence(s) of\n  {original}\n"
                f"in {utils.__file__}, found {found}. The pinned simulator has moved; "
                f"re-derive the mirror against it before trusting any bank built here."
            )
        source = source.replace(original, mirrored)

    namespace = dict(utils.__dict__)
    exec(compile(source, f"{utils.__file__} [mirrored]", "exec"), namespace)  # noqa: S102

    namespace["create_bend_straight"] = _mirrored_create_bend_straight(namespace)
    patched = {name: namespace[name] for name in _PATCHED_NAMES}
    for name, function in patched.items():
        setattr(utils, name, function)
    return patched


def _mirror_idm_lateral(idm_policy: Any) -> None:
    """Change 4: `IDMPolicy.steering_control` reads the mirrored lateral with the other sign.

    Rewritten from its own source text, the way change 3 is, so a MetaDrive whose controller
    has moved is an error at import rather than traffic that drifts to the kerb.
    """
    import textwrap

    policy = idm_policy.IDMPolicy
    source = textwrap.dedent(inspect.getsource(policy.steering_control))
    original, mirrored = _IDM_LATERAL_SITE
    found = source.count(original)
    if found != 1:
        raise HandednessError(
            f"cannot mirror MetaDrive: expected 1 occurrence of\n  {original}\n"
            f"in IDMPolicy.steering_control ({idm_policy.__file__}), found {found}. The pinned "
            "simulator has moved; re-derive the mirror against it before trusting any bank built "
            "here."
        )
    namespace = dict(idm_policy.__dict__)
    exec(  # noqa: S102
        compile(source.replace(original, mirrored), f"{idm_policy.__file__} [mirrored]", "exec"),
        namespace,
    )
    policy.steering_control = namespace["steering_control"]


def _mirrored_create_bend_straight(namespace: dict[str, Any]) -> Any:
    """The straight lane that follows a bend leaves on the other perpendicular.

    `get_vertical_vector` returns both perpendiculars of the bend's end radius and MetaDrive
    picks one by index off the `clockwise` argument. The arc is now mirrored, so the correct
    index is the other one. Everything else in this function is unchanged from
    `create_pg_block_utils.create_bend_straight` -- the mirrored centre and start phase fall out
    of change 1 on their own, because both are computed from the previous lane's lateral.
    """
    import numpy as np
    from metadrive.component.lane.circular_lane import CircularLane
    from metadrive.component.lane.pg_lane import PGLane
    from metadrive.component.lane.straight_lane import StraightLane
    from metadrive.utils.math import get_vertical_vector

    def create_bend_straight(
        previous_lane: Any,
        following_lane_length: float,
        radius: float,
        angle: float,
        clockwise: bool = True,
        width: float = PGLane.DEFAULT_WIDTH,
        line_types: Any = None,
        forbidden: bool = False,
        speed_limit: float = 20,
        priority: int = 0,
    ) -> tuple[Any, Any]:
        bend_direction = 1 if clockwise else -1
        center = previous_lane.position(previous_lane.length, bend_direction * radius)
        x, y = previous_lane.direction_lateral
        start_phase = np.arctan2(y, x) + (np.pi if clockwise else 0)
        bend = CircularLane(
            center, radius, start_phase, angle, clockwise, width, line_types, forbidden,
            speed_limit, priority
        )
        length = 2 * radius * angle / 2
        bend_end = bend.position(length, 0)
        perpendiculars = get_vertical_vector(bend_end - center)
        # The swapped index. MetaDrive reads `[0] if not clockwise else [1]`.
        next_direction = np.asarray(perpendiculars[1] if not clockwise else perpendiculars[0])
        following_lane = StraightLane(
            bend_end, next_direction * following_lane_length + bend_end, width, line_types,
            forbidden, speed_limit, priority
        )
        return bend, following_lane

    namespace["create_bend_straight"] = create_bend_straight
    return create_bend_straight


def _rebind(patched: dict[str, Any]) -> None:
    """Rebind the patched names inside every block module that imported them directly.

    `from ... import CreateRoadFrom` copies the reference, so replacing the attribute on
    `create_pg_block_utils` alone leaves every block class still calling the unmirrored
    original. This is the step whose absence produces a half-mirrored map.
    """
    import importlib

    for module_name in _BLOCK_MODULES:
        module = importlib.import_module(f"metadrive.component.pgblock.{module_name}")
        for name, function in patched.items():
            if hasattr(module, name):
                setattr(module, name, function)


__all__ = [
    "DRIVE_SIDE_LEFT",
    "DRIVE_SIDE_RIGHT",
    "HandednessError",
    "drive_side",
    "install",
]
