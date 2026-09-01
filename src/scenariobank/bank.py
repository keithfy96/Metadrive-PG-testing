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
    ExitRule,
    get_category,
    step_budget,
)
from scenariobank.config import base_config
from scenariobank.handedness import DRIVE_SIDE_LEFT
from scenariobank.sockets import (
    SocketError,
    read_sockets_from_env,
    route_rotation,
    select_exit,
    turn_pairs,
)

#: Bumped when a reader would break. The runner validates against it rather than duck-typing.
SCHEMA_VERSION = "1.0"

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

    schema_version: Literal["1.0"]
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
    seed: int,
    *,
    thumbnails: bool = True,
    progress: Callable[[str], None] | None = None,
) -> ScenarioRow:
    """Rebuild one scenario of an existing bank at a different seed, in place.

    The correction loop. A bank is generated, someone looks at it, and one scenario turns out to
    be a poor draw -- `curve` seed 4 builds a road 7% from seed 0's, near enough that the two
    thumbnails are the same picture. This swaps that one row rather than regenerating 35.

    **The bank never changes size and never renumbers.** `scenario_id` and the row's position are
    kept; only the seed and what was measured from it change.

    With `thumbnails=False` the old image is **deleted** and the row records no thumbnail. It
    cannot be kept: it is a picture of the seed that was just replaced, and a manifest pointing at
    it would be wrong rather than merely incomplete.

    `block_seq` and `exit_rule` come from the **manifest's own category entry**, not from
    `categories.py`. The bank is self-describing, and one generated before a code change should
    still be correctable afterwards.
    """
    from metadrive.envs.metadrive_env import MetaDriveEnv

    bank_dir = Path(bank_dir)
    manifest = read_manifest(bank_dir)
    say = progress or (lambda _message: None)

    name, entry, index = _locate(manifest, scenario_id_)
    taken = {row.seed for i, row in enumerate(entry.scenarios) if i != index}
    if seed in taken:
        raise BankError(
            f"seed {seed} is already used by {name}: each seed builds one scenario. "
            f"{name} currently holds seeds {sorted(row.seed for row in entry.scenarios)}."
        )

    try:
        rule = ExitRule(entry.exit_rule)
    except ValueError as error:
        raise BankError(
            f"{name} records exit rule {entry.exit_rule!r}, which this build does not know"
        ) from error

    category = Category(
        name=name,
        block_seq=entry.block_seq,
        exit_rule=rule,
        max_steps=entry.max_steps,
        description=entry.description,
    )
    env = MetaDriveEnv(
        base_config(map=entry.block_seq, start_seed=seed, num_scenarios=1)
    )
    try:
        _reset(env, seed, entry.block_seq)
        _assert_drive_side(env, entry.block_seq, seed)
        row, chosen = _measure(
            env, category, seed, read_sockets_from_env(env), int(env.agent.lane_index[2]),
            scenario_id_,
        )
        old = entry.scenarios[index]
        if thumbnails and old.thumbnail:
            row = row.model_copy(
                update={
                    "thumbnail": _draw_thumbnail(
                        bank_dir, scenario_id_, env, category, seed, chosen,
                        row.net_rotation_deg,
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

    earned = step_budget(row.route_length_m)
    if earned > entry.max_steps:
        say(
            f"warning: {scenario_id_} at seed {seed} runs {row.route_length_m:.1f} m, which "
            f"earns a budget of {earned} steps against {name}'s cap of {entry.max_steps}. The "
            "scenario is written; a policy may run out of steps before reaching the destination."
        )

    entry.scenarios[index] = row
    write_manifest(bank_dir, manifest)
    say(
        f"{scenario_id_}  seed {old.seed} -> {seed}  -> {row.destination}  "
        f"lane {row.spawn_lane_index}  {row.route_length_m:.1f} m  {row.net_rotation_deg:+.1f} deg"
    )
    return row


def _locate(manifest: Manifest, scenario_id_: str) -> tuple[str, CategoryEntry, int]:
    """Find a scenario by its public key, or say which keys exist."""
    for name, entry in manifest.categories.items():
        for index, row in enumerate(entry.scenarios):
            if row.scenario_id == scenario_id_:
                return name, entry, index
    known = [row.scenario_id for entry in manifest.categories.values() for row in entry.scenarios]
    raise BankError(
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
) -> tuple[ScenarioRow, Any]:
    """Pin this category's destination on the live env and read the route back off it.

    `set_route` runs a shortest path, so pointing it at an unreachable node raises here rather
    than at run time -- which is the same proof `measure_route` gets from a dedicated env build,
    for the price of a method call instead of a reset.

    Returns the row and the socket the rule resolved to, because the thumbnail's title names the
    rule and the node it chose, and resolving twice would be a second chance to disagree.
    """
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
    """
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
    "SCHEMA_VERSION",
    "BankError",
    "CategoryEntry",
    "Manifest",
    "ScenarioRow",
    "SimulatorInfo",
    "describe_config",
    "generate",
    "num_scenarios_for",
    "read_manifest",
    "replace_scenario",
    "scenario_id",
    "write_manifest",
]
