# MetaDrive PG Scenario Bank — Phased Implementation Plan

## Context

We need to hand the frontend team two things: a **scenario bank** (a stable, named set of
procedurally-generated MetaDrive scenarios) and a **runner container** that executes a model
against a chosen subset and returns pass/fail per scenario. The contract between us is two
versioned JSON schemas, nothing else.

The core design decision stands: **a PG scenario is `(config, seed)`. We store seeds, not
serialised scenarios.** MetaDrive's procedural generator is deterministic under a fixed config,
so the bank is a manifest of integers. The ScenarioNet `.pkl` export path is rejected because
replay requires `ScenarioEnv`, which swaps `NodeNetworkNavigation` for `TrajectoryNavigation`
and silently changes the task — it observes 31 numbers through `TrajectoryNavigation` where we
observe 19. Stated for the frontend in `CONTRACT.md` (Phase 6).

Three things shape the design beyond that:

1. **Difficulty is a form, not a bank.** Researchers pick from six dropdowns — traffic, cones,
   barriers, pedestrians, cyclists, lights — each `none | low | medium | high`. Options are stored
   separately from maps and applied at run time. See **Scenario options**.
2. **Seeds are fixed at 0–4 for every category.** 7 categories x 5 seeds = 35 maps. "Seed 3" means
   the same thing everywhere.
3. **A bank is a disposable, per-batch artifact.** It is regenerated whenever someone wants
   scenarios; nothing checks that a road matches a previous run's. The container pinning one
   MetaDrive commit is what makes a single batch self-consistent. See Phase 2, **Scope**.

Everything in this plan was verified against the MetaDrive source actually installed here
(`converter-scenarionet-stage2-redesign/.venv/.../metadrive/`). File:line references are real.

---

## How this ships — one queue, two orchestrators, two rigs

**Decision (2026-09-01, with Keith).** This bank is not driven by a CLI and a JSON file. Work
arrives from the existing webapp, is queued on a **NAS**, and runs on **two rigs**. Four processes,
and it matters which are ours:

```
  frontend
     |  put()
     v
  +---------------------------- NAS ------------------------------+
  |  wfqueue        multi-topic SQLite queue: lease / ack / nack   |
  |     |           at-least-once, priority then FIFO              |
  |     +-- lease -->  CARLA orchestrator      (Tyrone's)          |
  |     +-- lease -->  MetaDrive orchestrator  (OURS)              |
  |                      reads the options, picks a rig and a GPU, |
  |                      calls the runner, saves what comes back   |
  +---------------------------+------------------+----------------+
                      call    |                  |    call
                              v                  v
                      +--- rig A ----+   +--- rig B ----+
                      |  md-runner   |   |  md-runner   |   (OURS)
                      |  container   |   |  container   |
                      |  GPU lock <--+---+--> CARLA takes these cards too
                      +--------------+   +--------------+
```

**Ours:** the MetaDrive orchestrator on the NAS, the runner on each rig, and the container that
simulates. **Not ours:** the queue, the frontend, and the CARLA orchestrator.

This supersedes "Phase 7 — optional, build only if asked" and the shared-`jobs`-table design that
replaced it. Phase 7 is the deliverable. It also supersedes the state-vector policy boundary: see
**No lidar** and **Phase 4**.

**Out of scope, decided the same day:** getting the model checkpoint onto the rigs. It arrives by
some other route -- most likely a cronjob. The runner takes a local path and never fetches.

### The words, because two projects use one of them differently

wing-sim calls its **rig-side** service "the orchestrator" (`orchestrator/README.md`: *"Owns the
rig: one queue, one lock, one archive, one database"*). Here the orchestrator is on the NAS. Every
sentence in Phase 7 uses these four words in exactly this sense:

| word | means |
|---|---|
| **queue** | `wfqueue` on the NAS. Not ours. `docs/queue-docs/`. |
| **orchestrator** | our MetaDrive dispatcher on the NAS. Leases, plans, calls, saves. |
| **runner** | our HTTP service on each rig. Owns that rig's locks and its containers. |
| **container** | the image that actually simulates. Calls `run_bank()`. |

### R1 — the independence rule (hard constraint)

**Tyrone's code is a reference, never a dependency.** No module of his is imported by anything here.
Read it to avoid rediscovering failures he already paid for; write our own.

Under this topology R1 costs almost nothing, because **neither side imports the other**. Two
orchestrators lease from one queue and call their own runners; there is no seam between the two
codebases at all. What is shared is a queue and a lock *path*, and neither violates R1, because **a
schema is data and a path is not a library**:

- the queue -- HTTP against a documented API, using the stdlib client the server itself serves
- `~/simulation/.wing-sim.gpu*.lock` -- files opened with `flock`, not an import of `rig/lock.py`

| Was going to reuse | Write our own instead | Cost |
|---|---|---|
| `rig/lock.py` | flock helper against the same paths | ~150 lines |
| `staging.py` / `archive.py` | our own staging + archive | ~250 lines |
| `rig/session.py` | our own session | ~300 lines (his is preset/CARLA-coupled anyway) |
| `ingest/harvest.py` | our own ingest | small -- our schema, no mapping |

**R1 also removes a blocker.** `orchestrator/src/staging.py` and `archive.py` do not exist in his
checkout -- not gitignored, on no branch, while `runner/job_runner.py:11,25`, `api/uploads.py:14`
and `validation.py:37` import them. Under R1 that is his problem for starting his service, not a
prerequisite for anything here.

### Why a queue on the NAS, when each rig has a lock

The GPU argument is the weakest justification. A flock is advisory and kernel-released on holder
death, and his `001_initial.sql` says outright that the `gpu_lease` table is "Observability only,
NOT the authority." Two orchestrators polling one rig's lock already cannot double-book that card.

What the queue fixes is what a lock cannot express:

- **Ordering across two machines.** A lock says busy or free; it cannot say *whose turn*. `wfqueue`
  leases by priority then FIFO by id, so the order is a property of the queue rather than of who
  polled first.
- **Work that outlives its worker.** A lease expires and the message returns to `ready` on its own.
  An orchestrator that dies mid-dispatch loses nothing, which is not true of a lock plus a list.
- **A place for jobs that are wrong.** `nack` past `max_attempts` dead-letters, and dead is
  inspectable and requeueable. A wedged job stops being a mystery.
- **One screen**, not two tabs where a user guesses why their job is not moving.

### What is still open

- **One lock file per machine, or per GPU.** wing-sim's is per machine (`.wing-sim.gpu.lock`,
  `rig/lock.py:39-40`), which was right when a rig had one card. With two cards a machine-wide lock
  serialises the whole rig and "which GPU is free" cannot be expressed. Phase 7 names it per device
  and records the incompatibility as a question for Tyrone rather than assuming an answer.
- **Where the frontend reads results from.** Ours are saved on the NAS by our orchestrator; whether
  the webapp reads them from us or the queue carries them is not decided.

---

## What a person actually uses

Stated outright because the rest of this document is written in commands, and a reader would
otherwise conclude the command line is the product. It is not.

| surface | for | whose |
|---|---|---|
| **the studio** (Phase 2c) | authoring a bank: pick a scenario type, build it, look at every scenario, swap a poor seed — and later, submit a run onto the queue | ours |
| **the wing-sim webapp** | the other producer onto the same queue | Tyrone's |
| ~~the CLI~~ | **not a surface.** The studio's worker process, the container's entrypoint, and a maintenance tool for the generated references | ours |

**Exactly one command is user-facing: `scenariobank studio`, which starts the web page.** Everything
else in this document that looks like a command is run by a machine — the studio spawning a job, the
container's entrypoint, or CI.

**Why a CLI at all, given that.** `BaseEngine.singleton` (`engine/engine_utils.py:36-59`) is one
engine per *process*: a FastAPI server that built an env would hold that singleton for its lifetime,
could serve exactly one simulator request, and would die with it — a panda3d fault is a segfault,
not an exception a handler can catch. So a subprocess is not a style choice, and the CLI is simply
what that subprocess is. Keeping it a real CLI rather than a private protocol also means the
studio's forms and its server-side job validation are both generated from the CLI's own flags
(`docs.reference()`), so the page cannot come to offer a flag the program does not accept.

---

## Traps — verified, do not re-propose

Each of these is *the obvious thing you would reach for*, and why it does not work. They were
checked in the MetaDrive source installed here; the file:line references are real. They live in this
section rather than in a phase because the temptation recurs — someone will reach for
`accident_prob` again in six months.

| The tempting move | Why it fails, and what to do instead |
|---|---|
| Pin `metadrive-simulator==0.4.3` and trust the version string | `metadrive.constants.EDITION` reports `"MetaDrive v0.4.3"` for **both** the tag and the commit we actually run (`85e5dadc` = `MetaDrive-0.4.3-32-g85e5dadc`); `pyproject.toml:31-36` already warns about this trap. **The version string is not an integrity check.** Record the resolved dist version + git SHA + `asset_version()`, and pin the same commit. *Enforced in:* Phase 0 `doctor`. The Phase 2 manifest records all three as **information** — nothing refuses on them, because a road differing between batches is not an error here. |
| Treat `random_traffic=True` as a cosmetic knob | `traffic_manager.py:339-341`: with it on, the traffic manager is **never re-seeded**, so traffic differs on every reset at the same seed. *Enforced in:* `base_config`, and `tests/unit/test_invariance.py::test_random_traffic_breaks_invariance` — the one setting guaranteed to break invariance, which is what stops a green invariance run from being a silently empty loop. |
| Let the last block set the destination | True *only* when the ego spawns on a positive road — `node_network_navigation.py:80` falls back to `map.blocks[0]` on a negative one, and the socket within the block is a seeded random draw among three for X/O/T. **We never let it choose:** `node_network_navigation.py:60` reads `vehicle.config["destination"]` (`base_env.py:141`), and when it is set `auto_assign_task` is skipped entirely. Every category pins its destination. *Enforced in:* Phase 1 — this is what deletes the turn classifier. |
| Use `accident_prob` for the Cones and Barriers axes | Set it 0–1 and `TrafficObjectManager` scatters debris — but `object_manager.py:51-53` skips any block that is not `Straight`/`Curve`/`InRampOnStraight`/`OutRampOnStraight`, so `X`, `T` and `O` receive **nothing, with no error** — 5 of our 7 categories. And one scalar drives all three scene types mutually exclusively (`:54-91`), so "cones yes, barriers no" is inexpressible. **`accident_prob` stays `0.0` permanently**; write `obstacles.py`. See **Scenario options**. |
| Look for MetaDrive's left-hand-traffic option | **There is none**, and the negative is exhaustive rather than a keyword grep: all 249 keys of `BASE_DEFAULT_CONFIG`, `METADRIVE_DEFAULT_CONFIG` and `SCENARIO_ENV_CONFIG` dumped and filtered — nothing; OpenDRIVE carries drive side as `rule="RHT"/"LHT"` and MetaDrive never parses it (`utils/opendrive/parser.py:509-535` has no such field, upstream `main` still reads `# Rules` / `# TODO implementation`); SUMO's `lefthand="true"` is dropped too; ScenarioNet's `coordinate` means coordinate *frame*, not traffic side. Every "handed" word in the package is coordinate chirality. **And there is nothing to upgrade to** — upstream `main`'s `version.py` still reads `0.4.3`. A geometry reflection is the only route: `handedness.py`. *Enforced in:* Phase 1 build, Phase 0 `doctor`, and Phase 2 `generate`, which **measures** the drive side off the built map. |
| Reach for `need_inverse_traffic=True` to put traffic on the other side | It does not do that. It only lets the traffic manager *also* spawn NPCs on the opposing carriageway (`traffic_manager.py:246-247`, `:381-382`), and only for block IDs `S C r R` — so `X`, `T` and `O` are unaffected, silently, exactly like `accident_prob` above. Which side anyone **keeps** is geometry, not this flag. It is still worth turning on for the Traffic axis, because without it a two-way map has no oncoming traffic at all — but that is a **Scenario options** item, not a handedness switch. |
| Assume a fixed road means a fixed scenario | `X` builds **one identical road** at all five seeds (`StdInterSection` has a fixed radius, the map pins `lane_num=3`/`lane_width=3.5`), which invites the conclusion that its five seeds are one run repeated. They are not: `random_spawn_lane_index` defaults to `True` (`metadrive_env.py:61`) — the only `random_*` key that does — and `agent_manager.py:111-119` draws `randint(lane_num)` per reset, giving lanes `0, 1, 0, 1, 1`. **`route_length` will not show you**, because `navigation.total_length` is measured on a reference lane and reads `111.70` at all five. Kept on deliberately and recorded per scenario. *Enforced in:* `config.base_config` names the key; `sockets.measure_route` records the draw; `test_sockets.py` asserts it is invariant under the option axes. |
| Read `SocketReading.angle_deg` as how far the ego turns | It is the **final heading**, `wrap_to_pi`'d (`sockets.py:102`). Correct for choosing an exit — which is all `ExitRule` needs — and wrong for describing one. `curve` seed 0 sweeps **+239.5°** and this field reads **−120.5°**: half the rotation, and the opposite direction. `X`/`T`/`O` never pass 180° so they are unaffected, which is what let it sit unnoticed. Use `RouteMeasurement.net_rotation_deg`, integrated along the driven route. *Enforced in:* `destinations.md` reports both and says which is which; `test_sockets.py` pins the disagreement. |
| Count road variety by hashing the geometry | `lane_geometry_digest` is an **equality test**. It answers *is this the same road* and was read as answering *is this a different scenario*, which it cannot. It reported `CC` as 5/5 distinct when seeds 0 and 4 are **7% apart** — near enough that their thumbnails are the same picture — and `O` as 5/5 distinct when seeds 0 and 4 are **not measurably apart at all**. Only `CC` has real spread available (median pair gap 40% over seeds 0–25, against 12% for `O` and under 14% for every `rS` pair). *Enforced in:* `fingerprint.shape_gap` is the similarity measure; `destinations.md` prints the closest pair per sequence beside the distinct count; `scenariobank seeds` ranks what a candidate seed would add. |
| Read a `C` block as a pure arc | A MetaDrive `Curve` block is an arc **and a trailing straight**. `create_bend_straight` returns `(curve, straight)` and `pgblock/curve.py` builds both — part 1 the arc, part 2 a straight of `Parameter.length`. So `CC` is a road of straight → arc → straight → arc → straight, and the straights are not extra blocks. `turn_pairs` reports one letter per *arc*, which is the useful reading but not a whole block. *Enforced in:* `categories.py`'s `curve` description and the generated `destinations.md` both say so. |
| Read a turn direction off a map picture | A map alone does not say which end the ego starts at, and read from the wrong end every turn in it reverses. The ego always spawns heading **due east** — rightward in any figure — but where that lands in the frame moves with the seed: `curve` seeds 2 and 3 start at the *top* and bend down, 0, 1 and 4 start at the bottom and bend up. A map-only thumbnail was shipped and immediately misread as "no right turns in the bank", when seed 2 turns right twice. *Enforced in:* thumbnails draw the route, a spawn arrow and a destination star (`figures.render_route`); `net_rotation_deg` and `turn_pairs` are recorded per scenario; `test_bank.py` asserts two categories on one road cannot produce the same picture. |
| Defend seed identity by hashing config keys, or by fingerprinting the built map | Both rejected, for different reasons. The config-key audit cannot be proven complete — `curriculum_level`, which silently rewrites the seed you asked for, was missing from the first draft. Fingerprinting the output *would* have worked, but **cross-batch identity is not a goal**: a bank is regenerated per batch and a road that comes out different is simply a different batch. `map_id` and `config_hash` are both cut (2026-08-31); the container's pinned commit is the whole guarantee. |

One more that is not a trap but is easy to over-build: **`crash_human` termination is already wired
and free** — `TerminationState.CRASH_HUMAN` (`constants.py:28`), `crash_human_done=True` by default
(`metadrive_env.py:93,190`). Hitting a pedestrian ends the episode as a failure with no work from
us. What is missing is only the reward/cost pair: there is no `crash_human_penalty` and no
`crash_human_cost` beside the vehicle and object ones (`metadrive_env.py:74-83`). That is ~10 lines
mirroring `crash_object`'s 5.0 / 1.0, and it is already a Phase 4 build item.

---

## No lidar — what this pins down

**Decision: this bank runs with lidar disabled.** That is not a cosmetic setting; it changes the
observation, so it is part of scenario identity.

`base_config` must set, explicitly rather than by relying on defaults:

```python
agent_observation = StateObservation          # explicit; not LidarStateObservation
vehicle_config = dict(
    lidar             = dict(num_lasers=0, distance=0, num_others=0),
    side_detector     = dict(num_lasers=0, distance=50),
    lane_line_detector= dict(num_lasers=0, distance=20),
)
```

Three things worth knowing about that block:

1. **`num_lasers=0` is a real off switch, not a zero-length array.** `LidarStateObservation`
   (`obs/state_obs.py:176-178`) only adds lidar dims when `num_lasers > 0 **and** distance > 0`.
   Setting either to 0 drops the whole term. Naming `StateObservation` as `agent_observation`
   is belt-and-braces, and it makes `base_config` self-documenting for your colleague.
2. **`side_detector` and `lane_line_detector` are *not* lidar at their defaults.** At
   `num_lasers=0` they contribute 3 scalars computed geometrically — distance to the yellow line,
   distance to the sidewalk, and lateral offset in lane (`state_obs.py:145-162`). Set either
   above 0 and they *become* raycasting lidars and the obs grows. Pin both at 0, so nobody
   reintroduces lidar through the side door.
3. **Resulting observation: `Box(19,)`** — 6 ego + 10 navi + 3 detector scalars. Your original
   figure was right; it was only wrong against the MetaDrive *default*, which ships 240 lasers.

**Amended (2026-08-30, corrected): `Box(19,)` is what the runner sees too.** An earlier amendment
here claimed the AV3 path replaces the observation with `{"image", "state"}` and a 41-number state.
Both halves are wrong, and the source says why:

- `ImageStateObservation` composes a **plain `StateObservation(config)`** (`obs/image_obs.py:27`) —
  it is not a different state vector. Under our pinned config its state half would be the same
  6 + 10 + 3 = **19**. There is no 41 anywhere on our path.
- More decisively, **`agent_observation` wins over `image_observation`.** `base_env.py:674-678`
  checks `config["agent_observation"]` *first*, and only falls through to
  `ImageStateObservation` vs `LidarStateObservation` if it is unset. We pin
  `agent_observation = StateObservation` above, so the observation stays a bare `Box(19,)` with
  **no `"image"` key at all**.

**Decision: keep `agent_observation` pinned.** The AV3 rig reads its cameras directly, through
`CameraRig.read()` → `sensor.perceive()`, never through the env observation — so the dict buys
nothing, and `ImageObservation.observe()` would roll a 3-deep stack of a full camera frame every
step (`image_obs.py:81-88`) for data nothing reads. Reversible in one config key if you later want
the dict.

**So the `(19,)` assert stays on the runner path**, identical to Phase 0's `doctor`. The model under
test is still a camera model — the cameras simply do not arrive through the observation. The one
thing `image_observation=True` is still needed for has nothing to do with observations; see
**Phase 4 — the model boundary**, gotcha 1.

### The observation ceiling — read this before the options section

`obs/state_obs.py:36-56` documents the full 19 dimensions: distance to left/right lane line,
heading error, speed, steering, throttle, yaw rate, lateral position — then two checkpoints' worth
of route geometry.

**No channel carries another object.** Not a vehicle, not a cone, not a barrier, not a pedestrian,
not a cyclist, not a light. With lidar off, a state-vector policy drives blind to everything except
road markings and its own route.

**Therefore every option on the difficulty form is camera-only.** This is a consequence of the
no-lidar decision already taken, not a new constraint, but it governs the whole feature rather than
any single axis. It goes in `CONTRACT.md` as one statement at the top of the options section, so
nobody reads a state-vector model's collision result as a model defect.

**And therefore the model under test is a camera model** (amended 2026-08-30). The sentence above
was written as a caveat; it is really a specification. A state-vector policy cannot perceive a cone,
a barrier, a pedestrian, a cyclist or a light, so it cannot be scored on five of the six axes this
bank exists to vary. The evaluated models are AV3 checkpoints driving from the six-camera rig. See
**Phase 4 — the model boundary**.

**Consequence for the reference policy (Phase 4).** MetaDrive's bundled PPO expert cannot consume
a 19-dim observation — but it does not need to. `examples/ppo_expert/numpy_expert.py:39-62` takes
the **vehicle**, not the env obs, and builds its own 275-dim lidar observation internally: it
temporarily overwrites `vehicle.config` with a forced `expert_obs_cfg` (240 lasers, `num_others=4`),
observes, then restores. So the expert still works as the Phase 4 ceiling with lidar off. Two
caveats that follow from it:

- Our `ExpertPolicy` wrapper must hold the env and **ignore** the `observation` argument, keeping
  the `policy(observation) -> [steer, throttle]` signature intact. Document it as a diagnostic
  reference, not as an example of the contract.
- `numpy_expert.py:48-49` carries a TODO admitting the config restore is incomplete. Phase 4 must
  assert `env.observation_space.shape == (19,)` **after** an expert episode, not just before —
  cheap, and it catches the expert leaking 240 lasers back into the env config.

---

## Scenario options

> **Every option below is camera-only.** See "The observation ceiling" above. A state-vector policy
> cannot perceive any of it.

### The form

```
Traffic       [ none | low | medium | high ]
Cones         [ none | low | medium | high ]
Barriers      [ none | low | medium | high ]
Pedestrians   [ none | low | medium | high ]
Cyclists      [ none | low | medium | high ]
Lights        [ none | low | medium | high ]
```

A run is one permutation of that form applied across whichever maps are ticked.

### Stored normalized, applied at run time

**Bank (35 rows per batch):** `category`, `seed`, `block_seq`, `destination`,
`spawn_lane_index`, `max_steps`, thumbnail. Written once per batch, then disposable.

**Options (per run):** the six axes. They are never part of a bank **row** — and since
2026-09-04 a bank *does* carry them, once, at the top of its manifest (`manifest.options`, schema
1.2, `scenariobank options`).

The reversal is narrower than it looks, and rests on a distinction this section had not drawn:

| | what it is | changing it costs |
|---|---|---|
| `manifest.base_config` | generation **truth**: `traffic_density: 0.0`, `accident_prob: 0.0`, and they stay there | rebuilding every scenario and every thumbnail |
| `manifest.options` | declared **intent** for runs of this bank | one field of one JSON file |

Pinning options at *generation* time would bake the cross-product this section rejects below, and
would make changing a traffic level a reason to regenerate 35 scenarios. Pinned as intent, it is a
manifest edit in `budget`'s class: nothing is built, no row moves, and `base_config` still records
what generation actually used. The pin is a **default, not a lock** — a run flag overrides it, and
the result records the expanded levels, which is what keeps Phase 4b's single-axis sweep possible.
Per-category and per-scenario overrides are deliberately not built, for the cross-product reason
below; the resolver is `options_for(entry, row)` from the start so adding one later changes no
call site.

**Results:** each row carries the map row *and* the options fully expanded — level name *and*
resolved numeric — so a result is self-describing without the options file beside it.

This does not constrain the frontend. A picker that wants a tile per combination can expand the
cross product client-side; a bank baked per-combination could never be collapsed back. Thumbnails
would be identical across combinations anyway, because the map renderer draws no objects —
`top_down_renderer.py:68-96` iterates only `map.get_map_features()` and then returns, so no vehicle,
object, light, ego or route is ever drawn.

**Why it is sound:** managers hold independent RNG streams, all re-seeded from the same episode seed
every reset (`base_engine.py:562-569`). `PGMapManager` has `PRIORITY = 0` and its own `np_random`,
so map geometry and the ego route are unaffected by how many vehicles or cones other managers place.
It holds structurally — `Randomizable.__init__` hands every manager its *own* generator — but
`tests/unit/test_invariance.py` asserts it anyway, because the obstacle and VRU managers are ours.

One coupling to preserve: `traffic_manager.py:253` excludes `object_manager.accident_lanes` from
vehicle spawning. Our obstacle manager must publish the same attribute and carry `PRIORITY = 9`
(mirroring `TrafficObjectManager`), so traffic still avoids spawning on top of an obstacle field.

**Corrected 2026-09-04, after reading the pinned commit:** "our obstacle manager" is mostly not
ours. `TrafficObjectManager` (`metadrive/manager/object_manager.py`) already *is* it — `PRIORITY =
9`, driven by `accident_prob`, drawing every choice from `self.np_random`, publishing
`accident_lanes`, placing `TrafficCone`/`TrafficBarrier`/`TrafficWarning`, with `get_state`/
`set_state` for record-replay — and `metadrive_env.py:296-300` already registers it only when
`accident_prob > 1e-2`. Traffic is likewise `PGTrafficManager` entire. So the reproducibility
argument above rests on MetaDrive's code at the pinned commit for four of the six axes, exactly as
the map does. **`actors.py` is the only placement code that is ours**, because MetaDrive ships
`Pedestrian` and `Cyclist` as objects but nothing that decides where they walk on a PG map: there
is no pedestrian policy in `policy/`, and the only manager that spawns them replays a logged
trajectory from a recorded dataset, which a procedural road does not have. See `options.py` for
the full survey.

### The levels

```python
LEVELS = {                       # PROVISIONAL — calibrated in Phase 4b
    "traffic":     {"none": 0.0, "low": 0.05, "medium": 0.15, "high": 0.35},
    "cones":       {"none": 0,   "low": 1,    "medium": 3,    "high": 6},
    "barriers":    {"none": 0,   "low": 1,    "medium": 2,    "high": 4},
    "pedestrians": {"none": 0,   "low": 1,    "medium": 3,    "high": 6},
    "cyclists":    {"none": 0,   "low": 1,    "medium": 2,    "high": 4},
    "lights":      {"none":   None,                      # no lights spawned
                    "low":    dict(cycle=60, green=40),  # mostly green
                    "medium": dict(cycle=40, green=20),
                    "high":   dict(cycle=30, green=10)}, # frequently red
}

TIERS = {   # convenience aliases; resolved before the run and recorded expanded
    "easy":   dict(traffic="low",    cones="none",   barriers="none",   pedestrians="none",   cyclists="none",  lights="none"),
    "medium": dict(traffic="medium", cones="low",    barriers="low",    pedestrians="low",    cyclists="none",  lights="low"),
    "hard":   dict(traffic="high",   cones="medium", barriers="medium", pedestrians="medium", cyclists="low",   lights="medium"),
}
```

The six axes are the contract; tiers are aliases only. Explicit flags win: `--tier hard --cones none`
resolves to hard everywhere except cones. Keep raw values reachable — `--traffic medium` and
`--traffic-density 0.15` must both work, because house style in the converter repo is raw values.

Objects are placed along the **ego's route lanes** (`navigation.checkpoints`), not per block. That is
precisely what makes them work on intersection categories, which is the entire reason for writing a
custom manager instead of using `accident_prob`.

### Traffic, in detail

`traffic_density` is a **fill fraction, not a count** — one vehicle per `VEHICLE_GAP = 10 m` of
lane, applied per block in the default Trigger mode (`traffic_manager.py:35`, `:258-260`).

- `none` must be **exactly `0.0`** (see Phase 4b for why the number below it is unusable).
- Above ~1.0 it saturates against the spacing cap (`:265`); it never errors.
- Vehicle mix is hardcoded `[0.2, 0.3, 0.3, 0.2, 0.0]` (`:303-306`) — not configurable.
- Keep `traffic_mode = Trigger` (default). Respawn/Hybrid replenish vehicles as they leave, so
  exposure becomes episode-length-dependent and two policies that take different times through the
  same scenario meet different amounts of traffic. Trigger is the comparable one.

### Traffic lights, in detail

**Nothing exists.** Confirmed from MetaDrive source and independently from your converter repo,
which investigated this in August (`tools/signal_control.py:15-16`,
`src/osm_scenario/signal_plan.py:3-7`, `docs/reference/ego-route-and-signals.md:236-241`).

- No light manager is registered by `MetaDriveEnv` (`metadrive_env.py:292-300`); the string "light"
  appears zero times there and under `component/pgblock/`.
- `ScenarioLightManager` is hard-bound to `data_manager` and `DYNAMIC_MAP_STATES`, which a PG run
  does not have. **Pre-recording a tape does not help** — the tape is not the missing piece; the
  light itself and the scoring are.

**But two things make it cheap for a camera model, which is why it is Phase 8 and not a refusal:**

1. **3D rendering works fully** — four gltf models (`base_traffic_light.py:63-73`). A camera policy
   sees a red light natively. The observation problem exists only for state vectors.
2. **The violation flag already exists and is already correct.** `_state_check` sets
   `vehicle.red_light = True` on ghost contact with the light's wall (`base_vehicle.py:770-785`),
   and green sets `CollisionGroup.AllOff` while red/yellow set `InvisibleWall` — so contact happens
   *only* when crossing on red or yellow. The flag is written and read by nothing.

Lights are also the only thing that separates conflicting movements: your converter measured that
MetaDrive's IDM has **no give-way rule** — "IDM brakes only for cars on its own lane, so a junction
full of it collides." That matters for Phase 4b.

### New modules

**`src/scenariobank/obstacles.py`** (cones, barriers) — *rewritten 2026-09-04. Do not write a
manager from scratch: MetaDrive already ships this one, and the description below was a restatement
of it.*

`TrafficObjectManager` (`metadrive/manager/object_manager.py`) is `PRIORITY = 9`, is driven by
`global_config["accident_prob"]`, draws every choice from `self.np_random`, publishes
`accident_lanes`, places `TrafficCone` / `TrafficBarrier` / `TrafficWarning`, and has
`get_state`/`set_state`. `metadrive_env.py:296-300` already registers it **only** when
`accident_prob > 1e-2` — the conditional registration Phase 4's build list proposed to invent. So
the cones and barriers axes are a number, not a manager.

The one thing to write is a **subclass**, and only to separate the two axes: stock MetaDrive drives
both from `accident_prob` and splits internally at `PROHIBIT_SCENE_PROB = 0.67` between a cone
corridor and a barrier/breakdown scene. Override `reset()` to choose `prohibit_scene` (cones) or
`barrier_scene` (barriers) per axis; the placement maths is reused whole.

Three facts to carry in rather than rediscover:

- `break_down_scene` **spawns a vehicle**, so `cones` above `none` puts cars on the road even at
  `traffic=none`. Phase 4b's calibration has to account for that or the axes are not independent.
- Accidents are placed only on `Straight`, `Curve`, `InRampOnStraight`, `OutRampOnStraight`
  (`object_manager.py:51-53`) — the cause of the `X`/`T`/`O` trap already recorded in **Traps**.
- Its own floor is `abs(accident_prob) < 1e-2`, the same shape as the traffic one.

The old note that `accident_prob` "stays `0.0` permanently, so `TrafficObjectManager` is never
registered" is reversed: it stays `0.0` in **`base_config`**, which is generation truth, and a run
sets it from the resolved level.

**`src/scenariobank/actors.py` — `VRUManager(BaseManager)`** (pedestrians, cyclists)

**The only placement code in this project that is ours**, confirmed 2026-09-04. MetaDrive ships the
objects — `Pedestrian` and `Cyclist` (`component/traffic_participants/`), with physics bodies,
models and `set_velocity` — and nothing that decides where they walk on a PG map: `policy/` holds
no pedestrian policy, and `ScenarioTrafficManager.spawn_pedestrian` attaches
`ReplayTrafficParticipantPolicy` to a logged `track` from a recorded dataset, which a procedural
road does not have. (MetaUrban's `humanoid_manager.py` does have ORCA-planned crowds, but it is a
hard fork under its own namespace pulling torch, stable_baselines3, cv2, scipy and skimage —
adopting it means swapping simulators, not adding a manager.)

Two consequences for reproducibility, which for this axis alone rests on code we can change:

- **A determinism test ships with the manager**: two envs at the same seed and level produce
  identical actor spawn positions and patrol endpoints. The actor-side twin of the map's existing
  test, and the thing that catches a reordered draw in `reset()` moving every actor silently.
- **The result row carries an actor-layout digest** — `fingerprint.sha256_hex` over the sorted
  spawn positions, a sibling of `lane_geometry_digest`. In the **result**, never in the bank: it is
  a measurement of a run, and it turns "the actors were the same" into something checkable months
  later, for the same reason `drive_side` is measured rather than asserted.

- Spawns in `reset()`; steers in `after_step()` via
  `obj.set_velocity([1,0], speed, in_local_frame=True)` — the API MetaDrive's own
  `test_pedestrian.py:38-40` uses. `Pedestrian.SPEED_LIST = [0.4, 1.2]` with a walk animation keyed
  to speed.
- **No walking behaviour ships** — constant velocity in a straight line, no avoidance. Patrol
  between two fixed endpoints per actor, chosen once at reset from `self.np_random`. The turnaround
  is a position comparison, so `after_step` consumes no randomness and the episode stays
  reproducible.
- Wire `crash_human_penalty` / `crash_human_cost` to mirror `crash_object`'s 5.0 / 1.0. Termination
  is already free (`crash_human_done=True`); only the reward/cost pair is missing
  (`metadrive_env.py:74-83`).

**`src/scenariobank/lights.py` — `PGTrafficLightManager(BaseManager)`** — see Phase 8.

> The RNG requirement here is the **opposite** of the converter's live-signals case, where
> `signal_control.py` deliberately keeps its own `RandomState` *because* `self.np_random` gives the
> same value every time a seed comes round. That repeatability is exactly what we want.

---

## Decisions locked

- **Repo**: new standalone git repo in `metadrive-PG/`. Conventions copied from the converter repo (uv, Typer, `src/` layout, `[project.scripts]`), no shared code.
- **Frontend**: **two producers onto one queue** — the wing-sim webapp via a `/metadrive` section, and our own studio (Phase 2c, Step 12). `run_bank(...)` stays importable and CLI-free, so the container and the studio's worker are two callers of one core. *(Amended 2026-08-30 — was "CLI + `results.json`", with Phase 7 optional. Amended 2026-09-01 — the queue moved to the NAS and there are now two rigs and two orchestrators; see **How this ships**. Amended 2026-09-02 — the studio submits runs too, so the webapp is no longer the only way onto the queue.)*
- **The GUI is the surface; the CLI is the engine.** No authoring workflow requires a terminal. The CLI is not deleted because a subprocess is forced by `BaseEngine.singleton` — see **What a person actually uses** for the reasoning, so this is not reopened as a matter of preference. *(Decided 2026-09-02, Keith.)*
- **Categories**: the seven as drafted.
- **Seeds**: fixed at 0, 1, 2, 3, 4 for every category. 35 maps.
- **Options**: six axes, stored normalized, applied at run time.
- **Handedness**: **left-side traffic** (right-hand-drive market — Singapore, UK, Malaysia, Japan). This is not a MetaDrive setting; see **Traps**. Enforced by `handedness.install()`, called from `base_config()` so no caller can forget it, and **measured** rather than asserted by Phase 0 `doctor` and Phase 2 `generate`.
- **Policy**: the AV3 camera adapter is the contract from Phase 0, not adapted in later. *(Amended
  2026-08-30 — was "build against the state-vector callable now". Reversed because a state-vector
  policy cannot perceive five of the six option axes, so it could never have been the thing scored.)*
  `ConstantPolicy` and `ExpertPolicy` survive as internal diagnostics, never a product surface.
- **Independence (R1)**: no module of Tyrone's `wing-sim` is ever imported. Shared: the `jobs` table
  (a schema) and the GPU lock path (a path). See **How this ships**.

---

## Target layout

```
metadrive-PG/
  pyproject.toml            # uv, requires-python >=3.10,<3.11, [project.scripts] scenariobank=...
  uv.lock
  src/scenariobank/
    __main__.py             # `python -m scenariobank` — how the studio and the container spawn a job
    cli.py                  # the worker's entry point, not the product's front door. One Typer app:
                            #   doctor categories sockets inspect destinations examples
                            #   generate seeds replace commands studio
                            #   calibrate run selftest schema validate
    categories.py           # CATEGORIES dict: block_seq, destination, max_steps, description
    options.py              # LEVELS, TIERS, resolve_options() -> expanded dict
    config.py               # base config builder
    fingerprint.py          # sha256_hex, lane_geometry_digest (is it the same road?)
                            #            road_shape, shape_gap  (how different is it?)
    variety.py              # scan(): rank candidate seeds; closest_pair(): the least
                            #         distinct pair a sequence already has
    bank.py                 # generate(): the maps + thumbnails, and the pydantic Manifest
                            #             models that describe what it wrote
    workspace.py            # Phase 3 — reads a converter workspace: manifest, every dataset
                            #   directory found by walking, the rate measured off `ts`, the route
                            #   and who is recorded in it. Imports neither the converter nor
                            #   MetaDrive, and reads its pickles through an allowlist.
    importing.py            # Phase 3 — writes docs/reference/importing.md: the checklist an
                            #   import must satisfy, generated against workspace.py's own report
                            #   so a field the reader gains and the checklist forgets is an error
    obstacles.py            # ObstacleManager  — cones, barriers
    actors.py               # VRUManager       — pedestrians, cyclists
    lights.py               # PGTrafficLightManager  (Phase 8)
    runner.py               # run_bank(...) -> Results   (importable, no CLI deps)
    results.py              # pydantic models: Results, ScenarioResult, Summary
    policies.py             # ConstantPolicy, ExpertPolicy wrapper, load_policy("pkg.mod:Name")
    env.py                  # build_env(base_config, seeds, options) + start_seed/num_scenarios math
    av3/                    # ported from the converter (Phase 4). Ordinary modules, one interpreter.
      camera_rig.py         #   load_rig(), CameraRig.sensors/mount/read
      av3_model.py          #   AV3Model.observe/predict_with_navigation, FrameHistory, preprocess
      openpilot_policy.py   #   BridgeConnection, OpenpilotDriver, to_metadrive_action
    web/                    # Phase 2c — the studio: THE product surface. Never imports MetaDrive,
      api.py                #   because the engine is a per-process singleton, so every simulation
      jobs.py               #   is a subprocess. One job at a time; state read back off its log.
      invoke.py             #   builds a job's argv and validates it against the CLI's own flags,
                            #   so the page cannot offer a flag the program does not accept
      static/index.html     #   the whole frontend, one file, no build step
    nas/                    # Phase 7, NAS side — our MetaDrive orchestrator (R1: imports
      orchestrator.py       #   nothing of wing-sim's). The lease loop: consume, plan, call,
                            #   extend, save, ack. Busy is a nack, never a failure.
      rigs.py               #   the rig client + the GPU plan. The plan may be stale; the
                            #   rig's lock is the truth.
      results.py            #   our SQLite + results tree. Per-scenario rows; idempotent ingest.
      options.py            #   GET /options: the six axes as data, for the frontend's form
      queue_client.py       #   vendored verbatim from docs/queue-docs/. Do not reimplement.
    rig/                    # Phase 7, rig side — the service the orchestrator calls
      service.py            #   POST/GET/DELETE /runs, GET /health. Idempotent on job_id.
      lock.py               #   flock per GPU, holder file kept separate, /proc/locks check
      session.py            #   take lock, launch sibling container, supervise, tear down
      supervise.py          #   log file + exit-code file. No state held anywhere.
      archive.py            #   evidence, written before the run touches anything
  rigs/av3.txt              # the six AV3 cameras, ported from the converter
  docker/studio.Dockerfile  # the studio image: FROM metadrive-wingfin-sim, plus the web group
  compose.yaml              # `run` on the reused sim image, `studio` on ours. No build for `run`.
  scripts/bank-check.sh     # ruff -> pytest
  scripts/sim-image.sh      # is the base image here, and does its label carry what we need
  tests/
  docs/reference/
    destinations.md         # the resolved destination socket per category (Phase 1)
    level-calibration.md    # the Phase 4b sweep
  CONTRACT.md
  banks/pg-bank-2026-08/
  .studio/                  # Phase 2c job logs and scratch figures. Gitignored, disposable.
```

---

## Reading the markers

Every phase heading carries one, and so does every step inside Phase 2c, Phase 3, Phase 4,
Phase 5 and Phase 7:

| marker | means |
|---|---|
| ✅ | built, tested, and on `main` |
| 🔨 | started — the phase's **Status** line says what is left |
| ⬜ | not started |

The headings are the only record of progress in this document. There is deliberately no summary
table up here: a second copy of the status is a second thing to forget to update, and this plan
exists to stop two descriptions of one system drifting apart. The board, whenever you want it:

```bash
grep -n '^# Phase\|^### Step' IMPLEMENTATION_PLAN.md
```

A marker moves to ✅ when the phase's **Done when** is met — not when the code is written.

---

# Phase 0 — Skeleton and environment truth ✅

**Goal:** a package that installs, a CLI that runs, and one command that tells you exactly which
simulator you are on. Nothing scenario-specific yet.

**Build**
- `pyproject.toml` mirroring the converter's (uv, py3.10, Typer, pydantic, structlog, ruff, pytest).
  Pin `metadrive-simulator @ git+https://github.com/metadriverse/metadrive.git@85e5dadc` — the
  **same commit** the converter pins. A different commit means a different bank.
- `scenariobank doctor` prints: `metadrive.version.VERSION`, `constants.EDITION`,
  `importlib.metadata.version("metadrive-simulator")`, the resolved git SHA from the dist's
  `direct_url.json`, `asset_version()`, and versions of numpy / shapely / opencv / panda3d / pygame.
- `doctor` also builds a throwaway env from the canonical `base_config` and prints
  `env.observation_space` and `env.action_space`. **Expect `Box(19,)`** — this is the earliest
  point at which a stray lidar setting becomes visible, and it costs one reset.
  **The runner asserts the same `Box(19,)`.** An earlier note here claimed cameras turn the
  observation into a `{"image", "state"}` dict and cost `doctor` its signal; they do not — pinning
  `agent_observation = StateObservation` overrides `image_observation` outright
  (`base_env.py:674-678`). So `doctor` and the runner test one shape, which is the point of having
  `doctor` at all. See **No lidar**.

**How you test it** *(recorded as it was done, before the studio existed. The same question is now
answered by the commit prefix in the studio's header — Phase 2c, Step 1.)*
```bash
cd metadrive-PG && uv sync --group sim
uv run scenariobank doctor
```
- **Expect:** `EDITION: MetaDrive v0.4.3`, `dist: 0.4.3`, `commit: 85e5dadc...`, a non-empty
  `asset_version`, `obs_space: Box(19,)`, exit 0. If it prints `Box(259,)` the lidar block did not
  take.
- **The point of this phase:** if `commit:` prints `None` or a different SHA, stop — every later
  phase is built on a simulator you cannot identify.
- **Negative test:** `uv run scenariobank doctor --require-commit deadbeef` must exit non-zero.

**Done when:** `doctor` output pasted next to the converter container's `doctor` output shows
identical commit + asset_version.

---

# Phase 1 — Categories and forced destinations ✅

**Goal:** decide, once and permanently, which exit each category drives to — then never let
MetaDrive choose again.

**What changed and why.** The draft classified turns *after the fact*: reset a seed, measure the
angle between spawn heading and final-lane heading, keep the seeds that came out above +30 degrees,
and eyeball 35 of them to check the heuristic. All of that is gone. `vehicle.config["destination"]`
(`node_network_navigation.py:60`) lets us *specify* the exit instead of discovering it, which turns a heuristic label
into a stored fact and makes the same five seeds reusable across every category.

**Build**
- `handedness.py` — **mirror the PG geometry layer about the x-axis, before any map is built.** MetaDrive drives on the right and offers no way not to (see **Traps**), so this is where the market is decided. Three sign changes and no others: negate `StraightLane.direction_lateral` (positive lateral becomes the vehicle's left, which walks the whole map — opposing carriageway, lane lines, sidewalks — to the other side, because all of it is placed off lane frames); invert `clockwise` on every `CircularLane` (this is what makes roundabouts circulate clockwise); and invert the **three** `is_clockwise()` sites in `create_pg_block_utils` that use it for *lateral* arithmetic rather than for arc direction (`:130`, `:271`, `:339`), which flip a second time so the two cancel. Installed from `base_config()`; idempotent. Rewrites those three lines from the module's own source and raises `HandednessError` if they are not found verbatim — the commit pin exists so MetaDrive's internals cannot move under us, and a patch that silently stopped applying would leave a working bank that is simply the wrong market.
  - **Test it by exactness, not by plausibility.** A reflection is an isometry, so the mirrored map must be the *same road*: same node names, same lane count, same lane lengths, same radii. Build each block sequence twice — once here, once in a **subprocess that never imports `scenariobank`** so MetaDrive is unmodified there — and assert they agree lane by lane once one is reflected. That is the only honest way to check a monkey-patch of a global layer: it cannot be uninstalled, so the reference has to come from somewhere the patch never reached. It is also the check that earns its keep — an earlier version inverted the arcs but not the sibling-lane lateral arithmetic, and every node name, every lane count and every picture still looked right. The only symptom was that curved lanes came out the wrong length.
  - Consequence for this phase's table: mirroring swaps which physical exit an angle rule selects. `intersection_left` is now the **near** turn and `intersection_right` is the one that crosses oncoming; `roundabout` takes `RIGHT` to keep the long way round.
- `CATEGORIES` — one dict, one entry per category:

  | category | block_seq | destination | seeds |
  |---|---|---|---|
  | `intersection_left` | `X` | left exit socket | 0–4 |
  | `intersection_right` | `X` | right exit socket | 0–4 |
  | `intersection_straight` | `X` | far exit socket | 0–4 |
  | `t_junction` | `T` | chosen exit socket | 0–4 |
  | `roundabout` | `O` | chosen exit socket | 0–4 |
  | `curve` | `CC` | terminal socket | 0–4 |  ← two blocks **on purpose**: see below
  | `ramp_merge` | `rS` | terminal socket | 0–4 |

  Each entry also carries `description` and `max_steps` (roundabout needs more than curve).
- **`curve` is `CC`, and the second block is the point.** The two `Curve` blocks draw radius, arc
  and **direction** independently (`pg_space.py:284-289`), so a seed picks a *pair*, and seeds 0–4
  cover all four of `LL`, `LR`, `RR`, `RL` — two lefts and two rights are not enough on their own.
  Net rotation runs +67.5° to +239.5°, so two of the five sweep past a U-turn; that is a consequence
  of the pairing, not a defect. The coverage currently holds by luck — fixed seeds against
  MetaDrive's own RNG — so `test_categories.py` asserts it: a simulator bump that shifted the draw
  would collapse it to three combinations while every road still built and every route still
  resolved. Contrast `rS`, the other two-block sequence, where the trailing `S` exists so the route
  continues *past* the merge point rather than ending on it.

- **Validate every `block_seq` character on load**, against the keys of
  `BLOCK_TYPE_DISTRIBUTION_V2` (`blocks_prob_dist.py:22-41`) — the valid set is exactly
  `C S r R X T O f F y Y P $ B U`. `I` is the first block and is auto-prepended, so it is never
  written in a sequence. All seven categories above use valid chars; `"X"` resolves to
  `StdInterSection`. A typo'd char is otherwise a confusing failure deep inside the generator.
- `scenariobank sockets --block-seq X --seed 0` — a **one-off discovery command**. Resets once,
  enumerates the destination block's sockets, and for each prints the node name plus the turn angle
  it implies:
  ```python
  start = env.agent.heading_theta
  lane  = road.get_lanes(map.road_network)[-1]
  end   = lane.heading_theta_at(lane.length)
  deg   = np.degrees(wrap_to_pi(end - start))    # metadrive.utils.wrap_to_pi
  ```
  `heading_theta_at` does **not** wrap (circular lanes can exceed +/-pi), hence `wrap_to_pi`.
  Heading is counter-clockwise-positive, so **positive == left** (`straight_lane.py:56`).
  This code exists to *choose* the socket names once, not to run at generation time.
- Paste the chosen node names into `CATEGORIES` and record the table in
  `docs/reference/destinations.md`, with the angles measured.
- The three intersection categories deliberately share the same five maps and differ only in route —
  the same junction driven three ways.

**How you test it** *(recorded as it was done. The same questions are now answered by the
gallery's example figures — Phase 2c, Step 4 — and the utility tab, Step 11.)*
```bash
# 1. Discover the sockets (once per block type)
uv run scenariobank sockets --block-seq X --seed 0
```
**Expect:** three or four rows, one clearly near +90 (left), one near -90 (right), one near 0
(straight). If two sockets have the same sign and similar magnitude, the block is not what you think
it is — stop and look at it before pinning anything.

```bash
# 2. Socket names are stable across seeds (they must be, with random_lane_num off)
for s in 0 1 2 3 4; do uv run scenariobank sockets --block-seq X --seed $s | md5sum; done
```
**Expect:** five identical hashes. If not, the destination cannot be a per-category constant and
must move into the per-scenario manifest row instead.

```bash
# 3. Confirm visually — a much smaller job than the old eyeball gate
uv run scenariobank inspect --category intersection_left --seed 0 --render
```
**Expect:** the route drawn through the junction turns left. Repeat for right and straight on seed 0
only; because the destination is forced rather than sampled, confirming one seed confirms the
category — the remaining seeds change the geometry, not the turn.

**Done when:** `docs/reference/destinations.md` lists a node name and a measured angle for all seven
categories, and the three intersection variants visibly turn the right way on seed 0.

---

# Phase 2 — Generate: scenarios on disk ✅

**Goal:** one command turns the categories into scenarios a runner can consume.

**Scope, decided 2026-08-31.** A bank is a **disposable, per-batch artifact**. It is regenerated
whenever someone wants scenarios, and *nothing* checks that a road matches a previous run's — if it
comes out different, that is a different batch, and that is fine. The container pinning one
MetaDrive commit is what makes a single batch self-consistent; that is the whole guarantee.
*(Amended 2026-08-31 — `map_id`, `config_hash`, the commit gate and the entire Phase 3 `verify`
command are cut. They existed to prove cross-batch identity, which is not a goal. Roughly two
phases of work, deleted rather than corrected.)*

**Build**
- `base_config` built once and stored **in full** in the manifest, so a run is self-describing:
  `use_render`, `agent_observation`, the whole lidar/detector block, `traffic_density`,
  `random_traffic`, `accident_prob`, `random_spawn_lane_index`, `horizon`, `log_level`. Types are
  stored as dotted paths (`agent_observation` → `metadrive.obs.state_obs.StateObservation`), so it
  is a *record* of the config and not a config to load back. `map`, `start_seed` and
  `num_scenarios` are stripped: they vary per block sequence and are already recorded per category
  and per scenario, so leaving them in would put two answers in one file.
  *(Amended 2026-08-31 — `random_spawn_lane_index` was `False`. Reversed after measuring that it is
  the only thing distinguishing the five `X` seeds, which build one identical road.)*
  *(Amended 2026-08-31 — this bullet used to list `curriculum_level=1`, `random_lane_num=False`,
  `random_lane_width=False`, `store_map=True` and `navigation_module`. `base_config` sets none of
  them; the list was a draft's wish, not a reading of the code. Corrected against what the
  manifest actually contains.)*
- Record the resolved MetaDrive dist version, git SHA and `asset_version()` **as information**. They
  explain a result months later; nothing refuses on them.
- Per scenario record `destination` and `spawn_lane_index`, so the runner can set
  `vehicle_config["destination"]` and bypass `auto_assign_task` entirely.
  **`destination` is per scenario and cannot be per category** — Phase 1's check 2 above
  anticipated this and it came true: `StdTInterSection` exposes a different arm depending on the
  seed, so `t_junction` resolves to `1T0_1_` on seeds 0, 1 and 4 and `1T2_1_` on 2 and 3. The
  category level carries the `exit_rule` the node was resolved *from*; the node itself is a row.
- **Seeds are a default, not a constant.** `--seeds` overrides them, per category if wanted
  (`--seeds curve=0,1,2,3,22`), and `scenariobank replace` swaps one scenario's seed in a bank
  that already exists — the bank keeps its size, its ids and its numbering. This matters because
  **seed 4 is a poor draw for two categories**: 7% from seed 0 for `curve`, not measurably apart
  for `roundabout`. `SEEDS` stays `(0,1,2,3,4)` because that is the set every figure in this repo
  was measured at; `scenariobank seeds` ranks the alternatives on demand.
  *(Amended 2026-09-01 — was a fixed constant. Changed after the near-duplicate was found by
  looking at two thumbnails, not by any check here.)*
- Assert every requested seed builds for every category. Map generation is a **backtracking search**
  (`BIG.py:91-103`), so a requested block sequence can simply fail to plug in for a given seed. With
  seeds fixed at 0–4 that is a hard failure, not a scan: **fail loudly** and record the substitute
  seed explicitly in the manifest rather than shifting silently.
- **Assert the drive side, measured from the map** — not read off `handedness._installed`. Reuse
  `doctor.measure_drive_side`. This is the one check kept from the old `verify`, and it is not a
  reproducibility check: if the mirror fails to install, every map builds right-side, every manifest
  field stays correct, every thumbnail still looks like a road, and the only symptom is that a
  right-hand-drive model fails everything for reasons no result explains.
- Thumbnails: `figures.render_route` into `thumbs/`, one **per scenario** — the road in grey,
  the driven route in red, a blue arrow at the spawn and a green star at the destination,
  titled with the category, seed, rule, node, rotation and length.
  *(Amended 2026-09-01 — was `draw_top_down_map` → `cv2.imwrite`, map-only and therefore one
  image per **map**. Two failures, both found by looking at the output: the three `X` categories
  at one seed wrote three byte-identical files, and a map with no spawn marker cannot say which
  way it turns — the `curve` thumbnails were read from the wrong end and reported as having no
  right turns, when seed 2 turns right twice. Reusing Phase 1's route figure fixed both and is
  cheaper per image; the cost is the filled road-surface look.)*
- Write `manifest.json` **last and atomically** (temp file + `os.replace`).
- `scenario_id` is the stable public key; format `{category}_{index:04d}`. `index` is the position
  within the category, not the seed — they coincide at seeds 0–4 and stop coinciding for any
  other seed list.
- **One env per block sequence, one reset per seed, and the destination pinned *after* the reset**
  with `navigation.set_route` rather than through `vehicle_config["destination"]` at construction.
  That is what lets the three `X` categories share a single reset instead of paying for three:
  they are one junction driven three ways. `auto_assign_task` draws its throwaway destination
  from `get_np_random(random_seed)` — a *fresh* generator, not a manager's stream
  (`node_network_navigation.py:72-91`) — so leaving it to run and overriding it afterwards
  perturbs nothing. Measured: the full 35-scenario bank builds in **4.1 s**.

**Cost — this is on the critical path now.** With no durable bank, generation runs before every
batch. Measured on this machine: env *construction* is free (~0.00 s), all the cost is in `reset()`,
and `close()` throws away a warm engine. Reset cost is **superlinear in block count**, ~×1.6 per
added block:

| sequence | blocks | mean reset | worst |
|---|---|---|---|
| `T` | 2 | 0.044 s | 0.061 s |
| `TT` | 3 | 0.076 s | 0.077 s |
| `TTT` | 4 | 0.121 s | 0.144 s |
| `TTTT` | 5 | 0.196 s | **0.410 s** |
| `OOO` | 4 | 0.237 s | 0.271 s |

A 1,000-scenario bank of 5-block user maps is ~7 minutes of generation before a single step is
simulated. So `generate` **reuses one env per block sequence** and resets per seed, sizes it
`num_scenarios = max(seeds) - min(seeds) + 1`, and **emits per-scenario progress** — a silent
multi-minute command is not acceptable at that scale. (All 20 seeds built for every sequence above;
longer sequences are viable, they are just slower.)

**Manifest schema (v1.1)** — pydantic models in `bank.py`, every one `extra="forbid"`, so a field
a writer added and a reader does not know about is a failure rather than a silently ignored key.

```json
{
  "schema_version": "1.1",
  "bank_id": "pg-bank-2026-08",
  "created_utc": "2026-08-31T13:40:22Z",
  "metadrive": {
    "edition": "MetaDrive v0.4.3",
    "dist_version": "0.4.3",
    "commit": "85e5dadc6c7436d324348f6e3d8f8e680c06b4db",
    "asset_version": "0.4.3"
  },
  "base_config": { "...env config, types as dotted paths..." },
  "drive_side": "left",
  "categories": {
    "t_junction": {
      "description": "Three-way junction, taking whichever turn the seed offers. ...",
      "block_seq": "T",
      "exit_rule": "sharpest",
      "max_steps": 320,
      "scenarios": [
        {"scenario_id": "t_junction_0000", "seed": 0,
         "destination": "1T0_1_",
         "spawn_lane_index": 0,
         "route_length_m": 111.7,
         "net_rotation_deg": 90.0,
         "turn_pairs": "",
         "thumbnail": "thumbs/t_junction_0000.png",
         "exit_rule": null, "exit_node": null, "max_steps": null}
      ]
    }
  }
}
```

*(Amended 2026-08-31, three fields, all while building it.* `destination` **moved from the category
to the scenario** — see the bullet above; the category keeps `exit_rule`, the intent it was resolved
from. `drive_side` **is recorded**, so the guarantee the drive-side assert enforces is a stated fact
in the file rather than an assumption a reader has to make. `route_length_m` **is recorded per
scenario**, because it is measured for free by the `set_route` that already had to happen and
nothing else in the bank says how long a route is — `max_steps` is a category-level cap.*)

*(Amended 2026-09-01 — `net_rotation_deg` and `turn_pairs` added, on the same 1.0 schema since
nothing consumes a manifest yet. **Nothing in the bank said which way a scenario turned.** Both
come off the reset that already happens, via `sockets.route_rotation`. `net_rotation_deg` is
rotation along the driven route, unwrapped — not `SocketReading.angle_deg`, which is the
`wrap_to_pi`'d final heading and reads −120.5° for a route that sweeps +239.5°. `turn_pairs`
carries what a single number cannot: a gentle left and a left-then-right both read low.)*

*(Amended 2026-09-03 at Step 8b — **1.0 to 1.1**: three optional per-scenario overrides,
`exit_rule`, `exit_node` and `max_steps`, all `null` on a row that follows its category. They exist
because editing one item means saying something the category does not: its own destination, or its
own budget. 1.0 manifests still load — 1.1 only added optional fields — and the version moved
because the reverse does not hold, `extra="forbid"` being what makes a 1.0 reader refuse a row that
declares its own budget. A 1.0 bank this build edits is written back as 1.1: the version describes
the shape of the file, not the history of the bank. `CategoryEntry.rule_for` and `budget_for` are
the one place an override is resolved, so the review, the panel and a rebuild read the same
number.)*

**How you test it** *(recorded as it was done. Building a bank is now the Build tab — Phase 2c,
Step 5 — and reading it back is Steps 6 and 7.)*
```bash
uv run scenariobank generate --out ./banks/pg-bank-2026-08 --bank-id pg-bank-2026-08
```
`--category/-c` is repeatable and defaults to every category; `--seeds` takes a comma-separated
list and defaults to `0,1,2,3,4`; `--no-thumbnails` skips the PNGs.
*(Amended 2026-08-31 — the draft's `--categories a b c --count 5` is gone. `--count` had no meaning
once the seeds became a fixed, named list rather than a number of them to draw.)*

- **Expect:** exit 0; `manifest.json` + 35 PNGs; progress on stderr as it goes, one line per
  scenario naming the destination, spawn lane and route length. The whole bank takes ~4 s.
- Structural check:
  `jq '.categories | to_entries[] | {k:.key, n:(.value.scenarios|length)}' manifest.json`
  → every count is 5.
- Seeds are the same five everywhere:
  `jq -r '[.categories[].scenarios[].seed] | unique' manifest.json` → `[0,1,2,3,4]`.
- Every thumbnail exists and is non-trivial:
  `jq -r '.categories[].scenarios[].thumbnail' manifest.json | while read f; do test -s "$f" || echo "MISSING $f"; done`
- **Open the thumbnails.** `eog banks/pg-bank-2026-08/thumbs/` — the blue arrow is the spawn and
  its heading, red is the driven route, the green star is the destination. Check the route turns
  the way the title claims: `curve` seeds 2 and 3 must bend **away** from the arrow's left.
  They show no traffic, objects or lights — those are Phase 4's option axes and are not built at
  generation — and each is zoomed to fit, so a curve and a roundabout both fill their frame
  despite being very different sizes.

**Done when:** 35 rows and 35 thumbnails, every category count is 5, the seed list is `[0,1,2,3,4]`,
the drive-side assert passes, and the thumbnails match their labels.

**Built 2026-08-31.** All of the above checked on this machine: 35 rows, 35 thumbnails, counts of 5,
seeds `[0,1,2,3,4]`, `drive_side` `left`, thumbnails opened and matching their labels. Every
destination and spawn lane agrees with `docs/reference/destinations.md`, which was measured through
an entirely different path (two envs per scenario, destination pinned at construction) — so the
cheaper single-reset path in `bank.generate` is not just faster, it lands on the same answers.
`tests/unit/test_bank.py` covers it: 15 tests, 6 of them `needs_sim`.

*(Amended 2026-09-01 — the thumbnails were wrong on first delivery and it took someone looking
at them to find it. See the two Traps rows added the same day. Generation is now **~5 s** for the
full bank rather than 4.1 s: the route figure is cheaper per image than the map render, but there
are 35 of them instead of 25 and matplotlib costs about a second to import. The first run after
an install reads 6.2 s — that one also builds matplotlib's font cache.)*

---

# Phase 2b — *(folded into a unit test)* ⬜

**Status:** neither test is written. Everything below is a decision that already holds;
the two tests in `tests/unit/test_invariance.py` are the entire remaining deliverable.

The storage design assumes changing traffic, cones, actors or lights leaves the map and the route
untouched. That still matters: comparing a score at `traffic=none` against `traffic=high` only means
something if the road and the route were the same both times. Otherwise the difference is
"traffic is harder **and** it is a different junction", and the two cannot be separated.

**But it is not a phase.** `Randomizable.__init__` (`base_class/randomizable.py`) gives **every
manager its own `np_random`**, re-seeded per episode by `BaseEngine.seed()`
(`base_engine.py:562-569`) — so the traffic manager cannot perturb the map manager's draws. The
spawn lane was measured invariant under `traffic_density=0.4` and `accident_prob=0.8` besides.

So it becomes two tests in `tests/unit/test_invariance.py`, which **never run during generation**:

- `test_option_levels_do_not_move_the_map_or_route` (`needs_sim`): reset one scenario at several
  option levels; assert `fingerprint.lane_geometry_digest` and `navigation.checkpoints` match, with
  `assert_array_equal` rather than `allclose`. Only the object sets may differ.
- `test_random_traffic_breaks_invariance`: `random_traffic=True` leaves the traffic manager
  unseeded (`traffic_manager.py:339-341`), so it **must** fail the same comparison. Without it, a
  green run only proves two things were compared that were never going to differ.

Their real job is guarding `ObstacleManager` and `VRUManager` — code *we* write, and the code that
could plausibly get the RNG wiring wrong. Green today; it stays green or someone broke it.

---

# Phase 2c — Bank Studio: the authoring loop, in a browser 🔨

**Status:** Steps 1-3 are built. Step 4, the gallery, is next.
The studio's own Bank tab lists these same twelve steps and strikes through what is done
(`src/scenariobank/web/static/index.html`) — when a marker moves here, move it there too.

**Goal:** the correction loop of Phase 2 — spot a bad draw, rank alternatives, look at one, swap it
— done in one page instead of seven context switches.

## What the page is for (2026-09-02, Keith)

Four things, and the steps below are organised around them rather than around commands:

| | |
|---|---|
| **1** | pick a scenario type from a list of options, with an example picture for the one selected |
| **2** | see the PNGs of every scenario of that type in the dataset, to judge whether a seed needs changing |
| **3** | select one of those images and have it replaced |
| **4** | open the dataset, look at the images, and see what generated each one |

**Why this replaced the previous ordering.** Steps 4-9 used to be one CLI command each, which
produced a page you operate by typing flags into a generated form: *"this idea of using the cli
before the web feels very counter intuitive, it creates a web interface that's barely useable and
not much better."* Correct, and the fault was the organising principle. The subprocess engine below
is unaffected — the objection was to the interface, not to how a job runs.

Every field these four screens need already exists. `ScenarioRow` records the seed, destination,
spawn lane, route length, net rotation and turn pairs of each scenario; `generate` already writes a
thumbnail per scenario and records its path; `bank.replace_scenario` already swaps one row in
place. **Nothing below needs new simulation work.** It needs the pictures put on a page and made
clickable.

**Why (2026-09-01, Keith).** Phase 2 works and the loop it enables is the problem. Finding that
`curve` seed 4 was a near-duplicate of seed 0 took `generate`, then `eog` on a directory of PNGs,
then a guess, then `seeds -c curve --keep 0,1,2,3 --scan 0-30`, then `inspect -b CC -s 22 --rule
only -o /tmp/x.png`, then a second image viewer, then `replace`. The commands are right; the surface
is wrong. **The thing being judged is a picture, and pictures do not belong in a terminal.**

**This is not Phase 7.** Phase 7 runs a *submitted model* against a finished bank: an orchestrator
on the NAS, a runner on each rig, a queue between them. This is for us, authoring the bank, on
localhost, with no queue, no rig and no container. Nothing here is designed around Phase 7 and
nothing here blocks on it. The read endpoints live in their own router so a later decision *can*
mount them elsewhere; that is the entire extent of the coupling.

---

## Three decisions that shape everything

**1. Every simulation runs in a subprocess, and the CLI is that subprocess.**

Not a wrapper around a command-line product — the page **is** the product, and this is how it
executes. `BaseEngine.singleton` (`engine/engine_utils.py:36-59`) is one engine per *process*: a
web server that builds an env holds that singleton for its lifetime, cannot serve two requests that
each need one, and dies with it — a panda3d/bullet fault is a segfault, not an exception. So a
subprocess is forced, and `python -m scenariobank ...` is simply what it is:

- a crash kills a job, not the studio;
- no engine contention, and no thread-safety question to get wrong;
- **one implementation, generated two ways.** The studio's forms and its server-side job validation
  both come from `docs.reference()` — the CLI's own parameters — so the page cannot offer a flag
  the program does not accept. A private worker protocol would mean hand-writing that schema, which
  is the drift `docs.py` exists to prevent, one layer up.

Cost: ~1–2 s of MetaDrive + matplotlib import per job. Accepted. A warm persistent worker is a real
optimisation and is deliberately **not** built here.

**2. One job at a time, and progress is derived.**

A global slot, for the same reason the rig has one: two `generate`s into one bank directory is a
corrupt manifest. Job state is rebuilt by reading two files on disk — `.studio/jobs/<id>/log` and
`.studio/jobs/<id>/exit` — and never kept in a variable, which is what makes a page reload, or a
studio restart, show a running job rather than lose it. Same property Phase 7's runner turns on, for
the same reason.

**3. Localhost only, and it says so.**

These endpoints run subprocesses that write into the repo. Bind `127.0.0.1`; refuse a `--host` that
is not loopback, with an error naming why. No auth, because there is no network. Bank names resolve
against `--banks-root` and anything escaping it is a 400 — the thumbnail route serves files off disk
by name.

---

## Steps — each one testable in the page, alone

Stop after any step and what exists still works. Every **Test** below is phrased as clicks, because
a step whose only proof is a `curl` line is a step that was not delivered to the person asking for
it.

### Step 1 — the shell: `scenariobank studio` ✅

New dependency group, so `uv sync` stays fast for anything that does not serve a page:

```toml
[dependency-groups]
web = ["fastapi>=0.115,<1", "uvicorn>=0.30,<1"]
```

| file | what |
|---|---|
| `src/scenariobank/web/api.py` | `create_app(banks_root, state_dir) -> FastAPI` |
| `src/scenariobank/web/static/index.html` | the whole frontend: one file, vanilla JS, no build step |
| `src/scenariobank/cli.py` | `studio`: `--banks-root ./banks`, `--port 8770`, `--host 127.0.0.1` |

`create_app` takes its roots as arguments rather than reading globals, so tests drive it with
`fastapi.testclient.TestClient` against a temp directory — no server, no simulator.

One endpoint: `GET /api/doctor` -> `doctor.collect(probe=False)`, already a pydantic model, returned
as is. `probe=False` because a probe builds an env and Decision 1 says this process never does.

**Test:** `uv run --group web scenariobank studio`, open `http://127.0.0.1:8770/`, see the commit
prefix `85e5dadc` in the header. `curl -s localhost:8770/api/doctor | jq -r .commit` agrees.

### Step 2 — `categories` and `commands`: the reference tab ✅

No simulator, no subprocess. `GET /api/categories` serves `CATEGORIES`; `GET /api/commands` serves
`docs.render_commands()` — the page you already generate, one click from the buttons that use those
flags instead of in a file nobody opens.

**Test:** seven categories listed; the `--rule` row lists all five rules.

### Step 3 — the job engine, and the Run tab ✅  *(the tab is scaffolding)*

*(Reordered 2026-09-01 — Keith: "i should be able to test different stages, as you add the commands
in so i can gaurantee it works". The old order put this at Step 4, which made Steps 1-3 all
read-only: three deliveries in a row that could only be checked with `curl`. The engine comes
first now, and every step after it lands a button.)*

`src/scenariobank/web/jobs.py`:

- `submit(argv) -> job_id`, 409 if the slot is taken, naming what holds it
- one subprocess, `shell=False`, stderr merged into stdout, appended to `.studio/jobs/<id>/log`
- the exit code written to `.studio/jobs/<id>/exit` **when it ends** — its presence is what
  "finished" means, so status survives a reload and a restart
- `GET /api/jobs/{id}?since=<offset>` reads state off those two files and returns the new log
  bytes; `DELETE /api/jobs/{id}` terminates the process group

Polled with a byte offset rather than streamed: a reload resumes for free and there is no
reconnect logic to get wrong.

**`src/scenariobank/web/invoke.py` builds the argv, and nothing in it lists a flag.**
`docs.reference()` gained `params` — the same Typer parameters `options` renders, unrendered — so
the page generates its form from them and the API validates a submission against them. A flag the
page offers, a flag the reference documents and a flag the CLI accepts are one flag by
construction. Refusals are sentences about one flag (`--category does not accept 'banana'`), not a
Typer traceback fished out of a log afterwards. A path option that resolves outside the directory
the studio was started in is a 400, and `studio` itself is not runnable.

**The Run tab was provisional and Step 8 deleted it.** It existed only because the purpose-built
screens do not yet, and a generated form over the CLI's flags is the fastest possible way to make
every command reachable while they are being built. It is the last CLI-shaped thing in the product
and it goes when the screens that replace it exist. The job engine underneath is permanent — that
is what those screens run on.

**Test in the page:** pick `categories`, click **Run** — seven rows in the output pane inside a
second, and the pill goes green with `exit 0`. No simulator involved, so a failure here is the job
plumbing and nothing else. Pick `doctor`, tick `--json`, Run: the checkbox came from the CLI's own
flags. Run twice quickly — the second is refused, naming the job that holds the slot. Reload
mid-job — the log is still there and still filling. **Cancel** — red, with the signal's exit code.

### Step 4 — the gallery: pick what to build ✅  ⟵ *feature 1*

*(Reordered 2026-09-02 — Keith: "this idea of using the cli before the web feels very counter
intuitive, it creates a web interface that's barely useable and not much better". The steps below
were organised around **commands**; they are now organised around the job. The Run tab keeps
working and moves to the last tab, an escape hatch rather than the front door.)*

A new CLI command, **`scenariobank examples`**, draws one figure per category into
`docs/reference/examples/<category>.png`, and those PNGs are **checked in**. Generated rather than
hand-drawn for the same reason `destinations.md` is: a picture nobody regenerates goes stale, and
the failure is silent. Needs `docs.GROUPS` and `docs.EXAMPLES` entries or the reference refuses to
render, which is the existing rule doing its job.

**Drawing the picture is building an engine**, so this could not have been done in the server:
`figures.draw_route` constructs a `MetaDriveEnv`, and the `web` group deliberately installs neither
MetaDrive nor matplotlib. The subprocess is not a stylistic choice here, it is the only shape
available — and checking the output in is what lets the screen work on a `--group web` machine at
all.

Checked in rather than drawn on demand, because this is the screen you meet **before** you have a
bank, a simulator, or any patience: it has to be instant and it has to work on a machine with only
the `web` group installed.

- `GET /api/examples/{category}.png`. The name is looked up in `CATEGORIES` **before anything
  becomes a path**, so a traversal is not filtered out — it never reaches the filesystem. A
  missing picture is a 404 naming the command that draws it.
- the **Build** tab becomes the landing view: one card per category — the example picture, the
  description, the road, the exit rule, the step budget. Click to select; select several. The
  selection is the state Step 5 consumes.
- a card whose picture has not been drawn says so and offers to draw it: `examples` is a
  registered command, so the button is a `POST /api/jobs` and no new machinery at all.

*(Its own directory rather than reusing `docs/reference/figures/`, which is where an ad-hoc
`inspect --block-seq` lands by default — not a directory the page could safely show. The cost is
one duplicated picture per category, ~312 KB.)*

**Test in the page:** open the studio with no bank generated and nothing typed, and see seven
pictures. Tick `curve` and `roundabout`.

*(Done 2026-09-02. Verified: `uv run scenariobank examples` drew all seven; all seven serve as
`image/png` with correct PNG magic; `banana`, `..` and `curve-seed0` are each a 404; the page
lands on **Build** with **Run** hidden. 217 tests pass. A no-sim test asserts one checked-in
picture per category, so adding a category and forgetting to redraw fails on every machine.)*

### Step 5 — generate what you picked ✅  ⟵ *bridges 1 to 2*

The selection becomes a `generate` job through the existing `invoke.build_argv`, so the page gains
no second way of calling the CLI: one repeated `--category` per ticked type, `--out` and
`--bank-id` carrying the name. Banks are auto-named `bank-YYYY-MM-DD-hhmm` and renameable —
naming a directory is not a decision worth interrupting someone for.

The studio knows how many scenarios the selection asked for, so the bar counts the CLI's existing
per-scenario stderr lines against that total. **No new progress protocol**: structured JSON-line
progress belongs to Phase 7, Step 1, and is not brought forward.

*(Amended 2026-09-02 — Keith: "could you allow me to select a number of scenarios to generate? i
think having 5 hardcoded is not a good idea". **per type** is a number box on the Build tab, and
the seeds it asks for are `default_seeds` while that list reaches, then counting on from its end —
so the default count still generates exactly what `generate` would on its own, and the rule holds
if `SEEDS` is ever not `0..4`. The page shows the seed list, not just the count, because a
scenario is a seed. A seed list that is not a count — `--seeds curve=0,1,2,3,22` — stays the Run
tab's.)*

Two facts the page needs and may not invent, so both are served rather than assumed:

- `reference()["default_seeds"]` — how many seeds a category is built at. The parameter table
  reads `None`, because the CLI resolves the default inside `_parse_seed_options`, so the count
  had to come from `categories.SEEDS` itself. It also replaced the hand-typed `0,1,2,3,4` in two
  `--seeds`/`--keep` prose notes, which were the same constant written a second time.
- `GET /api/studio` — where a bank goes. Banks live under `--banks-root`, which is a flag, and a
  job may only write inside the directory the studio was started in. A studio pointed outside its
  own checkout can still *list* those banks; it says so and greys **Generate** out, rather than
  letting the click discover it.

**Test in the page:** two types ticked, Generate, watch the count climb to 10, and land in the
browser with the new bank open.

*(Done 2026-09-02. Verified end to end against a running studio: `curve` + `roundabout` submitted
the way the page submits it, ten `[n/m]` lines parsed, every one agreeing with the 10 the selection
predicted before the first arrived, `exit 0`, a manifest and ten thumbs on disk. Refusals: a name
climbing out of the workdir, a missing `--out`, and an unknown category are each a 400 naming the
one flag. 222 tests pass. The browser that would open the finished bank is Step 6, so success ends
in a line naming the directory instead. **per type** verified afterwards: 3 with two types ticked
submitted `--seeds 0,1,2`, six scenarios, the CLI's own total agreeing with the six the page
predicted. 224 tests pass.)*

### Step 6 — the dataset: every picture of the type you picked ✅  ⟵ *feature 2*

The step that replaces `eog`, and it has a bank to show because Step 5 made one.

- `GET /api/banks` — directories under `--banks-root` holding a `manifest.json`
- `GET /api/banks/{bank}` — `bank.read_manifest()`, returned as is
- `GET /api/banks/{bank}/thumbs/{name}.png` — the file, after resolving inside the bank dir

One row per category, one card per scenario, and **the picture is the unit** — not a table with a
thumbnail column. What is being judged is an image.

Three decisions the step forced, none of them in the sketch above:

- **The manifest is served unshaped.** It was written to explain itself — "declare the intent,
  store the fact" — so a studio that reformatted it into a page-shaped payload would be inventing
  a second description of a bank, and the second one is the one that drifts.
- **A manifest that does not parse is *listed*, carrying its error**, and opening it is a 422
  naming the reason rather than a 404 claiming there is no such bank. A bank vanishing silently
  from the list is the one failure a person cannot debug from the page, and Step 8b's schema bump
  is exactly when it would happen. A directory with *no* manifest is different and is left out:
  generation writes the manifest last, so that is an interrupted run, not a bank.
- **Names are checked on their shape, before either becomes a path.** `_NAME` is the same class
  the Build tab already holds bank names to; a name that cannot contain a separator and cannot
  begin with a dot is not a traversal that gets filtered out, it is one that cannot be spelled.
  The same rule `/api/examples/{category}.png` follows, which is why it is a regex here rather
  than an `is_relative_to` check on where the path landed.

The picture-is-the-unit rule also decided what a card carries: the id, the seed, and the
destination and route length — the two fields that say *why* two cards look alike. The rest of the
row is Step 7's panel, so the card stops there.

And the end of a build now leads somewhere: **Open the bank** on the Build tab fetches the list
again (the bank it just made is not in one fetched before it existed) and opens it, which is the
line Step 5 had to end on instead.

**Test in the page:** 35 cards, every thumbnail loads, and the two near-duplicate `curve` seeds are
visibly the same picture — which is the whole reason this phase exists.
`curl -s 'localhost:8770/api/banks/b/thumbs/../../../etc/passwd'` -> 400, not a file.

*(Done 2026-09-02. Verified against a running studio holding two real banks generated through the
page. `curves-only` at seeds 0-4 renders five cards, every thumbnail a real PNG, and **seeds 0 and
4 are visibly the same picture** — 452 m against 420 m, the near-duplicate the phase exists to
make visible. `step6-check` renders two category bands, roundabout then curve, in manifest order.
The picker is newest-first by `created_utc`, not by directory name. **Open the bank** carried a
finished build straight into the tab. No console errors. Refusals checked live: `.hidden` and
`-lead` as bank names are 400 on the shape of the name; `.ssh.png` as a scenario name is 400; a
scenario the manifest does not name is a 404 saying so; `..` and `%2f` never reach the name check
at all, because an HTTP client normalises them away and the router matches nothing — which is
recorded in the test rather than papered over. 237 tests pass, ruff clean.)*

### Step 6b — the statistics: what is actually in this bank ✅

*(Added 2026-09-02 — Keith: "it should also show me statistics, like which maps are a close
overlap... anything which is too similar should be raised as a warning with text".)*

**A bank of 35 rows is not automatically 35 scenarios, and nothing said so.** Running the measure
against the three banks on disk:

```
curve                 5 distinct of 5    all four turn pairs LL/LR/RL/RR
t_junction            4 distinct of 5    t_junction_0001 = t_junction_0004
intersection_left     2 distinct of 5    0000 = 0002,  0001 = 0003 = 0004
```

`intersection_left` holds five scenarios and two distinct ones: every seed resolved to the same
destination, the same 111.7 m route and the same +90.0° turn, and only the spawn lane separates
any of them. That is a property of the category — MetaDrive's `X` junction does not vary with the
seed, which `bank.py` already said about `spawn_lane_index` — so five seeds *cannot* draw five
scenarios, and the report says the lane count is the ceiling rather than leaving "2 of 5" as an
invitation to swap seeds that would not help.

**`variety.shape_gap` is not what this uses, despite being the similarity measure this package
already owns.** It compares the *road* — total lane length and bounding box — and for `X` and `T`
the road is identical across every seed by construction, so it scores 0.00 for every pair and says
nothing exactly where the trouble is. It also needs a live env. What a thumbnail shows, and what
differs, is the **route**, and every field for that is already in `ScenarioRow`. So `review.py` is
pure: no simulator, no job, no schema change, and it works on banks generated before it existed.
A test asserts that in a subprocess, because `sys.modules` is shared across a pytest session and
nothing else would notice one convenient import.

Route length alone would have been wrong, which is why the measure is multi-field with destination
and turn pairs as hard discriminators: `curve` seeds 1 and 3 are 5% apart in length — *closer* than
seeds 0 and 4, which are the real near-duplicates — but they drive `LR` against `RL`.

Four verdicts, because the data shows they are different things: `identical`, `same-drive` (only
the spawn lane differs), `near-duplicate` (under `NEAR_DUPLICATE = 0.10`), `distinct`. The
threshold is defined in `review.py` with its own measured justification rather than imported from
`variety.py`, which holds the same number for a different measure — one constant serving two would
be a coincidence the next change breaks.

Ships as `src/scenariobank/review.py`, `GET /api/banks/{bank}/review`, and
`scenariobank review --bank <dir> [--json]` beside `seeds` in **"Look before you commit"** — that
one needs the simulator and asks *what should I build*; this one reads a bank off disk and asks
*what did I build*. Warning sentences are written in `review.py` so the page and the CLI cannot
describe one bank two ways. On the page: chips under each category band led by `2 distinct of 5`
in the warning colour, the sentences beneath, and a marker on every card that repeats another
(`≡ 0002`, `≈ 0004 · 7%`, `lane only`).

*(Done 2026-09-02. 261 tests pass, ruff clean. Verified against the three real banks in the CLI and
in the page, agreeing exactly: `intersection_left` 2 of 5 naming both duplicate groups and the lane
ceiling, `t_junction` 4 of 5 naming `0001 = 0004`, `curve` 5 of 5 with all four turn pairs and
`curve_0000 / curve_0004` at 7%. The `review` command's bank argument is a `--bank` flag, not a
positional: `test_the_form_and_the_table_describe_the_same_flags` enforces that every parameter has
a flag, because the studio's run form is built from them and a positional has nothing to render.)*

### Step 7 — what generated this picture, and how does it compare ✅  ⟵ *feature 4*

*(Amended 2026-09-02 — Keith: "I should be able to select any 2 maps in a list and see how
different they are as well... i can select a maximum of 2 pictures at once". Chosen shape: a click
**selects**, one selected shows the row, two show the comparison, and a third drops the oldest so
comparisons chain. That unifies this step with the request rather than adding a second way to
touch a card, and the comparison calls `review.gap` and `review.verdict` from Step 6b, so this
step adds an interaction and no new measurement.)*

Click a card and read the row: seed, destination node, spawn lane, route length, net rotation, turn
pairs, step budget, road, exit rule, and the MetaDrive commit the bank was built on.

**Nothing here is computed.** Every field is already in `ScenarioRow` and `CategoryEntry`, because
the manifest was designed to explain itself — "declare the intent, store the fact". This step is
the first thing that reads it back to a person.

**Test in the page:** click `curve_0004` and read seed 4 and its `turn_pairs` off the panel.

Two decisions the step forced:

- **The comparison is computed on the server, not in the page.** Every field it needs is already
  in the manifest the page holds, so a JavaScript `gap()` would have worked and saved a round
  trip. It would also have been a *second* measure: `review.py` writes the warning sentences for
  exactly this reason, and the panel that describes a pair must not be able to word it differently
  from the warning the same pair earns in the band above it. `GET /api/banks/{bank}/compare` is its
  own endpoint rather than a slice of `/review`, because the review reports the pairs worth
  reporting and every pair of a 35-row category is 595 of them.
- **Two scenarios of different types are answered rather than refused.** A gap is only defined
  inside a category, so `compare` returns a fifth verdict, `incomparable`, with the reason and the
  fields still laid out side by side. The banks on disk say why that is not pedantry:
  `intersection_left_0000` and `t_junction_0000` share a route length, a net rotation, a spawn lane
  and a step budget on completely different roads. A measure willing to score across categories
  would call them identical.

`Budget` gained `earned` — what each route earns from `step_budget`, by id — because the panel
needs that number per scenario and `categories.py` owns the rounding rule. The page reads it
rather than dividing metres by a constant, which would have been a second rule to keep in step.

*(Done 2026-09-02. 278 tests pass, ruff clean. Verified in the page against the three real banks:
`curve_0000` against `curve_0004` reads `near-duplicate · 7% apart` with the length 32.1 m / 7%
apart and the rotation 14.1°; `t_junction_0000` against `t_junction_0002` reads `distinct · 100%`
and names the reason — `1T0_1_` against `1T2_1_`, a different exit rather than a near miss — while
their route lengths sit 5% apart; `intersection_left_0000` against `t_junction_0000` is
`incomparable`. Selecting a third card drops the oldest, `Esc` and **clear** let go, and opening
another bank clears the selection rather than leaving two ids picked that are no longer on screen.
No console errors.)*

### Step 8 — replace the seed behind an image ✅  ⟵ *feature 3*

From that panel, **Find a better seed** runs `seeds` and shows the ranked alternatives: the gap
column, near-duplicates flagged, and per row a **Look** button (draws that seed) and a **Use this
seed** button (replaces it).

Needs **`scenariobank seeds --json`**, matching the `--json` that `doctor` and `sockets` already
have, plus a test that the JSON and the aligned table report the same seeds in the same order.

`bank.replace_scenario` already refuses a seed the category is using, keeps the id and the
position, and deletes a thumbnail it did not redraw. The studio's job is to **show the refusal**,
not to reimplement it.

**And delete the Run tab.** With pick, build, look and swap all on their own screens, the generated
flag form is the last CLI-shaped thing in the product, and keeping it would leave two ways to do the
same job — one of them worse. `web/jobs.py` and `web/invoke.py` stay; only the tab goes.

**Test in the page:** swap `curve_0004` to seed 22; the card redraws and
`jq '.categories.curve.scenarios[4]'` agrees. Then try seed 0 — refused, and the reason is readable
on the page. There is no Run tab left to fall back to.

Three decisions this step made:

- **The candidates are ranked against the seeds that are *staying*.** `--keep` is the other
  scenarios of that type, never the one being replaced. A candidate earns its place by being
  unlike what remains in the bank; ranking it against the draw you are throwing away would score
  it on the wrong thing. (A lone scenario has nothing else to be unlike, so it stands in for
  itself.)
- **`Use this seed` is offered on every row, kept seeds included.** `replace_scenario` owns the
  rule that a bank does not build one seed twice, and the page shows the sentence it gives back
  rather than pre-empting it. The same reasoning put `near_duplicate` in the `--json` payload:
  `variety.NEAR_DUPLICATE` is a number that module owns, and the page reads the flag rather than
  comparing against a constant of its own. `SeedReading.is_near_duplicate` is now what the aligned
  table flags with too, so the two renderings cannot come to disagree.
- **The one thing the page checks itself** is that the bank's recorded road and exit rule for that
  category match this build's. A ranking is measured on `categories.py`'s road and a replacement
  is built on the manifest's; they are the same road until a category is edited, and when they are
  not, the panel says so and will not scan.

**Deleting the Run tab was mostly deleting a log pane.** The job engine is untouched; what went is
the generated form and the single place every job used to report. A job's log is now accumulated in
the page and read by whichever screen started the job — the build bar reads its `[n/N]` lines, the
seed scan reads the same lines for its bar and then parses the JSON document off the tail, and a
refused swap reads the last line. `GET /api/runnable` stays: it is what `POST /api/jobs` refuses
`studio` by, and it was never only the form's.

*(Done 2026-09-03. Verified against a running studio on a copy of `banks/curve`. `curve_0004` at
seed 4 ranked seed 22 top of 31 at 30% from seed 1; **Look** drew it under `.studio/looks/` and
showed it beside the card — a visibly different road, an S against a loop. **Use this seed**
rebuilt the row: the card redrew, the manifest reads `seed 22, 312.86 m, +6.06 deg, LR`, the step
budget moved 1060 → 800 and the band's closest pair moved from `0000/0004 7%` to `0001/0004 21%`,
so the near-duplicate warning the phase exists to raise went away. Then seed 0: refused on the
page with `replace failed: seed 0 is already used by curve: each seed builds one scenario. curve
currently holds seeds [0, 1, 2, 3, 22].` Three tabs, no Run tab, no console errors. 287 tests pass,
ruff clean. The scratch bank was removed afterwards; the three real banks were not touched.)*

### Step 8b — edit, add or remove one item ✅

*(Added 2026-09-02 — Keith: "could you add step after step 8, call it 8b, that will let me change a
specific item in the dataset?" Three things Step 8 does not do, chosen from four: type an exact
seed, edit an item's settings, and add or remove items. Swapping an item's **category** was offered
and declined — an id carries its category name, so that is a rename, and a rename is the
renumbering problem below wearing a different hat.)*

Step 8 re-rolls a seed and **deliberately** keeps the bank's size, its ids and its positions. This
is the step that changes those, plus the fields a re-roll never touches.

**1. Type an exact seed.** Needs nothing new: `bank.replace_scenario` already takes any seed and
already refuses one the category is using. Step 8's ranked table becomes one of two ways in; the
other is a box. Worth having because ranking is a thirty-seed scan and sometimes you already know
which seed you want.

**2. Edit the item's settings.** One distinction decides the whole design: **`max_steps` is
declared, `destination` and `exit_rule` are measured.**

- `max_steps` is a budget, not a measurement. Changing it is a manifest edit — no simulator, no
  rebuild, instant, and the only control on this panel that does not start a job.
- `destination` changes the route, and `route_length_m`, `net_rotation_deg` and `turn_pairs` are
  all measured *from* that route, with the thumbnail drawing it. Changing it is a rebuild at the
  same seed. The dropdown is filled from `read_sockets` — the exits the road actually has — never
  a text box, for the reason `--category` is a dropdown.
- `exit_rule` is the *declared intent* the destination was resolved from. Changing it per scenario
  is a re-resolve and a rebuild.

**This is the first change that touches the manifest schema, and that is the real cost of the
step.** `exit_rule` and `max_steps` live on `CategoryEntry` — "the fixed facts they share" — not on
`ScenarioRow`. A per-scenario override means new optional fields on `ScenarioRow`, which is
`extra="forbid"`, under a `schema_version` pinned to `Literal["1.0"]`: a manifest this build writes
will not load in an older one, and the reverse. So **bump to `1.1`**, and say what the new fields
mean in `CONTRACT.md` (Phase 6) before Tyrone builds a picker against the old shape.

The open sub-question, with a recommendation: does an overridden `exit_rule` mean the scenario has
left its category? **No — keep the category name and record the override.** The manifest's rule is
"declare the intent, store the fact", and an override *is* the declared intent for that row; a bank
that silently reclassified a scenario would be the manifest failing to explain itself.

**3. Add and remove items.** The renumbering decision, which has to be made before a line is
written. `scenario_id(name, index)` puts the index in the id, so `curve_0004` **is** position 4.
Deleting `curve_0002` either renumbers `_0003` and `_0004` down — changing the ids of scenarios
nobody touched — or leaves a gap, so an id is no longer a position.

- **Leave the gap.** An id is how a run refers to a scenario, and Phase 5's results are keyed on
  it; a bank that renumbers on delete makes every id anyone recorded ambiguous. `_locate` already
  searches by id rather than indexing, so nothing in `bank.py` cares.
- Adding appends at `max(index) + 1`, not `len(scenarios)`, for the same reason.
- Removing deletes the thumbnail with the row. `replace_scenario` already establishes that a
  picture of a seed that is gone is wrong rather than merely stale.
- Removing a category's last scenario removes its entry; removing the bank's last category is
  refused. An empty bank is a manifest with nothing in it, and `generate` is how a new one is made.

**What it needs:** `bank.remove_scenario` (no simulator), `bank.add_scenario` (one env, one reset)
and `bank.set_max_steps` (a manifest edit), each with a CLI command beside `replace`, because
everything still runs as a subprocess. `docs.GROUPS` and `docs.EXAMPLES` entries or `reference()`
raises — the existing rule doing its job.

**The Run tab is gone by the time this lands** (Step 8 deletes it), so there is no log to recover a
refusal from. Every one of these has to say what is wrong on the panel itself.

**Test in the page:** set `curve_0003`'s `max_steps` to 500 and watch it save without the simulator
ever starting. Remove `curve_0002`, and confirm `curve_0003` and `curve_0004` keep their ids and
their pictures while `thumbs/curve_0002.png` is gone. Add one to `t_junction` and get
`t_junction_0005`, not `t_junction_0000` reused. Type seed 137 into `curve_0004` and watch it
rebuild. Then remove every scenario of a one-category bank — refused, readably, on the panel.

Four decisions this step made:

- **The overrides are recorded, not applied and forgotten.** A destination changed in the page
  writes `exit_node` onto the row, not just the new `destination` it resolved to. The manifest's
  rule is "declare the intent, store the fact", and without the intent a later rebuild would
  silently revert to the category's rule. Three new fields rather than two, because `exit_rule`
  survives a seed change and an exact node cannot.
- **A pinned exit does not outlive its seed.** `1T0_1_` is an arm of *that* seed's road, and
  `StdTInterSection` offers a different arm on seeds 2 and 3. So a rebuild at a new seed drops the
  pin and says so on the job's own output, rather than failing on a node nobody typed or carrying
  an intent the road cannot honour.
- **`bank.set_max_steps` gets an endpoint, not a job.** It is the one edit that measures nothing,
  and `POST /api/banks/{bank}/scenarios/{id}/budget` writes the manifest in-process — no
  subprocess, no second of Python start-up to change one integer. It is refused with a 409 while a
  job is running, because `replace` holds a manifest in memory and writes it back at the end: an
  edit slipped in beside it would be lost silently, which is the one failure the page could not
  show you.
- **`add` re-creates a category the manifest no longer holds**, from this build's `categories.py`.
  Removing a category's last scenario removes the entry, and without this that removal would be
  the only edit here you could not undo.

**Schema 1.1 was the real cost, and it was smaller than it looked.** The three fields are optional,
so every 1.0 bank on disk still opens and `_locate`, `review` and the studio needed no version
branch. What they did need was one place to resolve an override: `CategoryEntry.rule_for` and
`budget_for`. `review.Budget` grew `caps` — the cap that applies to each id — so the page reads the
number rather than working out whose cap applies, exactly as it already reads `earned` rather than
dividing metres by a constant.

*(Done 2026-09-03. 311 tests pass, ruff clean. Verified in a running studio against copies of
`banks/t-junction-left-intersection` and `banks/curve`. `curve_0003`'s budget went to 500 with no
job created — the panel read `820 / 500 · its own` and the band's warning became `curve_0003
(820/500)`. Removing `curve_0002` left `0000, 0001, 0003, 0004` with their pictures and took
`thumbs/curve_0002.png` with it; the review noticed the bank had lost a turn pair and said `no
scenario drives RR`. Seed 137 typed into `curve_0004` rebuilt it to 204.3 m / −126.6° / RR. Adding
a `t_junction` at seed 7 gave `t_junction_0005`, and pinning its exit to `1T1_1_` — listed by a
`sockets` job on the manifest's own road — rebuilt it to 122.5 m / 0.0° and recorded
`exit_node: "1T1_1_"`, the panel reading `1T1_1_ · pinned on this scenario`. Then the last
scenario of a one-category bank: refused on the panel with `remove failed: curve_0004 is the only
scenario in this bank…`. No console errors. The scratch banks were removed; the three real banks
were never opened for writing and are still 1.0.)*

### Step 9 — four new scenario types ✅

Measured 2026-09-02 against the installed MetaDrive, because "which blocks could be categories" is
a question with an answer rather than an opinion:

| candidate | sequence | result |
|---|---|---|
| off-ramp | `RS` | builds, one exit — rule `only` |
| lane merge | `yS` | builds, one exit — rule `only` |
| lane split | `YS` | builds, one exit — rule `only` |
| tollgate | `$S` | builds, one exit — rule `only` |
| U-turn junction | `U` | builds, but its exits are +90 / 0 / −90 — the same shape as `X`. The U-turn arm is **not** a destination socket, so it cannot be a category distinct from the three intersections |
| in-fork, out-fork | `f`, `F` | MetaDrive refuses: `ValueError: Bug exists in this block, Recommend to use Ramp` |
| parking lot | `P` | refuses: `Lane number of previous block must be 1 in each direction`, and `base_config` pins `lane_num=3` |
| `B` | `BS` | builds, one exit — rule `only`. *(The note here first said `B` was the abstract `PGBlock` base class. It is not: `B` is `Bidirection`, a two-way road block, re-checked at Step 9. Left out anyway — oncoming traffic on an undivided road is a hazard axis, which is Phase 4's business, not an eleventh turn)* |

So: **`off_ramp_hold` (`RS`), `lane_merge` (`yS`), `lane_split` (`YS`), `tollgate` (`$S`)**, all
rule `only`. Eleven candidates, four survivors — recorded here with the errors so nobody re-tries
the other seven.

`off_ramp_hold` is named for what it is. `R` exposes a single socket on the through lane, exactly
as `r` does for the on-ramp, so the category is *"hold the through lane while a lane leaves"* and
**not** *"take the exit"* — a route down the ramp is not expressible as a destination socket.

Each needs its destination resolved across seeds, `max_steps` from a **measured** route length
(the house rule: re-measure a figure, never quote one), `destinations.md` regenerated and an
example drawn. They appear in the gallery automatically, because the gallery is generated from
`CATEGORIES`.

**Test in the page:** eleven cards in the gallery; generate `lane_merge` and look at it.

**Three things the measurement decided that the table above did not.**

- **`tollgate` is the only category whose cap is deliberately above `step_budget(longest route)`.**
  `step_budget` assumes 6 m/s everywhere; `TollGate._add_building_and_speed_limit` calls
  `lane.set_speed_limit(3)` on every lane it lays and parks a `TollGateBuilding` in every second
  one. So the toll section is driven at half the reference speed and costs twice the time its
  length earns. The cap is `step_budget(route + toll section)` at the worst seed — 168.6 + 43.5 m,
  giving 540 where the route alone earns 440. Recorded in `categories.py` and in the generated
  `destinations.md`, because a number that disagrees with the stated formula has to say why.
- **`lane_merge` and `lane_split` measure the same route at every seed**, to the tenth of a metre.
  `Merge` and `Split` are one block drawn in either direction and `navigation.total_length` reads
  the reference lane, which survives both. They are still two categories: the roads differ (five
  distinct each, and different from each other's), and what changes is how many lanes are beside
  the ego — three narrowing to one or two, against three widening to four or five. Asserted, so a
  later edit cannot quietly collapse them.
- **All four are thin at the default seeds, and both measures say so.** Each is a straight of
  drawn length, so a seed varies how long the road is and nothing else. By road shape the closest
  pair of each is 2–4% apart; by route, which is what `review` compares, `off_ramp_hold` seeds 1
  and 3 are 0.9% apart and each of the four earns four to seven near-duplicate warnings on a
  default bank. Nothing is wrong: all four still report `5 distinct of 5`, and this is exactly
  what `scenariobank seeds` and `replace` exist for. The bank's distinct-road count went from
  18-of-35 to **38-of-55**, and the closest pair of *every* sequence is now a near-duplicate.

*(Done 2026-09-03. 11 categories, 55 scenarios, `destinations.md` and all eleven example pictures
regenerated on this simulator. Measured longest routes: `off_ramp_hold` 260.0 m, `lane_merge` and
`lane_split` 188.6 m, `tollgate` 168.6 m; caps 660, 480, 480, 540. A full bank is 55 scenarios in
about 7 seconds. 322 tests pass, ruff clean. Checked in a running studio against a scratch bank:
eleven cards in the gallery with their fact lines inside their borders, the bank header reading
`55 scenarios · 45 distinct · 11 types`, and `lane_merge` reading `5 distinct of 5`, `150-189 m`,
`budget 480/480`. The scratch bank was removed; the three real banks were never opened.)*

### Step 10 — the road builder ✅

Compose a sequence from the fifteen block ids, pick an exit rule, pick a seed, and draw it.

**Preview only.** An ad-hoc road is not a bank row: a row needs a category name and a manifest
entry, and inventing names for one-offs is how a bank stops meaning anything. This is the escape
hatch for everything Step 9 excluded — look at a fork or a parking lot here and watch it fail
honestly, rather than having a category invented to accommodate it. *(Superseded by Step 10b: a
road can now be added, under a name **derived** from the road and the rule rather than invented.)*

**Test in the page:** build `CCX`, rule `left`, seed 0, and see the road. Build `fS` and see
MetaDrive's own refusal quoted back rather than a spinner.

*(Decided 2026-09-03, Keith: a second card on the **Build** tab rather than a fourth tab, and a
named palette plus a text box rather than a bare text box.)*

- **Almost no new backend.** `inspect --block-seq --rule --seed --out` already drew any sequence
  through `cli._ad_hoc_category`, and the studio already had a place for a picture that is in no
  bank (`.studio/looks`, from Step 8's seed swap). The step is a form, a picture, `--json` on
  `inspect` so the page reads facts instead of parsing a prose line, and `GET /api/blocks`.
- **The palette is a table, not a list of letters.** `categories.BLOCKS` holds id, MetaDrive
  class and a label in words, in MetaDrive's registration order; `VALID_BLOCK_IDS` is now derived
  from it, and a `needs_sim` test asserts every `(id, class)` pair and the order against
  `PGBlockDistConfig.all_blocks("v2")`. `f` (`InFork`) is listed although MetaDrive refuses it —
  the refusal is the simulator's to make and its to quote.
- **The refusal is a sentence.** `sockets.reset_or_explain` wraps the reset the way
  `bank._reset` does, so a seed that does not lay out reads `seed 0 does not build for block
  sequence 'fS': Bug exists in this block, Recommend to use Ramp` and exits 1, rather than a
  traceback. `read_sockets` and `figures.draw_route` both go through it.
- **`would earn`.** `inspect --json` adds `earned_max_steps = step_budget(route_length_m)`: the
  number to have in hand when deciding whether a road drawn here deserves to become a twelfth
  category. It is computed in the CLI, not the page — the formula lives in one place.
- **Two screens run `inspect`.** The seed swap and the road builder both start one; `WATCHERS`
  routes by which screen is waiting, the same way `replace` already did for the swap and the
  edit panel.

*(Done 2026-09-03. The test above was written without measuring, and the builder's first job
was to correct it: `CCX` at rule `left`, seed 0 is **refused** -- `no exit near +90 degrees:
closest is 3X2_1_ at +149.5` -- because two curves rotate the crossroads so far that none of its
arms is a left turn from the spawn heading. At rule `sharpest` it draws: exit `3X2_1_` at
+149.5 deg, 519.3 m, and would earn 1300 steps. `fS` is refused on the card in MetaDrive's
words. The palette shows fifteen named blocks. 327 tests pass, ruff clean; checked in a running
studio with a scratch banks root, no bank opened, the scratch removed afterwards.)*

### Step 10b — and put it in a bank ✅

*(Asked for 2026-09-04, Keith: "after drawing it, i need to add the option to add it to the
current banks". Decided the same day: **one seed** -- the one on screen, more added afterwards
from the Bank tab's "Add another" -- and **named automatically** from the road and the rule
rather than typed.)*

**This reverses Step 10's stance, and auto-naming is what keeps that stance honest.** "Inventing
names for one-offs is how a bank stops meaning anything" stays true when the name is not
invented: `categories.composed_name` derives it from the road and the rule, so `CCX` at
`sharpest` is `CCX_sharpest` for everyone, and a road composed twice is one category rather than
two spellings of one. `$` is spelled `toll` -- a name is also a directory entry and a URL
segment, and dropping the character would make `$S` and `S` the same category. A `needs_sim`
test asserts every derived name against the studio's own `_NAME` pattern.

- **`add` grew the road, not a new command.** `--category` became optional beside `--block-seq`
  and `--rule`; `bank._road_to_add` resolves the three ways to know a road (a composed one, an
  entry the manifest holds, a category this build ships) and is the only place that decides.
  Nothing else in the pipeline learned a new concept: a composed category is an ordinary
  `CategoryEntry`, so the manifest schema did not move, and `review`, the comparison panel,
  `replace`, `remove` and `budget` all worked on one without being touched.
- **The cap is measured, not chosen.** A new composed category is capped at `step_budget` of the
  route just built -- the number the card shows as *would earn* -- set after the measurement
  rather than before it. Every later seed of it is then warned against that cap exactly as the
  eleven shipped ones are.
- **The page reads the name rather than spelling it.** `inspect --json` now reports
  `composed_name`, so the card can say what the road would be called without a second copy of the
  rule in JavaScript. `WATCHERS.add` routes by which screen is waiting, as `inspect` and
  `replace` already did.
- **Two things a composed category does not get**, both because `categories.py` has never heard
  of it: a picture in the Build gallery, and a seed ranking -- **Find a better seed** already said
  so in its own words (`roadDrift`), which turned out to be the right sentence unchanged.

**Test in the page:** draw `CCX` at `sharpest`, press **Add to bank**, then **Open the bank**.

*(Done 2026-09-04. Measured on a scratch bank: `CCX_sharpest` is created capped at 1300 steps,
which is `step_budget(519.3)`, and seed 1 of it earns 1020 against that cap. Adding the same seed
twice is refused in the CLI's words on the card -- `seed 0 is already used by CCX_sharpest`.
`$S` at `only` lands as `tollS_only`, 168.6 m, cap 440 -- **optimistically**, because
`step_budget` assumes 6 m/s and a toll plaza holds its lanes to 3 m/s, the same exception the
shipped `tollgate` records; its cap is raisable on the Bank tab without a rebuild. Removing a
composed category's last scenario removes the category, and composing the same road again brings
it back under the same name, which is what makes that an undo. 337 tests pass, ruff clean;
checked in a running studio, scratch removed afterwards, no bank in `banks/` opened.)*

### Step 10c — pin the option levels in the bank ✅

*(Asked for 2026-09-04, Keith: "i want them pinned in the bank, because i don't want them to be
regenerated everytime, people might make mistakes". That is an argument about **retyping**, not
about baking a cross-product, and the design follows from the difference.)*

**Amends the standing rule under "Stored normalized, applied at run time" rather than deleting
it.** Options are still never part of a bank *row*; a bank now carries them once, at the top of
its manifest. What made that safe was separating two things this plan had been calling one:
`base_config` is generation **truth** and stays at `traffic_density: 0.0`, while `options` is
declared **intent** for runs. Changing the first would rebuild 35 scenarios; changing the second
is one field of one JSON file, in `budget`'s class of edit.

- **Schema 1.2.** `Manifest.options` is an `OptionLevels` model — the six axes, each defaulting to
  `none`. The number moved for the reason it moved at 1.1: `extra="forbid"` means a 1.1 reader
  refuses a manifest carrying a field it does not know. A 1.1 bank reads as all-`none` and comes
  back stamped 1.2 when edited.
- **`options.py` ships names, not numbers.** `AXES` and `LEVEL_NAMES` only; `LEVELS` stays Phase
  4b's, because a level name is schema and a number is a calibration, and they change at different
  rates. The module docstring carries the survey of what MetaDrive already provides.
- **Three surfaces, no new concept.** `bank.set_options`, `scenariobank options --bank X --traffic
  medium`, and `POST /api/banks/{bank}/options` — copied from `set_max_steps`, `budget_cmd` and
  the budget endpoint respectively, including the refusal while a job holds the manifest. The
  studio's six dropdowns read their axes and levels off the `options` command's own flags through
  `/api/commands`, so the page cannot offer an axis the CLI does not have.
- **A default, not a lock.** A run flag overrides the pin and the result records the expanded
  levels, which is what keeps Phase 4b's single-axis sweep possible. An `options_locked` flag is
  available if enforcement is ever wanted; it was not built.
- **Per-category and per-scenario overrides deliberately not built** — that is how a bank quietly
  becomes the cross-product this plan says can never be collapsed back. The resolver is
  `options_for(entry, row)` from the start so adding one later changes no call site.

**The survey that came out of it changed Phase 4 more than this step changed the code.** Reading
the pinned commit showed `TrafficObjectManager` already is the `ObstacleManager` this plan
described writing, registered conditionally by `metadrive_env.py:296-300`; and that `actors.py` is
the only placement code that is ours. Both notes are rewritten under **New modules**, along with
two reproducibility items now scoped to actors alone.

*(Done 2026-09-04. Verified on a scratch copy of `banks/curve`: after `options --traffic medium
--pedestrians low` the `categories` and `base_config` blocks are byte-identical to the original,
`base_config.traffic_density` is still `0.0`, no thumbnail was rewritten, and the schema moved
1.1 -> 1.2. `review` ends with `runs at traffic=medium, pedestrians=low, everything else none`.
353 tests pass, ruff clean; no bank in `banks/` was opened for writing.)*

### Step 11 — the road utilities ✅

A utility tab: which exits does this road offer, and where does each category's route end. Both
answer questions *about a road* rather than doing the job, which is why they are last rather than
first. `sockets` and `destinations` are what run behind them.

**Test in the page:** ask for `X` at seed 0 and see one exit near +90, one near −90, one near 0, and
the entry marked. Re-measure the destinations reference and `git diff docs/reference/destinations.md`
is empty.

**The second half of that test passed and the first half was wrong**, in the same way Step 10's
`CCX` was: written without measuring. `X` at seed 0 offers exactly three exits at +90, 0 and −90,
and **no entry is marked** — a block's `get_socket_list()` returns the connections it offers
onward, and the arm it was driven in through belongs to the block before it. Measured across all
fifteen block ids at seed 0: of the twelve that build, not one marks an entry.
`SocketReading.is_entry` and the filter every rule applies stay — they guard a case MetaDrive does
not currently produce — but nothing composed from these blocks will fill that column in, so the
page says why rather than showing a column that is always empty.

Four things it needed beyond a tab.

- **`sockets --json` became a document.** It was a bare list of sockets, and the rules — which are
  what turn an exit into a destination — had nowhere to live. Now `{block_seq, seed, sockets,
  rules}`, with `rules` a **list** in `ExitRule`'s own order so `json.dumps(sort_keys=True)` cannot
  alphabetise it and leave the page printing them in a different order from the terminal. The one
  existing reader — the edit panel's exit dropdown — moved to `.sockets` with it.
- **A rule that finds nothing is a row, not a gap.** The terminal skipped an unsatisfiable rule
  silently; both surfaces now quote it: `no exit near -90 degrees: closest is 1T1_1_ at +0.0`.
  That sentence is why a road cannot carry a category, which is the whole reason to look at one
  here before pinning anything to it. `cli._rule_outcomes` computes it once for both.
- **`destinations` reports as it goes** — the parked item, now warranted: twenty seconds of silence
  is indistinguishable from a hung job on a page. Twenty units, `[n/m]`, **one count across both
  passes** (nine sequences fingerprinted, then eleven categories resolved and driven) so the bar
  does not refill halfway and read as a job starting over. The shape is `generate`'s own, which
  the studio's bar already parses — no second progress protocol.
- **`GET /api/reference/destinations` serves the file as text**, and `renderMarkdown` draws the
  three blocks `destinations.render` emits — headings, paragraphs, pipe tables — reusing the same
  `table()` and `inline()` the Reference tab uses. Text rather than a parsed structure: the
  generator is the authority on that file's shape, and re-parsing it in the API would be a second
  opinion about it. `inline()` gained italics, which the command help had been rendering as bare
  asterisks all along.

**It shipped as a fourth tab, and that was the wrong place for both cards.** *(Corrected the same
day, on the question "what is the point of this?" — the answer named the problem.)*

- **The exit reader belongs beside the road builder**, because what it answers is the question
  **Draw** leaves open. A refused rule says it failed, not which rule would work; from a separate
  tab you had to retype the road to ask. Merged into `#road-card` as a **second button on one
  form** — same palette, same blocks box, same seed box, `Draw` running `inspect` and **Read the
  exits** running `sockets`. Two `.argv` lines, one per button. The `or a type` dropdown moved
  with it, so the eleven types' roads fill the same one box.
- **The destinations reference belongs on the Reference tab**, which is already the tab for
  documents the CLI generates. It sits above the command reference, **collapsed behind a toggle**
  — an always-open 34rem document would push the `How do I…` index below the fold on every visit.
  Fetched when that tab is first opened, for the reason the banks are.

The Roads tab is gone; three tabs again. `paletteButton` reverted to its single-box form — with
one box the parameterisation it had grown was dead weight — and `refreshExits` folded into
`refreshRoad`, so one function reads the box and holds both buttons while either job runs. The
exits section reports its own state where the answer will be rather than adding a second pill
beside the drawing's. **No Python changed**: the JSON shape, the rule outcomes, the progress lines
and the endpoint are all independent of where the cards live.

*(Done 2026-09-04. Verified in a running studio, twice — once as the Roads tab and again after the
fold. The loop the merge exists for: `CCX` at rule `left`, seed 0 refused with `no exit near +90
degrees: closest is 3X2_1_ at +149.5`, then **Read the exits** without retyping gave `left`
refused, `right` → `3X1_1_`, `straight` → `3X0_1_`, `sharpest` → `3X2_1_` — and drawing at
`sharpest` then worked, 519.3 m, `would earn 1300`, `CCX_sharpest`. `X` at seed 0 read three exits
at +90.0, 0.0 and −90.0, none an entry. `or a type` → `t_junction` filled `T`: seed 0 refused
`right`, seed 2 refused `left`, which is the evidence for that category's `sharpest`. `fS` was
refused with MetaDrive's own `Bug exists in this block, Recommend to use Ramp`. **Clear** emptied
the box and both results. On Reference: the card is first and compact, **Show the document**
rendered seven tables and eight headings, and **Re-measure** ran 20 units with the bar advancing —
`docs/reference/destinations.md` came back with the same md5 it went in with, `git diff` empty,
which is the test. The Bank tab's **List this seed's exits** still fills from the new document
shape (`curve_0000` → `2C0_1_`). No console errors. 359 tests pass, ruff clean. The scratch bank
was a copy of `banks/curve` under `scratch-banks/`, removed afterwards; the three real banks were
never opened for writing.)*

### Step 11c — measure the turn from the junction, not from the spawn ✅

*(2026-09-04, Keith, looking at `CSX` drawn to rule `left`: "i fail to see how this is left, this
seems like straight? is the angle based on the direction of the car when it enters the block?" —
then "please change it so it applies from where the car enters the last block".)*

It was not. `SocketReading.angle_deg` is the arm's final heading minus the **spawn** heading, and
both `_turn_word` and every angle in `select_exit` read it. On a road that rotates the car before
its last block that is the wrong frame, and the errors it produced were not cosmetic:

```
CSX seed 0, as it read before
  3X0_1_   -154.5   right       <- the driver turns LEFT into this arm
  3X1_1_   +115.5   left        <- the driver goes STRAIGHT through
  3X2_1_    +25.5   straight    <- the driver turns RIGHT
```

The curve in front of the crossroads swings the car **+115.5°**, so rule `right` — hunting near
−90° from the spawn — found nothing inside its 45° tolerance and refused on a road with an obvious
right turn, and rule `left` answered `3X1_1_`, the arm the drawing goes straight up. **A silently
wrong destination, not a bad label.**

**The missing number was already in the map.** `blocks[-1].pre_block_socket` is the socket the
final block was attached through — the road the car arrives on — so its last lane's final heading
is where the car is pointing when it reaches the junction. New `_entry_heading` reads it, falling
back to `0.0` rather than raising, because this serves a choice and must not stop `bank.generate`
mid-run.

`SocketReading` now carries **`turn_deg`** (the turn at the junction) beside `angle_deg` (from the
spawn) and `entry_heading_deg` (the difference, the same on every reading of one map).
`_turn_word` and **`select_exit`'s angle rules read `turn_deg`**. `CSX` seed 0 now reads +90 / 0 /
−90, and all three angle rules resolve to three different arms.

**Both angles are kept, and both are shown.** The terminal and the card print `turn` and
`from spawn` side by side, with a line above the table naming the rotation when there is one:
"this road turns the car +115.5° before it reaches the last block". `angle_deg` is still what the
drawing's title and `destinations.md` report, and still what folds at ±180 — `curve` seed 0 sweeps
+239.5° and shows as −120.5°, which is why `RouteMeasurement.net_rotation_deg` exists and is
untouched here.

**Nothing shipped moved.** `X`, `T` and `O` are single blocks, so their entry heading is 0 and
`turn_deg == angle_deg`; `CC`, `rS`, `RS`, `yS`, `YS` and `$S` use rule `only`, which ignores
angles. `docs/reference/destinations.md` re-measures byte-for-byte identical — `git status` does
not list it. That invariant is now a test in its own right, alongside a pure test of the two-arm
case and a `needs_sim` test of `CSX`.

**Step 11's verification record below is now out of date in one respect**: `CCX` at rule `left`,
seed 0 no longer refuses. Two curves rotate that crossroads −120.5°, so from the spawn its arms
read −30.5 / −120.5 / +149.5 and `left` found nothing; from the junction they are +90 / 0 / −90
and `left` is `3X0_1_`. The docs' refusal example moved to `T` at rule `right`, which refuses
because a T junction genuinely has no right arm at seed 0 rather than because it was measured from
the wrong place.

*(Done 2026-09-04. Verified in a running studio on a throwaway copy of `banks/curve` under
`scratch-banks/`, removed afterwards; the three real banks were never opened for writing. `CSX`
seed 0 **Read the exits** printed the rotation line and +90.0 / 0.0 / −90.0 against −154.5 /
+115.5 / +25.5. **Draw** at `left` → `3X0_1_`, net rotation +205.48°, and the picture turns left
at the junction; at `right` → `3X2_1_`, +25.48°, and it turns right — that one was refused
outright before. The Bank tab's **List this seed's exits** still fills from the document
(`curve_0000` → `exit:2C0_1_`). No console errors. 362 tests pass, ruff clean.
`uv run scenariobank destinations` reproduced the checked-in file exactly.)*

### Step 11d — say "Read the exits" in words, not in MetaDrive's ✅

*(2026-09-04, Keith, reading the card cold: "i get what you're trying to do but this is way too
cryptic, it needs to be worded in a way that a first time user with no knowledge of metadrive will
understand how to use".)*

Step 11c fixed the numbers. The card still explained them in the simulator's vocabulary: a column
headed `socket` holding `3X-socket0`, a column headed `node`, two angle columns one of which read
−30.5 / −120.5 / +149.5 on a plain crossroads, a caption about "the connections a block offers
onward", and a refusal in raw Python — `ExitRule.ONLY needs a single-exit block`. Nothing wrong,
and all of it assuming the reader already had the model.

What the card actually carries is small: **this road ends at a crossroads; you can go left,
straight or right; here is which setting gets you each one.** It now says that.

**The refusals were reworded at the source**, in `select_exit`, because both the terminal and the
card show them verbatim and a second wording would be a second truth:

```
this junction has 3 ways out (3X0_1_, 3X1_1_, 3X2_1_), and "only" means "take the single way
out". Choose left, right, straight or sharpest instead.

nothing here turns right. The closest is 1T1_1_, which carries straight on. Try a different exit
setting, or a different seed.
```

The second needed the target as a word rather than `+90`, which is `_RULE_PHRASE` and
`_describe_turn` beside `_turn_word`.

**One table, "which setting sends the car which way"** — every value of the `exit` dropdown, what
it does in English, and where it lands, with the ones that cannot be used marked *can't be used
here* and carrying the command's own sentence.

A second table sat above it for one round, one row per way out. Keith read it and asked what it
was for, which was the right question: **no block MetaDrive builds offers more than three arms**,
and their turns are always −90 / 0 / +90, so `left`, `right` and `straight` between them already
name every arm there is. Measured at seed 0 across all fifteen ids — `C S r R $` one arm, `y` one
arm on 1 lane, `Y` one on 5, `B` one on 1, `T` two, `X O U` three, and `f F P` do not build. The
arm table was the settings table rearranged. Removed on his word — *"just remove it, i don't find
it very useful, i'll add in again in the future if i think it makes sense"*. The one thing it
carried that the settings table does not is the per-exit lane count, which is a column here if it
is ever wanted.

**Nothing was deleted.** `socket`, `from spawn` and the never-marked-entry note moved into a
`<details>` labelled **show the raw measurements**, closed by default, whose caption says why the
two angle columns agree or disagree on the road in front of you. They are what a road that is not
the shape you assumed shows up in, so they stay one click away.

Two smaller things from the same reading: the `exit` dropdown moved to sit immediately left of
**Draw**, the only button that uses it, so the control and the table that explains it use the same
word; and an empty `blocks` box now says what the greyed buttons are waiting for
(`click a block above, or pick a type`, muted rather than red — the span's `failed` class resolves
red, so a `.hint` class was added).

*(Done 2026-09-04. Verified in a running studio on a throwaway copy of `banks/curve` under
`scratch-banks/`, removed afterwards; the three real banks were never opened for writing. `CCX`
seed 0 read "This road ends at a **crossroads**, so the car has 3 ways to leave it", then five
settings with `only` refused in the new words. **show the raw measurements** opened to the socket indices and
+90.0/−30.5, 0.0/−120.5, −90.0/+149.5, with "This road turns the car −120.5° before it reaches the
last block, which is exactly why the two columns disagree here". `X` seed 0 read the same shape
and "They agree here, because nothing turns the car before the last block". `T` at `right` was
refused with "nothing here turns right. The closest is 1T1_1_, which carries straight on." An
empty box greyed both buttons and showed the hint. No console errors. 362 tests pass, ruff clean,
and `destinations` reproduced the checked-in file byte-for-byte — the refusal wording cannot reach
it, because all eleven categories resolve.)*

### Step 12 — pick a model, submit a run ⬜  ⟵ *blocked on Phase 7*

*(2026-09-02, Keith: "eventually the ui needs to handle selecting a model and running it against a
bank, but the output is essentially merely adding it to the queue".)*

Choose a bank, choose a model, choose the six option axes, and press submit. **The action is
`queue.put(...)` onto the NAS topic and nothing else** — nothing runs on this machine, and no
results come back to this page. The studio becomes the second producer onto the queue, alongside
the wing-sim webapp.

The form is rendered from Phase 7's `GET /options`, for the same reason every other form here is
generated: a hand-written copy of the six axes is a second declaration of them.

**Blocked on Phase 7** for the payload schema and the topic name. Recorded here rather than in
Phase 7 because the screen is ours and the queue is not.

**Test in the page:** submit a run and see it appear in the queue's own `/admin` console, with the
bank, the model and the options in the payload.

---

**Reused rather than rewritten:** `bank.read_manifest` / `Manifest`, `bank.replace_scenario`,
`bank.ScenarioRow`, `doctor.collect`, `categories.CATEGORIES`, `figures.render_route`,
`docs.reference`, `variety.SeedReading`. The API is thin on purpose — a route doing arithmetic
belongs in a module the CLI shares, or the two front doors will disagree.

**Done when** all four are true **without a terminal at any point**: you can **pick** a scenario
type from pictures, **build** it, **look** at every scenario of that type in the dataset, and
**swap** a poor draw for a better seed after reading what generated it. The `curve` seed-4
correction is the worked example: spot it in the grid, rank alternatives, look at seed 22, swap it,
see the card change. And the Run tab is gone — while it is still there, the page has not replaced
the command line, it has only wrapped it.

**Not doing:** anything in `wing-sim`; a React build; a warm persistent MetaDrive worker;
JSON-line progress from `generate`; auth or any non-loopback bind; categories on the blocks Step 9
measured as unusable; ad-hoc roads as bank rows.

---

# Phase 3 — Import: stored scenarios from the converter 🔨  ⟵ *the number is reused*

**Status:** Steps 1-4 are built — `scenariobank workspace` reads a converter workspace and says
what is in it, `scenariobank importing` turns that reading into
[`docs/reference/importing.md`](docs/reference/importing.md), the checklist an import must satisfy,
and `scenariobank import` satisfies it: one workspace becomes one bank under schema 1.3, beside the
procedural ones. Steps 1-4 are built: the studio now reads `source` off the manifest and lists the
two kinds under their own headings. Step 5, what a real-world bank's review *is*, is next.

> **This is not the old Phase 3.** `scenariobank verify` was the gate over `map_id`, `config_hash`
> and the recorded MetaDrive commit; all three were cut with the durable-bank premise on
> 2026-08-31 (see Phase 2, **Scope**). Of its seven checks the drive-side measurement moved into
> Phase 2's `generate` and into `doctor`, and the rest were reproducibility checks a per-batch bank
> does not need. Later phases were deliberately **not** renumbered then, which left the number
> free; it now holds the import, and no phase after it moved.

**Goal:** a scenario somebody already generated and saved — a real road, from OSM, with recorded
actors — sits in a bank beside the procedural ones and runs through the same runner.

## What a stored scenario is, next to a generated one

*(Asked for 2026-09-05, Keith: "i eventually want to add the scenarios i generated via scenario net
to this application ... in those cases i would have already fully generated and saved the scenario
before it is run".)*

One correction to the premise that followed, because it decides the shape of Phase 4: **env
construction is still required.** A stored scenario needs `ScenarioEnv(...)` exactly as a PG road
needs `MetaDriveEnv(...)`. What goes away is the **seed → map contract**. Measured on
`workspaces/junction-1`:

| | PG (`MetaDriveEnv`) | stored (`ScenarioEnv`) |
|---|---|---|
| construct | 9 ms | 2 ms |
| cold reset | 39-354 ms, ~50 ms per block | 488 ms at 10 Hz, **1.5 s at 100 Hz** |
| warm reset | 5-35 ms | 66 ms |
| one step | 0.46 ms | 2.9-4.1 ms (151 tracks replayed) |
| what `seed` means | the map generator's seed | an **index** into the dataset |
| `num_scenarios` | bounds an index (`num_scenarios_for`) | a real count; `-1` means all |
| `horizon` | our `max_steps` | `None` — and it does **not** end the episode |

So opening a stored scenario is *slower* than generating a PG road, not faster: the map is
deserialised and rebuilt from 974 stored features. "Already generated" does not mean "free to
open", and `start_seed` / `num_scenarios_for` / `set_route` — the whole apparatus of Phase 4's env
construction — does not apply on this path.

**Feasibility is settled, not assumed.** `junction-1` loads and runs in the pinned MetaDrive with
no conversion, at both rates: reset OK, `ScenarioMap` built, 24 traffic objects spawned, light
manager live, `route_completion` climbing.

## Steps

### Step 1 — read a converter workspace ✅

A reader over `wingfin-osm-scenarionet-converter/workspaces/<name>/`. No writing, no env. Inputs
are `source/manifest.json` — the converter's own provenance chain — and
`scenarionet-<rate>/dataset_summary.pkl`. **R1 holds:** read the files, do not import the
converter. The format read here is ScenarioNet's, which is MetaDrive's, not Tyrone's.

**It ships as `scenariobank workspace <path>`** — one word, like every other command in this CLI.
Not `inspect-workspace`: nothing here is hyphenated, and `inspect` already means "draw a route".

**That is all it takes to reach the studio**, in two of the three senses — and the third is the
only page work this phase has:

- **The Reference tab is automatic.** `docs.reference()` walks the Typer app, `/api/commands`
  serves that dict, the tab renders it. The command, its help and its flags appear with no page
  code, `docs/reference/commands.md` regenerates from the same dict, and
  `test_the_checked_in_reference_matches_the_cli` fails until it does.
- **The Run tab is automatic — and then refuses the path.** `catalog()` (`web/invoke.py:30`) is
  that same reference minus `NOT_RUNNABLE`, which today holds only `studio`, so `/api/runnable`
  does offer `workspace` with no list edited anywhere. But `invoke._one_value` applies one rule to
  every flag whose type is `path`: it must resolve **inside the directory the studio was started
  in**. Measured — a submitted `--path` of
  `~/Desktop/work/wingfin/metadrive-complete/wingfin-osm-scenarionet-converter/workspaces/junction-1`
  is refused with *"--path must stay inside …"*. Every workspace is out of tree by construction, so
  the Run tab lists this command and cannot run it. That containment rule is what keeps a typo in a
  text box from writing outside the checkout and is **not** loosened here for a command that only
  reads; telling a read-only path from a written one is Step 4's, when the studio grows a place to
  put one.
- **A screen of its own is not automatic.** The road builder, the gallery and the bank list are
  hand-built. Here that is Steps 4 and 5.

**Verify alone:** `scenariobank workspace <path>` prints identity, drive side, rate, the route
table and the actor counts for both `junction-1` and `mosque`, building nothing; the studio's
Reference and Run tabs both show it without either file being edited.

*(Done 2026-09-06. `src/scenariobank/workspace.py` + `scenariobank workspace --path/-p`, 37 tests
in `tests/unit/test_workspace.py`, **428 pass**, ruff clean. `docs/reference/commands.md`
regenerated; no page file edited.*

*Four things came out of the reading that the step did not assume, all measured on
`wingfin-osm-scenarionet-converter/workspaces/` rather than read off the manifest:*

*1. **`stage_6` is one conversion's record, not the workspace's index.** `junction-1` holds three
dataset directories — `scenarionet/`, `scenarionet-100hz/`, `scenarionet-10hz/` — and names one.
`mosque-1`'s `dataset_dir` is `null` while `mosque-1/scenarionet/` holds a converted scenario. So
datasets are **discovered by walking the workspace**. A reader that trusted `stage_6` would have
reported a third of `junction-1` and would not have said so.*

*2. **The rate is measured from `ts`, not read from `step_hz`.** `junction-1`'s manifest says
`100.0`; `scenarionet-10hz/` steps at 10 — the same 37.8 s drive in 379 steps rather than 3782.
That is the difference between a 379-step budget and a 3782-step one, so it is measured per file
and a disagreement with the manifest is a warning rather than a silent correction.*

*3. **The actor counts are not in `dataset_summary.pkl`.** They are in the `sd_*.pkl`, which is
~1 MB and unpickles in 0.02 s, so it is read. This confirms Step 2's checklist from the files:
`junction-1` at both rates carries **101 `PEDESTRIAN`, 25 `CYCLIST`, 24 `TRAFFIC_BARRIER`, 1
`VEHICLE`** and **8 `TRAFFIC_LIGHT`**, route `route-1`, 395.11 m, 37.82 s, 14 junction movements,
974 map features — asserted in `test_the_junction_1_checklist_is_what_the_files_say` so the day a
conversion changes, the checklist is rewritten from files rather than from memory. `mosque`'s
10 Hz conversion carries a fifth type the checklist does not name, `TRAFFIC_CONE`.*

*4. **Workspace pickles are read through a restricted unpickler.** This is the command you point at
somebody else's conversion, and a pickle is code. All 21 pickles across the four workspaces name
exactly one global between them, `numpy.array`, so the allowlist is tight enough to be worth
having; anything else is refused by name and the file is not read.*

*The report also carries a `warnings` list, on the model of `review`'s: an unrecorded dataset
directory, a rate that disagrees with the manifest, a stage 5 that did not pass, a summary naming a
file that is not on disk, a scenario built from a different lane model than the manifest's, a
missing `stage-6-map` thumbnail, and a recording holding nothing but the ego. `junction-1a` has no
dataset at all and says so.*

*The flag is `--path/-p`, not a bare positional: `docs._params` and `invoke.build_argv` both key on
a flag string, so a Typer `Argument` would have published a row reading `` `path <path>` `` in the
reference and built `scenariobank workspace path <value>` from the page. Teaching both about
positionals touches `index.html` too, which is a page change this step is not owed.)

### Step 2 — what must be brought over: the `junction-1` checklist ✅

*(Asked for 2026-09-05, Keith: "please take all the necessary inputs from junction one, highlight
what needs to be brought over so future users will know".)*

It becomes `docs/reference/importing.md`, **generated** the way `commands.md` and
`destinations.md` are, so the checklist and the reader cannot drift apart.

**The dataset — all three files, from `scenarionet-<rate>/`.** ScenarioNet needs the triple:
`dataset_summary.pkl`, `dataset_mapping.pkl`, and each `sd_*.pkl`. `data_directory` points at this
directory and nothing else in the workspace is read at run time.

**Identity and provenance**, from `source/manifest.json` and mirrored in the summary's
`metadata.provenance`:

| field | `junction-1` | why it must come |
|---|---|---|
| `driving_side` + `driving_side_source` | `left`, `explicit_cli` | the one field that silently invalidates every result — the reason Phase 2 measures drive side from the map |
| `provenance.generation_fingerprint` | `57dcd345…` | names the exact lane model this was built from |
| `provenance.source_osm_sha256` | `4607fd46…` | the OSM extract |
| `provenance.reviewed_lane_model_sha256` | `fc205bca…` | the human-reviewed model, stage 4 |
| `provenance.stage_5_status` | `passed` | refuse anything else at import |
| `stage_6.step_hz` | `100.0` | the rate the tracks were sampled at — see Step 3 |
| `metadata.coordinate` | `metadrive` | already in MetaDrive's frame; no transform on our side |
| `stage_1b.projection.origin` | 3.18589 N, 101.61155 E | where on earth this is; a PG bank has no such thing |
| `graph.bounds_wgs84` | the extract box | ditto |
| `attribution` | `OpenStreetMap contributors` | a licence obligation, and it must survive into a result |
| `tool_versions` | osmnx 2.0.7, pyproj 3.7.1, … | the analogue of `manifest.metadrive` |

**Per scenario**, from `metadata.sdc_route` (equivalently `stage_6.routes[]`): `name` → the row id;
`start_lane` / `end_lane` / `lanes[]` → the route, which is what `destination` is on the PG side;
`distance_m` (395.11) → `route_length_m`; `duration_s` (37.82) with the scenario's own `length`
(3782 at 100 Hz, 379 at 10 Hz) → the step budget, **measured rather than chosen**, which is the one
place a real-world bank is better off than a PG one; plus `speed_kph`, `slowest_kph`,
`lane_changes` (3), `junction_movements` (14), `waiting_s` and `stops[]`.

**What replaces the six option axes.** They are not knobs here. They are contents of the recording,
and the bank records what is in it rather than pretending it can set it:

- tracks: **101 `PEDESTRIAN`, 25 `CYCLIST`, 24 `TRAFFIC_BARRIER`, 1 `VEHICLE`** — that one is the ego
- `dynamic_map_states`: **8 `TRAFFIC_LIGHT`**, three phase groups, a 60 s cycle
- `map_features`: 974 — 434 lane surfaces, 455 road edges, 85 broken white lines
- and the signals note, carried **verbatim** into the manifest because it is a caveat about the
  data and not about us: OSM records only that a signal exists, so every cycle, split and offset
  was *synthesised* in the Stage 6 signal builder, never surveyed.

**Brought over as artefacts:** `stage-6-map-<rate>.png` becomes the thumbnail — a real-world bank
has no `figures.render_route` to draw — and `reports/scenario-conversion-<rate>.json`.

**Deliberately not brought over:** `bags/`, `drives/`, `inspection/*.html` (about 1 MB each),
`lane-model/*.json` (1.5 MB), `normalized/`, `source/map.osm`. A bank records their **checksums**,
which `source/manifest.json` already carries for every one of them. Same choice Phase 2 made about
`base_config`: a *record* of the input, not the input.

**Verify alone:** four checks. Three of them need no converter checkout, none of them needs a
simulator, and none of them writes anything into the converter — measured with `find -newer` over
`workspaces/`, which reports **0 files touched** by a full render.

```bash
# 1. the page reproduces from the reader, byte for byte
uv run scenariobank importing --out /tmp/importing.md
diff docs/reference/importing.md /tmp/importing.md            # no output

# 2. the coverage guarantee fires: drop one row, and the generator refuses to write a page
uv run python -c "
from scenariobank import importing
importing.LEFT = {k: v for k, v in importing.LEFT.items() if k != 'path'}
importing._check()"
# ValueError: the checklist and workspace.py disagree: ['path'] are read and not on the
# checklist, [] are on the checklist and not read.

# 3. a workspace with no conversion is a loud failure, not an empty page
uv run scenariobank importing -p ../wingfin-osm-scenarionet-converter/workspaces/junction-1a
# importing failed: junction-1a holds no converted scenario, so there is nothing to measure a
# checklist against.                                    (exit 1, and no page written)

# 4. the tests
uv run pytest tests/unit/test_importing.py -q                 # 21 passed
```

**What each one is for.** (1) is the drift guard a reader can run: it is the same comparison
`test_the_checked_in_page_matches_the_workspace_it_was_measured_on` makes, and it is honest —
appending one line to the page makes that test fail with *"docs/reference/importing.md is out of
date. Run: uv run scenariobank importing"*, and regenerating makes it pass again. (2) is the half
of the guarantee that does not depend on this machine having a converter checkout: the same error
appears with the arrow reversed (`['invented'] are on the checklist and not read`) if a row names a
field nothing reads, and a third phrasing (`more than once`) if a field is listed twice. (3) says
the failure mode of a generator over missing data is an exit code rather than a shorter document;
`junction-1`, `mosque` and `mosque-1` all render, `junction-1a` is the one that cannot.

**On a machine with no converter checkout, 13 of the 21 tests still run and 8 skip by name**
(`needs_workspaces: no converter checkout at …`) — and all four coverage tests are in the 13. That
split is the point: the strong half of "cannot drift apart" is a property of the source tree, so it
is checked everywhere; only the comparison against a real conversion needs the workspaces present.

**And the studio is not a fifth check.** `importing` reaches `/api/commands`, `/api/runnable` and
`docs/reference/commands.md` from the same walk of the Typer app that Step 1 measured — verified by
`catalog()` carrying it with no list edited — and the Run tab refuses its `--path` for the same
containment rule, which Step 4 is where it stops mattering.

*(Done 2026-09-07. `src/scenariobank/importing.py` + `scenariobank importing --path/-p --out/-o`,
`docs/reference/importing.md` generated, 21 tests in `tests/unit/test_importing.py`, **450 pass**,
ruff clean. No page file edited; the Reference and Run tabs pick the command up on their own, and
the Run tab refuses its `--path` for the reason Step 1 measured.)*

*The checklist is generated **against the reader**, not beside it. Every row names a field of
`workspace.py`'s report and every field of that report is on one of the page's two lists; `_check`
raises rather than publishing either gap, the way `docs.reference` raises for a command in no
group. That is what "cannot drift apart" had to mean in code: a doc regenerated from the same
files the reader reads could still describe fields nothing reads, and this one cannot.*

*Three corrections to the draft above, all from walking the workspace rather than reading the
manifest — the same lesson Step 1 learned about `stage_6`:*

*1. **`bags/` does not exist.** No workspace in the converter checkout has one. What is actually
left behind is `actors/`, `drives/`, `routes/`, `signals/`, `traffic/` and `review.json`.*

*2. **The manifest does not carry a checksum for everything excluded.** It carries 28, covering
`inspection/`, `lane-model/`, `normalized/`, `reports/`, `review/` and `source/map.osm` — and none
at all for the six above. So "recorded rather than copied" is true of some of what is left and
false of the rest: 5.1 MB of a `junction-1` workspace can only be dropped, and the page says which
and why. The verdicts are keyed, so a converter that starts writing a new directory makes the page
**raise** instead of quietly omitting it.*

*3. **An import moves 50.4 MB of the 70.8 MB on disk**, not all three dataset directories. The
page weighs one, because that is what an import takes.*

*The reader grew four fields to answer the checklist rather than the checklist inventing them:
`Scenario.map_feature_types` (434 lane surfaces, 455 road edges, 85 broken white lines — the split
the draft quoted), `WorkspaceReport.signals` (the synthesised plan, its 60 s cycle, its three phase
groups, and the converter's note carried **verbatim**), `WorkspaceReport.artifacts` (the 28
checksums), and `WorkspaceReport.contents` (every top-level entry, its weight, and how much of it
the manifest can record). `report_version` stays 1: the additions are additive and nothing has
consumed version 1 yet, so there is no reader in the world a bump would help.*

*`mosque` is the workspace that shows why the signals block is two numbers and not one: its lane
model declares **four** signals and stage 6 built **no** phase groups from them, so the recording
has no light in it and looks exactly like a junction that never had one. Its `scenarionet-100hz/`
also carries no actors at all while its `scenarionet-10hz/` carries four — a second instance of the
finding that dataset directories in one workspace are different conversions, not different views of
one.*

### Step 3 — `scenariobank import`, and schema 1.3 ✅

The command that turns a workspace into a bank under `--banks-root`, beside the PG ones.

- **`Manifest.source`** — `"pg"` by default, so every existing bank reads as 1.2 → 1.3 unchanged,
  or the summary's own `metadata.dataset` value, which for these is `"osm-scenario"`.
  `extra="forbid"` forces the version bump, exactly as 1.1 → 1.2 did for `options`.
- A real-world category carries `dataset_dir`, `step_hz`, `origin`, `attribution` and the
  provenance block where a PG category carries `block_seq` and `exit_rule`; a row carries
  `scenario_index`, `route` and the measured budget where a PG row carries `seed` and
  `destination`.
- **The dataset is copied into the bank, not referenced.** A bank is mounted into the container and
  shipped to a rig; a path into somebody's home directory is not. Cost: `junction-1` is 5.5 MB at
  10 Hz and **48 MB at 100 Hz** (150 actors × 3782 frames), while `mosque` is 1.3/1.5 MB because it
  has no actors at all. A 35-scenario real-world bank could reach ~1.7 GB, which is the first thing
  in this project that makes a bank expensive to move. **The mounts themselves are described once**,
  in Phase 5 Step 1: the runner takes the repo read-only and writes to `/out` alone, so a bank
  this large is carried in, never written back.
- **Import at 100 Hz, and set the physics step to match.** ScenarioNet replay advances **one
  recorded frame per `env.step`**, so the recording's rate *is* the env's rate — and MetaDrive's
  default does not match it. Measured: `physics_world_step_size=0.02` × `decision_repeat=5` is an
  env dt of 0.1 s, i.e. 10 Hz. A 100 Hz recording opened at that default replays every actor at a
  tenth of its speed **silently**: the map is right, the tracks are right, nothing raises. So a
  real-world category records `step_hz`, and the runner sets
  `physics_world_step_size = 1 / step_hz` with `decision_repeat = 1`. Verified on the 48 MB
  `scenarionet-100hz`: env dt 0.0100 s, 3782 frames, 4000 steps at 2.87 ms/step.

  This is gotcha 3 of Phase 4 Step 6 from the other side — `--step-hz 100` is the sim rate the AV3
  rig already wants, so a 100 Hz import is the one that agrees with it.
- `--rate` stays a flag, but **100 Hz is the default and the choice is asymmetric**: `--decision-hz`
  is a stride in our own loop and is never stored in a bank, so it stays adjustable per run
  forever, while `step_hz` is fixed when the pickle is written. A 100 Hz import keeps every
  decision rate that divides 100 available; a 10 Hz import caps you at 10 and cannot be undone
  without going back to the converter. It costs bytes, and it is the only one of the two choices
  that is irreversible.
- `import` refuses `stage_5_status != "passed"`, and refuses a drive side that disagrees with the
  installed handedness — reusing `doctor.measure_drive_side`, the check Phase 2 kept from the old
  Phase 3.

**Verify alone:** import `junction-1` and `mosque` into a scratch root; both manifests validate and
re-read, the thumbnail resolves, the dataset triple is complete, and no bank under `banks/` and no
file under `workspaces/` is opened for writing. `review` **refuses** rather than runs -- see the
correction below.

*(Done 2026-09-07. `scenariobank import --path/-p --out/-o --bank-id --rate`, schema 1.3 in
`bank.py`, the importer in `src/scenariobank/importing.py` beside the checklist it implements, 23
tests in `tests/unit/test_import.py`, **474 pass**, ruff clean. `docs/reference/commands.md`
regenerated; no page file edited.)*

*Measured on `junction-1` at 100 Hz: **50.0 MB** copied -- `dataset/` (the summary, the mapping and
one 49.9 MB `sd_*.pkl`), `source/manifest.json`, `reports/scenario-conversion-100hz.json`, and
`thumbs/junction-1.png` from `stage-6-map-100hz.png`. At `--rate 10` the same import is 5.5 MB and
379 frames instead of 3782. `mosque` at 10 Hz is 1.3 MB.*

**Two models, not one with half its fields optional.** `RealWorldEntry` carries `dataset_dir`,
`step_hz`, `origin`, `attribution`, the provenance chain, `tool_versions`, the 28 `artifacts`
checksums, `copied`, and the signals block; `RealWorldRow` carries `scenario_index`, `file`,
`stored_id`, the measured `max_steps`, `route_length_m`, `duration_s`, `tracks`, `lights` and the
converter's `route`. `Manifest.source` is the discriminator and a validator refuses a mixture --
a category holding both a `block_seq` and a `dataset_dir` would be a bank nothing could rebuild and
nothing could replay. `budget_for` keeps its name on both, so a runner asking for a scenario's cap
does not have to know which kind of bank it holds.

*Four corrections to the bullets above, all from building it:*

*1. **`doctor.measure_drive_side` cannot be reused here.** It reads the side off a **built map**,
and an import builds nothing -- it copies pickles. Measuring a stored scenario's map means
constructing a `ScenarioEnv`, which is Step 6's round trip. What `import` checks is the side the
converter **declared**, against `DRIVE_SIDE_LEFT`, and a right-side workspace is refused on it.*

*2. **`--rate` alone does not name a conversion.** `junction-1` holds **two** 100 Hz conversions:
`scenarionet` (3695 frames, a 403.75 m route) and `scenarionet-100hz` (3782 frames, 395.11 m).
They are different drives, not two views of one, so the rate narrows and `stage_6`'s
`last_conversion` breaks the tie -- the same rule `importing.choose` already used for the page.
`mosque-1` holds only a 10 Hz conversion, and `--rate 100` there is refused by name with the rates
it does hold.*

*3. **`review` refuses an imported bank rather than running on one.** Its every measure is built on
a seed, a block sequence and a resolved exit -- duplicates, spawn-lane coverage, and a declared cap
against `step_budget(route_length_m)`. A recording has none of them, and a review reporting
"0 duplicates" from a computation that never ran is worse than a refusal. The five editing
commands refuse the same way, by name. What a real-world bank's review **is** stays Step 5's, and
the refusal names it.*

*4. **An imported manifest records no `base_config` and no simulator.** MetaDrive did not build
this bank, the converter did, so `base_config` is `{}` and the `metadrive` block is empty --
recording `base_config()` would need the sim group to write a file about a recording that uses
neither the PG observation nor the PG physics rate. Which simulator *replays* it is a property of
the run and is the runner's to record. The result is that `import` imports no simulator and builds
no environment, like `workspace` and `importing` before it, and a test pins that.*

*The import and the checklist are one module on purpose. `_verify` refuses to copy a file
`importing.VERDICTS` does not call `partly copied`, so the page saying "this comes over" and the
code bringing it cannot drift apart -- Step 2's field coverage, on the other axis. An imported bank
also cannot pin option levels: the six axes are contents of a recording here, and `Manifest`
raises rather than storing a promise nothing keeps.*

### Step 4 — the studio splits its bank list ✅

*(Asked for 2026-09-05, Keith: "currently bank just shows the different banks generated in a list,
but lets split it up by PG banks and real world simulations".)*

Today `GET /api/banks` returns one flat list (`web/api.py:218`) and `renderBankList`
(`index.html:1448`) filters and renders it in one column.

- The endpoint gains `source` per entry, read from the manifest. No new endpoint and no second
  listing, because the discriminator is already in the file Step 3 writes.
- `renderBankList` groups the filtered rows under two headings — **Procedural** and **Real world** —
  each hidden when empty, so a studio with only PG banks looks exactly as it does now. One search
  box, filtering across both.
- A real-world row shows what a PG row cannot: the place, the rate, and the actor counts. A PG row
  is unchanged.

**Verify alone:** studio on a scratch root holding `curve` and an imported `junction-1`; two
sections, the search filters both, and a PG-only root shows one section with no empty heading.

*(Done 2026-09-07. 481 tests pass, ruff clean. Verified in a running studio on a scratch root
holding `roundy` and `curve` procedural, `junction-1` and `mosque` imported, and a `broken`
directory whose manifest will not parse: three headings, the search filters across all of them,
`junction` narrows to one group and the headings vanish, and a PG-only root renders byte for byte
what it rendered before. No console errors.)*

*Four things the plan above did not say, all from building it:*

*1. **A third group: unreadable.** A bank whose manifest will not parse is listed with its error --
that rule is older than this step -- but nothing was read, so which kind it is is unknown. Filing
it under **Procedural** would put a broken import under the wrong heading, which is a worse lie
than the one the listing exists to avoid. So its row carries no `source` at all and gets a heading
of its own, on the same hidden-when-empty rule.*

*2. **A single heading is not drawn.** "Each hidden when empty" leaves a PG-only studio with one
"Procedural" label over the whole list, which divides nothing and is not what it looked like
before. The heading appears when there is something to tell it apart from -- which also means a
search that narrows to one kind drops the chrome rather than keeping it.*

*3. **The list was not the only thing that had to know.** Splitting the list makes an imported bank
a first-class thing to click, and four screens behind it were written for a seed: the category band
read `road undefined · exit undefined`, the option-levels card offered six dropdowns every one of
which the API refuses, the scenario card said `seed undefined`, and the road builder's "add to"
dropdown offered banks `add` refuses by name. All four now branch on `source`: the band shows the
rate, the options card shows the sentence `Manifest` raises with, the card shows frames and
duration, and the dropdown lists procedural banks only. What is **not** here is the selection panel
-- comparing and replacing are built on seeds, so a recorded card is a tile rather than a button
until Step 5 says what a real-world measure is.*

*4. **The actor counts are summed, and the ego is one of them.** `tracks` is per row; the listing is
a picker, so it carries the bank's total. The converter counts the ego as a `VEHICLE` track like
every other, and subtracting it here would make the row disagree with what `scenariobank workspace`
prints about the same recording.*

### Step 5 — `review` for a bank with no seeds ⬜

`review`, the duplicate detection and the coverage chips are built on seeds, block sequences and
turn pairs. None of those exist here. This step decides what a real-world bank's review *is* —
route length, duration, actor mix, signal coverage — and makes the dataset tab render that instead
of the PG chips.

**Verify alone:** open an imported bank in the studio; every chip shown is a measured property of
the recording, and nothing reads "0 duplicates" from a computation that never ran.

### Step 6 — the one-scenario round trip ⬜  ⟵ *gate*

An imported bank drives one episode end to end under a diagnostic policy, and the result record has
the same shape as a PG one. This is what says the two bank kinds are one runner, before Phase 4
builds on the assumption.

**Two things this step must get right, both measured rather than assumed:**

- **Nothing ends the episode on its own.** `ScenarioEnv` ran 4000 steps on a 3782-frame scenario
  with `max_step: False` and `horizon: None` — it does **not** truncate at the end of the
  recording. The cap comes from the scenario's own `length`, enforced in our step loop. Phase 4's
  "enforce the same cap in our own step loop as well" stops being defence in depth here and becomes
  the only thing that ends a stored episode.
- **The 19-dimensional observation is a PG fact.** With `agent_observation=StateObservation` a
  stored scenario observes **41** dimensions, because `ScenarioEnv`'s navigation module reports
  differently; the default with no override is 161. See **The observation ceiling**, which was
  written from `MetaDriveEnv` alone.

**Verify alone:** `junction-1` route-1 runs to its own length and terminates; `--decision-hz` 20,
10 and 5 each act on the expected stride of a 100 Hz import; the result row carries the provenance
fingerprint and the attribution string.

**Done when:** Steps 1-6 are ✅ and `docs/reference/importing.md` is checked in and reproduces from
the reader.

---

# Phase 4 — Runner, results schema, reference policies ⬜

> **The commands in this phase are machine-run.** `run`, `calibrate`, `validate` and `selftest`
> are executed by the container's entrypoint, by the orchestrator, and by CI — never typed by a
> person, and **no studio screen is owed for them**. The command blocks below are developer
> verification, and they stay in that form deliberately. See **What a person actually uses**.

**Goal:** the piece the frontend calls.

## Steps — the runner first, the cameras on top of it

Eight checkpoints, each one testable alone. **They deliberately do not run in the order the
build notes were written in.** The notes lead with the AV3 port because that is the interesting
part; the work leads with `resolve_options` and env construction because a runner that is
reproducible against a two-line `ConstantPolicy` is the thing every later step is debugged
against. Steps 1-5 need no camera, no bridge and no GPU, and they end at **Test 3, the one that
matters**. Steps 6-8 are the port, and they land on a runner that is already known-good — so
when a six-camera run disagrees with a diagnostic one, the disagreement is the rig.

Markers follow the table under **Reading the markers**: ⬜ not started, 🔨 started, ✅ done when
that step's **Verify alone** is met.

**Every step below is written for a procedural bank.** Phase 3 adds a second kind, and the four
places that differ are marked *(PG only)* where they appear. The seam that keeps them one runner
is a single function between "a category and its rows" and "an env plus a per-row prepare step":
`MetaDriveEnv` + `set_route` for PG, `ScenarioEnv` + `start_scenario_index` for a stored scenario.
Above that line the run loop, the result record and the reproducibility diff never learn which kind
they are driving.

### Step 1 — `resolve_options()`: names in, numerics out ⬜

The only piece of this phase with no environment in it, so it goes first and stays unit-tested.

- `resolve_options()` reads `manifest.options` as its defaults, expands tiers, applies explicit
  flag overrides, and returns level names *and* numerics.
- The axis names and levels already exist in `options.py` (schema 1.2, `scenariobank options`);
  only the `LEVELS` numerics and this resolver are new. `LEVELS` is Phase 4b's to calibrate — this
  step ships placeholder numerics and the resolver that reads them, not the values.
- The resolver is `options_for(entry, row)` from the start (Step 10c), so per-scenario overrides
  can be added later without touching a call site.
- **The six axes are PG-only.** On a stored scenario (Phase 3) traffic, cones, barriers and lights
  come from the recording, and `ScenarioEnv` offers `no_traffic`, `no_light` and `reactive_traffic`
  instead. `resolve_options` must not assume an axis name means the same thing on both kinds; the
  resolved record says which kind it resolved for.

**Verify alone:** unit tests only, no simulator. A bank pinned at `traffic medium` resolves to
`medium` with no flags, to `hard`'s numerics under `--tier hard`, and to `low` under an explicit
`--traffic low` over a `--tier hard`; every returned record carries both the name and the number.

### Step 2 — env construction from a bank ⬜  *(PG only)*

Build the env the manifest describes, and prove the three config keys that silently do nothing
when they are wrong. **A bank is a recipe, not a saved scenario** — see Phase 2's **Scope** and
**Stored normalized, applied at run time** — so this step is that recipe's reader, and everything
in it is procedural-only: a stored scenario has no seed, no `start_seed` and no `horizon`.

**Already built — reuse it, do not restate it.** Three of the things this step used to describe as
new work already exist, with the reasoning in their own docstrings:

- **`num_scenarios_for(seeds)` — `bank.py:256`**, whose docstring already carries the
  `base_env.py:926` argument: `num_scenarios` reads as a count and is not one, it bounds an
  *index*, and sizing it `len(seeds)` raises `scenario_index (seed) should be in [0:N)` the moment
  the seeds are not contiguous. That is live rather than hypothetical — `banks/curve` holds seeds
  `[30, 1, 2, 3, 22]`, so its `num_scenarios` is 30 for five scenarios, and
  `banks/t-junction-left-intersection` holds `[0, 28, 2, 4]`.
- **Grouping and `start_seed` — `bank.py:411-412`.** `generate` already groups by
  `(block_seq, seeds)`; the runner groups by **category** instead, because what it needs one env
  for is one `horizon`.
- **`base_config()` — `config.py:38`**, with `_PER_RUN_KEYS` (`bank.py:80`) already naming `map`,
  `start_seed` and `num_scenarios` as the three a caller supplies.

**New here:**

- **`horizon` = the category's `max_steps`.** `base_config` pins `horizon: 1000` (`config.py:80`)
  and nothing maps a category onto it yet, which matters — `t_junction` is 320 and `CCS_only` 1320.
  **`horizon` is the config key** (`metadrive_env.py:60`); `max_step` is a `TerminationState` field
  and setting it does nothing.
- **The step loop enforces `budget_for(row)`** (`bank.py:165`), which is a *per-row* cap: a row's
  own `max_steps` when it declares one, else its category's. One env cannot carry two horizons, so
  these two are not the same number — `horizon` is a coarse env-level guard, and the loop cap is
  what actually bounds the episode and what the result records. It is also what makes a `horizon`
  that failed to take a bounded run rather than a silent 1000-step one.
- **Pin the destination *after* the reset**, with `navigation.set_route(env.agent.lane_index,
  node)` as `bank.py:1121` and `variety.py:124` already do — **not** through
  `vehicle_config["destination"]`, which is read at construction. Destinations vary *inside* a
  category: `banks/t-junction`'s `t_junction` is `1T0_1_` at seeds 0 and 4 and `1T2_1_` at 2 and 3,
  because `StdTInterSection` exposes a different arm per seed. One env per category and a
  construction-time destination cannot both hold. `set_route` is reproducibility-safe for the
  reason Phase 2 recorded — `auto_assign_task` draws its throwaway destination from
  `get_np_random(random_seed)`, a *fresh* generator rather than a manager's stream — and it raises
  on an unreachable node (`bank.py:1093`), which is the failure you want.
  **`bank.py:10-12` says the opposite and must be corrected with this step**: the module docstring
  tells the runner to pin `vehicle_config["destination"]`, which is where this step's wording came
  from.
- Register `VRUManager` and (Phase 8) `PGTrafficLightManager` only when their axis is above
  `none` — the obstacle manager is MetaDrive's own and `metadrive_env.py:296-300` already
  registers it conditionally.
- Wire `crash_human_penalty` / `crash_human_cost`, mirroring `crash_object`'s 5.0 / 1.0 —
  termination is already wired, the reward/cost pair is not (`metadrive_env.py:74-83`).
  `grep -rn crash_human src/` returns nothing today.

**No map cache on disk — measured, do not re-propose.** `store_map=True` is MetaDrive's own default
(`metadrive_env.py:34`) and `base_config` does not touch it, so `PGMapManager.maps`
(`pg_map_manager.py:23`) already caches per seed within an env: `reset()` builds only when
`self.maps[current_seed] is None`. Five seeds per road, measured:

| road | blocks | cold reset (mean) | warm reset |
|---|---|---|---|
| `T` | 1 | 39 ms | 5 ms |
| `C` | 1 | 48 ms | 6 ms |
| `CC` | 2 | 79 ms | 10 ms |
| `CCS` | 3 | 93 ms | 12 ms |
| `SCXCS` | 5 | 182 ms | 18 ms |
| `OSCTSCO` | 7 | 354 ms | 35 ms |

Linear at roughly 50 ms per block cold, 5 ms warm; env construction is 9 ms and a step is 0.46 ms.
A 35-scenario bank of 1-3 block roads costs 2-3 s of generation **once per run**, all-7-block roads
about 12 s — against Step 8's own figure of ~64 s per scenario for AV3, or ~37 minutes for 35. That
is about 0.1% of a run. A persistent cache would buy those seconds back while re-introducing the
cross-batch map identity the old Phase 3's `verify` existed to prove, and that was deleted on
2026-08-31.

**Verify alone:** drive a bank category with zero actions and no result file. Every seed in the
manifest resets without the `start_index` assert; an episode on a category whose `max_steps` is
200 ends at 200 steps and not 1000; a row with its own budget ends at *its* number; and the ego's
route matches the manifest row on a category whose destinations differ by seed.

### Step 3 — the result record, and a batch that never aborts ⬜

The schema everything downstream reads. Built before any policy worth scoring, so the record is
designed once rather than grown around whatever the first run happened to emit.

- Per scenario record: `success` (`info["arrive_dest"]`), `failure_reason` taken from the
  `TerminationState` fields — `arrive_dest, out_of_road, max_step, crash_vehicle, crash_object,
  crash_human, crash_building, crash_sidewalk, idle` — as a **string, not a boolean**, plus steps,
  cumulative reward, cumulative cost, wall time, and the fully expanded options.
- **Collision accounting** (needed by Phase 4b, cheap to build now): record ego-involved
  collisions separately from total, and count a collision **once per vehicle per episode, not per
  step**. Your converter's note: a per-step count "reports one collision as thirty, and the number
  describes the frame rate."
- `--save-trajectories` optional (off by default; the only large artifact).
- **Never abort the batch**: catch per-episode, record `status:"error"` + traceback, continue.
- **The step-loop cap is not only defence in depth.** On a stored scenario `horizon` is `None` and
  `ScenarioEnv` runs past the end of its recording without truncating — measured, 4000 steps on a
  3782-frame scenario with `max_step: False`. There the cap is the *only* thing that ends the
  episode, so it belongs here rather than in Step 2 with the PG env keys.

**Verify alone:** *(old test 4)*

```bash
uv run scenariobank run --policy scenariobank.policies:RaisingPolicy ...
```

**Expect:** every scenario present in `results` with `status:"error"` and a traceback; the process
still exits 0 and writes the file.

### Step 4 — the reference policies: floor and ceiling ⬜

- **Diagnostic policies keep the old signature.** `load_policy("pkg.mod:Name")` still instantiates
  and checks callability, and `ConstantPolicy` / `ExpertPolicy` still take
  `(observation: np.ndarray) -> Sequence[float]`. They are CLI-only floor and ceiling checks,
  never the thing under evaluation.
- Two are shipped: `ConstantPolicy` (a fixed action) and `ExpertPolicy`, a wrapper around
  MetaDrive's bundled PPO expert (`metadrive/examples/ppo_expert/`). Per the
  **No lidar** section, it holds the env and ignores its `observation` argument.
- The runner records the observation shape *after* the last expert episode and **fails the run if
  it moved**: `numpy_expert.py:48-49` admits its config restore is incomplete. The check is *"the
  shape did not move during this run"*, **not** a literal 19: measured, the same
  `agent_observation` on a stored scenario observes 41, because `ScenarioEnv`'s navigation module
  reports differently.

**Verify alone:** *(old tests 1, 2 and 2b)*

```bash
# 1. Floor: a constant-action policy should mostly fail
uv run scenariobank run --bank ./banks/pg-bank-2026-08 \
  --categories intersection_left --policy scenariobank.policies:ConstantPolicy \
  --out floor.json
jq '.summary' floor.json
```
**Expect:** `success_rate` near 0, `by_failure_reason` dominated by `out_of_road` / `max_step`.

```bash
# 2. Ceiling: the bundled PPO expert should mostly pass
uv run scenariobank run --bank ./banks/pg-bank-2026-08 \
  --categories intersection_left --policy scenariobank.policies:ExpertPolicy \
  --out ceiling.json
jq '.summary.success_rate' ceiling.json
```
**Expect:** substantially above the floor. If floor is approximately ceiling, the runner is not
actually feeding actions to the env — that is the bug this test exists to catch.

```bash
# 2b. The expert must not leak lidar back into the env config
jq '.env.observation_space_after' ceiling.json
```
**Expect:** `[19]` — for a **PG** bank. 19 is a fact about `MetaDriveEnv` with our
`agent_observation`, not about MetaDrive; the assertion is that it did not move mid-run.

### Step 5 — reproducibility, and options that do something ⬜  ⟵ *gate*

Nothing after this step is worth debugging until this step passes, which is why the AV3 port
starts on the other side of it.

**Verify alone:** *(old tests 3 and 6)*

```bash
# 3. Reproducibility — the real acceptance test
uv run scenariobank run ... --out r1.json
uv run scenariobank run ... --out r2.json
diff <(jq 'del(.started_utc) | del(.results[].wall_time_s)' r1.json) \
     <(jq 'del(.started_utc) | del(.results[].wall_time_s)' r2.json)
```
**Expect: empty.** Identical steps, reward, cost, failure_reason for every scenario. Run it again
with `--tier hard` so the reproducibility claim covers the option managers too, not just the map.

```bash
# 6. Options actually do something
uv run scenariobank run --categories intersection_left --tier easy --policy ...:ExpertPolicy --out easy.json
uv run scenariobank run --categories intersection_left --tier hard --policy ...:ExpertPolicy --out hard.json
jq -s '[.[0].summary.success_rate, .[1].summary.success_rate]' easy.json hard.json
```
**Expect:** hard is lower than easy. If they match, the option managers are registered but not
placing anything — the same class of bug as floor equals ceiling.

### Step 6 — the camera rig: six cameras alive on `DefaultVehicle` ⬜

**The model boundary is an AV3 camera submission** *(amended 2026-08-30; replaces the
`policy(observation: Box(19,)) -> [steer, throttle]` contract)*. Everything needed already exists
in `wingfin-osm-scenarionet-converter/` and is already MetaDrive-shaped — **port it, do not
rewrite it**. This step ports the rig half only:

**No new container for any of this** *(added 2026-09-06, Phase 5)*. `metadrive-wingfin-sim`
already carries the whole rig stack, and the gate that decides whether a rendered frame can stay
in GPU memory — `base_camera.py:10-18`, one `try:` over cupy, PyOpenGL and `cuda.cudart` — was
measured **open** in it: cupy 14.2.0, PyOpenGL 3.1.10, cuda-python 12.9.7. That gate does not
fall back when it is shut, it trips an assert whose hint names cupy even when what is missing is
one of the other two, so having it verified in the image this runs in is worth more than the
version numbers are.

- `tools/camera_rig.py` — `load_rig()`, `CameraRig.sensors/mount/read`
- `rigs/av3.txt` — the six AV3 cameras, ISO-8855 → CARLA sign rules applied, datum resolved onto
  MetaDrive's `DefaultVehicle`
- `tools/av3_probe.py` + `scripts/av3-probe.sh` — the sign-convention probe

`rigs/av3.txt`'s header records two open gaps, and they stay open: fisheye is rendered as an
unwarped pinhole, and 4:3 is rendered then squashed by preprocess, never native 16:9.

The camera rig is selected as a **path, not a registry entry**: `--camera-rig rigs/av3.txt`.

Six things that bite, in the order they will bite:

1. **`image_observation=True` is mandatory, and not for the observation.** `base_env.py:342-347`
   filters **every `BaseCamera` out of `config["sensors"]`** when `use_render` and
   `image_observation` are both false, to save render passes in headless mode. Leave it off and
   the six-camera rig is silently deleted — no error until `env.engine.get_sensor(camera.name)`
   raises inside `CameraRig.mount()`. It stays on purely to keep the cameras alive; the
   observation it would produce is overridden by `agent_observation` anyway (see **No lidar**).
   Set `vehicle_config["image_source"]` to a rig camera via `CameraRig.image_source()` while you
   are there: left at its `"rgb_camera"` default it registers a **seventh** 320x240 camera that
   nothing reads, renders it every step, and spends one of the nine buffers gotcha 6 is rationing.
2. **A partial `sensors=` override wipes `rgb_camera`** and kills the env at construction. Mount
   through `CameraRig.sensors()`, never by hand.
3. **Rates: `--step-hz 100 --decision-hz 20`.** The bridge's `_DT_MDL` is 0.05 s. `--decision-hz`
   is a stride counted in our own loop — it is *not* a MetaDrive config key, and it is never stored
   in a bank, so it stays adjustable per run on both bank kinds. `--step-hz` is not free in the
   same way: on a **stored** scenario it must equal the recording's `step_hz`, because ScenarioNet
   replay advances one recorded frame per `env.step`. See Phase 3 Step 3.
4. **Both ends negate.** MetaDrive is left-positive, CARLA right-positive, so the waypoints' `y`
   and the action's steering each flip. Six conversions stand between the model and the car and
   **not one of them raises when it is wrong** — which is why `scripts/av3-probe.sh` runs before
   anything is scored.
5. **A rig's `tick_rate` must equal the interval it is actually read at.** Nothing resamples.
6. **`MAX_IMAGE_BUFFERS = 9` is a hard cap.** panda3d fails *intermittently* past it, so a rig one
   camera over the line looks like it works and then fails on a run somebody is relying on.

**Verify alone:** `scripts/av3-probe.sh` passes on one scenario — six named sensors mounted,
buffer count at or under nine, no seventh camera registered, and every sign convention confirmed
by the probe rather than by reading. No model, no bridge.

### Step 7 — the AV3 model and the openpilot bridge ⬜

The other half of the port, plus the two things about it that are not a copy:

- `tools/av3_model.py` — `AV3Model.observe/predict_with_navigation`, `FrameHistory`, `preprocess`,
  `ego_state`, `navigation`, `waypoints`
- `tools/openpilot_policy.py` — `BridgeConnection`, `OpenpilotDriver`, `to_metadrive_action`
- `metadrive-complete/openpilot/bridge/` — the zapeta bridge image (Python 3.8, its own container).
  Use **our own** openpilot bridge, not wing-sim's — and that tree is **byte-identical** to the
  one baked into the already-built `metadrive-wingfin-openpilot:prod` (`diff -rq`, empty), which
  is what makes "our own" checkable rather than asserted. **Nothing to build**: the image mounts
  no volumes and needs no repo, and `scripts/bridge.sh start` runs it. See Phase 5 Step 3.

- **The submitted `model_dev.yml` is not the converter's.** Both repos have a file by that name
  with different schemas. `tools/av3_model.load_config` **requires every field and defaults none**
  — deliberately — and reads `MODEL_CONFIG`, defaulting to the converter's
  `config/model_dev.yml`. In the container, read the **submitted** one from the staged tree. Map
  it onto `Config`'s required keys at load, or widen the loader, but **keep the no-defaults rule**:
  a silently defaulted preprocessing field is a wrong score, not a crash. Load the submission's
  `modifiers.py` explicitly, never by importing whatever is on the path.
- **One interpreter, not two.** The converter's host setup splits MetaDrive (3.8 / numpy 1.24)
  from the converter (3.10 / numpy 2.2), which is why `tools/` uses path-inserted imports and
  exchanges through files. This container does not inherit that — it pins MetaDrive at `85e5dadc`
  on one 3.10 interpreter, as the converter's own `docker/Dockerfile` already does. So the ported
  tools become ordinary package modules under `src/scenariobank/av3/`, and `_PortablePickler` is
  unnecessary. The only real process boundary left is the zapeta bridge, which stays 3.8 in its
  own container.

**Verify alone:** one scenario, `--camera-rig rigs/av3.txt`, bridge up. The model returns an
action per decision tick at `--decision-hz 20`, a deliberately missing field in the submitted
`model_dev.yml` raises at load rather than defaulting, and steering sign matches Step 6's probe.

### Step 8 — an AV3 submission scored end to end ⬜  ⟵ *gate*

Steps 1-5 with Step 7's policy in place of `ExpertPolicy`. Nothing new is built here; this is the
step that says the two halves are one runner.

Cost, and it drives the ETA model in Phase 7 Step 8: **the AV3 forward pass is ~1 s**, about 20x a
50 ms decision. Price a 35-scenario bank before quoting anyone a runtime — and record the measured
per-scenario wall time here, because Phase 7 Step 8 reads it rather than re-measuring it.

**Verify alone:** Step 5's reproducibility diff, run against the AV3 policy on a small category,
is still empty; the run records the rig path, the resolved options and a per-scenario wall time.

**Done when:** every step above is ✅. Step 5 is the one that matters.

---

# Phase 4b — Calibrate the levels ⬜

> **Machine-run.** `calibrate` is a measurement tool; its product is
> `docs/reference/level-calibration.md`, not a screen. See **What a person actually uses**.

**Goal:** replace the provisional numbers with measured ones. Requires the invariance tests green.

The `LEVELS` table is a guess. A `high` traffic setting that makes every intersection unpassable is
not a test point, it is a broken scenario.

```bash
uv run scenariobank calibrate --axis traffic --values 0 0.05 0.1 0.2 0.3 0.4 \
  --category intersection_left --policy scenariobank.policies:ExpertPolicy
```
Pick four values that spread success rate apart; bake them into `options.py`; record the sweep in
`docs/reference/level-calibration.md`. Repeat per axis.

**The floor: usable traffic values are `0`, or `0.01` and up — nothing in between.**
`traffic_manager.py:65-67` short-circuits on `abs(density) < 1e-2`, so anything smaller is silently
*no traffic at all*. This bites when tuning downward: if `low = 0.05` is too heavy, halving toward
0.025 → 0.012 → 0.006 lands you on a "low" that is identical to "none" while still being labelled
`low` in every result. Invisible from the form; a constraint on this sweep only.

**Expect a confound and control for it.** PG has neither traffic lights nor an IDM give-way rule, so
at higher densities the intersection categories will show traffic-vs-traffic collisions the ego did
not cause. The two mitigations are already built in Phase 4 — ego-involved collisions recorded
separately, and collisions counted once per vehicle per episode. Read the ego-involved number when
calibrating; the total is context.

**Done when:** `docs/reference/level-calibration.md` shows, per axis, four levels with visibly
separated success rates, measured rather than guessed, and `options.py` matches it.

---

# Phase 5 — Containers ⬜

> **Machine-run.** These are entered by CI, by the orchestrator and by the studio's own worker, not
> by a person at a terminal — with one exception, `docker compose up studio`, which serves a page a
> person does use. See **What a person actually uses**.

**Goal:** the same numbers on your machine, in CI, and on the frontend's host.

**Reuse, do not rebuild.** This phase used to say "adapt `docker/Dockerfile` from the converter" and
list the hard parts it already solves — `ubuntu:22.04`, `uv`, `UV_PROJECT_ENVIRONMENT=/opt/venv`,
`metadrive.pull_asset`, the panda3d `Config.prc` patch preferring EGL over GLX, the
`glvnd/egl_vendor.d` manifest, `HOME=/tmp`. That was written before anyone checked whether the
image those lines produce already runs this repo. **It does**, measured 2026-09-06:

```
$ docker run --rm -v $PWD:/work:ro metadrive-wingfin-sim:latest python -m scenariobank doctor
commit:        85e5dadc6c7436d324348f6e3d8f8e680c06b4db     requested: 85e5dadc
asset_version: 0.4.3    python: 3.10.21    numpy: 2.2.6
obs_space:     Box(-0.0, 1.0, (19,), float32)               drive_side: left
```

No build, no install, no `PYTHONPATH` — and that output *is* what this phase's acceptance asks for.
So the from-scratch Dockerfile is cut, and what replaces it is one two-line image for the studio.

**Why no `PYTHONPATH`.** The base image's editable install is a single bare path line,
`/work/src`. `site` evaluates it at every interpreter start, so whatever is mounted at `/work` has
its `src/` on `sys.path`; `/work` is also that image's `WORKDIR`, so `banks/curve` resolves the way
it does on the host. The cost is that the mount shadows the converter's own source, so
`import osm_scenario` does not work in there — nothing of ours imports it.

**R1 is untouched.** We import none of that repo's code. We name one of its build products, the way
a lockfile names a wheel.

## Three containers, one of them ours

| | image | who builds it |
|---|---|---|
| 1 | `metadrive-wingfin-sim` | the converter repo. **Reused unchanged** — `generate`, `run`, the camera rig, the AV3 model |
| 2 | `metadrive-wingfin-openpilot:prod` | the converter repo. **Reused unchanged** — the control stack behind TCP 5558 |
| 3 | `scenariobank-studio` | **here.** `FROM` #1 plus fastapi, uvicorn, httpx2 |

**Phase 7 adds no fourth image.** Its Step 1 runner image is "extends Phase 5", and its Step 3
launches each run as a *sibling container* — a run of #1 under a supervisor, not an image to build.

## What is already in the base, measured by import rather than read off a label

| | version | matters to |
|---|---|---|
| metadrive | `85e5dadc` — **the commit `pyproject.toml` pins** | everything |
| numpy / pydantic / structlog / typer | 2.2.6 / 2.13.4 / 25.5.0 / 0.27.0 | all inside our declared ranges |
| matplotlib | 3.10.9 | `figures.py` |
| pytest / ruff | 8.4.2 / 0.16.1 | the suite runs in there |
| torch / tensorrt | 2.8.0+cu128 / 10.12.0.36 | Phase 4 Step 7 |
| cupy / PyOpenGL / cuda-python | 14.2.0 / 3.1.10 / 12.9.7 — the `image_on_cuda` gate is **open** | Phase 4 Step 6 |
| `metadrive.envs.scenario_env` | imports | Phase 3 |
| fastapi / uvicorn / httpx | **absent** | the studio, and only the studio |

So **Phase 4 Steps 6 and 7 need no new container**, and neither does Phase 3.

**Size costs disk, not startup, and this is measured so it is not re-argued.** `docker run` on the
13.4 GB base exits in **0.29 s**, against **0.27 s** for a 78 MB `ubuntu:22.04` — the layers are
already unpacked and a run mounts an overlay and execs. What costs time is imports, and they are
the same libraries in any image: bare interpreter 17 ms, `import scenariobank.cli` **204 ms**,
`import metadrive` 2.3 s, `import torch` 1.3 s. The studio pays only the 204 ms; the 2.3 s is per
*job subprocess*; the 10.5 GB of torch/TensorRT/CuPy is never imported at all unless the AV3 policy
runs. It is inert weight on disk, not latency and not memory. A right-sized studio image was
considered and rejected on the same measurement: ~3 GB alone, but it shares almost no layers with
the base, so on a machine that has both — which every machine does, the runner needs #1 regardless
— it *adds* ~2.9 GB where the layer adds 40 MB.

## Steps

### Step 1 — `compose.yaml`: our repo, their image, no build ⬜

Two services over one image, plus `scripts/sim-image.sh`, which is the guard.

- **`image:` with no `build:` key on the runner.** A `docker compose build` in this repo must be
  unable to produce something under the tag `metadrive-wingfin-sim`; that is the failure the
  converter's own `wingfin.groups` label exists to catch, and the cheapest fix is to make it
  impossible here.
- **The runner mounts `.:/work:ro`.** Read-only *is* the test: a runner that can rewrite the bank
  it is scoring makes "the same numbers everywhere" uncheckable. `${OUT_DIR:-./out}:/out` is the
  only writable path.
- **The studio mounts `.:/work` writable**, because authoring is the point — `generate` writes into
  `banks/`, `replace` rewrites a row, and the job log and queue live in `.studio/`.
- `user: "${DOCKER_UID:-1000}:${DOCKER_GID:-1000}"` with `/etc/passwd:ro` and `/etc/localtime:ro`.
  Neither is tidiness and both are inherited traps: without the passwd mount
  `pwd.getpwuid(os.getuid())` raises and `torch_tensorrt` calls it at **module scope**, so
  `import torch_tensorrt` dies with `KeyError: getpwuid()` before the model can load; without the
  clock mount glibc falls back to UTC and a bank's `generated_utc` comes out hours adrift of the
  same run made outside the container.
- **`gpus: all` on the runner only.** `generate` needs no GPU — top-down rendering is pygame on the
  CPU — so the studio asks for none and a machine with no NVIDIA runtime can still author.
- **Entry is `python -m scenariobank`, never the console script**: that script is not in the base
  image, and `__main__.py` exists precisely because this is already how the studio spawns jobs.
- **`scripts/sim-image.sh`** reports what is present, reads the `wingfin.groups` label the way that
  repo's own `sim.sh` does, and — the whole reason it exists — replaces compose's `pull access
  denied for metadrive-wingfin-sim`, which names a registry that was never involved. An image with
  *no* label is reported as silent, not as stale: the label was added after the groups were.

**Verify alone:** `doctor` in the container prints the same commit and `drive_side: left` as the
host; a `generate --out /work/banks/x` inside the runner fails on the read-only mount; the guard
names the build command on a machine without the image.

### Step 2 — `docker/studio.Dockerfile`: the one image built here ⬜

`FROM metadrive-wingfin-sim:latest`, then the web group. Nothing else — no `pull_asset`, no EGL
patch, no glvnd manifest, all inherited.

- **`uv pip install`, never `uv sync`.** A sync makes the environment match the lock *exactly*, so
  it would strip MetaDrive, torch, TensorRT and CuPy back out of `/opt/venv` — the entire thing
  being inherited. The versions still come from `uv.lock` rather than a second list:
  `uv export --frozen --only-group web` resolves that group out of it, so the three packages and
  the seventeen they pull are declared once, in the file that already declares them.
- **No `chmod -R /opt/venv`.** On overlayfs, modifying a file in a lower layer copies it up, so a
  recursive chmod writes a fresh 13 GB copy of the venv into a layer whose content is 40 MB. uv
  writes 644/755 under the build's umask, which the runtime uid reads.
- **`--network host`, not `ports:`.** `cli.py:955` refuses any `--host` outside `_LOOPBACK`, so a
  published port — which needs a bind to `0.0.0.0` inside — is refused by our own guard, correctly.
  Host networking means the server binds the host's real 127.0.0.1, so the guard keeps meaning what
  it says.
- **Only `pyproject.toml` and `uv.lock` are copied**, so editing any source file leaves the layer
  cached; `.dockerignore` keeps `.venv` and `banks/` out of the context.

**Jobs stay subprocesses, and this records the decision not to change that.** `web/invoke.py` runs
`sys.executable -m scenariobank <cmd>`, so a job is a child of the studio process and runs in the
studio's own container — which is the whole reason this image carries MetaDrive. The alternative is
Phase 7 Step 3's shape, a sibling container per job through the docker socket. Rejected here on
three grounds: it rewrites `invoke.py` and `jobs.py` for no behaviour a person would notice; it
mounts `/var/run/docker.sock` into a server with no authentication, the same exposure `cli.py:955`
exists to limit; and Phase 7's supervisor is unwritten, so converging on it now means guessing at
an interface its own step has not defined. Revisit when Phase 7 Step 3 is real.

**Verify alone:** the studio comes up on 127.0.0.1:8770, the Run tab lists the commands, and a
`generate` job launched from the page writes a bank into the mounted repo — which is the claim that
this image must contain MetaDrive.

### Step 3 — the bridge: confirm, do not build ⬜

`metadrive-wingfin-openpilot:prod` is reused as it stands. This step exists so that "nothing to do"
is a checked fact and not an assumption.

- It mounts nothing and needs no repo: `bridge.sh start` is `docker run -d --network host … python3
  -m zapeta.server`, and the wire protocol is 29 lines of length-prefixed JSON on TCP 5558.
- The vendored fork at `metadrive-complete/openpilot/` and the image's own build context are the
  same tree — `diff -rq` is empty. Record that; it is what makes "our own bridge, not wing-sim's"
  checkable rather than asserted.
- **One coupling worth writing down for future users:** `AV3_MPC_MENU="4 16 20 32"` prebuilds one
  acados solver per waypoint count. A model with a count outside that menu still runs — the solver
  is generated and compiled on first use — but the first decision then pays a compile it should not.
- We port the client, not the container: `tools/openpilot_policy.py` → `src/scenariobank/av3/`,
  which Phase 4 Step 7 owns.

**Verify alone:** `bridge.sh status` reports the image present and something listening on
127.0.0.1:5558, and the ported client's `init` handshake gets `ready` back with nothing rebuilt.

### Step 4 — host and container agree ⬜  ⟵ *gate*

```bash
docker compose run --rm run run --bank /work/banks/pg-bank-2026-08 \
    --categories intersection_left \
    --policy scenariobank.policies:ExpertPolicy --out /out/docker.json
diff <(jq 'del(.started_utc)|del(.results[].wall_time_s)' out/docker.json) \
     <(jq 'del(.started_utc)|del(.results[].wall_time_s)' ceiling.json)
```

**Expect: empty.** If it is not, the bank is not portable and the premise needs revisiting before
the frontend touches it.

There is no `selftest` command and this phase no longer asks for one. It was going to build a known
seed and assert left-side drive as a build check for an image we now do not build — and `doctor`
already prints `drive_side` from a real reset, in the container, on demand.

**Done when:** the diff is empty, the read-only mount refuses a write, and the studio image serves
a page that can launch a job.

---

# Phase 6 — `CONTRACT.md` and handoff ⬜

> **Machine-run.** `validate` and `schema` are CI's, checking that the examples in `CONTRACT.md`
> still parse. See **What a person actually uses**.

**Goal:** your colleague can build the frontend without reading any of your Python.

**Build**
- `CONTRACT.md`: both JSON schemas field-by-field, with the rules that matter to him:
  - **The camera-only statement, at the top of the options section.** A state-vector policy cannot
    perceive traffic, cones, barriers, pedestrians, cyclists or lights. Nobody should read a
    state-vector model's collision result as a model defect.
  - **The bank is left-side traffic** (right-hand-drive market), stated as plainly as the
    camera-only note above and for the same reason. The ego keeps left, roundabouts
    circulate clockwise, on-ramps join from the left, and the turn that crosses oncoming
    traffic is the **right** turn — so `intersection_right`, not `intersection_left`, is
    the unprotected one. A model trained for right-side traffic will fail this bank for
    reasons that are not model defects, and nobody should read those results as one.
  - **Seeds vary geometry and route; options vary difficulty.** State it explicitly — it is the
    distinction most likely to be misread, and it decides whether a result means "the model can do
    left turns" or "the model can do *this* left turn".
  - The six axes, their four levels each, and the resolved numeric each maps to; the tier aliases.
  - Options are echoed **expanded** in results, level name *and* resolved numeric.
  - `scenario_id` is the key he stores; **seeds are ours and may change between banks**.
  - `metadrive.commit` is echoed back in results as a **label**, so an old result can be read
    later. Nothing refuses on it: a bank is per-batch, and roads are not promised stable across
    batches.
  - **Success rates are over 5 scenarios per category — 20% granularity.** So the UI must not render
    `0.6` as though it meant 60% +/- 1%.
  - Thumbnails are relative paths inside the bank dir, 512x512 RGB PNG, and **map-only** — never
    showing traffic, cones, barriers, pedestrians, cyclists or lights, whatever the options say.
    Also **not to scale between scenarios**: each map is zoomed to fit its own frame, so a
    junction that looks larger in one tile is not larger, just in a smaller map. Do not size or
    compare anything off the pixels. **The resolution must stay square**: `draw_top_down_map`
    (`utils/draw_top_down_map.py:11`) renders a square 2000x2000 canvas and scales the map's longest
    axis to fill it (`top_down_renderer.py:53-61`), so a non-square output stretches a square source.
  - **What the evaluated model observes.** The six AV3 cameras, read off the rig; the env
    observation is a `Box(19,)` state vector (6 ego + 10 navigation + 3 line-detector scalars, no
    lidar) used only by the CLI diagnostic policies. Note alongside it why the ScenarioNet `.pkl`
    replay path is rejected: `ScenarioEnv` swaps `NodeNetworkNavigation` for `TrajectoryNavigation`
    and changes the task, observing **31** numbers rather than 19 (`trajectory_navigation.py:20-21,211`).
  - `failure_reason` is a closed enum — list every value so he can build the grouping UI. Includes
    `crash_human`, and `run_red_light` once Phase 8 lands (schema v1.1).
  - `status: "error"` is distinct from `success: false`; an error means we learned nothing.
  - Exit codes: 0 = ran, 2 = integrity refusal, 1 = internal.
- Machine-readable schemas emitted from the pydantic models:
  `scenariobank schema --manifest > schemas/manifest.v1.json` and `--results`.
- `scenariobank validate --results results.json` so he can self-check.
- Committed example `manifest.json` and `results.json` in `examples/`.

**How you test it**
- Hand him `CONTRACT.md` + the two example files and nothing else. He builds the picker against
  the examples. If he has to ask you a question answerable from the code, the doc is incomplete.
- `uv run scenariobank validate --results examples/results.json` → exit 0.
- Round-trip: `validate` a results file with `failure_reason: "banana"` → exit non-zero.

---

# Phase 7 — The orchestrator and the rig runner ⬜  ⟵ *the deliverable*

**Goal:** a MetaDrive job put on the NAS queue is leased by our orchestrator, dispatched to a free
GPU on one of two rigs, run in a container, and its results saved back on the NAS — with nothing of
Tyrone's imported (R1).

Superseded: the wrapper sketch that used to sit here, and the shared-`jobs`-table design that
replaced it. **The queue is not a table we write SQL against.** It is `wfqueue`, an HTTP service on
the NAS with lease/ack/nack semantics, documented in `docs/queue-docs/queue-doc-v0.json`, with a
392-line stdlib-only Python client at `docs/queue-docs/queue-client-v0.py`. **Use that client. Do
not reimplement the HTTP calls** — it already handles leasing, ack/nack, retry backoff and
long-polling, and every one of those is a thing to get subtly wrong.

Read **How this ships** first for the topology and, in particular, for what the four words mean.

---

## Three properties of the queue, and what each one forces

These are not background. Each dictates a specific piece of code, and each is a silent failure if
missed.

**1. Delivery is at-least-once.** The queue doc says it outright: *"Leasing is at-least-once: make
handlers idempotent, or use `dedupe_key` upstream."* If our orchestrator dies mid-run, the lease
expires, the message returns to `ready`, and it is leased again — **while a rig is still running
it.**

> **Forces:** `POST /runs` on the rig is **keyed by job id and idempotent**. A job already in
> flight returns its existing run, `200`, rather than starting a second one. That single rule is
> what makes redelivery harmless instead of a double-booked GPU, and it is why the orchestrator can
> be restarted at any moment without a reconciliation dance.

**2. A lease is a clock, and our work is longer than it.** `visibility_timeout` defaults to 30 s
(`POST /topics/{topic}/lease`); a 35-scenario bank is minutes, and a 1,000-scenario one is far
longer.

> **Forces:** the orchestrator calls `msg.extend()` on a timer for the whole run, and stops the
> instant the run ends. A missed extend does not lose the job — it *duplicates* it, which is worse,
> and property 1 is the only thing standing between that and two runs on one card.

**3. `nack` is not the same as failure.** A rig that is busy, or whose lock is held by CARLA, has
not failed. Dead-lettering after `max_attempts` is for jobs that are **wrong**, not jobs that were
**unlucky**.

> **Forces:** busy → `msg.nack(retry_after=...)`, so the job returns to `ready` and is tried again,
> on this rig or the other. Only a job that cannot ever run — a bad options file, a missing
> checkpoint, a validation failure — is allowed near the dead-letter pile.
>
> This is the same distinction the previous draft called `waiting`, and it remains **the single
> most likely wrong behaviour in this whole phase.**

---

## The lock is rig-local, and CARLA shares the cards

Confirmed 2026-09-01: CARLA jobs run on these same two rigs. So the flock stays the authority, for
the reason it always was — things outside *both* queues take a card: `deployment/run_local.sh`,
hand-run scripts, and `free_gpu.sh --free`, which his attempt script runs as **root** with
`--pid=host`. Inside that container `id -un` is root, so its "spare a python3 if it is mine" rule
does not apply: **it will terminate a MetaDrive run holding the card.**

The split that follows, and it is the design:

- **The orchestrator's GPU map is a plan.** It is allowed to be stale, and it usually is.
- **The rig's lock is the truth.** Checked *on the rig*, because it cannot be checked anywhere
  else: `rig/lock.py:43,73-123` excludes by **inode** and reads liveness out of local `/proc/locks`
  plus `/proc/<pid>/stat` starttime. Host-local by construction. On an NFS mount it would not mean
  the same thing, and from the NAS it cannot be seen at all.
- **A rig that cannot take the lock answers `busy`**, and the orchestrator re-plans. It does not
  fail the job, it does not tear anything down, and it never signals a holder it did not start.

---

## Why the rig half is a service rather than an SSH command

The orchestrator *calls* the runner, so something must be listening. Three properties are worth
having and all three come from the same choice — **the runner holds no state of its own**:

- Every answer it gives is read back off disk: the log file, the exit-code file, and the
  per-scenario record directory. `rig/session.py:458-521` is the reference for this and is worth
  reading before writing it.
- So a runner restarted mid-run still describes that run correctly, and adoption after a restart is
  **the same code path** as a normal poll rather than a special case.
- And a run is owned by the Docker daemon, not by the service: `start_new_session` escapes a
  process group but not a PID namespace or a cgroup, so a 25-minute run must not be a child of
  anything that can be restarted. `rig/session.py:236-258` paid for that lesson.

**A runner that keeps progress in a variable breaks restart recovery silently.** Same reason
`rig/progress.py` has no table: progress is derived, never stored.

---

## Steps

Each is buildable and verifiable on its own, and the order is deliberate: **the rig half first**,
because it can be driven by hand with `curl` long before a queue is involved.

### Step 1 — the container image and entrypoint ⬜

Extends Phase 5. The container reads an options file plus a scenario list, calls `run_bank()`,
writes `results.json`, exits 0. Four additions, all so a supervisor never has to parse prose:

**And one question this step inherits, deliberately unanswered until here.** Phase 5 reuses
`metadrive-wingfin-sim`, an image built by the converter repo and published to no registry. That
is right for a laptop, where the image is already present. A rig is the other case, and it has two
answers: carry the image across (`docker save | gzip`, the way `bridge.sh save` already does for
the bridge — 13.4 GB), or build a runner image from **our** `uv.lock`, which owes the converter
nothing and drops the ~10 GB of osmnx, geopandas, torch and TensorRT a `run` never imports. Decide
it when there is a rig to decide it on; do not pre-empt it in Phase 5.

- **Structured JSON lines on stdout.** One object per event. No regexes — his `rig/progress.py`
  scrapes four prose patterns out of CARLA's log because it has no choice; we do not.
- **A per-scenario record directory and its exit-code file**, written when each scenario starts and
  ends. These two files are the progress signal, so a bar moves without anything reading the log.
- **Teardown inside the launched script**, not the supervisor — a dead runner must still bring the
  stack down and still record exit codes.
- **Never let `KeyboardInterrupt` raise into `env.close()`.** It unwinds panda3d's GL context and
  bullet's world; that segfaulted and wedged the GPU until a reboot. `tools/drive.py` is the
  precedent — it keeps its exit handler armed until teardown returns. A cancelled run that still
  writes its results is a scored partial run; one that does not is a lost one, so budget the stop
  timeout rather than taking a 10 s default. A wedged GPU blocks CARLA too.

**Verify alone:** `docker run` it by hand with a one-scenario options file; get a `results.json`.

### Step 2 — the lock helper (R1: our own, same paths) ⬜

Advisory `flock`, **exclusion by inode**, one lock file per GPU. ~150 lines.

- **Name it per device** — `.wing-sim.gpu<N>.lock` — and record in `CONTRACT.md` that wing-sim's
  single `.wing-sim.gpu.lock` serialises a whole rig. *Open question for Tyrone; do not assume he
  will change it.* Until he does, a two-GPU rig behaves as a one-GPU rig whenever CARLA is running.
- **Publish holder identity as a separate file.** Atomic replacement is a rename, and a rename gives
  the path a new inode, voiding every outstanding lock — so never write into the lock file itself.
- Confirm acquisition by finding the launched process in `/proc/locks`, rather than trusting a
  return value.

**Verify alone:** hold it from a shell (`bash wing-sim/deployment/with_rig_lock.sh sleep 60 &`),
confirm the helper reports it foreign and refuses.

### Step 3 — the rig session (R1: our own) ⬜

Takes the lock, launches the run as a sibling container, supervises, tears down. ~300 lines. His
`rig/session.py` is the reference, but it takes `presets=` and emits CARLA compose commands, so this
is a sibling rather than a reuse.

- **Sibling container, not a detached child** — see above.
- **Never inherit the environment wholesale.** His `rig/compose.py::child_environment` returns only
  `HOME/PATH/HEADLESS/QUALITY/COMPOSE_MENU`, and the reason is that a developer's exported setting
  otherwise silently changes what a model is scored on.
- **Own compose project name, container prefix and labels**, so a stray-container sweep on either
  side can never reach the other. Label with the job id and the attempt, as he does — that is what
  makes a sweep able to tell whose container it found.
- The zapeta bridge listens on 5558 in both stacks and both use host networking. **Pick a different
  port**, so a collision is an error rather than a wrong number — on a shared rig they *can* now
  run at the same time on different cards.

**Verify alone:** one scenario end to end, driven from a Python REPL. No HTTP anywhere yet.

### Step 4 — `metadrive-runner`: the service on each rig ⬜

The thing the orchestrator calls. Small, and stateless by construction.

```
POST   /runs                    {job_id, gpu, bank, scenarios[], options, checkpoint_path}
GET    /runs/{job_id}           state, per-scenario progress, exit code
GET    /runs/{job_id}/results   results.json once it exists
DELETE /runs/{job_id}           cancel: stop the container, keep what was scored
GET    /health                  per-GPU lock state, disk, image tag, runner version
```

- **`POST /runs` is idempotent on `job_id`** — property 1. In flight → return the existing run.
  Already finished → return its result. Never a second container for one id.
- **Structural validation before the job can take a card**: the options file parses, the bank
  manifest is readable, exactly one checkpoint at the given path with a suffix that matches, and
  `modifiers.py` **parsed to AST and never imported** — it is a file an authenticated stranger
  uploaded.
- **Lock held by anything → `409 busy`**, naming which GPU and whether the holder is ours. Do not
  tear down, do not signal.
- Every `GET` answer is read off disk, so the service can restart under a running job.
- Own auth: a bearer token per rig, checked against a hashed value (~20 lines). Not because the
  network is hostile, but because "the orchestrator" and "someone's laptop" must not be the same
  caller.

**Verify alone:** `curl` a two-scenario job; poll it; cancel one; **restart the service mid-run and
confirm the next `GET` describes the same run.** That last one is the whole design in one test.

### Step 5 — the orchestrator: the lease loop ⬜

On the NAS. `QueueClient` from `docs/queue-docs/`, one topic, long-polled.

1. **Before leasing anything, ask every rig what it is running.** That is the recovery path, and it
   is the same code path as a normal poll — no special case, no reconciliation table.
2. `consume(topic, wait=...)`, one message at a time.
3. Read the options; choose a rig and a GPU from the plan; `POST /runs`.
4. `409 busy` → `nack(retry_after=...)` and move on. **Not a failure.**
5. Extend the lease on a timer while polling `GET /runs/{job_id}`; stop extending the moment it ends.
6. Fetch results, save them (Step 6), `ack`.
7. A run that failed *for a reason that will recur* → `nack(dead=True)`. Everything else retries.

**Verify alone:** `put()` a job by hand and watch it land on a rig, with both rigs' runners up.

### Step 6 — results storage on the NAS ⬜

Our own SQLite plus a results tree. Not his schema, and no mapping — the shape is ours.

- **Per-scenario rows, not columns on a job.** A job legitimately ends with 30 of 35 scored, and
  `skipped` is a real outcome distinct from `failed`, because "never ran" and "ran and failed" lead
  to different next actions.
- **Mint the per-scenario identities before dispatch.** This is the one design choice worth stealing
  outright from him: because the name is decided in advance, attributing a result is reading a path.
  The version it replaced diffed `out/` before and after, which is how results get attached to the
  wrong submission.
- **Ingest is idempotent** (`REPLACE` on the scenario id), because property 1 means the same result
  can arrive twice.
- **Archive before you overwrite anything**, so evidence exists before anything can go wrong. The
  archive root must be **absolute**, must **not** be inside the repo checkout (or a `git add -A`
  sweeps up a colleague's weights), and must not be inside anything a round wipes.

**Verify alone:** ingest the same `results.json` twice; the row count does not move.

### Step 7 — thin round-trip end to end ⬜  ⟵ *gate*

Before the bank is correct, prove the whole path with a stub:

1. The Step 1 image, taking one scenario and writing a `results.json`.
2. A real message on the real queue, leased by the real orchestrator.
3. Dispatched to one real rig, results saved on the NAS, message acked.

Then wire the real bank behind it. **A green round-trip against a stub is worth more than a correct
bank nothing can run**, and here it also proves the routing before either side is finished.

### Step 8 — `GET /options` and the ETA ⬜

- **The six axes served as data**, so a frontend renders the form from the schema instead of
  hard-coding it. This is what keeps the picker in step when an axis is recalibrated in Phase 4b.
  **Two consumers**, not one: the wing-sim webapp and our own studio's submit screen (Phase 2c,
  Step 12). Two producers onto the queue, for the same reason — the webapp is no longer the only
  way a run gets enqueued.
- **The ETA.** Bootstrap from measured per-category wall time — Phase 4b's calibration runs produce
  it for free — keyed on `(category, tier)`, then replace it with a **median** of the last N real
  runs. Median, not mean: one degraded run is a 5x outlier that poisons a mean for weeks. With a
  ~1 s AV3 forward pass a 35-scenario run is long enough that an absent estimate is a visible gap.
  Two rigs means the estimate is per rig, or it is wrong on the slower one.

---

## How you test it

Four failures that are all **silent**, which is why each gets an explicit test rather than a hope.

**A double-lease must not double-run** — property 1, and the one that costs a GPU:
```bash
# lease with a short visibility_timeout and then do nothing; let it expire mid-run
python3 -c "from client import QueueClient; QueueClient(NAS).lease('metadrive', visibility_timeout=5)"
curl -s rig-a:9000/runs | jq 'length'      # expect 1, still 1 after redelivery
```

**A busy rig must not fail a job** — hold the card from outside both queues:
```bash
bash wing-sim/deployment/with_rig_lock.sh sleep 120 &      # on rig A, not a queued job
curl -s nas:8080/topics/metadrive/stats | jq '.dead'       # expect 0 throughout
curl -s rig-b:9000/runs | jq '.[].job_id'                  # it went to the other rig
```

**The two orchestrators must not interfere** — queue a CARLA job and a MetaDrive job together; each
is leased by its own orchestrator only, and the rig lock serialises them on a shared card.

**Restart recovery, on both halves** — kill the orchestrator mid-run and restart it; separately kill
a rig runner mid-run and restart it. Both must adopt the run rather than orphan it, and no second
container may appear.

**Parity with the CLI** — `put()` a two-scenario job and assert the stored result is identical to
the `results.json` the CLI produces for the same inputs. Both are callers of `run_bank()`; if they
disagree, one of them is configuring the env differently.

---

# Phase 8 — Traffic lights, camera-model scope  (~1 day) ⬜

> **Machine-run.** The two command blocks below are developer verification of the lights work, not
> a surface. See **What a person actually uses**.

**Goal:** the Lights axis. Requires the invariance tests green.

Two pieces, not four. The observation extension is skipped because a camera policy sees the light
natively; the stop-line geometry is skipped because ghost contact already yields the correct
violation semantics. See "Traffic lights, in detail" for why nothing exists to reuse.

1. **`lights.py` — `PGTrafficLightManager(BaseManager)`.** Walk `map.blocks`, find `InterSection` /
   `TInterSection`, spawn `BaseTrafficLight` on incoming lanes, cycle phases on a per-seed schedule.
   Port the phase model from `tools/signal_control.py`: one shared `cycle_seconds`, per-group
   `green_seconds` / `yellow_seconds` / `offset_seconds`, **red as the remainder so the three can
   never fail to add up.** Four traps that repo already paid for:
   - Take the time step from the engine (`physics_world_step_size * decision_repeat`), not from any
     configured rate. Its note: *"reading the tape's rate here was right only by coincidence. Do not
     put it back."*
   - Randomise **one offset per episode applied to every group**, never per group — "randomising
     each group separately would put crossing movements green at once."
   - Place the light at the stop line, not the default `PLACE_LONGITUDE = 5` (5 m *into* the lane).
   - Destroy and respawn every episode: `engine._object_clean_check` only asserts on
     `BaseVehicle`/`TrafficObject`, so a stray light survives `reset()` with `self.lane` pointing at
     freed map memory.
2. **`TerminationState.RUN_RED_LIGHT`** plus done/cost/reward wiring, reading the already-correct
   `vehicle.red_light` flag. ~20 lines. Bumps the results schema to v1.1 (one new enum value).

**How you test it**
```bash
uv run scenariobank inspect --category intersection_left --seed 3 --lights medium --render
```
Watch a full cycle: the light changes colour, and IDM traffic stops for red.

```bash
uv run scenariobank run --scenarios intersection_left_0000 --lights medium \
  --policy scenariobank.policies:ExpertPolicy --out lights.json
jq '.results[].failure_reason' lights.json
```
**Expect `run_red_light` to appear.** The PPO expert has no light awareness whatsoever, so it should
sail through reds. If it never appears, the termination is not wired up. That is the test: the
*expert failing* is the pass condition.

**Known limitation to document:** a state-vector policy cannot perceive these lights at all. Note it
in `CONTRACT.md` beside the axis — though the camera-only statement at the top of that section
already covers it.

---

## Still open

1. ~~**Who owns the Policy adapter?**~~ **Resolved 2026-08-30.** We do. The evaluated model is an
   AV3 camera submission and the adapter is ported from
   `wingfin-osm-scenarionet-converter/tools/` into `src/scenariobank/av3/` — see **Phase 4 — the
   model boundary**. The `RemotePolicy` callable signature survives only for the CLI diagnostics.
   What is genuinely still open in its place: **the two rig fidelity gaps** recorded in
   `rigs/av3.txt`'s header — fisheye rendered as an unwarped pinhole, and 4:3 rendered then squashed
   by preprocess rather than native 16:9. Both are known, neither is fixed, and both belong in
   `CONTRACT.md` so nobody reads their effect as a model defect.
2. **`max_steps` per category** — the numbers are a guess until you watch the expert run each
   category. Set them at the end of Phase 4 from observed step counts (e.g. p95 x 1.5), not before.
3. **Which exit for `t_junction` and `roundabout`.** Phase 1 discovers the sockets; the choice
   between them is yours and should be recorded with a reason in `docs/reference/destinations.md`.
4. **Bank size beyond 5 seeds.** 35 is the shipping bank. `--count` is a flag, so growing it is one
   regeneration away — but `CONTRACT.md` states the 20% granularity, so growing it later changes
   what a success rate means to the frontend. Decide before handoff, not after.

---

## Appendix — house conventions to copy verbatim

Taken from `converter-scenarionet-stage2-redesign`. `scenariobank` is a separate repo, but a
colleague moving between the two should not have to relearn anything.

**Packaging** (`pyproject.toml`)
- Backend `hatchling`; `[tool.hatch.build.targets.wheel] packages = ["src/scenariobank"]`.
- Distribution name `wingfin-scenariobank`, import name `scenariobank`; `__version__` mirrored in
  `src/scenariobank/__init__.py`. `.python-version` file containing `3.10`.
- Bounded ranges, alphabetised, one per line (`"pydantic>=2.8,<3"`). Exact/commit pins **only**
  where a tag is ambiguous — and always with a comment saying what breaks otherwise. That is the
  house style and it is why the `85e5dadc` trap was caught here rather than in production.
- **PEP 735 `[dependency-groups]`, not `[project.optional-dependencies]`.** `dev` = pytest + ruff;
  `sim` = metadrive (opt-in, so `uv sync` stays fast). Commit `uv.lock`.
  `[tool.uv] environments = ["sys_platform == 'linux' and platform_machine == 'x86_64'"]`.
- ruff: `line-length = 100`, `target-version = "py310"`, `select = ["E","F","I","UP","B","SIM"]`.
  **`ruff check` is the gate; `ruff format --check` deliberately is not.**
- pytest: `addopts = "-ra"`, `testpaths = ["tests"]`. Nothing else.

**CLI**
- `[project.scripts] scenariobank = "scenariobank.cli:app"` — point at the **Typer app object**,
  which is callable; no `main()`, no `__main__.py`.
- Flat command list, no `add_typer` sub-apps. `@app.callback()` carries global `--verbose/-v`.
- Options as `Annotated[T, typer.Option(...)]`; shared ones factored into module-level aliases
  (`Bank = Annotated[Path, typer.Option("--bank","-b",...)]`). Choices as `str, Enum` subclasses —
  which is exactly what the six option axes are.
- Errors: each module defines `XxxError(RuntimeError)`; `cli.py` catches, writes to stderr, and
  `raise typer.Exit(code=1) from error`. Thin CLI, fat modules.

**Artifacts**
- pydantic v2 everywhere, `model_config = ConfigDict(extra="forbid")`, version field as
  `Literal[1]` with **no default** so an unknown version fails validation instead of coercing.
- Write JSON `json.dumps(..., indent=2, sort_keys=True) + "\n"` — byte-stable and diffable.
- Record `tool_versions` (metadrive, numpy, shapely, opencv, panda3d, pygame,
  `platform.python_version()`) read **live**, not hardcoded.
- **One shared `_sha256` / `canonical_checksum` helper imported across modules, never
  reimplemented.** `fingerprint.sha256_hex` already is that helper; `lane_geometry_digest` is built
  on it and is used by `destinations` and by the invariance tests.

**Tests**
- `tests/unit/*.py`, no `conftest.py`, fixtures local to the module that needs them, heavy
  `@pytest.mark.parametrize`. Test names are full English sentences —
  `test_option_levels_do_not_move_the_map_or_route`.
- Simulator-dependent tests: `pytest.importorskip("metadrive")`, and **named** `skipif` guards
  (`needs_sim`, `needs_bank`) rather than bare ones. The repo's rule: *"a skipif that stops running
  silently is worse than one that fails."* Applies directly to `test_invariance.py` — a check
  skipping its comparison loop because MetaDrive was missing would be the worst possible failure
  mode, because it looks exactly like a pass.

**Container / compose**
- One service. `user: "${DOCKER_UID:-1000}:${DOCKER_GID:-1000}"` set from `id -u`/`id -g` in a
  wrapper script, so bank output is not root-owned.
- Venv at `/opt/venv`, **outside** the `.:/work` bind mount, so the mount cannot shadow it.
- Every `${VAR:-default}` fallback must be an **in-repo path that exists** (`${BANK_DIR:-./banks}`),
  so an unset variable can never make Docker create a root-owned dir in `$HOME`.
- Mount `/etc/localtime:ro` (else timestamps go UTC-adrift) and `/etc/passwd:ro` (else
  `pwd.getpwuid()` raises for a host uid with no passwd entry — this bites at `import torch_tensorrt`
  module scope, so it will matter the moment the AV3 adapter lands).

**Docs**
- The sibling repo has no root `CONTRACT.md`; its convention would be `docs/reference/<topic>.md`
  with a 1-3 line trap summary in `CLAUDE.md` pointing at it. Because this contract is
  cross-team and the repo is standalone, keep `CONTRACT.md` at the root and *also* add the trap
  lines to `CLAUDE.md`.
- If you write a `CLAUDE.md` here: **hard budget under 30 KB**, traps only (1-3 lines + pointer),
  measurements live in `docs/reference/`. The sibling's grew to 223 KB by appending before it had
  to be split.
- Two standing rules worth carrying over: **never quote a measured figure from a doc, re-measure
  it** — which is why the `LEVELS` table is marked provisional until Phase 4b writes
  `level-calibration.md` — and **blast radius is an acceptance criterion**: a fix that changes
  things that were not wrong gets reverted however good its numbers look.

---

## Reference checkouts (read these, do not guess)

- `/home/keith/Desktop/work/wingfin/metadrive/` — MetaDrive source. Every file:line in this plan.
  - `component/navigation_module/node_network_navigation.py:60,72-91` — `destination` and the
    `auto_assign_task` draw it bypasses.
  - `manager/object_manager.py:51-91` — why `accident_prob` cannot build the obstacle axes.
  - `component/traffic_participants/` — `Pedestrian`, `Cyclist`.
  - `tests/test_functionality/test_pedestrian.py:38-40` — the `set_velocity` pattern `VRUManager` uses.
  - `component/static_object/base_traffic_light.py` — the light body Phase 8 spawns.
- `converter-scenarionet-stage2-redesign/tools/policy_client.py:337` — `RemotePolicy`. Its
  signature is now the **diagnostic** contract only (see Phase 4); the evaluated model is AV3.
- `converter-scenarionet-stage2-redesign/tools/{camera_rig,av3_model,openpilot_policy,av3_probe}.py`
  and `rigs/av3.txt` — ported into `src/scenariobank/av3/` by Phase 4. Already MetaDrive-shaped.
- `/home/keith/Desktop/work/wingfin/wing-sim/orchestrator/src/` — **reference only; import nothing
  (R1).** Read before writing Phase 7:
  - `runner/queue.py` — the single-slot loop, and `recover()`'s adopt-don't-requeue discipline.
  - `runner/job_runner.py` — the step ordering, and the unconditional `finally:` data-root wipe.
  - `rig/session.py`, `rig/script.py` — sibling container, teardown inside the launched script.
  - `rig/lock.py` — flock by inode, and why holder identity is published as a separate file.
  - `rig/compose.py::child_environment` — why a child never inherits the environment wholesale.
  - `rig/progress.py` — progress as derived state with no persistence (keep the property, drop the
    prose regexes).
  - `db/migrations/001_initial.sql` — the comment stating the GPU lease row is observability,
    not authority. We share no table with him now, but the reasoning still applies to ours.
  - `db/jobs.py` — the FIFO ordering rule, and `claim_next` (`:104-106`): a bare `SELECT ...
    LIMIT 1` with no transaction and no lease. Read it to see what one in-process loop was
    carrying, and why a NAS queue with two rigs needs `wfqueue`'s lease instead.
  - `db/job_presets.py`, `db/migrations/005_job_presets.sql` — the model for our `job_scenarios`
    table: identity minted before launch, `skipped` distinct from `failed`.
- `converter-scenarionet-stage2-redesign/tools/signal_control.py` — the phase model Phase 8 ports,
  including the timestep and per-group-offset traps already paid for there.
- `wingfin-osm-scenarionet-converter/docker/Dockerfile` — **not a recipe to copy any more.** The
  image it builds, `metadrive-wingfin-sim`, is what Phase 5 reuses; read it for the reasoning
  behind the EGL patch, the glvnd manifest and the `uv cache clean` in the same layer, all of
  which our studio image inherits rather than repeats.
- `wingfin-osm-scenarionet-converter/docker/openpilot/Dockerfile` and `scripts/bridge.sh` — the
  bridge image Phase 4 Step 7 talks to, reused unchanged. `bridge.sh` is how it is started.
- `converter-scenarionet-stage2-redesign/src/osm_scenario/acquisition.py:199-247` — the manifest
  writer to model `manifest.py` on.
