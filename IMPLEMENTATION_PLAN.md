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
3. **Integrity is a fingerprint of the map that came out, not an audit of the config that went in.**
   See **Integrity by fingerprint**.

Everything in this plan was verified against the MetaDrive source actually installed here
(`converter-scenarionet-stage2-redesign/.venv/.../metadrive/`). File:line references are real.

---

## How this ships — one queue, two backends

**Decision (2026-08-30, with the manager).** This bank is not driven by a CLI and a JSON file. It is
driven by the existing webapp, `~/Desktop/work/wingfin/wing-sim` (Tyrone Tang) — a FastAPI
orchestrator plus a React frontend owning a single-GPU rig. The frontend gets two sections, one
CARLA and one MetaDrive, and **both post into the same queue**. A `backend` label on the job row
decides which simulator the queue starts.

This supersedes "Phase 7 — optional, build only if asked". Phase 7 is the deliverable, and it is now
an orchestrator rather than a wrapper. It also supersedes the state-vector policy boundary: see
**No lidar** and **Phase 4**.

```
  React frontend
  ┌───────────────┬───────────────┐
  │ CARLA section │  MD section   │      two forms, two POST endpoints
  └───────┬───────┴───────┬───────┘
          │               │
      POST /jobs      POST /metadrive/jobs
          └───────┬───────┘
                  ▼
        ┌───────────────────────┐
        │  jobs  (one table)    │   backend = 'carla' | 'metadrive'
        │  FIFO: priority,      │   params_json, backend-interpreted
        │        queued_at      │
        └──────────┬────────────┘
                   ▼
            one worker, one slot
                   │
              ┌────┴────┐  reads job["backend"]
              ▼         ▼
        JobRunner   MetaDriveJobRunner
              └────┬────┘
                   ▼
        ~/simulation/.wing-sim.gpu.lock   ◄── still the authority;
                   │                          run_local.sh and free_gpu.sh
                   ▼                          are outside the queue
              one GPU, one run

  separate per backend: data root, archive, staging, compose project,
                        images, results tables
```

### R1 — the independence rule (hard constraint)

**Tyrone's code is a reference, never a dependency.** No module of his is imported by anything here.
Read it to avoid rediscovering failures he already paid for; write our own.

This resolves cleanly because **the dependency arrow points the other way**: his `QueueWorker`
imports our `MetaDriveJobRunner`, not the reverse. He depends on us — the correct direction, since
we own MetaDrive.

Two things are shared and neither violates R1, because **a schema is data and a path is not a
library**:

- the `jobs` table — our own SQL against a documented schema
- `~/simulation/.wing-sim.gpu.lock` — a file path opened with `flock`, not an import of `rig/lock.py`

| Was going to reuse | Write our own instead | Cost |
|---|---|---|
| `rig/lock.py` | flock helper against the same path | ~150 lines |
| `api/auth.py` `CurrentUser` | verify bearer token vs `api_tokens` (sha256) | ~20 lines |
| `staging.py` / `archive.py` | our own staging + archive | ~250 lines |
| `rig/session.py` | our own session | ~300 lines (his is preset/CARLA-coupled anyway) |
| `ingest/harvest.py` | our own ingest | small — our schema, no mapping |

**R1 also removes a blocker.** `orchestrator/src/staging.py` and `archive.py` do not exist in his
checkout — not gitignored, on no branch, while `runner/job_runner.py:11,25`, `api/uploads.py:14` and
`validation.py:37` import them. Under R1 that is his problem for starting his service, not a
prerequisite for anything here.

### Why one queue, when the flock already exists

The GPU argument is the weakest justification. The flock is advisory and kernel-released on holder
death, and his `001_initial.sql` says outright that the `gpu_lease` table is "Observability only,
NOT the authority." Two independent queues on that lock already cannot double-book the card.

What one queue fixes is what the lock cannot express:

- **Ordering.** Two queues polling one lock means whoever polls first wins — no FIFO across
  backends, no priority, possible starvation. His `db/jobs.py` defends FIFO precisely because "a
  queue whose order cannot be predicted by looking at it is one people stop trusting."
- **Honest waits.** `api/queue.py:197` does `eta_seconds = estimate.seconds * position` — one
  estimate scaled by queue position. Wrong in both directions the moment backends mix. See
  **Phase 7, Step 7**.
- **One screen**, not two tabs where a user guesses why their job is not moving.

### Deferred, deliberately: where the loop lives

Either his `QueueWorker` gains a runner registry, or a new jointly-owned dispatcher owns the queue
and his loop retires into it. Both are one process, one loop; they differ only in whose file the
loop lives in, and every step of Phase 7 is identical under either. Ruled out: a separate scheduler
process calling each orchestrator over HTTP — it adds a network hop and a failure nobody has an
answer for (callee dies mid-run, scheduler still holding the lock).

---

## Traps — verified, do not re-propose

Each of these is *the obvious thing you would reach for*, and why it does not work. They were
checked in the MetaDrive source installed here; the file:line references are real. They live in this
section rather than in a phase because the temptation recurs — someone will reach for
`accident_prob` again in six months.

| The tempting move | Why it fails, and what to do instead |
|---|---|
| Pin `metadrive-simulator==0.4.3` and trust the version string | `metadrive.constants.EDITION` reports `"MetaDrive v0.4.3"` for **both** the tag and the commit we actually run (`85e5dadc` = `MetaDrive-0.4.3-32-g85e5dadc`); `pyproject.toml:31-36` already warns about this trap. **The version string is not an integrity check.** Record the resolved dist version + git SHA + `asset_version()`, and pin the same commit. *Enforced in:* Phase 0 `doctor`, the Phase 2 manifest, Phase 3 `verify` step 2. |
| Treat `random_traffic=True` as a cosmetic knob | `traffic_manager.py:339-341`: with it on, the traffic manager is **never re-seeded**, so traffic differs on every reset at the same seed. *Enforced in:* Phase 3's hard-assert. It is also **Phase 2b's negative test** — the one setting guaranteed to break invariance, which is what gives that check teeth. |
| Let the last block set the destination | True *only* when the ego spawns on a positive road — `node_network_navigation.py:80` falls back to `map.blocks[0]` on a negative one, and the socket within the block is a seeded random draw among three for X/O/T. **We never let it choose:** `node_network_navigation.py:60` reads `vehicle.config["destination"]` (`base_env.py:141`), and when it is set `auto_assign_task` is skipped entirely. Every category pins its destination. *Enforced in:* Phase 1 — this is what deletes the turn classifier. |
| Use `accident_prob` for the Cones and Barriers axes | Set it 0–1 and `TrafficObjectManager` scatters debris — but `object_manager.py:51-53` skips any block that is not `Straight`/`Curve`/`InRampOnStraight`/`OutRampOnStraight`, so `X`, `T` and `O` receive **nothing, with no error** — 5 of our 7 categories. And one scalar drives all three scene types mutually exclusively (`:54-91`), so "cones yes, barriers no" is inexpressible. **`accident_prob` stays `0.0` permanently**; write `obstacles.py`. See **Scenario options**. |
| Look for MetaDrive's left-hand-traffic option | **There is none**, and the negative is exhaustive rather than a keyword grep: all 249 keys of `BASE_DEFAULT_CONFIG`, `METADRIVE_DEFAULT_CONFIG` and `SCENARIO_ENV_CONFIG` dumped and filtered — nothing; OpenDRIVE carries drive side as `rule="RHT"/"LHT"` and MetaDrive never parses it (`utils/opendrive/parser.py:509-535` has no such field, upstream `main` still reads `# Rules` / `# TODO implementation`); SUMO's `lefthand="true"` is dropped too; ScenarioNet's `coordinate` means coordinate *frame*, not traffic side. Every "handed" word in the package is coordinate chirality. **And there is nothing to upgrade to** — upstream `main`'s `version.py` still reads `0.4.3`. A geometry reflection is the only route: `handedness.py`. *Enforced in:* Phase 1 build, Phase 0 `doctor`, Phase 3 `verify`. |
| Reach for `need_inverse_traffic=True` to put traffic on the other side | It does not do that. It only lets the traffic manager *also* spawn NPCs on the opposing carriageway (`traffic_manager.py:246-247`, `:381-382`), and only for block IDs `S C r R` — so `X`, `T` and `O` are unaffected, silently, exactly like `accident_prob` above. Which side anyone **keeps** is geometry, not this flag. It is still worth turning on for the Traffic axis, because without it a two-way map has no oncoming traffic at all — but that is a **Scenario options** item, not a handedness switch. |
| Defend seed identity by hashing a hand-picked list of config keys (+ pin `curriculum_level=1`) | Rejected outright rather than corrected. An audit of the config that went *in* cannot catch a simulator that builds a different map from it. Replaced wholesale by fingerprinting **the map that actually came out** — see **Integrity by fingerprint**. `config_hash` survives only as a diagnostic that *explains* a `map_id` failure; it is never itself the gate. |

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

## Integrity by fingerprint

The bank stores integers. Something has to guarantee that integer still means the same road when
the runner rebuilds it months later on someone else's machine.

**The rejected approach** was to enumerate every config key that could change the road, hash them,
and assert the dangerous ones. It fails for a structural reason: the list is only as good as what
somebody thought to put on it, and `curriculum_level` — the nastiest of them, which silently
rewrites the seed you asked for — was missing from the first draft. You cannot prove such a list is
complete.

**The approach taken** is to check the output instead of enumerating the inputs.

`pg_map.py:111` `get_meta_data()` returns the full structure of a generated map: every block, its
parameters, its socket connections, plus `map_config`. Combine that with the route and hash it:

```python
map_id = sha256(canonical_json([
    round_floats(map.get_meta_data()["block_sequence"], 3),   # structure only
    map.config["lane_num"], map.config["lane_width"],
    vehicle.config["spawn_lane_index"],
    vehicle.config["destination"],
    list(vehicle.navigation.checkpoints),
]))
```

Written per row at generation. Recomputed at run time and compared before the first episode.
Mismatch → refuse, and print which field differs.

This subsumes the whole config-key question. A wrong `curriculum_level`, a different MetaDrive
commit, a config key neither of us thought of — every one of them produces a different road, and a
different road produces a different `map_id`. One check, and it cannot go stale.

**Two implementation notes that matter:**

- **Do not hash `map_features`.** It is raw lane polylines — floats that can differ in their last
  bits across numpy versions, producing mismatches on roads that are actually identical. Hash the
  structural block config, rounded to 3 dp (millimetres), which is discrete enough to be stable.
- **`config_hash` stays, demoted to a diagnostic.** `map_id` tells you *that* something drifted;
  `config_hash`'s key-by-key diff tells you *what*. Because it is no longer the gate, its key list
  no longer has to be perfect.

`curriculum_level = 1`, `random_spawn_lane_index = False`, `random_lane_num = False` and
`random_lane_width = False` are still set in `base_config` because they are the sane values.
Nothing now *depends* on anyone remembering to.

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

**Bank (fixed, 35 rows):** `category`, `seed`, `block_seq`, `destination`, `spawn_lane_index`,
`map_id`, `max_steps`, thumbnail. Written once.

**Options (per run):** the six axes. They never enter `map_id`.

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
**That assumption has teeth, and Phase 2b tests it rather than trusting it.**

One coupling to preserve: `traffic_manager.py:253` excludes `object_manager.accident_lanes` from
vehicle spawning. Our obstacle manager must publish the same attribute and carry `PRIORITY = 9`
(mirroring `TrafficObjectManager`), so traffic still avoids spawning on top of an obstacle field.

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

**`src/scenariobank/obstacles.py` — `ObstacleManager(BaseManager)`** (cones, barriers)

- `PRIORITY = 9`; publishes `accident_lanes`.
- Spawn in `reset()` via `self.spawn_object(...)` so the inherited `before_reset` cleans up. Going
  through `engine.spawn_object` directly trips `_object_clean_check` (`base_engine.py:645-657`) with
  `AssertionError: You should clear all generated objects...` on the next reset.
- Draw from **`self.np_random`**, never `engine.np_random` — the engine stream is consumed by every
  other manager's `spawn_object` call and is therefore sensitive to spawn ordering.
- `accident_prob` stays `0.0` permanently, so `TrafficObjectManager` is never registered and cannot
  compete for the `object_manager` name.

**`src/scenariobank/actors.py` — `VRUManager(BaseManager)`** (pedestrians, cyclists)

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
- **Frontend**: the wing-sim webapp, via a `/metadrive` section posting into the shared queue. The CLI stays, and `run_bank(...)` stays importable and CLI-free, so the CLI and the orchestrator are two callers of one core. *(Amended 2026-08-30 — was "CLI + `results.json`", with Phase 7 optional.)*
- **Categories**: the seven as drafted.
- **Seeds**: fixed at 0, 1, 2, 3, 4 for every category. 35 maps.
- **Options**: six axes, stored normalized, applied at run time.
- **Handedness**: **left-side traffic** (right-hand-drive market — Singapore, UK, Malaysia, Japan). This is not a MetaDrive setting; see **Traps**. Enforced by `handedness.install()`, called from `base_config()` so no caller can forget it, and **measured** rather than asserted by Phase 0 `doctor` and Phase 3 `verify`.
- **Policy**: the AV3 camera adapter is the contract from Phase 0, not adapted in later. *(Amended
  2026-08-30 — was "build against the state-vector callable now". Reversed because a state-vector
  policy cannot perceive five of the six option axes, so it could never have been the thing scored.)*
  `ConstantPolicy` and `ExpertPolicy` survive as CLI-only diagnostics.
- **Independence (R1)**: no module of Tyrone's `wing-sim` is ever imported. Shared: the `jobs` table
  (a schema) and the GPU lock path (a path). See **How this ships**.

---

## Target layout

```
metadrive-PG/
  pyproject.toml            # uv, requires-python >=3.10,<3.11, [project.scripts] scenariobank=...
  uv.lock
  src/scenariobank/
    cli.py                  # Typer app: doctor generate inspect sockets verify options-invariance
                            #            calibrate run selftest schema validate
    categories.py           # CATEGORIES dict: block_seq, destination, max_steps, description
    options.py              # LEVELS, TIERS, resolve_options() -> expanded dict
    config.py               # base config builder, canonicalisation, config_hash (diagnostic)
    fingerprint.py          # map_id: get_meta_data -> round -> canonical json -> sha256
    manifest.py             # pydantic models: Manifest, Category, Scenario  (schema_version)
    generate.py             # build the 35 maps + thumbnails
    verify.py               # the gate
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
    orchestrator/           # Phase 7 — our own; imports nothing of wing-sim's (R1)
      router.py             #   /api/v1/metadrive/... jobs, submissions, options, banks, runs, SSE
      auth.py               #   bearer token -> api_tokens by sha256 (~20 lines)
      lock.py               #   flock on ~/simulation/.wing-sim.gpu.lock, holder file kept separate
      session.py            #   take lock, launch sibling container, supervise, tear down
      job_runner.py         #   MetaDriveJobRunner: run / resume / cancel
      staging.py            #   upload -> staged tree -> data root
      archive.py            #   evidence, written before the run touches anything
      ingest.py             #   results.json -> our tables
      db.py                 #   our tables + the shared `jobs` row
  rigs/av3.txt              # the six AV3 cameras, ported from the converter
  docker/Dockerfile         # adapted from converter-scenarionet-stage2-redesign/docker/Dockerfile
  compose.yaml
  scripts/bank-check.sh     # ruff -> pytest -> verify every bank in banks/
  tests/
  docs/reference/
    destinations.md         # the resolved destination socket per category (Phase 1)
    level-calibration.md    # the Phase 4b sweep
  CONTRACT.md
  banks/pg-bank-2026-08/
```

---

# Phase 0 — Skeleton and environment truth

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

**How you test it**
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

# Phase 1 — Categories and forced destinations

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
  | `curve` | `CC` | terminal socket | 0–4 |
  | `ramp_merge` | `rS` | terminal socket | 0–4 |

  Each entry also carries `description` and `max_steps` (roundabout needs more than curve).
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

**How you test it**
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

# Phase 2 — Manifest, map_id, thumbnails

**Goal:** the bank becomes a real on-disk artifact, fingerprinted by what it actually built.

**Build**
- `base_config` built once and stored **in full** in the manifest. It sets `curriculum_level=1`,
  `random_traffic=False`, `random_spawn_lane_index=False`, `random_lane_num=False`,
  `random_lane_width=False`, `accident_prob=0.0`, `store_map=True`, the whole lidar/detector block,
  `agent_observation` and `navigation_module`.
- `config_hash` over a **canonical JSON** dump (sorted keys, no whitespace) of that config, minus
  cosmetics (`use_render`, `log_level`, `debug`). **Diagnostic only** — it is not the gate, and the
  per-run option keys (`traffic_density` and friends) are deliberately **not** in it, because they
  are not part of map identity.
- **`map_id` per scenario** — the gate. Computed as in "Integrity by fingerprint".
- Per scenario also record `destination` and `spawn_lane_index`, so the runner can set
  `vehicle_config["destination"]` and bypass `auto_assign_task` entirely.
- Assert all five fixed seeds build for every category. Map generation is a **backtracking search**
  (`BIG.py:91-103`), so a requested block sequence can simply fail to plug in for a given seed. With
  seeds fixed at 0–4 that is a hard failure, not a scan: **fail loudly** and record the substitute
  seed explicitly in the manifest rather than shifting silently.
- Thumbnails: `draw_top_down_map(env.current_map, resolution=(512,512))` → `cv2.imwrite` into
  `thumbs/`. Note RGB→BGR for cv2. Map-only by construction.
- Write `manifest.json` **last and atomically** (temp file + `os.replace`).
- `scenario_id` is the stable public key; format `{category}_{index:04d}`.

**Manifest schema (v1.0)**
```json
{
  "schema_version": "1.0",
  "bank_id": "pg-bank-2026-08",
  "created_utc": "2026-08-27T10:00:00Z",
  "metadrive": {
    "edition": "MetaDrive v0.4.3",
    "dist_version": "0.4.3",
    "commit": "85e5dadc...",
    "asset_version": "0.4.3"
  },
  "config_hash": "sha256:a3f1c2...",
  "base_config": { "...full env config dict..." },
  "categories": {
    "intersection_left": {
      "description": "Unprotected left turn at a 4-way intersection",
      "block_seq": "X",
      "destination": "1X0_1_",
      "max_steps": 500,
      "scenarios": [
        {"scenario_id": "intersection_left_0000", "seed": 0,
         "map_id": "sha256:9c4e1b...", "spawn_lane_index": 1,
         "thumbnail": "thumbs/intersection_left_0000.png"}
      ]
    }
  }
}
```

**How you test it**
```bash
uv run scenariobank generate \
  --categories intersection_left intersection_right intersection_straight \
               t_junction roundabout curve ramp_merge \
  --count 5 --out ./banks/pg-bank-2026-08 --bank-id pg-bank-2026-08
```
- **Expect:** exit 0; `manifest.json` + 35 PNGs.
- Structural check:
  `jq '.categories | to_entries[] | {k:.key, n:(.value.scenarios|length)}' manifest.json`
  → every count is 5.
- Seeds are the same five everywhere:
  `jq -r '[.categories[].scenarios[].seed] | unique' manifest.json` → `[0,1,2,3,4]`.
- The three intersection categories share maps:
  ```bash
  jq -r '.categories.intersection_left.scenarios[].map_id'     manifest.json > l.txt
  jq -r '.categories.intersection_straight.scenarios[].map_id' manifest.json > s.txt
  diff l.txt s.txt
  ```
  **Expect: non-empty.** `map_id` includes the destination and route, so the same junction driven
  two ways must fingerprint differently. If the diff is empty, `map_id` is not covering the route
  and the whole integrity story is weaker than it looks — fix it here.
- Every thumbnail exists and is non-trivial:
  `jq -r '.categories[].scenarios[].thumbnail' manifest.json | while read f; do test -s "$f" || echo "MISSING $f"; done`
- **Open the thumbnails.** `eog banks/pg-bank-2026-08/thumbs/` — an intersection thumbnail should
  look like an intersection. Remember they show the map only — never traffic, objects, lights,
  the ego or its route — and that each is zoomed to fit, so a curve and a roundabout both fill
  their frame despite being very different sizes.
- **Idempotence:** regenerate into a second directory with the same args; the two `manifest.json`
  files must differ only in `created_utc` and `bank_id`.
  `diff <(jq 'del(.created_utc,.bank_id)' a/manifest.json) <(jq 'del(.created_utc,.bank_id)' b/manifest.json)`

**Done when:** the idempotence diff is empty, the intersection `map_id` diff is *not*, and the
thumbnails match their labels.

---

# Phase 2b — Prove options do not move the map  ⟵ *gates the whole design*

**Goal:** the storage design assumes that changing traffic, cones, actors or lights leaves the map
and the route untouched. Test it. If it fails, options must be baked into the bank and everything
downstream changes.

**Build** — `scenariobank options-invariance --bank ... --sample N`. For each sampled scenario,
reset under **every level of every axis** and assert identical:
- `map_id` (recomputed, not read),
- ego spawn position and heading,
- `navigation.checkpoints`.

Compare with `np.testing.assert_array_equal`, not `allclose`. Only the object sets may differ.

**How you test it**
```bash
uv run scenariobank options-invariance --bank ./banks/pg-bank-2026-08 --sample 10
```
**Expect:** exit 0, and a printed count of how many (scenario x level) combinations were compared
so a silently-empty loop cannot pass.

```bash
# The negative test — this is the one that gives the check teeth
uv run scenariobank options-invariance --bank ./banks/pg-bank-2026-08 --sample 10 \
  --force-config random_traffic=true
```
**Expect: non-zero exit**, naming the scenario and what differed. `random_traffic=True` leaves the
traffic manager unseeded (`traffic_manager.py:339-341`), so this is the one setting guaranteed to
break invariance.
Without this case passing, a green run only proves you compared two things that were never going to
differ.

**Done when:** the positive run is green with a non-zero comparison count, and the negative run
fails. **Do not build Phase 4b or Phase 8 until this passes** — both assume run-time options.

---

# Phase 3 — `verify`: the gate

**Goal:** the one command that catches the ways a seed bank goes quietly wrong — simulator drift,
config drift, and platform float nondeterminism.

**Build** — `scenariobank verify --bank ./banks/pg-bank-2026-08` checks, in order:
1. `schema_version` is supported.
2. Compare `metadrive.commit` and `asset_version` against the running container. Commit mismatch
   is fatal — the `EDITION` string alone would not have caught it; see **Traps**.
3. **Measure the drive side of each rebuilt scenario and refuse on `right`.** Not read off
   `handedness._installed` — measured from the map, the way `doctor.measure_drive_side` does it.
   A bank whose manifest claims left-side traffic while its maps are right-side is worse than a
   commit mismatch: every other field is correct, every thumbnail looks like a road, and the only
   symptom is that a right-hand-drive model fails everything for reasons no result explains.
4. **Rebuild each sampled scenario and recompute `map_id`; compare.** This is the gate. On mismatch,
   name the scenario and print which fingerprint field differs.
5. Re-derive `config_hash` from `base_config` and compare. **Diagnostic:** on mismatch print a
   key-by-key diff. It explains a `map_id` failure; it is not itself the failure.
6. Hard-assert the determinism-hostile settings: `random_traffic is False`, `curriculum_level == 1`,
   `store_map is True`.
7. Sample `--sample N` (default 10). For each: reset twice and assert **byte-identical** initial
   state — ego position/heading, `navigation.checkpoints`, and the sorted array of traffic vehicle
   spawn positions.

**How you test it**
```bash
uv run scenariobank verify --bank ./banks/pg-bank-2026-08          # exit 0
```
Then each negative case, all of which **must** exit non-zero with a specific message
(work on a `cp -r` copy):
```bash
# a) the gate itself
jq '.categories.intersection_left.scenarios[0].map_id = "sha256:0"' manifest.json > tmp && mv tmp manifest.json
uv run scenariobank verify --bank ...   # -> "intersection_left_0000: map_id mismatch"

# b) the bug the old assert was aimed at — caught without knowing it exists
jq '.base_config.curriculum_level = 2' manifest.json > tmp && mv tmp manifest.json
uv run scenariobank verify --bank ...   # -> map_id mismatch on every scenario, AND
                                        #    "curriculum_level=2: seeds are remapped by level"

# c) simulator drift
jq '.metadrive.commit = "0000000"' manifest.json > tmp && mv tmp manifest.json
uv run scenariobank verify --bank ...   # -> "simulator mismatch: bank built on 0000000, running 85e5dadc"

# d) the silent killer
jq '.base_config.random_traffic = true' manifest.json > tmp && mv tmp manifest.json
uv run scenariobank verify --bank ...   # -> "random_traffic=True: traffic is not reproducible"
```

Case (b) is the point of this phase. The draft would have caught it only because someone
remembered to write the assertion; `map_id` catches it because the road came out different.

**Done when:** all four negatives fail with their specific message, and (b) fails on `map_id`
*before* it fails on the named assertion.

> **Note on "the CI gate".** The sibling repo has **no `.github/workflows`** — its gate is the
> manual pair `uv run pytest` + `uv run ruff check`, plus `scripts/container-check.sh` (build →
> GPU check → pytest → sweep, stopping at the first failure, non-zero exit). Follow that: add
> `scripts/bank-check.sh` running `ruff check` → `pytest` → `verify` on every bank in `banks/`,
> and make *that* the thing you run before a handoff. Do not invent a CI system for this repo alone.

---

# Phase 4 — Runner, results schema, reference policies

**Goal:** the piece the frontend calls.

**Build**
- **The model boundary is an AV3 camera submission** *(amended 2026-08-30; replaces the
  `policy(observation: Box(19,)) -> [steer, throttle]` contract)*. Everything needed already exists
  in `wingfin-osm-scenarionet-converter/` and is already MetaDrive-shaped — **port it, do not
  rewrite it**, and use our own openpilot bridge, not wing-sim's:

  - `tools/camera_rig.py` — `load_rig()`, `CameraRig.sensors/mount/read`
  - `rigs/av3.txt` — the six AV3 cameras, ISO-8855 → CARLA sign rules applied, datum resolved onto
    MetaDrive's `DefaultVehicle`
  - `tools/av3_model.py` — `AV3Model.observe/predict_with_navigation`, `FrameHistory`, `preprocess`,
    `ego_state`, `navigation`, `waypoints`
  - `tools/openpilot_policy.py` — `BridgeConnection`, `OpenpilotDriver`, `to_metadrive_action`
  - `metadrive-complete/openpilot/bridge/` — the zapeta bridge image (Python 3.8, its own container)
  - `tools/av3_probe.py` + `scripts/av3-probe.sh` — the sign-convention probe

  `rigs/av3.txt`'s header records two open gaps, and they stay open: fisheye is rendered as an
  unwarped pinhole, and 4:3 is rendered then squashed by preprocess, never native 16:9.

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
     is a stride counted in our own loop — it is *not* a MetaDrive config key.
  4. **Both ends negate.** MetaDrive is left-positive, CARLA right-positive, so the waypoints' `y`
     and the action's steering each flip. Six conversions stand between the model and the car and
     **not one of them raises when it is wrong** — which is why `scripts/av3-probe.sh` runs before
     anything is scored.
  5. **A rig's `tick_rate` must equal the interval it is actually read at.** Nothing resamples.
  6. **`MAX_IMAGE_BUFFERS = 9` is a hard cap.** panda3d fails *intermittently* past it, so a rig one
     camera over the line looks like it works and then fails on a run somebody is relying on.

  The camera rig is selected as a **path, not a registry entry**: `--camera-rig rigs/av3.txt`.

  Cost, and it drives the ETA model in Phase 7 Step 7: **the AV3 forward pass is ~1 s**, about 20x a
  50 ms decision. Price a 35-scenario bank before quoting anyone a runtime.

- **The submitted `model_dev.yml` is not the converter's.** Both repos have a file by that name with
  different schemas. `tools/av3_model.load_config` **requires every field and defaults none** —
  deliberately — and reads `MODEL_CONFIG`, defaulting to the converter's `config/model_dev.yml`. In
  the container, read the **submitted** one from the staged tree. Map it onto `Config`'s required
  keys at load, or widen the loader, but **keep the no-defaults rule**: a silently defaulted
  preprocessing field is a wrong score, not a crash. Load the submission's `modifiers.py`
  explicitly, never by importing whatever is on the path.

- **One interpreter, not two.** The converter's host setup splits MetaDrive (3.8 / numpy 1.24) from
  the converter (3.10 / numpy 2.2), which is why `tools/` uses path-inserted imports and exchanges
  through files. This container does not inherit that — it pins MetaDrive at `85e5dadc` on one 3.10
  interpreter, as the converter's own `docker/Dockerfile` already does. So the ported tools become
  ordinary package modules under `src/scenariobank/av3/`, and `_PortablePickler` is unnecessary. The
  only real process boundary left is the zapeta bridge, which stays 3.8 in its own container.

- **Diagnostic policies keep the old signature.** `load_policy("pkg.mod:Name")` still instantiates
  and checks callability, and `ConstantPolicy` / `ExpertPolicy` still take
  `(observation: np.ndarray) -> Sequence[float]`. They are CLI-only floor and ceiling checks, never
  the thing under evaluation.
- **Refuse on mismatch before the first episode** — `map_id` per scenario plus the commit check.
  Precedent: `tools/drive.py:300` `_refuse_mismatch`. A result computed on a drifted map is worse
  than no result.
- **Options resolution:** `resolve_options()` expands tiers, applies explicit flag overrides, and
  returns level names *and* numerics. Registers `ObstacleManager`, `VRUManager` and (Phase 8)
  `PGTrafficLightManager` only when their axis is above `none`.
- **Env construction:** `start_seed = min(seeds)`, `num_scenarios = max(seeds) - min(seeds) + 1`,
  because `base_env.py:926` asserts `start_index <= seed < start_index + num_scenarios`. Group
  scenarios by category so one env serves a category and `horizon` = that category's `max_steps`.
  Set `vehicle_config["destination"]` from the manifest per scenario. **`horizon` is the config key**
  (`metadrive_env.py:60`, default 1000) — `max_step` is a `TerminationState` field and setting it
  does nothing. **Enforce the same cap in our own step loop as well**, so a `horizon` that failed to
  take is a bounded run rather than a silent 1000-step one.
- Wire `crash_human_penalty` / `crash_human_cost`, mirroring `crash_object`'s 5.0 / 1.0 —
  termination is already wired, the reward/cost pair is not (`metadrive_env.py:74-83`).
- Per scenario record: `success` (`info["arrive_dest"]`), `failure_reason` taken from the
  `TerminationState` fields — `arrive_dest, out_of_road, max_step, crash_vehicle, crash_object,
  crash_human, crash_building, crash_sidewalk, idle` — as a **string, not a boolean**, plus steps,
  cumulative reward, cumulative cost, wall time, and the fully expanded options.
- **Collision accounting** (needed by Phase 4b, cheap to build now): record ego-involved collisions
  separately from total, and count a collision **once per vehicle per episode, not per step**. Your
  converter's note: a per-step count "reports one collision as thirty, and the number describes the
  frame rate."
- `--save-trajectories` optional (off by default; the only large artifact).
- **Never abort the batch**: catch per-episode, record `status:"error"` + traceback, continue.
- Reference policies shipped: `ConstantPolicy` (fixed action) and `ExpertPolicy`, a wrapper around
  MetaDrive's bundled PPO expert (`metadrive/examples/ppo_expert/`). Per the **No lidar** section,
  `ExpertPolicy` holds the env and ignores its `observation` argument.

**How you test it**
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
**Expect:** `[19]`. `numpy_expert.py:48-49` admits its config restore is incomplete, so the runner
records the obs shape *after* the last expert episode and fails the run if it moved.

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
# 4. Refusal
cp -r banks/pg-bank-2026-08 /tmp/bad
jq '.categories.intersection_left.scenarios[0].map_id="sha256:0"' /tmp/bad/manifest.json > tmp && mv tmp /tmp/bad/manifest.json
uv run scenariobank run --bank /tmp/bad ...    # must exit non-zero, run zero episodes

# 5. Batch resilience
uv run scenariobank run --policy scenariobank.policies:RaisingPolicy ...
```
**Expect:** every scenario present in `results` with `status:"error"` and a traceback; the process
still exits 0 and writes the file.

```bash
# 6. Options actually do something
uv run scenariobank run --categories intersection_left --tier easy --policy ...:ExpertPolicy --out easy.json
uv run scenariobank run --categories intersection_left --tier hard --policy ...:ExpertPolicy --out hard.json
jq -s '[.[0].summary.success_rate, .[1].summary.success_rate]' easy.json hard.json
```
**Expect:** hard is lower than easy. If they match, the option managers are registered but not
placing anything — the same class of bug as floor equals ceiling.

**Done when:** tests 1-6 all behave as described. Test 3 is the one that matters.

---

# Phase 4b — Calibrate the levels

**Goal:** replace the provisional numbers with measured ones. Requires Phase 2b green.

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

# Phase 5 — Container

**Goal:** the same numbers on your machine, in CI, and on the frontend's host.

**Build — adapt, do not reinvent.** Start from
`converter-scenarionet-stage2-redesign/docker/Dockerfile`, which already solves the hard parts:
`ubuntu:22.04`, `uv` copied from `ghcr.io/astral-sh/uv`, `UV_PROJECT_ENVIRONMENT=/opt/venv`,
`RUN python -m metadrive.pull_asset` baked in, the panda3d `Config.prc` patch preferring
`libp3headlessgl.so` (EGL) over GLX for display-free rendering, the `glvnd/egl_vendor.d` manifest,
and `HOME=/tmp`.

Changes for scenariobank:
- Top-down rendering is pygame/CPU only, so `generate` / `verify` need **no GPU**. Keep the EGL
  layer anyway — Phase 8's lights and any camera-model run need real 3D, and the AV3 runner will
  later want the same image. One image, not two.
- `ENV SDL_VIDEODRIVER=dummy MPLBACKEND=Agg`.
- Build-time smoke test: generate one scenario and render one thumbnail. Fail the build if it fails.
- `scenariobank selftest`: reset one known seed and assert its **`map_id`** matches a value baked
  into the image. Reuses the Phase 2 fingerprint rather than inventing a second one — two commands
  must not be able to disagree about what a map is.
- Mount banks **read-only** (`compose.yaml` already uses `${RIG_DIR}:/rig:ro`, `${MODEL_DIR}:/models:ro`
  — follow that pattern with `${BANK_DIR}:/bank:ro`). Do not bake banks into the image.

**How you test it**
```bash
docker build -f docker/Dockerfile -t scenariobank:85e5dad .
docker run --rm scenariobank:85e5dad doctor
```
**Expect:** the *same* commit + asset_version your host `doctor` printed in Phase 0.

```bash
docker run --rm scenariobank:85e5dad selftest        # exit 0, prints matching map_id
docker run --rm -v $PWD/banks/pg-bank-2026-08:/bank:ro scenariobank:85e5dad verify --bank /bank
```

**The acceptance test — host vs container must agree:**
```bash
docker run --rm -v $PWD/banks/pg-bank-2026-08:/bank:ro -v $PWD/out:/out scenariobank:85e5dad \
  run --bank /bank --categories intersection_left \
      --policy scenariobank.policies:ExpertPolicy --out /out/docker.json
diff <(jq 'del(.started_utc)|del(.results[].wall_time_s)' out/docker.json) \
     <(jq 'del(.started_utc)|del(.results[].wall_time_s)' ceiling.json)
```
**Expect: empty.** If it is not, the bank is not portable and the whole premise needs revisiting
before the frontend touches it. Also confirm the read-only mount holds: a `generate --out /bank`
inside the container must fail.

**Done when:** the host/container diff is empty and `selftest` passes on a fresh `--no-cache` build.

---

# Phase 6 — `CONTRACT.md` and handoff

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
  - Options are echoed **expanded** in results; `map_id` deliberately excludes them.
  - `scenario_id` is the key he stores; **seeds are ours and may change between banks**.
  - `map_id` + `metadrive.commit` must be echoed back in results; a runner refuses on mismatch.
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

# Phase 7 — The orchestrator  ⟵ *the deliverable, no longer optional*

**Goal:** MetaDrive jobs submitted from the webapp, ordered in the shared queue, run on the rig, and
read back — with nothing of Tyrone's imported (R1).

Superseded: "optional, build only if asked" and the wrapper sketch that used to sit here. The queue,
the ordering and the single-slot discipline are **not** rebuilt; they are the shared queue. What is
built here is a runner, a session, a router, and the storage behind them.

---

## How an orchestrator and a runner actually work

Read this before writing any of it. "Orchestrator" is the whole service; "runner" is one component
inside it. `wing-sim/orchestrator/src/` is the reference — read it, import nothing.

Think of a kitchen with **one oven**:

- **The API is the front desk.** It takes orders and writes tickets. It never touches the oven.
- **The `jobs` table is the ticket rail.** Tickets in order: priority first, then oldest first.
- **The queue worker is the head chef** (`runner/queue.py`, ~180 lines, doing almost nothing on
  purpose): take the next ticket, read `job["backend"]`, hand it to that runner, wait, repeat.
- **The runner is the cook** (`runner/job_runner.py`, 644 lines) — one order, start to finish.

**The runner does not itself simulate.** It launches a *container* that simulates, and supervises
it. `MetaDriveJobRunner` never imports MetaDrive; the code calling `run_bank()` lives inside the
image.

The sequence, and every step is ordered by a failure it prevents:

1. **Is the oven taken?** Read the lock. If anything holds it — a hand-run `run_local.sh`, a
   leftover pipeline — return `waiting` and stop. **Never tear it down and never signal it.**
2. **Archive before you touch anything.** Copy the upload into the archive *first*, then promote it
   into the data root, so evidence exists before anything can go wrong.
3. **Write the receipt before cooking.** Mint the output folder names *now*, in the database. This
   is the one design choice worth stealing outright: because the name is decided in advance,
   attributing a result is reading a path. The version it replaced diffed `out/` before and after,
   which is how results get attached to the wrong submission.
4. **Book the oven and start.** Take the flock, launch a sibling container, record its pgid. Lock
   refused → requeue.
5. **Watch.** Poll ~1 s: re-read the log, look for the exit-code file. That is all.
6. **Plate it** — ingest results. 7. **File it** — archive. 8. **Wipe the counter** in a `finally:`,
   whatever happened, so one team's checkpoint is never on disk when the next team's job starts.

**The load-bearing property: the runner holds no state of its own.** Supervision rebuilds everything
from the log file and the exit-code file. That is the entire reason a run which outlived a service
restart can simply be watched again — same code path, no special case. **A runner that keeps
progress in a variable breaks restart recovery silently.** Same reason `rig/progress.py` has no
table: progress is derived, never stored.

### The interface to satisfy

His worker calls exactly three things, so these three are the whole contract:

```python
class MetaDriveJobRunner:
    async def run(self, job: dict) -> JobResult: ...      # normal path
    async def resume(self, job: dict) -> JobResult: ...    # adopt a run already in flight
    async def cancel(self) -> bool: ...
```

`JobResult.state` ∈ `waiting` | `completed` | `failed`.

**`waiting` is not a failure.** It means nothing ran and the job goes back on the rail. Recording it
as a failure is the single most likely wrong behaviour in this whole phase.

---

## Steps

Each is buildable and verifiable on its own. **Steps 1–5, 7 and 8 need nothing from Tyrone**; only
Step 6's routing does.

### Step 1 — the runner image and entrypoint

Extends Phase 5. The container reads an options file plus a scenario list, calls `run_bank()`,
writes `results.json`, exits 0. Four additions, all so the supervisor never has to parse prose:

- **Structured JSON lines on stdout.** One object per event. No regexes — his `rig/progress.py`
  scrapes four prose patterns out of CARLA's log because it has no choice; we do.
- **A per-scenario record directory and its exit-code file**, written when each scenario starts and
  ends. These two files are the progress signal, so a bar moves without anything reading the log.
- **Teardown inside the launched script**, not the supervisor — a dead orchestrator must still bring
  the stack down and still record exit codes.
- **Never let `KeyboardInterrupt` raise into `env.close()`.** It unwinds panda3d's GL context and
  bullet's world; that segfaulted and wedged the GPU until a reboot. `tools/drive.py` is the
  precedent — it keeps its exit handler armed until teardown returns. A cancelled run that still
  writes its results is a scored partial run; one that does not is a lost one, so budget the stop
  timeout rather than taking a 10 s default. Under one queue this is sharper than it was: a wedged
  GPU now blocks **both** queues.

**Verify alone:** `docker run` it by hand with a one-scenario options file; get a `results.json`.

### Step 2 — the lock helper (R1: our own, same path)

`~/simulation/.wing-sim.gpu.lock`, advisory `flock`, **exclusion by inode**. ~150 lines.

- **Publish holder identity as a separate file.** Atomic replacement is a rename, and a rename gives
  the path a new inode, voiding every outstanding lock — so never write into the lock file itself.
- **The flock stays the authority even under one queue**, because things outside the queue take the
  card: `deployment/run_local.sh`, hand-run scripts, and `free_gpu.sh --free` — which his attempt
  script runs as **root** with `--pid=host` on every attempt. Inside that container `id -un` is
  root, so its "spare a python3 if it is mine" rule does not apply: **it will terminate a MetaDrive
  run holding the card without the lock.**
- Confirm acquisition by finding the launched process in `/proc/locks`, rather than trusting a
  return value.

**Verify alone:** hold it from a shell (`bash wing-sim/deployment/with_rig_lock.sh sleep 60 &`),
confirm the helper reports it foreign and refuses.

### Step 3 — the rig session (R1: our own)

Takes the lock, launches the run as a sibling container, supervises, tears down. ~300 lines. His
`rig/session.py` is the reference, but it takes `presets=` and emits CARLA compose commands, so this
is a sibling rather than a reuse.

- **Sibling container, not a detached child.** `start_new_session` escapes a process group but not a
  PID namespace or a cgroup. A 25-minute run must not die because the service reloaded.
- **Never inherit the environment wholesale.** His `rig/compose.py::child_environment` returns only
  `HOME/PATH/HEADLESS/QUALITY/COMPOSE_MENU`, and the reason is that a developer's exported setting
  otherwise silently changes what a model is scored on.
- **Own compose project name, container prefix and labels**, so a stray-container sweep on either
  side can never reach the other.
- Supervision holds **no state** — re-read the log, look for the exit-code file — so adoption after
  a restart is the same code path as a normal run.
- The zapeta bridge listens on 5558 in both stacks and both use host networking. Under one queue
  they never run together, but **pick a different port anyway**, so a mistake is an error rather
  than a wrong number.

### Step 4 — `MetaDriveJobRunner`

~400 lines satisfying `run` / `resume` / `cancel`, plus our own staging and archive (R1). The order
is his, because each step prevents a specific failure:

1. Lock held by anything → `waiting`. Do not tear down, do not signal.
2. Archive a copy of the upload **before** promoting it into the data root.
3. **Mint the per-scenario output identities before launching.** For MetaDrive this is a
   `job_scenarios` table — the analogue of his `job_presets`. A job legitimately ends with 30 of 35
   scored, and *that* is a table, not columns on `jobs`: the rows start at `waiting`, and `skipped`
   is a real outcome distinct from `failed`, because "never ran" and "ran and failed" lead to
   different next actions.
4. Launch via Step 3. Lock denied → requeue, **not** failure.
5. Supervise, with a tracker task persisting each scenario as it finishes, plus a **backstop pass**
   afterwards that re-runs the same idempotent step for anything the tracker missed — the last
   scenario, which can finish in the instant the tracker is cancelled, and every scenario of an
   adopted run.
6. Ingest `results.json` into our tables. **No shape mapping** — the schema is ours.
7. Archive.
8. `finally:` wipe the data root, **unconditionally**.

**Separate data root: `~/simulation-md/`**, with its own `data/`, `staging/`, `archive/`, `banks/`
and database. Not negotiable: his `runner/job_runner.py` calls `wipe_run_owned(paths.data_root)` in
a `finally` at the end of every job, so a shared tree means his cleanup deletes a staged MetaDrive
checkpoint mid-run. Mirror his archive validator's two rules — the archive root must be **absolute**,
must **not** be inside the repo checkout (or a `git add -A` sweeps up a colleague's weights), and
must not be inside anything a round wipes.

### Step 5 — the FastAPI section

Own router, own auth (R1: verify the bearer token against the `api_tokens` table by sha256, ~20
lines, rather than importing his `CurrentUser`). Mounted with one `include_router` call in his
`create_app()` — the only line of his that this phase touches.

```
POST /api/v1/metadrive/submissions          open an upload, reserve the job id
PUT  /api/v1/metadrive/submissions/{id}/chunks   resumable; HEAD for the offset
POST /api/v1/metadrive/jobs                 -> jobs row, backend='metadrive'
GET  /api/v1/metadrive/options              the six axes, LEVELS and TIERS, as data
GET  /api/v1/metadrive/banks/{id}/manifest
GET  /api/v1/metadrive/banks/{id}/thumbs/{scenario_id}.png
GET  /api/v1/metadrive/runs, /runs/{id}
GET  /api/v1/metadrive/runs/{id}/log        SSE
```

- **A job is `(scenarios[], options)`, never a preset integer.** It goes in `jobs.params_json`, and
  the queue never reads either backend's params.
- **Resumable chunked upload** for ~1.3 GB checkpoints: `HEAD` returns the offset, `PUT` demands a
  strict offset match, and the atomic `.part` → final rename is the commit point.
- **Structural validation before the job can take the rig**: `model_dev.yml` parses, `model.type` is
  known, exactly one checkpoint whose suffix matches the backend, and `modifiers.py` **parsed to AST
  and never imported** — it is a file an authenticated stranger uploaded.
- **`GET /options` serves the six axes as data**, so the frontend renders the form from the schema
  instead of hard-coding it. This is what keeps the picker in step when an axis is recalibrated in
  Phase 4b.
- Queue reads stay on his existing `/api/v1/queue`, which now returns both backends.

### Step 6 — thin round-trip end to end  ⟵ *gate*

Before the bank is correct, prove the whole path with a stub:

1. The Step 1 image, taking one scenario and writing a `results.json`.
2. A `backend='metadrive'` row routed by the shared queue, taking the lock, launching, ingesting.
3. One frontend route showing it in the **same** queue as CARLA jobs.

Then wire the real bank behind it. A green round-trip against a stub is worth more than a correct
bank nothing can run — and here it also proves the routing before either side is finished.

**This is the only step blocked on Tyrone** (see below).

### Step 7 — the ETA model

Under one queue a bad MetaDrive estimate corrupts the wait shown to every CARLA job behind it, so
this is load-bearing rather than cosmetic. His model keys on actor count and `run_cost_samples.
actor_count` is `NOT NULL`; a PG bank has no such axis, so it needs a nullable column or a
per-backend keying.

Bootstrap from measured per-category wall time — **Phase 4b's calibration runs produce it for
free** — keyed on `(category, tier)`, then replace it with a **median** of the last N real runs as
they arrive. Median, not mean: one degraded run is a 5x outlier that poisons a mean for weeks. With
a ~1 s AV3 forward pass a 35-scenario run is long enough that an absent estimate is a visible gap.

### Step 8 — the frontend section

New React routes under `/metadrive`: the six-axis options form rendered from `GET /options`, the
scenario picker, and run results. Reuse his `useStream` SSE hook and shadcn components; do not reuse
his Submit page.

---

## What Tyrone must do (four small changes)

Only Step 6 waits on these.

1. **Migration `008_backend.sql`:**
   ```sql
   ALTER TABLE jobs ADD COLUMN backend TEXT NOT NULL DEFAULT 'carla'
       CHECK (backend IN ('carla', 'metadrive'));
   ALTER TABLE jobs ADD COLUMN params_json TEXT;  -- backend-interpreted, opaque to the queue
   ```
   `DEFAULT 'carla'` backfills every existing row with no data migration, and the `jobs_queue` index
   (`state, priority, queued_at`) is unchanged — routing reorders nothing, so the ordering rule
   people already trust does not move.
2. **A runner registry** in `runner/queue.py`: `{"carla": JobRunner(...), "metadrive":
   MetaDriveJobRunner(...)}`, selected on `job["backend"]` in `run_forever`, `_resume` and `cancel`.
   The import points at our package — this is the dependency arrow, and it points from him to us.
3. **ETA becomes a sum** over the jobs actually ahead, not `estimate.seconds * position`
   (`api/queue.py:197`). A correctness fix for his own queue either way.
4. **`recover()` sweeps both compose projects.** It is currently scoped to one, so a crashed
   MetaDrive stack would survive a restart invisibly and then fight the next job for the card.

Whether the loop ends up in his `QueueWorker` or in a new jointly-owned dispatcher is deliberately
undecided; every step above is identical either way.

---

## How you test it

**Routing, and that ordering survives it** — the point of the whole design, so prove the
interleaving rather than assume it:
```bash
# queue CARLA, MetaDrive, CARLA in that order; expect them to run in that order
curl -s localhost:8080/api/v1/queue | jq '.jobs[] | {position, backend, state}'
```

**The lock is still the authority** — this failure is silent, so prove it against something the
queue does not know about:
```bash
bash wing-sim/deployment/with_rig_lock.sh sleep 60 &   # not a queued job
curl -s localhost:8080/api/v1/queue | jq '.rig'        # expect held, foreign
stat -c '%i %n' ~/simulation/.wing-sim.gpu.lock        # same inode from both sides
```

**The data roots do not collide** — run a CARLA job to completion while a MetaDrive submission sits
staged; the staged tree must survive his `wipe_run_owned`:
```bash
ls ~/simulation-md/staging/<job_id>/
```

**Restart recovery** — kill the orchestrator mid-MetaDrive-run, restart it, and confirm the run is
adopted rather than orphaned, and that a crashed stack is swept.

**Parity with the CLI** — `POST` a two-scenario job and assert the stored result is identical to the
`results.json` the CLI produces for the same inputs. Both are callers of `run_bank()`; if they
disagree, one of them is configuring the env differently.

---

# Phase 8 — Traffic lights, camera-model scope  (~1 day)

**Goal:** the Lights axis. Requires Phase 2b green.

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
  reimplemented.** This matters more than usual here: `map_id` is computed at generation, again in
  `verify`, again in `selftest`, and again in the runner's refusal check. Four call sites that must
  never be able to disagree about what a map is.

**Tests**
- `tests/unit/*.py`, no `conftest.py`, fixtures local to the module that needs them, heavy
  `@pytest.mark.parametrize`. Test names are full English sentences —
  `test_changing_traffic_level_does_not_change_map_id`.
- Simulator-dependent tests: `pytest.importorskip("metadrive")`, and **named** `skipif` guards
  (`needs_sim`, `needs_bank`) rather than bare ones. The repo's rule: *"a skipif that stops running
  silently is worse than one that fails."* Applies directly to Phases 2b and 3 — an invariance
  check skipping its comparison loop because MetaDrive was missing would be the worst possible
  failure mode, because it looks exactly like a pass.

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
  - `component/map/pg_map.py:111` — `get_meta_data()`, the source of `map_id`.
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
  - `db/migrations/001_initial.sql` — the schema this plan shares a `jobs` table with, and the
    comment stating the GPU lease row is observability, not authority.
  - `db/jobs.py` — the FIFO ordering rule and why it is not cleverer than that.
  - `db/job_presets.py`, `db/migrations/005_job_presets.sql` — the model for our `job_scenarios`
    table: identity minted before launch, `skipped` distinct from `failed`.
  - `api/queue.py:197` — the ETA bug that one queue forces us to fix.
- `converter-scenarionet-stage2-redesign/tools/drive.py:300` — `_refuse_mismatch`, the precedent
  for Phase 4's integrity refusal.
- `converter-scenarionet-stage2-redesign/tools/signal_control.py` — the phase model Phase 8 ports,
  including the timestep and per-group-offset traps already paid for there.
- `converter-scenarionet-stage2-redesign/docker/Dockerfile` — the base for Phase 5.
- `converter-scenarionet-stage2-redesign/src/osm_scenario/acquisition.py:199-247` — the manifest
  writer to model `manifest.py` on.
