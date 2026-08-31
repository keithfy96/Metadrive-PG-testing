# scenariobank

A bank of procedurally generated MetaDrive scenarios with fixed seeds, forced destinations, and
run-time difficulty options — plus the runner that scores a camera model against it.

`IMPLEMENTATION_PLAN.md` is the source of truth for what gets built and in what order.
`CONTRACT.md` (Phase 6) is what the bank will promise its consumers.

All scenarios are **left-side traffic** (right-hand-drive market). See
[Which side of the road](#which-side-of-the-road) — it is not a MetaDrive setting, and it is the
first thing to check if a figure ever looks wrong.

**Built so far:** Phase 0 (skeleton, environment truth) and Phase 1 (categories, forced
destinations, left-side traffic). `generate`, `run` and the rest arrive in later
phases.

## Install

```bash
uv sync --group sim     # MetaDrive included — needed by every command except `categories`
uv sync                 # default + dev only; fast. `categories` still works; the rest exit 1
                        # with "this command needs MetaDrive"
```

MetaDrive is pinned to a **commit**, not a version. The 0.4.3 tag and the commit 32 patches past
it both report `EDITION: MetaDrive v0.4.3`, so a version pin would let two machines run different
simulators while claiming to be the same. The first command that builds an environment downloads
MetaDrive's asset tree into the venv (~1 minute, once).

> **Going back with a plain `uv sync` leaves a mess.** Those downloaded assets live in
> `site-packages/metadrive/assets/`, uv does not own them, so dropping the `sim` group removes
> every module but leaves the directory standing — and Python then reads it as a namespace
> package. Anything checking `find_spec("metadrive")` will believe MetaDrive is installed. This
> repo checks `spec.origin` instead (`doctor.has_simulator`), which a namespace package never
> has. Delete the directory if you want a truly clean state.

## Which side of the road

**MetaDrive drives on the right. This bank drives on the left.**

There is no setting for it — no `drive_side`, no `traffic_side`, nothing — because handedness is
baked into MetaDrive's geometry layer: the sign of `StraightLane.direction_lateral`, and the
`clockwise` flags the block classes hand to `create_bend_straight`. So `scenariobank.handedness`
mirrors the whole PG geometry layer about the x-axis before any map is built. `base_config()`
installs it; every command in this repo goes through `base_config()`.

What that gives you, in a driver's terms:

| | MetaDrive | this bank |
|---|---|---|
| ego keeps | right | **left** |
| oncoming traffic is on your | left | **right** |
| roundabouts circulate | counter-clockwise | **clockwise** |
| the turn that crosses oncoming | left | **right** |
| on-ramps join from the | right | **left** |

**The mirror is exact, and that is load-bearing.** A reflection is an isometry, so the mirrored
map is the *same road*: same node names, same lane count, same lane lengths, same curve radii.
`tests/unit/test_handedness.py` proves it lane by lane — it builds each map twice, once here and
once in a subprocess that never imports `scenariobank` (so MetaDrive is unmodified there), and
asserts the two agree once one is reflected. That is the only honest way to check a monkey-patch
of a global geometry layer: the patch cannot be uninstalled, so the unpatched reference has to
come from somewhere the patch never reached.

It is also the test that caught the real bug. An earlier version flipped the arcs but not the
lateral arithmetic that places the *sibling* lanes of a multi-lane road. Every map still built,
every node name matched, every lane count matched, and the pictures looked plausibly left-side.
The only symptom was that curved lanes came out the wrong length.

Two consequences worth knowing:

- **`doctor` measures the drive side; it does not report a flag.** It finds the opposing
  carriageway of the ego's own road and asks which side of the ego it is on. A patch that
  silently stopped applying would produce a working bank that is simply the wrong market, and
  nothing else about it would look wrong.
- **`intersection_left` is now the near turn and `intersection_right` crosses oncoming.** Their
  route lengths swapped (111.7 m ↔ 117.2 m) — which is itself a mirror, at the route level. The
  category names still describe the manoeuvre correctly; it is the *difficulty* that moved.

If MetaDrive is ever unpinned and its `create_pg_block_utils` changes, the mirror raises
`HandednessError` at install rather than quietly not applying.

## Commands

Every command takes a global `--verbose/-v` before the subcommand name, which switches structured
logging to DEBUG.

### `doctor` — which simulator is this, exactly

Run this first on any machine and inside any container. Nothing else in the repo is meaningful
until it passes.

| option | effect |
|---|---|
| `--require-commit <prefix>` | exit non-zero unless the installed MetaDrive resolves to a commit with this prefix |
| `--probe` / `--no-probe` | build a throwaway env to report the observation space. On by default; costs one reset |
| `--json` | emit the report as JSON instead of aligned text |

```bash
uv run scenariobank doctor
uv run scenariobank doctor --require-commit 85e5dadc     # what CI should run
uv run scenariobank doctor --json > doctor-host.json     # to diff against a container
```

Reports `metadrive.VERSION`, `EDITION`, the installed dist version, the **resolved git SHA** read
out of the distribution's `direct_url.json`, the asset version, python, and the versions of numpy,
shapely, opencv-python, panda3d and pygame — then the observation space, the action space, and the
**measured drive side**.

**Expect `obs_space: Box(-0.0, 1.0, (19,), float32)` and `drive_side: left`.** `Box(259,)` means
the lidar block in `base_config()` did not take; `drive_side: right` means the mirror did not.
Exits 1, with a `FAIL:` line on stderr, if the commit is unidentifiable, the assets are missing,
the required commit does not match, the observation is the wrong shape, or traffic is on the
wrong side.

**Writes:** nothing. (First run populates `.venv/.../metadrive/assets/`.)

### `categories` — what is in the bank

```bash
uv run scenariobank categories
```

Lists the seven categories with block sequence, exit rule, step budget and description. The only
command that does not need the `sim` group.

**Writes:** nothing.

### `sockets` — which exits does a block offer, and which way do they turn

The discovery command. It is how each category's destination was chosen, and it is what to re-run
when MetaDrive changes shape.

| option | effect |
|---|---|
| `--block-seq/-b <seq>` | a block sequence, e.g. `X`, `CC`, `rS`. `I` is prepended automatically |
| `--category/-c <name>` | use that category's block sequence instead. Exactly one of `-b`/`-c` |
| `--seed/-s <int>` | map seed, default `0` |
| `--json` | emit the readings as JSON |

```bash
uv run scenariobank sockets --block-seq X --seed 0
uv run scenariobank sockets --category roundabout --seed 3 --json
```

Prints one row per exit — socket index, destination node, angle from the spawn heading, and the
turn that implies — then what each `ExitRule` would resolve to. Angles are
counter-clockwise-positive, so **positive is a left turn**.

**The angle is the final heading, `wrap_to_pi`'d — not how far the ego turns.** That is what
`ExitRule` needs, but it folds: `curve` seed 0 sweeps +239.5° and shows here as −120.5°, half the
rotation and the wrong direction. For total rotation use `destinations`, which reports both.

Expect one exit near `+90`, one near `-90` and one near `0` for a four-way junction. **Two exits
of the same sign and similar magnitude mean the block is not what you think it is** — stop and
look before pinning anything to it.

**Writes:** nothing.

### `inspect` — see the route rather than trust it

| option | effect |
|---|---|
| `--category/-c <name>` | **required**; one of the seven |
| `--seed/-s <int>` | map seed, default `0` |
| `--out/-o <path>` | PNG to write. Defaults to `docs/reference/figures/<category>-seed<N>.png` |

```bash
uv run scenariobank inspect --category intersection_left --seed 0
uv run scenariobank inspect -c t_junction -s 2 -o /tmp/t2.png
```

Draws the road network in grey with the **pinned route in red**, a blue arrow at the spawn pose
and a green star at the destination. Headless — no display, no window, no image buffer. The route
comes from the navigation module after the destination is pinned, so the picture shows what the
runner will actually drive.

**Writes:** one PNG, at `--out` or under `docs/reference/figures/`.

### `destinations` — regenerate the reference document

| option | effect |
|---|---|
| `--out/-o <path>` | document to write, default `docs/reference/destinations.md` |

```bash
uv run scenariobank destinations
```

Resolves every category at every seed, proves each destination is reachable by running the
shortest path, measures the route, and fingerprints each block sequence's drivable surface. Two
env builds per category per seed plus one per sequence — about a minute.

The document it writes has six sections: the resolved destination and angle per category and
seed; route length against the earned step budget; the turn actually taken; the **spawn lane**;
the **curve direction pairs**; and **distinct roads per block sequence**, which is where the
bank's least obvious property is recorded — `X` builds one identical road at all five seeds, `T`
builds two, and the other three build five each, so there are **18 distinct roads across the 35
scenarios**.

Those `X` seeds are still not five identical runs. `random_spawn_lane_index` is left on
deliberately, so the ego starts in lane `0, 1, 0, 1, 1` — the only thing separating them until
Phase 4's options arrive, and invisible in `route_length`, which is measured on a reference lane.
The curve section records the other property that holds by luck: `CC` draws each block's direction
independently, and seeds 0–4 happen to cover all four of `LL`, `LR`, `RR`, `RL`. Both are asserted
by tests so a MetaDrive bump cannot quietly take them away.

Regenerate it after any MetaDrive bump. That is how a change in block geometry becomes visible
instead of silently changing what the bank means.

**Writes:** `docs/reference/destinations.md` (or `--out`). Overwrites in place.

### `scripts/bank-check.sh` — the one command CI and a human both run

```bash
./scripts/bank-check.sh
```

`ruff check` (the gate — `ruff format --check` deliberately is not), then `pytest`. There is no
per-bank step: `scenariobank verify` was cut on 2026-08-31 with the durable-bank premise it
enforced, since a bank is regenerated per batch and there is nothing to check it against.

**Writes:** `.ruff_cache/`, `.pytest_cache/` — both gitignored.

## What gets generated, and where

| path | written by | in git? |
|---|---|---|
| `docs/reference/destinations.md` | `destinations` | yes — it is the Phase 1 deliverable |
| `docs/reference/figures/*.png` | `inspect` | yes — one per category at seed 0 |
| `banks/<bank-name>/` | `generate` (Phase 2) | yes |
| `.venv/`, `.ruff_cache/`, `.pytest_cache/` | tooling | no |

Nothing writes outside the repo, and no command writes to `$HOME`.

## Tests

```bash
uv run pytest        # 118 tests, ~22s with the sim group installed
```

| | pass | skip |
|---|---|---|
| `uv sync --group sim` | 118 | 0 |
| `uv sync` | 63 | 55 |

Simulator-dependent tests are guarded by a **named** `needs_sim` skipif rather than a bare one, so
a skip is legible in the report instead of being a silent absence:

```
SKIPPED [1] tests/unit/test_config.py:53: needs_sim: MetaDrive is not installed (uv sync --group sim)
```
