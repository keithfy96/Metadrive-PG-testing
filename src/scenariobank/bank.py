"""Generating a bank: the scenarios on disk, and the manifest a runner reads.

A bank is a **disposable, per-batch artifact**. It is regenerated whenever someone wants
scenarios, and nothing here checks that a road matches a previous run's -- if it comes out
different, that is a different batch. The container pinning one MetaDrive commit is what makes a
single batch self-consistent, and that is the whole guarantee. So there is no `map_id`, no
`config_hash`, and no gate on the recorded commit: the simulator block below is *information*
that explains a result months later, not something anything refuses on.

What the manifest is for is the runner. It carries the destination node and the spawn lane, so
the runner can pin `vehicle_config["destination"]` and never let `auto_assign_task` draw a
random one, and the rotation along the route, so the bank itself says which way a scenario
turns.

**A thumbnail shows the route, not just the map** (`figures.render_route`): the road in grey,
the driven route in red, a blue arrow at the spawn and a green star at the destination. A
map-only picture was the first thing built here and it could not answer the one question anyone
asks of it -- which way does this go -- because nothing in it said which end the ego starts at.

**Generation is on the critical path.** With no durable bank it runs before every batch, and
reset cost is superlinear in block count (~x1.6 per added block). So one env is built per *block
sequence* and reset once per seed -- not one env per scenario -- and the three `X` categories
share a reset, being the same junction driven three ways.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from scenariobank.categories import (
    CATEGORIES,
    SEEDS,
    Category,
    CategoryError,
    ExitRule,
    composed_name,
    get_category,
    step_budget,
    validate_block_seq,
)
from scenariobank.config import base_config
from scenariobank.handedness import DRIVE_SIDE_LEFT
from scenariobank.options import AXES, LEVEL_NAMES, Level
from scenariobank.sockets import (
    SocketError,
    read_sockets_from_env,
    route_rotation,
    select_exit,
    turn_pairs,
)

#: Bumped when a reader would break. The runner validates against it rather than duck-typing.
SCHEMA_VERSION = "1.2"

#: Every version this build can read. 1.1 added the three optional per-scenario overrides below,
#: so a 1.0 manifest is a 1.1 one that overrides nothing and the banks already on disk keep
#: opening; 1.2 added `options`, so a 1.1 manifest is a 1.2 one that pins nothing. The reverse
#: does not hold -- `extra="forbid"` means a 1.0 reader refuses a row that declares its own
#: budget, and a 1.1 reader refuses a manifest that declares option levels -- which is why the
#: number moved rather than the fields being slipped in quietly under the old one.
READABLE_VERSIONS = ("1.0", "1.1", "1.2")

#: Where thumbnails go, relative to the bank root. Stored in the manifest as a relative path so
#: a bank directory can be moved or mounted anywhere.
THUMBNAIL_DIR = "thumbs"

MANIFEST_NAME = "manifest.json"

#: Keys `base_config()` carries that generation overrides per block sequence. Recorded per
#: category (`block_seq`) and per scenario (`seed`) instead, so leaving them in the stored
#: config would put two different answers in the same file.
_PER_RUN_KEYS = ("map", "start_seed", "num_scenarios")


class BankError(RuntimeError):
    """Raised when a bank cannot be generated -- a seed that will not build, a wrong drive side."""


class ScenarioNotFound(BankError):
    """No scenario by that id in this bank.

    A `BankError`, so every existing handler still catches it, and its own type so a caller that
    answers over HTTP can tell "this bank does not hold that" (a 404) from "that is not a thing
    you may ask for" (a 400) without reading the sentence.
    """


class ScenarioRow(BaseModel):
    """One scenario: everything the runner needs to rebuild it and nothing else."""

    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    seed: int
    #: The node the route ends at, resolved from the category's rule at generation time. **Per
    #: scenario, not per category**: `StdTInterSection` exposes a different arm depending on the
    #: seed, so `t_junction` resolves to `1T0_1_` on seeds 0, 1 and 4 and `1T2_1_` on 2 and 3.
    destination: str
    #: The lane `random_spawn_lane_index` drew. Recorded because for the `X` categories it is the
    #: only thing separating the five seeds -- the road is identical at all five.
    spawn_lane_index: int
    #: `navigation.total_length` with the destination pinned. Measured on a reference lane, so it
    #: does not vary with `spawn_lane_index`.
    route_length_m: float
    #: Total rotation **along the driven route**, unwrapped, in degrees. Positive is left.
    #: Deliberately not `SocketReading.angle_deg`, which is the `wrap_to_pi`'d *final heading*:
    #: right for choosing an exit, wrong for describing one. A `curve` seed that sweeps +239.5
    #: reads -120.5 that way -- half the rotation, opposite direction.
    net_rotation_deg: float
    #: One character per `Curve` block on the route, `L` or `R`, in order. `""` for a sequence
    #: with no curves. This is what says a `CC` scenario is left-then-right rather than just
    #: "net +81 degrees", which a left-right pair and a gentle single left both produce.
    turn_pairs: str
    thumbnail: str | None

    # The three overrides below are what makes this schema 1.1, and all three are `None` on a row
    # that follows its category. They are *declared intent* for one scenario, the way the entry's
    # fields are the declared intent for the type -- an overridden row keeps its category name,
    # because a bank that silently reclassified a scenario would be the manifest failing to
    # explain itself.

    #: This row's own exit rule, or `None` to follow the category's. `destination` above is still
    #: the fact it resolved to.
    exit_rule: str | None = None
    #: An exact exit, pinned instead of resolved from a rule. Only meaningful at *this row's*
    #: seed: `StdTInterSection` offers a different arm on seeds 2 and 3, so a node pinned at one
    #: seed may not exist at another, and a rebuild at a new seed drops the pin rather than
    #: failing on a node nobody typed.
    exit_node: str | None = None
    #: This row's own step cap, or `None` to follow the category's. The only field of a scenario
    #: that is declared rather than measured, and so the only one an edit can change without
    #: building a road again.
    max_steps: int | None = None


class CategoryEntry(BaseModel):
    """One category's scenarios, and the fixed facts they share."""

    model_config = ConfigDict(extra="forbid")

    description: str
    block_seq: str
    #: The rule the destinations were resolved *from*. The resolved node is per scenario; this is
    #: the declared intent, kept so a manifest explains itself.
    exit_rule: str
    max_steps: int
    scenarios: list[ScenarioRow]

    def rule_for(self, row: ScenarioRow) -> str:
        """The exit rule that applies to one row: its own if it declares one, else this one.

        Resolved here rather than wherever a row is read, so the review, the studio's panel and a
        rebuild cannot come to different conclusions about which rule a scenario was built on.
        """
        return row.exit_rule or self.exit_rule

    def budget_for(self, row: ScenarioRow) -> int:
        """The step cap that applies to one row: its own if it declares one, else this one."""
        return self.max_steps if row.max_steps is None else row.max_steps


class OptionLevels(BaseModel):
    """The six option axes at their declared levels. All `none` is a bank that pins nothing.

    **Declared run intent, not generation truth.** `base_config` records what generation actually
    used -- `traffic_density: 0.0`, `accident_prob: 0.0`, and they stay there at zero -- because a
    thumbnail and a route are what generation produced and no option changes either: the map
    renderer draws no objects, and object placement runs at a lower priority than the map. This
    block records what *runs* of this bank should use, which is why setting it is a manifest write
    and not a rebuild. Pinning options at generation time would mean regenerating a bank to change
    a traffic level, which is the cost this exists to avoid.

    **A default, not a lock.** A run flag overrides what is pinned here, and the result records the
    expanded options, so an override is visible in the artifact afterwards. Phase 4b's calibration
    sweeps one axis across one bank, which a manifest that refused overrides would make impossible.
    """

    model_config = ConfigDict(extra="forbid")

    traffic: Level = "none"
    cones: Level = "none"
    barriers: Level = "none"
    pedestrians: Level = "none"
    cyclists: Level = "none"
    lights: Level = "none"


class SimulatorInfo(BaseModel):
    """Which MetaDrive built this bank. Information only -- nothing refuses on it."""

    model_config = ConfigDict(extra="forbid")

    edition: str | None
    dist_version: str | None
    commit: str | None
    asset_version: str | None


class Manifest(BaseModel):
    """The bank, as a file. Written last and atomically, so a partial bank has no manifest."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0", "1.1", "1.2"]
    bank_id: str
    created_utc: str
    metadrive: SimulatorInfo
    #: `base_config()` in full, minus the keys generation overrides per block sequence, so a run
    #: is self-describing. Types are stored as dotted paths: this is a record of the config, not
    #: a config that can be loaded back.
    base_config: dict[str, Any]
    #: Recorded so the drive side is a stated fact in the file rather than an assumption. It is
    #: also *measured* from every map during generation; a bank that says `left` was checked.
    drive_side: str
    #: The option levels runs of this bank default to. Absent from a 1.0 or 1.1 manifest, which
    #: reads as every axis at `none` -- the same state as a bank that was asked and pinned nothing.
    options: OptionLevels = OptionLevels()
    categories: dict[str, CategoryEntry]


def describe_config(config: dict[str, Any]) -> dict[str, Any]:
    """Render a MetaDrive config as JSON, turning classes into dotted paths.

    `base_config` pins `agent_observation` to a *class*, which is the point of it -- it is what
    keeps the observation a bare `Box(19,)`. So the manifest records the name rather than
    dropping the key, and a reader can see which observation the bank was built against.
    """
    if isinstance(config, dict):
        return {key: describe_config(value) for key, value in config.items()}
    if isinstance(config, type):
        return f"{config.__module__}.{config.__qualname__}"
    if isinstance(config, (list, tuple)):
        return [describe_config(item) for item in config]
    if config is None or isinstance(config, (str, int, float, bool)):
        return config
    return repr(config)


def scenario_id(category_name: str, index: int) -> str:
    """The stable public key of a scenario.

    `index` is the position within the category, not the seed. They coincide at the fixed
    seeds 0-4 and stop coinciding the moment a seed list is not `range(len(seeds))`.
    """
    return f"{category_name}_{index:04d}"


def num_scenarios_for(seeds: Sequence[int]) -> int:
    """Size an env's seed *range* for `seeds`.

    `num_scenarios` reads as a count and is not one. `base_env.py:926` asserts
    `start_index <= seed < start_index + num_scenarios`, so it bounds an **index**: sizing it
    `len(seeds)` works only while the seeds are contiguous from `start_seed`, and raises
    `scenario_index (seed) should be in [0:N)` the moment they are not. Harmless for the fixed
    0-4, fatal for a user-defined seed list.
    """
    return max(seeds) - min(seeds) + 1


def _simulator_info() -> SimulatorInfo:
    """Read the simulator back out of the installed distribution. No env build.

    Called *after* generation, never before: MetaDrive downloads its asset tree lazily on the
    first engine start, so on a clean machine `asset_version()` has nothing to read until a map
    has been built.
    """
    from scenariobank.doctor import collect

    report = collect(probe=False)
    return SimulatorInfo(
        edition=report.edition,
        dist_version=report.dist_version,
        commit=report.commit,
        asset_version=report.asset_version,
    )


def _draw_thumbnail(
    out_dir: Path,
    name: str,
    env,
    category: Category,
    seed: int,
    exit_socket,
    net_rotation: float,
    intent: str | None = None,
) -> str:
    """Draw one scenario's route and return the PNG's path relative to the bank root.

    Drawn **per scenario, after the route is pinned** -- not per map. The first version of this
    rendered the map alone, which meant the three `X` categories at one seed produced three
    byte-identical images, and a `curve` map gave no way to tell a right turn from a left one:
    read from the wrong end of the road they look the same. `tests/unit/test_bank.py` asserts
    two categories on one road no longer collide.

    Zoomed to fit its own extent, so a curve and a roundabout both fill their frame despite
    being very different sizes. For recognising a scenario, not for comparing two.
    """
    from scenariobank.figures import render_route

    relative = f"{THUMBNAIL_DIR}/{name}.png"
    render_route(
        env,
        category=category,
        seed=seed,
        exit_socket=exit_socket,
        out_path=out_dir / relative,
        net_rotation=net_rotation,
        intent=intent,
    )
    return relative


def _reset(env, seed: int, block_seq: str):
    """Reset onto `seed`, turning a layout failure into a message that names what failed.

    Map generation is a backtracking search (`BIG.py:91-103`), so a block sequence can simply
    fail to plug in at a given seed. With the seeds fixed that is a hard failure and not a scan:
    it is reported, never silently substituted.
    """
    try:
        env.reset(seed=seed)
    except Exception as error:
        raise BankError(
            f"seed {seed} does not build for block sequence {block_seq!r}: {error}. "
            "Map layout is a backtracking search and can fail for a specific seed; the seed is "
            "not substituted, because a bank whose seeds moved silently is not the bank asked for."
        ) from error


def _assert_drive_side(env, block_seq: str, seed: int) -> None:
    """Fail unless the map the env just built is left-side traffic.

    The one check kept from the deleted `verify`, and it is not a reproducibility check. If
    `handedness.install` fails to take, every map builds right-side while every manifest field
    stays correct and every thumbnail still looks like a road -- and the only symptom is a
    right-hand-drive model failing everything for reasons no result explains.

    Measured from the map, never read off `handedness._installed`: a flag that says "mirrored"
    while the maps come out right-side is exactly the failure being guarded against.
    """
    from scenariobank.doctor import measure_drive_side

    side = measure_drive_side(env)
    if side != DRIVE_SIDE_LEFT:
        found = side or "a one-way spawn road, so the drive side is unreadable"
        raise BankError(
            f"{block_seq!r} seed {seed} builds {found}, not {DRIVE_SIDE_LEFT}-side traffic. "
            "`handedness.install` did not take; every scenario in this bank would be mirrored "
            "the wrong way."
        )


def generate(
    out_dir: Path,
    *,
    bank_id: str,
    category_names: Sequence[str] = (),
    seeds: Sequence[int] = SEEDS,
    category_seeds: Mapping[str, Sequence[int]] | None = None,
    thumbnails: bool = True,
    progress: Callable[[str], None] | None = None,
) -> Manifest:
    """Build every category at its seeds and write the bank to `out_dir`.

    One env per block sequence, one reset per seed. The destination is pinned **after** the
    reset with `navigation.set_route` rather than through `vehicle_config["destination"]` at
    construction, which is what lets the three `X` categories share a single reset instead of
    paying for three.

    `seeds` is the default for every category; `category_seeds` overrides it by name. A category
    with its own seeds no longer shares a reset with the others on its road, which is why the
    grouping below is by `(block_seq, seeds)` rather than by `block_seq` alone.
    """
    from metadrive.envs.metadrive_env import MetaDriveEnv

    selected = _select(category_names)
    per_category = _resolve_seeds(selected, seeds, category_seeds)
    say = progress or (lambda _message: None)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if thumbnails:
        (out_dir / THUMBNAIL_DIR).mkdir(exist_ok=True)

    rows: dict[str, list[ScenarioRow]] = {name: [] for name in selected}
    total = sum(len(per_category[name]) for name in selected)
    done = 0

    for (block_seq, group_seeds), categories in _by_block_seq(selected, per_category).items():
        env = MetaDriveEnv(
            base_config(
                map=block_seq,
                start_seed=min(group_seeds),
                num_scenarios=num_scenarios_for(group_seeds),
            )
        )
        try:
            for seed in group_seeds:
                _reset(env, seed, block_seq)
                _assert_drive_side(env, block_seq, seed)
                readings = read_sockets_from_env(env)
                spawn_lane = int(env.agent.lane_index[2])

                for category in categories:
                    name = scenario_id(category.name, len(rows[category.name]))
                    row, chosen = _measure(env, category, seed, readings, spawn_lane, name)
                    if thumbnails:
                        # After `_measure`, so the env's navigation holds *this* category's
                        # route. Drawing before the loop is what made the three `X` categories
                        # share one picture.
                        row = row.model_copy(
                            update={
                                "thumbnail": _draw_thumbnail(
                                    out_dir, name, env, category, seed, chosen,
                                    row.net_rotation_deg,
                                )
                            }
                        )
                    rows[category.name].append(row)
                    done += 1
                    say(
                        f"[{done:>{len(str(total))}}/{total}] {row.scenario_id}  seed {seed}  "
                        f"-> {row.destination}  lane {row.spawn_lane_index}  "
                        f"{row.route_length_m:.1f} m"
                    )
        finally:
            env.close()

    manifest = Manifest(
        schema_version=SCHEMA_VERSION,
        bank_id=bank_id,
        created_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        metadrive=_simulator_info(),
        base_config=_stored_config(),
        drive_side=DRIVE_SIDE_LEFT,
        # A new bank pins nothing. Generation stays option-free on purpose: no flag here sets an
        # axis, so what `base_config` records is what was actually built, and the levels are set
        # afterwards by an edit that constructs no environment.
        options=OptionLevels(),
        categories={
            name: CategoryEntry(
                description=category.description,
                block_seq=category.block_seq,
                exit_rule=category.exit_rule.value,
                max_steps=category.max_steps,
                scenarios=rows[name],
            )
            for name, category in selected.items()
        },
    )
    write_manifest(out_dir, manifest)
    return manifest


def replace_scenario(
    bank_dir: Path,
    scenario_id_: str,
    seed: int | None = None,
    *,
    exit_rule: str | None = None,
    destination: str | None = None,
    inherit_exit: bool = False,
    thumbnails: bool = True,
    progress: Callable[[str], None] | None = None,
) -> ScenarioRow:
    """Rebuild one scenario of an existing bank at a different seed, in place.

    The correction loop. A bank is generated, someone looks at it, and one scenario turns out to
    be a poor draw -- `curve` seed 4 builds a road 7% from seed 0's, near enough that the two
    thumbnails are the same picture. This swaps that one row rather than regenerating 55.

    **The bank never changes size and never renumbers.** `scenario_id` and the row's position are
    kept; only the seed and what was measured from it change.

    With `thumbnails=False` the old image is **deleted** and the row records no thumbnail. It
    cannot be kept: it is a picture of the seed that was just replaced, and a manifest pointing at
    it would be wrong rather than merely incomplete.

    `block_seq` and `exit_rule` come from the **manifest's own category entry**, not from
    `categories.py`. The bank is self-describing, and one generated before a code change should
    still be correctable afterwards.

    Omit `seed` to rebuild at the one the row already has, which is what changing *where it drives
    to* means: `exit_rule` gives this row its own rule, `destination` pins an exact exit, and
    `inherit_exit` puts it back on the category's. Those are schema 1.1's per-scenario overrides,
    and they are recorded on the row rather than applied and forgotten -- a rebuild months later
    has to resolve the same way, and "declare the intent, store the fact" is how every other field
    here already works.
    """
    from metadrive.envs.metadrive_env import MetaDriveEnv

    bank_dir = Path(bank_dir)
    manifest = read_manifest(bank_dir)
    say = progress or (lambda _message: None)

    name, entry, index = _locate(manifest, scenario_id_)
    old = entry.scenarios[index]
    # A rebuild that names no seed is a rebuild at this row's own seed. That is what an edit to
    # the exit is: same draw, different destination.
    seed = old.seed if seed is None else seed
    taken = {row.seed for i, row in enumerate(entry.scenarios) if i != index}
    if seed in taken:
        raise BankError(
            f"seed {seed} is already used by {name}: each seed builds one scenario. "
            f"{name} currently holds seeds {sorted(row.seed for row in entry.scenarios)}."
        )

    over_rule, over_node = _exit_intent(
        entry,
        old,
        seed,
        exit_rule=exit_rule,
        destination=destination,
        inherit=inherit_exit,
        say=say,
    )
    category = _category_for(name, entry, rule=over_rule, max_steps=entry.budget_for(old))
    env = MetaDriveEnv(
        base_config(map=entry.block_seq, start_seed=seed, num_scenarios=1)
    )
    try:
        _reset(env, seed, entry.block_seq)
        _assert_drive_side(env, entry.block_seq, seed)
        row, chosen = _measure(
            env, category, seed, read_sockets_from_env(env), int(env.agent.lane_index[2]),
            scenario_id_, pinned=over_node,
        )
        # The row is measured; the overrides are declared. They are written on afterwards so
        # `_measure` stays the one thing that reads a road, and `max_steps` rides along untouched
        # because a rebuild changes what a scenario drives, never the budget someone chose for it.
        row = row.model_copy(
            update={
                "exit_rule": over_rule,
                "exit_node": over_node,
                "max_steps": old.max_steps,
            }
        )
        if thumbnails and old.thumbnail:
            row = row.model_copy(
                update={
                    "thumbnail": _draw_thumbnail(
                        bank_dir, scenario_id_, env, category, seed, chosen,
                        row.net_rotation_deg, _pin_label(over_node),
                    )
                }
            )
        else:
            # The row's seed has changed, so the old picture is of a *different* scenario.
            # Delete it and record no thumbnail rather than leave the manifest pointing at an
            # image that contradicts it: missing is honest, wrong is not.
            row = row.model_copy(update={"thumbnail": None})
            if old.thumbnail:
                (bank_dir / old.thumbnail).unlink(missing_ok=True)
    finally:
        env.close()

    _warn_budget(entry, row, name, say)

    entry.scenarios[index] = row
    write_manifest(bank_dir, manifest)
    say(
        f"{scenario_id_}  seed {old.seed} -> {seed}  -> {row.destination}  "
        f"lane {row.spawn_lane_index}  {row.route_length_m:.1f} m  {row.net_rotation_deg:+.1f} deg"
    )
    return row


def add_scenario(
    bank_dir: Path,
    category_name: str | None,
    seed: int,
    *,
    block_seq: str | None = None,
    rule: str | None = None,
    thumbnails: bool = True,
    progress: Callable[[str], None] | None = None,
) -> ScenarioRow:
    """Build one more scenario and append it to an existing bank.

    **The new id is one past the highest, never `len(scenarios)`.** Removing leaves a gap, so a
    bank that counted rows would eventually re-issue `curve_0002` for a different road -- and an
    id is how a run refers to a scenario and how Phase 5's results are keyed. A re-used id makes
    every recorded one ambiguous; a gap costs nothing, because `_locate` already searches by id.

    Three ways to know which road to build, in the order they are tried:

    1. **`block_seq` and `rule`** -- a road composed by hand, filed under `composed_name`, which
       is the sequence and the rule and nothing else. This is what the studio's road builder
       sends. A category that does not exist yet is created here, and its `max_steps` is the
       budget its **first route earns** -- `step_budget` of the length that was just measured,
       the same number the builder showed as *would earn*.
    2. **A `category_name` the manifest holds** -- built from the manifest's own entry, the way
       `replace_scenario` is, so a bank generated before a code change grows the way it was built.
    3. **A `category_name` this build knows** -- re-created from `categories.py`, because
       otherwise removing a category's last scenario would be the one edit this tool cannot undo.

    Removing a composed category's last scenario removes it too, and `categories.py` has never
    heard of it -- so the way back is to compose the same road again. The name is derived, so the
    same road and rule land in the same category, which is what keeps that an undo rather than a
    second spelling.
    """
    from metadrive.envs.metadrive_env import MetaDriveEnv

    bank_dir = Path(bank_dir)
    manifest = read_manifest(bank_dir)
    say = progress or (lambda _message: None)

    category_name, entry, category = _road_to_add(manifest, category_name, block_seq, rule, say)

    taken = sorted(row.seed for row in (entry.scenarios if entry else []))
    if seed in taken:
        raise BankError(
            f"seed {seed} is already used by {category_name}: each seed builds one scenario. "
            f"{category_name} currently holds seeds {taken}."
        )

    name = scenario_id(category_name, _next_index(entry) if entry else 0)
    env = MetaDriveEnv(base_config(map=category.block_seq, start_seed=seed, num_scenarios=1))
    try:
        _reset(env, seed, category.block_seq)
        _assert_drive_side(env, category.block_seq, seed)
        row, chosen = _measure(
            env, category, seed, read_sockets_from_env(env), int(env.agent.lane_index[2]), name,
        )
        if thumbnails:
            row = row.model_copy(
                update={
                    "thumbnail": _draw_thumbnail(
                        bank_dir, name, env, category, seed, chosen, row.net_rotation_deg,
                    )
                }
            )
    finally:
        env.close()

    if entry is None:
        # The cap is set *after* measuring, so a composed category is capped by what its own road
        # earns rather than by a number nobody chose. Every later seed of it is then warned about
        # against this one, exactly as the eleven shipped categories are.
        entry = CategoryEntry(
            description=category.description,
            block_seq=category.block_seq,
            exit_rule=category.exit_rule.value,
            max_steps=step_budget(row.route_length_m),
            scenarios=[],
        )
        manifest.categories[category_name] = entry
        say(
            f"{category_name} is new to this bank: {entry.block_seq} driven to the "
            f"{entry.exit_rule} exit, capped at {entry.max_steps} steps, which is what its "
            f"first route earns."
        )

    _warn_budget(entry, row, category_name, say)
    entry.scenarios.append(row)
    write_manifest(bank_dir, manifest)
    say(
        f"added {name}  seed {seed}  -> {row.destination}  lane {row.spawn_lane_index}  "
        f"{row.route_length_m:.1f} m  {row.net_rotation_deg:+.1f} deg"
    )
    return row


def _road_to_add(
    manifest: Manifest,
    category_name: str | None,
    block_seq: str | None,
    rule: str | None,
    say: Callable[[str], None],
) -> tuple[str, CategoryEntry | None, Category]:
    """Resolve what `add_scenario` is about to build: its name, its entry, and its road.

    The entry comes back `None` for a category this bank does not hold yet **and** whose road was
    given here rather than looked up -- the one case whose `max_steps` cannot be known until the
    route has been measured. Every other case has an entry, and building against it is what makes
    an edit reproduce the bank rather than this build.
    """
    if (block_seq is None) == (category_name is None):
        raise BankError(
            "name a category to add to, or give a block sequence and a rule to compose one -- "
            "not both, and not neither"
        )

    if block_seq is None:
        if rule is not None:
            raise BankError(
                f"a rule composes a road, so {rule!r} needs a block sequence with it. To point "
                f"an existing scenario at another exit, that is `replace --exit-rule`."
            )
        assert category_name is not None
        entry = manifest.categories.get(category_name)
        if entry is not None:
            return category_name, entry, _category_for(category_name, entry)
        try:
            known = get_category(category_name)
        except CategoryError as error:
            raise BankError(
                f"{error}. A road this build does not ship can still be added by composing it: "
                f"give a block sequence and a rule instead of a name."
            ) from error
        # Re-created whole, cap included, from this build's own declaration of it -- not from
        # what the route happens to earn. A shipped category's `max_steps` is a number somebody
        # chose, and losing it here would make removing its last scenario a lossy edit.
        entry = CategoryEntry(
            description=known.description,
            block_seq=known.block_seq,
            exit_rule=known.exit_rule.value,
            max_steps=known.max_steps,
            scenarios=[],
        )
        manifest.categories[known.name] = entry
        say(
            f"{known.name} was not in this bank, so its road and rule come from this build: "
            f"{known.block_seq} / {known.exit_rule.value}."
        )
        return known.name, entry, known

    validate_block_seq(block_seq)
    if rule is None:
        raise BankError(
            f"composing {block_seq!r} needs a rule, because a bare sequence has no category to "
            f"say which exit to drive to: choose one of "
            f"{', '.join(member.value for member in ExitRule)}"
        )
    try:
        chosen = ExitRule(rule)
    except ValueError as error:
        raise BankError(
            f"unknown exit rule {rule!r}: choose one of "
            f"{', '.join(member.value for member in ExitRule)}"
        ) from error

    name = composed_name(block_seq, chosen)
    entry = manifest.categories.get(name)
    if entry is None:
        return name, None, Category(
            name=name,
            block_seq=block_seq,
            exit_rule=chosen,
            # Provisional. Replaced by what the first route earns, once there is a route.
            max_steps=0,
            description=f"Composed by hand: {block_seq} driven to the {chosen.value} exit.",
        )
    # The name is derived from the road and the rule, so these can only disagree in a manifest
    # somebody edited. Said plainly rather than silently building on the other road.
    if entry.block_seq != block_seq or entry.exit_rule != chosen.value:
        raise BankError(
            f"this bank's {name} is {entry.block_seq} / {entry.exit_rule}, and you asked for "
            f"{block_seq} / {chosen.value}. A composed category is named after its road and its "
            f"rule, so those should not differ -- the manifest has been edited by hand."
        )
    return name, entry, _category_for(name, entry)


def remove_scenario(
    bank_dir: Path,
    scenario_id_: str,
    *,
    progress: Callable[[str], None] | None = None,
) -> ScenarioRow:
    """Take one scenario out of a bank, with its picture. No simulator.

    **The ids that remain do not move.** Renumbering `curve_0003` down because `curve_0002` went
    would change the id of a scenario nobody touched, and an id already written into a result is
    not this tool's to re-point. So the position is left empty and `scenario_id` stops being a
    row number, which `_locate` never assumed anyway.

    The thumbnail goes with the row, on the rule `replace_scenario` already set: a picture of a
    scenario that is not in the manifest is wrong, not merely stale. A category whose last
    scenario is removed goes too -- an entry with no scenarios describes nothing -- and the
    bank's last scenario is refused, because an empty bank is a manifest with nothing in it.
    """
    bank_dir = Path(bank_dir)
    manifest = read_manifest(bank_dir)
    say = progress or (lambda _message: None)

    name, entry, index = _locate(manifest, scenario_id_)
    held = sum(len(one.scenarios) for one in manifest.categories.values())
    if held == 1:
        raise BankError(
            f"{scenario_id_} is the only scenario in this bank, and a bank with nothing in it is "
            "a manifest describing no scenarios. Delete the directory instead, or generate a new "
            "bank over it."
        )

    row = entry.scenarios.pop(index)
    if row.thumbnail:
        (bank_dir / row.thumbnail).unlink(missing_ok=True)
    if not entry.scenarios:
        del manifest.categories[name]
        say(f"{scenario_id_} was the last {name} in this bank, so the category went with it.")
    write_manifest(bank_dir, manifest)
    say(
        f"removed {scenario_id_}  seed {row.seed}  -> {row.destination}. The ids after it keep "
        f"their numbers: {name} does not renumber."
    )
    return row


def set_max_steps(
    bank_dir: Path,
    scenario_id_: str,
    max_steps: int | None,
    *,
    progress: Callable[[str], None] | None = None,
) -> ScenarioRow:
    """Give one scenario its own step budget, or `None` to put it back on its category's.

    **The one edit here that builds nothing.** Every other field of a row is read off a road, so
    changing it means building that road again; `max_steps` is a cap somebody chose, and choosing
    a different one is an edit to the manifest and nothing else.

    It is still checked against what the route earns from `step_budget`, because a budget below
    that is the one setting on this panel that can make a scenario unfinishable.
    """
    bank_dir = Path(bank_dir)
    manifest = read_manifest(bank_dir)
    say = progress or (lambda _message: None)

    if max_steps is not None and max_steps < 1:
        raise BankError(
            f"a step budget of {max_steps} would end the episode before it began: "
            "give at least 1, or clear the override to use the category's cap."
        )

    name, entry, index = _locate(manifest, scenario_id_)
    row = entry.scenarios[index].model_copy(update={"max_steps": max_steps})
    entry.scenarios[index] = row
    _warn_budget(entry, row, name, say)
    write_manifest(bank_dir, manifest)

    whose = "its own" if max_steps is not None else f"{name}'s"
    say(
        f"{scenario_id_} runs on a budget of {entry.budget_for(row)} steps ({whose}); its "
        f"{row.route_length_m:.1f} m route earns {step_budget(row.route_length_m)}."
    )
    return row


def set_options(
    bank_dir: Path,
    levels: Mapping[str, str],
    *,
    progress: Callable[[str], None] | None = None,
) -> OptionLevels:
    """Pin some of the bank's option levels. Builds nothing.

    The second edit here in `set_max_steps`'s class: what it changes is *declared* rather than
    measured off a road, so it is a manifest read and a manifest write and no environment is
    constructed. Every scenario row, every thumbnail and `base_config` come through untouched --
    changing a traffic level is not a reason to build 35 roads again, and the whole point of
    storing intent separately from generation truth is that it never becomes one.

    Only the axes named in `levels` move; the rest keep what the manifest already says. An unknown
    axis or an unknown level is refused by name, because a typo that silently pinned nothing would
    be indistinguishable from a bank somebody deliberately left alone.
    """
    bank_dir = Path(bank_dir)
    manifest = read_manifest(bank_dir)
    say = progress or (lambda _message: None)

    chosen = dict(levels)
    for axis, level in chosen.items():
        if axis not in AXES:
            raise BankError(
                f"{axis!r} is not an option axis. The six are: {', '.join(AXES)}."
            )
        if level not in LEVEL_NAMES:
            raise BankError(
                f"{level!r} is not a level for {axis}. The four are: {', '.join(LEVEL_NAMES)}."
            )

    options = manifest.options.model_copy(update=chosen)
    manifest = manifest.model_copy(update={"options": options})
    write_manifest(bank_dir, manifest)

    for axis in AXES:
        if axis in chosen:
            say(f"{axis} pinned at {chosen[axis]}")
    return options


def _next_index(entry: CategoryEntry) -> int:
    """One past the highest index any of these ids carries, or 0 for an empty category.

    Read off the **ids** rather than counted, because removal leaves gaps and a count would walk
    back into one. A row whose id does not end in a number does not vote: the id is still the key
    either way, and guessing at its shape would be worse than ignoring it.
    """
    highest = -1
    for row in entry.scenarios:
        suffix = row.scenario_id.rpartition("_")[2]
        if suffix.isdigit():
            highest = max(highest, int(suffix))
    return highest + 1


def _exit_intent(
    entry: CategoryEntry,
    row: ScenarioRow,
    seed: int,
    *,
    exit_rule: str | None,
    destination: str | None,
    inherit: bool,
    say: Callable[[str], None],
) -> tuple[str | None, str | None]:
    """What this row's destination should be resolved from after an edit: a rule, or an exit.

    Four ways in, and the last is the one worth spelling out. An edit that says nothing about the
    exit carries the row's own override forward -- except a pinned node when the seed moves. A
    node names an arm of *that* seed's road, so it cannot outlive the seed it was pinned at, and
    carrying it would fail the rebuild with a message about a node nobody typed.
    """
    if exit_rule is not None and destination is not None:
        raise BankError(
            "give either an exit rule or an exact exit, not both: one resolves the destination "
            "and the other names it outright."
        )
    if inherit:
        if exit_rule is not None or destination is not None:
            raise BankError(
                "inheriting the category's exit and naming one of your own are two different "
                "instructions; give one."
            )
        return None, None
    if exit_rule is not None:
        try:
            ExitRule(exit_rule)
        except ValueError as error:
            known = ", ".join(member.value for member in ExitRule)
            raise BankError(f"unknown exit rule {exit_rule!r}: one of {known}") from error
        return exit_rule, None
    if destination is not None:
        return None, destination
    if row.exit_node and seed != row.seed:
        say(
            f"note: {row.scenario_id} pinned the exit {row.exit_node}, which is an arm of seed "
            f"{row.seed}'s road. Seed {seed} builds a different road, so this rebuild resolves "
            f"its exit from {entry.exit_rule} instead."
        )
        return row.exit_rule, None
    return row.exit_rule, row.exit_node


def _category_for(
    name: str, entry: CategoryEntry, *, rule: str | None = None, max_steps: int | None = None
) -> Category:
    """The `Category` an edit builds against: the manifest's entry, with a row's overrides on top.

    From the entry rather than from `CATEGORIES`, because a bank is self-describing and one
    generated before a code change still has to be correctable afterwards.
    """
    value = rule or entry.exit_rule
    try:
        resolved = ExitRule(value)
    except ValueError as error:
        raise BankError(
            f"{name} records exit rule {value!r}, which this build does not know"
        ) from error
    return Category(
        name=name,
        block_seq=entry.block_seq,
        exit_rule=resolved,
        max_steps=entry.max_steps if max_steps is None else max_steps,
        description=entry.description,
    )


def _pin_label(node: str | None) -> str | None:
    """What a thumbnail's title says the destination was chosen by.

    A picture outlives the session that drew it, so a route to a pinned exit must not be captioned
    with a rule that would have chosen a different one.
    """
    return "pinned" if node else None


def _warn_budget(
    entry: CategoryEntry, row: ScenarioRow, name: str, say: Callable[[str], None]
) -> None:
    """Say so when a route earns more steps than the cap that applies to it.

    Written anyway, never refused: the scenario is real and the cap is a decision. `review`
    reports the same thing across a whole bank, from `entry.budget_for` -- the same one place the
    override is resolved.
    """
    earned = step_budget(row.route_length_m)
    cap = entry.budget_for(row)
    if earned > cap:
        whose = "its own cap" if row.max_steps is not None else f"{name}'s cap"
        say(
            f"warning: {row.scenario_id} at seed {row.seed} runs {row.route_length_m:.1f} m, "
            f"which earns a budget of {earned} steps against {whose} of {cap}. The scenario is "
            "written; a policy may run out of steps before reaching the destination."
        )


def _locate(manifest: Manifest, scenario_id_: str) -> tuple[str, CategoryEntry, int]:
    """Find a scenario by its public key, or say which keys exist."""
    for name, entry in manifest.categories.items():
        for index, row in enumerate(entry.scenarios):
            if row.scenario_id == scenario_id_:
                return name, entry, index
    known = [row.scenario_id for entry in manifest.categories.values() for row in entry.scenarios]
    raise ScenarioNotFound(
        f"no scenario {scenario_id_!r} in this bank. It holds {len(known)}: "
        f"{', '.join(known[:6])}{', ...' if len(known) > 6 else ''}"
    )


def _select(category_names: Sequence[str]) -> dict[str, Category]:
    """Resolve the requested categories, in the canonical order rather than the argument order."""
    if not category_names:
        return dict(CATEGORIES)
    wanted = {get_category(name).name for name in category_names}
    return {name: category for name, category in CATEGORIES.items() if name in wanted}


def _resolve_seeds(
    selected: dict[str, Category],
    seeds: Sequence[int],
    category_seeds: Mapping[str, Sequence[int]] | None,
) -> dict[str, tuple[int, ...]]:
    """Work out which seeds each category is built at, and reject the lists that cannot work."""
    overrides = dict(category_seeds or {})
    unknown = sorted(set(overrides) - set(CATEGORIES))
    if unknown:
        raise BankError(
            f"seed override for unknown categor{'y' if len(unknown) == 1 else 'ies'} "
            f"{unknown}: choose from {', '.join(sorted(CATEGORIES))}"
        )

    resolved = {}
    for name in selected:
        chosen = tuple(overrides.get(name, seeds))
        if not chosen:
            raise BankError(f"no seeds for {name}: a category needs at least one")
        if len(set(chosen)) != len(chosen):
            raise BankError(
                f"duplicate seeds {sorted(chosen)} for {name}: each seed builds one scenario"
            )
        resolved[name] = chosen
    return resolved


def _by_block_seq(
    selected: dict[str, Category], per_category: Mapping[str, tuple[int, ...]]
) -> dict[tuple[str, tuple[int, ...]], list[Category]]:
    """Group categories by the road **and the seeds** they are built at.

    One env is built per group. Keyed on the seeds as well as the sequence because a category
    with its own `--seeds` cannot share a reset with the others on the same road -- and because
    the env's `num_scenarios` is sized to the seed range, so a group with different seeds needs
    a differently sized env.
    """
    grouped: dict[tuple[str, tuple[int, ...]], list[Category]] = {}
    for name, category in selected.items():
        grouped.setdefault((category.block_seq, per_category[name]), []).append(category)
    return grouped


def _measure(
    env,
    category: Category,
    seed: int,
    readings,
    spawn_lane: int,
    name: str,
    *,
    pinned: str | None = None,
) -> tuple[ScenarioRow, Any]:
    """Pin this category's destination on the live env and read the route back off it.

    `set_route` runs a shortest path, so pointing it at an unreachable node raises here rather
    than at run time -- which is the same proof `measure_route` gets from a dedicated env build,
    for the price of a method call instead of a reset.

    Returns the row and the socket the rule resolved to, because the thumbnail's title names the
    rule and the node it chose, and resolving twice would be a second chance to disagree.

    `pinned` names an exit outright, for a row that declares one instead of a rule. It is checked
    against the exits **this seed's** road actually offers, which is the whole hazard of pinning a
    node: the arm that exists at one seed need not exist at the next.
    """
    if pinned is not None:
        chosen = next((reading for reading in readings if reading.node == pinned), None)
        if chosen is None:
            offered = ", ".join(reading.node for reading in readings)
            raise BankError(
                f"{category.name} at seed {seed} has no exit {pinned!r}: this road offers "
                f"{offered}. An exit is resolved per seed, so a node pinned at one seed is not "
                "guaranteed at another."
            )
    else:
        try:
            chosen = select_exit(readings, category.exit_rule)
        except SocketError as error:
            raise BankError(f"{category.name} at seed {seed}: {error}") from error

    navigation = env.agent.navigation
    try:
        navigation.set_route(env.agent.lane_index, chosen.node)
    except Exception as error:
        raise BankError(
            f"{category.name} at seed {seed}: no route from the spawn to {chosen.node}: {error}"
        ) from error

    rotation, rotations = route_rotation(env)
    return (
        ScenarioRow(
            scenario_id=name,
            seed=seed,
            destination=chosen.node,
            spawn_lane_index=spawn_lane,
            route_length_m=round(float(navigation.total_length), 2),
            net_rotation_deg=round(rotation, 2),
            turn_pairs=turn_pairs(rotations),
            thumbnail=None,
        ),
        chosen,
    )


def write_manifest(out_dir: Path, manifest: Manifest) -> Path:
    """Write `manifest.json` atomically, so a bank never has a half-written one.

    Written **last**, after every map is built and every thumbnail is on disk. An interrupted
    generation therefore leaves a directory with no manifest, which reads as "no bank here"
    rather than as a bank that is quietly missing rows.

    Stamped with this build's `SCHEMA_VERSION` on the way out, because the version describes the
    shape of the file rather than the history of the bank: a 1.0 manifest edited by a build that
    can write per-scenario overrides is a 1.1 file, whether or not this particular edit used one.
    """
    manifest = manifest.model_copy(update={"schema_version": SCHEMA_VERSION})
    path = out_dir / MANIFEST_NAME
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(manifest.model_dump_json(indent=2) + "\n")
    os.replace(temporary, path)
    return path


def read_manifest(bank_dir: Path) -> Manifest:
    """Load and validate a bank's manifest. The runner's entry point into a bank."""
    path = Path(bank_dir) / MANIFEST_NAME
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError as error:
        raise BankError(
            f"no {MANIFEST_NAME} in {bank_dir}: generation writes it last, so an interrupted "
            "run leaves the directory without one. Re-run `scenariobank generate`."
        ) from error
    except json.JSONDecodeError as error:
        raise BankError(f"{path} is not valid JSON: {error}") from error
    return Manifest.model_validate(raw)


def _stored_config() -> dict[str, Any]:
    """`base_config()` as the manifest records it."""
    config = describe_config(base_config())
    for key in _PER_RUN_KEYS:
        config.pop(key, None)
    return config


__all__ = [
    "MANIFEST_NAME",
    "READABLE_VERSIONS",
    "SCHEMA_VERSION",
    "BankError",
    "CategoryEntry",
    "Manifest",
    "OptionLevels",
    "ScenarioNotFound",
    "ScenarioRow",
    "SimulatorInfo",
    "add_scenario",
    "describe_config",
    "generate",
    "num_scenarios_for",
    "read_manifest",
    "remove_scenario",
    "replace_scenario",
    "scenario_id",
    "set_max_steps",
    "set_options",
    "write_manifest",
]
