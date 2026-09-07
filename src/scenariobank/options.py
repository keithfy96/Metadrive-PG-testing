"""The six option axes a bank pins, by name -- and the resolver that turns a name into a number.

**Names are schema; numbers are calibration.** A bank stores a *level* -- `traffic=medium` -- and a
run resolves that level to a number the simulator understands. A level name is written into a
manifest and read back months later, so it moves only with a version bump. The number behind it
is a measurement: it changes whenever Phase 4b redoes the calibration, and no bank on disk has to
be touched when it does. `LEVELS` below is the table that measurement will overwrite, and it is
marked provisional for that reason.

**Two shapes of the table are not provisional**, because they are traps rather than calibration:
`traffic none` is exactly `0.0`, and every other traffic numeric is at or above `TRAFFIC_FLOOR` --
`PGTrafficManager.reset` (`traffic_manager.py:65-67`) returns before placing a single vehicle when
`abs(traffic_density) < 1e-2`, so a "low" written below that would run as "none" while every
result claimed otherwise. `resolve_options` refuses a raw value in that gap for the same reason.

**What the simulator already provides**, surveyed at the pinned MetaDrive commit `85e5dadc`, because
it decides how much of this is ours to write:

* `traffic` is `PGTrafficManager` -- `traffic_density`, `traffic_mode`, IDM policies, with the
  `< 1e-2` short-circuit above.
* `cones` and `barriers` are both `TrafficObjectManager` (`manager/object_manager.py`), driven by
  one knob, `accident_prob`. It splits internally at `PROHIBIT_SCENE_PROB = 0.67` between a cone
  corridor and a barrier/breakdown scene, so **stock MetaDrive offers one knob for two of these
  axes**. They are kept as two names anyway, because the names are schema and splitting them later
  would cost another version bump; Phase 4 Step 4b subclasses that manager and overrides `reset()`
  to pick `prohibit_scene` (cones) or `barrier_scene` (barriers) per axis, reusing the placement
  maths whole. Two facts to carry into that: `break_down_scene` spawns a *vehicle*, so cones above
  `none` puts cars on the road even at `traffic=none`; and accidents are placed only on `Straight`,
  `Curve`, `InRampOnStraight` and `OutRampOnStraight` blocks, which is why `X`, `T` and `O` roads
  quietly ignore the axis.
* `pedestrians` and `cyclists` are half provided. MetaDrive ships the objects -- `Pedestrian` and
  `Cyclist`, with physics bodies, models and `set_velocity` -- but nothing that decides where they
  walk on a procedurally generated map: `policy/` holds no pedestrian policy, and the only manager
  that spawns them replays a logged trajectory from a recorded dataset, which a PG road does not
  have. Phase 4's `actors.py` is therefore the only placement code in this project that is ours.
* `lights` is Phase 8, and until it lands `resolve_options` refuses the axis above `none` by name
  rather than letting a level through to an env that has nothing to spend it on.

**The six axes are procedural-only.** On a recorded bank (Phase 3) traffic, lights and the rest
are contents of the recording, and `ScenarioEnv` offers three replay switches instead. The
resolver says which kind it resolved for, so nothing downstream has to infer it from the shape of
the record.

Nothing here imports MetaDrive, and nothing here imports `bank` at module scope -- `bank` imports
this module for the names, so the manifest types below are annotations only.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from scenariobank.bank import (
        CategoryEntry,
        Manifest,
        RealWorldEntry,
        RealWorldRow,
        ScenarioRow,
    )

#: The six axes, in the order the studio's form shows them. The order is part of the reading: the
#: three that change what is *on* the road come before the two that change who is *beside* it.
AXES: tuple[str, ...] = ("traffic", "cones", "barriers", "pedestrians", "cyclists", "lights")

#: Every level, weakest first. `none` is the floor rather than an absence, which is what lets
#: "the axis was never set" and "the axis is at zero" be the same state -- the same reasoning as
#: `config.base_config` naming `traffic_density` at `0.0` instead of omitting it.
LEVEL_NAMES: tuple[str, ...] = ("none", "low", "medium", "high")

Level = Literal["none", "low", "medium", "high"]

#: The five axes with a plain number behind each name, and so the five a raw value can be given
#: for. `lights` is a schedule, not a number, and has no raw form.
NUMERIC_AXES: tuple[str, ...] = ("traffic", "cones", "barriers", "pedestrians", "cyclists")

#: Below this, `PGTrafficManager.reset` places nothing (`traffic_manager.py:65-67`). Named so the
#: table check and the raw-value refusal cannot disagree about where the trap is.
TRAFFIC_FLOOR = 0.01

#: The number behind each level. **PROVISIONAL -- Phase 4b measures these.** Everything in this
#: table except the two shapes named in the module docstring is a placeholder waiting for the
#: sweep that replaces it, which is why nothing else in this module reads a value out of it by
#: number: the resolver looks names up, and the tests pin shapes rather than figures.
#:
#: `traffic` is `traffic_density`; the four counts are how many of that thing `obstacles.py` and
#: `actors.py` (Phase 4 Step 4b) place along the ego's route. `lights` is the phase schedule
#: Phase 8's `PGTrafficLightManager` will read -- kept in the shape that plan gives it so the
#: record's field does not change shape when the axis arrives.
LEVELS: dict[str, dict[str, Any]] = {
    "traffic": {"none": 0.0, "low": 0.05, "medium": 0.15, "high": 0.35},
    "cones": {"none": 0, "low": 1, "medium": 3, "high": 6},
    "barriers": {"none": 0, "low": 1, "medium": 2, "high": 4},
    "pedestrians": {"none": 0, "low": 1, "medium": 3, "high": 6},
    "cyclists": {"none": 0, "low": 1, "medium": 2, "high": 4},
    "lights": {
        "none": None,
        "low": {"cycle": 60, "green": 40},
        "medium": {"cycle": 40, "green": 20},
        "high": {"cycle": 30, "green": 10},
    },
}

#: Convenience aliases. **The six axes are the contract; a tier is a spelling of six names** and
#: is expanded before the run, so a result never says "hard" without also saying what hard was
#: on the day. Every tier carries `lights=none` until Phase 8 lands; `PHASE_8_LIGHTS` records
#: what that phase flips them to, so the intent is in the file rather than in a commit message.
TIERS: dict[str, dict[str, Level]] = {
    "easy": {
        "traffic": "low",
        "cones": "none",
        "barriers": "none",
        "pedestrians": "none",
        "cyclists": "none",
        "lights": "none",
    },
    "medium": {
        "traffic": "medium",
        "cones": "low",
        "barriers": "low",
        "pedestrians": "low",
        "cyclists": "none",
        "lights": "none",
    },
    "hard": {
        "traffic": "high",
        "cones": "medium",
        "barriers": "medium",
        "pedestrians": "medium",
        "cyclists": "low",
        "lights": "none",
    },
}

#: The `lights` level each tier takes once Phase 8 lands. `easy` stays at `none`.
PHASE_8_LIGHTS: dict[str, Level] = {"medium": "low", "hard": "medium"}

#: What a recorded bank runs with instead of the six axes: `ScenarioEnv`'s three replay switches,
#: all off, so the recording plays back as recorded. `replay.replay_config` spreads this into the
#: env config and the resolver copies it into the record, so the two cannot drift apart.
REPLAY_FLAGS: dict[str, bool] = {"no_traffic": False, "no_light": False, "reactive_traffic": False}

Kind = Literal["pg", "recorded"]

#: Where each resolved level came from, so an override is visible in the result without the
#: manifest beside it. `raw` is a number given directly; its level name is the nearest one.
Origin = Literal["manifest", "tier", "flag", "raw"]


class OptionError(ValueError):
    """A level, axis, tier or raw value that cannot be resolved. Always says which and why."""


class ResolvedOptions(BaseModel):
    """The options one run actually uses: names *and* numbers, and where each came from.

    This is the block a result record carries under `options`, which is why it is a model with
    `extra="forbid"` rather than a dict: Phase 5 reads it back. On a procedural bank `levels`,
    `values` and `origin` each hold the six axes and `replay` is empty; on a recorded bank the
    three are empty and `replay` holds the switches. `kind` says which, so a reader does not have
    to infer it from which dicts are empty.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Kind
    #: The tier that was expanded, if one was named. Recorded for the reader; nothing resolves
    #: from it after expansion.
    tier: str | None = None
    #: Axis -> level name. On a raw value, the nearest name.
    levels: dict[str, Level] = Field(default_factory=dict)
    #: Axis -> the number (or, for lights, the schedule) the env is given. On a raw value, the
    #: raw number itself rather than the level's.
    values: dict[str, Any] = Field(default_factory=dict)
    #: Axis -> where its level came from.
    origin: dict[str, Origin] = Field(default_factory=dict)
    #: The raw numbers that were given, by axis. Empty when every axis came in by name.
    raw: dict[str, float] = Field(default_factory=dict)
    #: `REPLAY_FLAGS`, on a recorded bank. Empty on a procedural one.
    replay: dict[str, bool] = Field(default_factory=dict)


def nearest_level(axis: str, value: float) -> Level:
    """The level name whose number is closest to `value`. A tie goes to the weaker level.

    Weaker on a tie because a raw value is somebody choosing to sit between two calibrated points,
    and calling that the harder of the two would report a run as more demanding than it was.
    """
    if axis not in NUMERIC_AXES:
        raise OptionError(
            f"{axis!r} has no number behind its levels, so a raw value cannot be placed on it. "
            f"The axes that do: {', '.join(NUMERIC_AXES)}."
        )
    table = LEVELS[axis]
    # Distances are rounded so a value sitting exactly between two levels reads as a tie rather
    # than as whichever side floating point lands on (0.15 - 0.10 is a hair under 0.05).
    return min(  # type: ignore[return-value]
        LEVEL_NAMES,
        key=lambda name: (round(abs(table[name] - value), 9), LEVEL_NAMES.index(name)),
    )


def _check_axis(axis: str) -> None:
    if axis not in AXES:
        raise OptionError(f"{axis!r} is not an option axis. The six are: {', '.join(AXES)}.")


def _check_level(axis: str, level: str) -> None:
    if level not in LEVEL_NAMES:
        raise OptionError(
            f"{level!r} is not a level for {axis}. The four are: {', '.join(LEVEL_NAMES)}."
        )


def _check_raw(axis: str, value: float) -> None:
    if axis not in NUMERIC_AXES:
        raise OptionError(
            f"{axis!r} has no number behind its levels, so a raw value cannot be placed on it. "
            f"The axes that do: {', '.join(NUMERIC_AXES)}."
        )
    if value < 0:
        raise OptionError(f"{axis}={value} is negative; the floor of every axis is 0.")
    if axis == "traffic" and 0 < value < TRAFFIC_FLOOR:
        raise OptionError(
            f"traffic={value} is below {TRAFFIC_FLOOR}, and `PGTrafficManager.reset` "
            "(traffic_manager.py:65-67) places nothing below that: it would run as `none` while "
            "the result said otherwise. Give 0 to mean none, or a density at or above the floor."
        )
    if axis != "traffic" and value != int(value):
        raise OptionError(f"{axis}={value} is not a whole number, and {axis} is a count.")


def _refuse_lights(level: str, origin: str, where: str) -> None:
    if level != "none":
        raise OptionError(
            f"the Lights axis is Phase 8: lights={level} ({where}) has nothing to run on yet. "
            + (
                "Unpin it with `scenariobank options --bank <bank> --lights none`."
                if origin == "manifest"
                else "Leave lights at none until then."
            )
        )


def resolve_options(
    manifest: Manifest,
    *,
    tier: str | None = None,
    levels: Mapping[str, str] | None = None,
    raw: Mapping[str, float] | None = None,
) -> ResolvedOptions:
    """Names in, numerics out, for one run of one bank.

    Precedence, weakest first: the manifest's pinned block, then the tier, then an explicit level
    or raw value per axis. A tier is expanded to its six names before anything else is applied,
    so `tier="hard", levels={"traffic": "low"}` is hard everywhere except traffic. The same axis
    given both as a level and as a raw value is refused rather than ordered: two answers for one
    knob is a mistake, not a precedence question.

    On a recorded bank there are no axes to resolve. The record says `kind="recorded"` and carries
    `REPLAY_FLAGS`; any tier, level or raw value is refused, because a recording's traffic is not
    a knob that was left at `none` -- it is not a knob at all.

    Every refusal is an `OptionError` that names the axis, the level, the tier or the number, so
    the caller can print it and stop.
    """
    levels = dict(levels or {})
    raw = dict(raw or {})

    if manifest.source != "pg":
        asked = [name for name, given in (("tier", tier), ("level", levels), ("raw", raw)) if given]
        if asked:
            raise OptionError(
                f"a {manifest.source!r} bank has no option axes: traffic, pedestrians and the "
                f"rest are contents of the recording, not knobs a run sets ({', '.join(asked)} "
                "given). See `docs/reference/importing.md`."
            )
        return ResolvedOptions(kind="recorded", replay=dict(REPLAY_FLAGS))

    if tier is not None and tier not in TIERS:
        raise OptionError(f"{tier!r} is not a tier. The three are: {', '.join(TIERS)}.")
    for axis, level in levels.items():
        _check_axis(axis)
        _check_level(axis, level)
    for axis, value in raw.items():
        _check_axis(axis)
        _check_raw(axis, value)
    twice = sorted(set(levels) & set(raw))
    if twice:
        raise OptionError(
            f"{', '.join(twice)} given both as a level and as a raw value; one or the other."
        )

    pinned = manifest.options.model_dump()
    chosen: dict[str, Level] = {}
    origin: dict[str, Origin] = {}
    values: dict[str, Any] = {}
    for axis in AXES:
        level, came_from = pinned[axis], "manifest"
        if tier is not None:
            level, came_from = TIERS[tier][axis], "tier"
        if axis in levels:
            level, came_from = levels[axis], "flag"
        if axis in raw:
            level, came_from = nearest_level(axis, raw[axis]), "raw"
        if axis == "lights":
            _refuse_lights(level, came_from, f"from the {came_from}")
        chosen[axis] = level  # type: ignore[assignment]
        origin[axis] = came_from  # type: ignore[assignment]
        values[axis] = raw[axis] if axis in raw else LEVELS[axis][level]

    return ResolvedOptions(
        kind="pg",
        tier=tier,
        levels=chosen,
        values=values,
        origin=origin,
        raw={axis: float(value) for axis, value in raw.items()},
    )


def options_for(
    resolved: ResolvedOptions,
    entry: CategoryEntry | RealWorldEntry,
    row: ScenarioRow | RealWorldRow,
) -> ResolvedOptions:
    """The options one scenario runs with: the bank's, unchanged.

    Per-category and per-scenario overrides are deliberately not built -- that is how a bank
    quietly becomes the cross-product the plan says can never be collapsed back. This is the seam
    they would go through if that decision is ever reversed, so the runner calls it per row from
    the start and adding an override later changes no call site. `entry` and `row` are unused
    today for exactly that reason.
    """
    del entry, row
    return resolved


__all__ = [
    "AXES",
    "LEVELS",
    "LEVEL_NAMES",
    "NUMERIC_AXES",
    "PHASE_8_LIGHTS",
    "REPLAY_FLAGS",
    "TIERS",
    "TRAFFIC_FLOOR",
    "Kind",
    "Level",
    "OptionError",
    "Origin",
    "ResolvedOptions",
    "nearest_level",
    "options_for",
    "resolve_options",
]
