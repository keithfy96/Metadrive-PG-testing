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
observe 19. Stated for the webapp in Phase 7 Step 6's results notes (Phase 6 retired 2026-09-14).

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

## How this ships — one queue, one agent per rig, two rigs

**Decision (2026-09-01, with Keith).** This bank is not driven by a CLI and a JSON file. Work
is queued on a **NAS** and runs on **two rigs**.

**Revised 2026-09-13, with Keith.** The producer is **our studio**, served from the NAS; Tyrone's
webapp no longer puts jobs on the queue, it receives our results. And the "MetaDrive orchestrator"
box is gone: nothing on the NAS dispatches. A **rig agent**, one container per rig with a worker
per GPU, takes a card's lock and *only then* leases a job — *lease when a card is free*. The queue
itself knows messages, not GPUs (see Phase 7, **What the queue knows**), so the only process that
can know a card is free is the one holding it. Four containers, and it matters which are ours:

```
 NAS ─────────────────────────────────────────────────────────────────────────────────
   [1] studio      scenariobank-studio   OURS. Authors banks; writes the bank to the share,
                                         then put()s the job; reads results off the NAS store
                                         and pushes each new one to Tyrone's webapp
       wfqueue     not a container of ours: the queue service already on the NAS
                   (docs/queue-docs/, WFQUEUE_URL). Lease / ack / nack, at-least-once,
                   priority then FIFO. Also leased by the CARLA orchestrator (Tyrone's)
       share       banks/<bank_id>  models/  results/<job_id>   mounted on both rigs

 rig A (rig B identical) ─────────────────────────────────────────────────────────────
   [2] agent       scenariobank-sim, no GPU   OURS. One per rig, a worker per card; the
                                              docker socket, the share and the lock dir
                                              mounted. Per worker: lock the card, lease
                                              one job, run [3]+[4], DELIVER the results to
                                              the NAS, ack, release
        worker gpu0 ─► flock gpu0 ─► lease ─► [3] bridge-gpu0  openpilot, port 5558
                                              [4] sim-gpu0     scenariobank-sim, device 0
        worker gpu1 ─► flock gpu1 ─► lease ─► [3] bridge-gpu1  openpilot, port 5559
                                              [4] sim-gpu1     scenariobank-sim, device 1
                        ▲ CARLA and hand-run scripts take these same cards, through the
                          same lock files and no queue of ours
```

| # | container | image | where | lifetime | GPU |
|---|---|---|---|---|---|
| 1 | studio | `scenariobank-studio` (built here, FROM the sim image) | NAS | always up | no |
| 2 | rig agent | `scenariobank-sim` (`python -m scenariobank agent`) | each rig, one | always up | no |
| 3 | openpilot bridge | `metadrive-wingfin-openpilot:prod` | each rig, one per *running* simulation | for the job | no |
| 4 | simulator + model | `scenariobank-sim` (`scripts/sim-run.sh run …`) | each rig, one per running job | for the job | that card |

**Ours:** the studio, the rig agent, and the two containers a job runs in. **Not ours:** the
queue, Tyrone's webapp (a consumer of our results), and the CARLA orchestrator. A rig with two busy
cards runs five containers; three images in total, all built from this repo on the rig (Phase 5).
The queue is not something we run — on the laptop its stand-in is Phase 7 Step 0's replica, a
plain Python process.

Phase 7 is the deliverable. The evaluated model is a camera model: see **No lidar** and **Phase 4**.

**Out of scope, decided the same day:** getting the model checkpoint onto the rigs. It arrives by
some other route -- most likely a cronjob. The runner takes a local path and never fetches.

### The words, because two projects use one of them differently

wing-sim calls its **rig-side** service "the orchestrator" (`orchestrator/README.md`: *"Owns the
rig: one queue, one lock, one archive, one database"*). Here there is no orchestrator of ours at
all (since 2026-09-13): the rig-side **agent** is our only long-running process, and it pulls.
Every sentence in Phase 7 uses these five words in exactly this sense:

| word | means |
|---|---|
| **queue** | `wfqueue` on the NAS. Not ours. `docs/queue-docs/`. At `http://192.168.1.90:9090` today (the doc's own header says `localhost:8080` because it was rendered by a dev copy); read from `WFQUEUE_URL`, never hard-coded. Our topic is **`metadrive`** (pinned 2026-09-08); CARLA's is Tyrone's to name. |
| **orchestrator** | *retired 2026-09-13; there is none of ours.* The word still names Tyrone's rig-side CARLA service. |
| **agent** (was **runner**) | our one container per rig, a worker per GPU: lock, lease, run, deliver, ack. Owns that rig's locks, its bridges and its simulator containers. |
| **bridge** | openpilot's planner and controller behind a TCP port — one process per *running* simulation, a resource the worker starts, not a controller. |
| **container** | the image that actually simulates. Calls `run_bank()`. |

### R1 — the independence rule (hard constraint)

**Tyrone's code is a reference, never a dependency.** No module of his is imported by anything here.
Read it to avoid rediscovering failures he already paid for; write our own.

Under this topology R1 costs almost nothing, because **neither side imports the other**. His
orchestrator and our agent lease from one queue, each running its own containers; there is no
seam between the two codebases at all. What is shared is a queue and a lock *path*, and neither violates R1, because **a
schema is data and a path is not a library**:

- the queue -- HTTP against a documented API, using the stdlib client the server itself serves
- `~/simulation/.wing-sim.gpu*.lock` -- files opened with `flock`, not an import of `rig/lock.py`

### Why a queue on the NAS, when each rig has a lock

The GPU argument is the weakest justification. A flock is advisory and kernel-released on holder
death, and his `001_initial.sql` says outright that the `gpu_lease` table is "Observability only,
NOT the authority." Two consumers polling one rig's lock already cannot double-book that card.

What the queue fixes is what a lock cannot express:

- **Ordering across two machines.** A lock says busy or free; it cannot say *whose turn*. `wfqueue`
  leases by priority then FIFO by id, so the order is a property of the queue rather than of who
  polled first.
- **Work that outlives its worker.** A lease expires and the message returns to `ready` on its own.
  An agent that dies mid-run loses nothing, which is not true of a lock plus a list.
- **A place for jobs that are wrong.** `nack` past `max_attempts` dead-letters, and dead is
  inspectable and requeueable. A wedged job stops being a mystery.
- **One screen**, not two tabs where a user guesses why their job is not moving.

### Three things the served client does that the doc does not say

*(Added 2026-09-08, re-reading `docs/queue-docs/queue-client-v0.py` after the queue's real
address arrived. The file is byte-identical to the 2026-09-01 copy; only the doc's address
changed, and the plan already knew the queue's semantics. These three are behaviours of the
client rather than of the API, and each names the step that must handle it.)*

- **`_request` retries a POST** on 5xx and transport errors (`retries=2`, backoff 0.5 s). A
  retried `put` without `dedupe_key` can enqueue one run twice — so a producer mints the job id
  *before* `put()` and passes it as `dedupe_key` (Phase 2c Step 12). A retried `ack` after the
  first one landed returns `409` as a `QueueHTTPError`, which the agent reads as "already
  settled", not as failure (Phase 7 Step 5).
- **`consume()` defaults to `poll_interval=60`.** Long-polling is `poll_interval=0, wait=N`; with
  the default an empty long-poll is followed by a 60 s sleep (Phase 7 Step 5).
- **There is no automatic lease extension.** `msg.extend()` is a call; the timer is ours (Phase 7
  Step 5, property 2).

---

## What a person actually uses

Stated outright because the rest of this document is written in commands, and a reader would
otherwise conclude the command line is the product. It is not.

| surface | for | whose |
|---|---|---|
| **the studio** (Phase 2c) | authoring a bank: pick a scenario type, build it, look at every scenario, swap a poor seed — and later, submit a run onto the queue. **Served from the NAS**, and the only producer onto the queue (2026-09-13) | ours |
| **the wing-sim webapp** | receives our results, pushed by the studio; no longer a producer (2026-09-13) | Tyrone's |
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
| Trust two runs of one seeded job to agree because every manager draws from a seeded stream | They did not, on `curve` at `hard`, and neither draw nor seed was the cause (Phase 4 Step 5, measured 2026-09-09). `Lidar.get_surrounding_objects` (`lidar.py:170`) returns a **`set` of objects**, iterated by address, and the IDM policy keeps the nearest object per lane off it with a strict comparison (`idm_policy.py:83`) — so with cones tied on longitude, which cone a traffic car sees is the heap layout, and the same row ended at 339 steps or 348 depending on the size of the process's environment block. And an episode run after another in one env is not the episode run alone (339 alone, 218 after one row), through something `reset` does not clear and `force_destroy` does not touch. `env.pinned_lidar_class()` sorts the sets and `run_bank` builds one env per row; `test_reproducibility.py` holds both. |
| Trust the mirror because the geometry test passes and the expert arrives | The map was exact and two controllers were not (Phase 4 Step 5c, found by looking at a film). `IDMPolicy` read the mirrored lateral and drove unmirrored steering, so every lane change drifted to the kerb; and its `act` swallows any exception into "no front object", so an object without a `lane` attribute in a car's lidar radius -- our pedestrians and cyclists -- made that car drive blind. A check behind an *idle* ego showed neither. Measure traffic with the expert driving, against stock MetaDrive in a subprocess, and give every object the traffic can see a `lane`. |
| Build a rig env and then start a subprocess | The first `reset` of an env with `image_observation` on runs `asset_loader.py:116`, which writes `PYTHONUTF8=on` into `os.environ` -- a value CPython rejects at startup -- so every child process started afterwards in that process dies with `preconfig_init_utf8_mode: invalid PYTHONUTF8`. Headless envs never reach that line, which is why nothing noticed before Phase 4 Step 6. `env._with_rig` puts the variable back after every reset; `test_camera_rig.py` starts a child to prove it. |
| Trust a render-mode env to drive the headless row, because the cameras never enter the observation | The observation is not the only path. `preload_models` (default True) runs only in a render mode (`base_engine.py:749`): it spawns a pedestrian, a light, a barrier and a cone at reset and returns them to the object pool, and the row then reuses those warmed objects where a headless env builds fresh ones. `curve_0000` at `hard` parted from the headless drive at decision 60, in the seventh decimal, reproducibly; a check at the pinned levels, with nothing placed, passed. `env.build_env` sets `preload_models=False` with a rig; `test_camera_rig.py` compares whole records at `hard` (Phase 4 Step 6b). |

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
any single axis. It goes first in Phase 7 Step 6's results notes (the camera-only statement), so
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
LEVELS = {                       # measured in Phase 4b, 2026-09-13 — see level-calibration.md
    "traffic":     {"none": 0.0, "low": 0.1,  "medium": 0.3,  "high": 0.5},
    "cones":       {"none": 0,   "low": 1,    "medium": 4,    "high": 6},
    "barriers":    {"none": 0,   "low": 1,    "medium": 2,    "high": 3},
    "pedestrians": {"none": 0,   "low": 1,    "medium": 3,    "high": 6},
    "cyclists":    {"none": 0,   "low": 2,    "medium": 3,    "high": 6},
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

*(As shipped by Phase 4 Step 1, every tier carries `lights="none"` and `options.PHASE_8_LIGHTS`
records the `low` / `medium` above for Phase 8 to flip them to; `resolve_options` refuses the axis
above `none` until then.)*

The six axes are the contract; tiers are aliases only. Explicit flags win: `--tier hard --cones none`
resolves to hard everywhere except cones. Keep raw values reachable — `--traffic medium` and
`--traffic-density 0.15` must both work, because house style in the converter repo is raw values.

Cones and barriers are placed by MetaDrive's own `TrafficObjectManager` (straight and curve blocks
only); pedestrians and cyclists by `VRUManager`, per actor on a road drawn off the map after the
first block (Phase 4 Step 4b). See **New modules**.

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
- **Frontend**: **one producer onto the queue, our studio** (Phase 2c, Step 12), served from the NAS; the wing-sim webapp receives our results. `run_bank(...)` stays importable and CLI-free, so the container and the studio's worker are two callers of one core. *(Settled 2026-09-13; see **How this ships**.)*
- **The GUI is the surface; the CLI is the engine.** No authoring workflow requires a terminal. The CLI is not deleted because a subprocess is forced by `BaseEngine.singleton` — see **What a person actually uses** for the reasoning, so this is not reopened as a matter of preference. *(Decided 2026-09-02, Keith.)*
- **Categories**: the seven as drafted.
- **Seeds**: fixed at 0, 1, 2, 3, 4 for every category. 35 maps.
- **Options**: six axes, stored normalized, applied at run time.
- **Handedness**: **left-side traffic** (right-hand-drive market — Singapore, UK, Malaysia, Japan). This is not a MetaDrive setting; see **Traps**. Enforced by `handedness.install()`, called from `base_config()` so no caller can forget it, and **measured** rather than asserted by Phase 0 `doctor` and Phase 2 `generate`.
- **Policy**: the AV3 camera adapter is the contract from Phase 0, not adapted in later. *(Amended
  2026-08-30 — was "build against the state-vector callable now". Reversed because a state-vector
  policy cannot perceive five of the six option axes, so it could never have been the thing scored.)*
  `ConstantPolicy` and `ExpertPolicy` survive as internal diagnostics, never a product surface.
- **Independence (R1)**: no module of Tyrone's `wing-sim` is ever imported. Shared: the queue's
  HTTP API (a schema) and the GPU lock path (a path). See **How this ships**.

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
                            #   calibrate run schema validate
    categories.py           # CATEGORIES dict: block_seq, destination, max_steps, description
    options.py              # LEVELS, TIERS, resolve_options() -> ResolvedOptions (names, numbers, origins)
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
    video.py                # a top-down film of a run, for the eye (Phase 4 Step 5b)
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
    agent/                  # Phase 7 — the rig agent (R1: imports nothing of wing-sim's).
                            #   One container per rig, a worker per GPU. Rewritten 2026-09-13
                            #   from a nas/ orchestrator + rig/ HTTP service: nothing on the
                            #   NAS dispatches, nothing on the rig listens.
      loop.py               #   per worker: lock the card, lease one job, run, deliver, ack.
                            #   A held lock is a sleep, never a nack.
      lock.py               #   the rig lock shared + this card's exclusive, holder file
                            #   kept separate, /proc/locks as a witness and never an authority
      session.py            #   bridge up on this card's port, sibling sim container via
                            #   scripts/sim-run.sh, supervise, harvest, deliver, tear down.
                            #   `agent --once job.json` is this file with a file for a queue.
                            #   supervise.py and deliver.py were planned beside it and are IN
                            #   it (2026-09-15): supervision is twenty lines around one file
                            #   read, delivery is a copy and a rename, and three files would
                            #   have been three import cycles around one object.
      jobs.py               #   names in the job -> paths on the rig, under the four roots, and
                            #   every refusal that must happen before a card is taken. Planned
                            #   as paths.py; it is the validation that made it worth a name.
      queue_client.py       #   vendored verbatim from the NAS (`curl -O $WFQUEUE_URL/source/client.py`,
                            #   so its header carries the real address); a test pins its sha256
                            #   against docs/queue-docs/ so a server-side change to the client is
                            #   noticed rather than absorbed. Do not reimplement.
    web/ (Phase 7 additions)
      options.py            #   GET /options: the six axes as data, for the studio's submit form
      results.py            #   the NAS-side index the studio reads; the push to Tyrone's webapp
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
  CONTRACT.md               # written with Phase 7 Step 6, when the webapp's payload is known (Phase 6 retired)
  banks/<bank_id>/          # generated and imported banks; not in git
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
- `handedness.py` — **mirror the PG geometry layer about the x-axis, before any map is built.** MetaDrive drives on the right and offers no way not to (see **Traps**), so this is where the market is decided. Four sign changes and no others *(the fourth found 2026-09-09, Phase 4 Step 5c)*: negate `StraightLane.direction_lateral` (positive lateral becomes the vehicle's left, which walks the whole map — opposing carriageway, lane lines, sidewalks — to the other side, because all of it is placed off lane frames); invert `clockwise` on every `CircularLane` (this is what makes roundabouts circulate clockwise); and invert the **three** `is_clockwise()` sites in `create_pg_block_utils` that use it for *lateral* arithmetic rather than for arc direction (`:130`, `:271`, `:339`), which flip a second time so the two cancel; and negate the lateral term back inside `IDMPolicy.steering_control`, because the traffic reads the mirrored lateral and drives unmirrored steering, so without it every car that changed lanes drifted to the kerb (Phase 4 Step 5c). Installed from `base_config()`; idempotent. Rewrites those three lines from the module's own source and raises `HandednessError` if they are not found verbatim — the commit pin exists so MetaDrive's internals cannot move under us, and a patch that silently stopped applying would leave a working bank that is simply the wrong market.
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
  and per scenario, so leaving them in would put two answers in one file. `random_spawn_lane_index`
  stays `True`: it is the only thing distinguishing the five `X` seeds (see **Traps**).
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
  titled with the category, seed, rule, node, rotation and length. One per scenario, never per
  map, and always with the route drawn: see **Traps**, "Read a turn direction off a map picture".
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

Generation is **~5 s** for the full bank with route thumbnails (35 figures; matplotlib costs about
a second to import). The first run after an install reads 6.2 s — that one also builds matplotlib's
font cache.

---

# Phase 2b — *(folded into a unit test)* ✅  ⟵ *written 2026-09-08, with Phase 4 Step 4b*

**Status:** both tests are written, in `tests/unit/test_invariance.py`, and green on `curve` and
`intersection_left`. They shipped with the two managers they guard (Phase 4 Step 4b), which is
where the decision below said they belonged. Everything below is a decision that already held.

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

**This is not Phase 7.** Phase 7 runs a *submitted model* against a finished bank: an agent on
each rig, a queue on the NAS between it and the studio. This is for us, authoring the bank, on
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
studio restart, show a running job rather than lose it. Same property Phase 7's agent turns on, for
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
mean in Phase 7 Step 6's results notes before the webapp renders the old shape.

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

Choose a bank, choose a model, choose the six option axes, and press submit. **The action is:
write the bank to the share, then `queue.put(...)` onto the NAS topic — in that order, and nothing
else** — nothing runs on this machine, and no results come back to this page. The studio is the
only producer onto the queue (2026-09-13; the wing-sim webapp receives results instead). The order
is not tidiness: the bank goes to `banks/<bank_id>` on the NAS share *before* the job is visible,
so a rig never leases a job whose bank is not there yet (it would dead-letter it), and no card
idles on a transfer once it has leased — Keith's requirement, 2026-09-13. The job carries the bank
**id**, not a path; the agent resolves it on the rig (Phase 7, **Files**).

The form is rendered from Phase 7's `GET /options`, for the same reason every other form here is
generated: a hand-written copy of the six axes is a second declaration of them.

**The payload is Phase 4 Step 3's `Job`, and the topic is `metadrive`** (pinned under **How this
ships**, 2026-09-08). The studio mints `job_id` (uuid4) *before* `put()` and passes it as
`dedupe_key`, so the client's own POST retry cannot enqueue a run twice. **Still blocked on Phase
7 Step 8's `GET /options`** for the form. Recorded here rather than in Phase 7 because the screen
is ours and the queue is not.

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

# Phase 3 — Import: stored scenarios from the converter ✅  ⟵ *the number is reused*

**Status:** all six steps are built. `scenariobank workspace` reads a converter workspace and says
what is in it, `scenariobank importing` turns that reading into
[`docs/reference/importing.md`](docs/reference/importing.md), the checklist an import must satisfy,
and `scenariobank import` satisfies it: one workspace becomes one bank under schema 1.4, beside the
procedural ones. The studio reads `source` off the manifest, lists the two kinds under their own
headings, and reviews a recording with measures of its own. **Step 6 closed the gate**:
`scenariobank replay` drives `banks/junction-1` end to end, which is the first environment built
anywhere in this phase and the first step loop anywhere in this package. A stored episode ends at
the recording's own length because `horizon` is set to it; the observation is `Box(31,)` rather than
the PG bank's `Box(19,)`, so a policy cannot be moved between the two kinds; and `--decision-hz` is
a stride in our own loop, moving the action count and never the episode length.

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
holding `roundy` and `curve` procedural, `junction-1` and `mosque` **written by a test helper as
real-world manifests** -- not run through `import`, so they carried no thumbnail and no `route`
block -- and a `broken` directory whose manifest will not parse: three headings, the search filters across all of them,
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

*(Amended 2026-09-07, in Step 5: the scratch banks above were **manifests**, written directly by
`tests/unit/test_web.py:_real_bank`, and the note first said they were "imported". Nothing Step 4
reads was missing from them -- the rate, the frame count, the route length, the duration and the
actor counts are all there -- but the thumbnail and the `route` block were `None`, so what the
scratch root did not exercise was the page rendering a real picture and a real route. Both are
verified against `banks/junction-1` in Step 5 below.)*

### Step 5 — `review` for a bank with no seeds ✅

`review`, the duplicate detection and the coverage chips are built on seeds, block sequences and
turn pairs. None of those exist here. This step decides what a real-world bank's review *is* —
route length, duration, actor mix, signal coverage — and makes the dataset tab render that instead
of the PG chips.

**Verify alone:** open an imported bank in the studio; every chip shown is a measured property of
the recording, and nothing reads "0 duplicates" from a computation that never ran.

*(Done 2026-09-07. 509 tests pass, ruff clean. Verified in a running studio against the real
`banks/` root — `banks/junction-1` re-imported at schema 1.4 and the four procedural banks
untouched. Every number the review reports matches what `scenariobank workspace` prints for
`scenarionet-100hz`: 395.1 m over 18 lanes, 3 lane changes, 14 junction moves, 37.8 s, up to 50 kph
slowest 10.42, 0 s waiting, 101 pedestrians / 25 cyclists / 24 barriers / 1 vehicle, 8 traffic
lights, 974 map features. No console errors; the procedural banks render exactly as before.)*

**The design constraint, and where it came from.** `scenariobank workspace` already prints exactly
this report about the conversion a bank was imported from (`workspace.format_report`), so the
review is **that, read out of the bank instead of the workspace** — same fields, same wording,
reusing `workspace._counts_line`. A bank that described its recording differently from the
workspace it came from would be a second description of one thing, and the two would part company
the day either was edited. If a number here disagrees with `workspace`, the review is wrong.

**Two models, never a mixture**, the rule schema 1.3 set. `Report.source` is the discriminator and
`Report.categories` is `list[CategoryReview | RealWorldReview]`. `RealWorldReview` carries `Drive`,
`Actors`, `SignalCover`, `Replay` and `MapSize` — siblings of `Duplicates` / `Coverage` / `Budget`
/ `Spread`, and not one of them a PG measure in disguise.

*Six things the plan above did not say, all from building it:*

*1. **Duplicates are absent, not zero — and `null` is a trap in JavaScript.** An import writes one
workspace as one bank as one recording, and every conversion in all four workspaces holds exactly
one scenario (measured with `workspace`), so there is no second drive to be distinct from.
`Report.distinct` is `None` rather than equal to `total`, because an equal number reads as a
computation that ran and found no repetition. But `null < 4` is **true** in JS, so the page's
"N distinct" pill needed an explicit `!== null` test — a truthiness check would have drawn
"· null distinct" on every imported bank.*

*2. **`review` gave up the refusal; `compare` kept it.** `_procedural()` is now `compare`'s guard
alone, and its message says why: a bank holds one recording, so there is no second drive in it to
compare the first against. The `/compare` endpoint was returning **500** on an imported bank —
`BankError` is a `RuntimeError` and only `LookupError` and `ValueError` were caught — which is now
a 422 with the sentence. Found by writing the test, not by looking at the page.*

*3. **The map-size drift, and the guarantee that now prevents it.** `importing.SECTIONS` has told
the reader since Step 2 that a bank carries `scenario.map_features` and
`scenario.map_feature_types`. It carried neither: `_rows()` never passed them, and nothing raised,
because `_verify` checks copied **files** and `_check` checks the **reader** — a claim about a
manifest field sat between the two guarantees. Fixed by schema 1.4, and closed structurally by
`importing.CARRIED` / `MINTED` and `_check_carried`, which raises unless every field of
`RealWorldRow` names the checklist key it came from. `workspace` reports 974 map features for
`junction-1` and 1322 for `mosque`, and neither number used to survive an import.*

*4. **Schema 1.4 costs a re-import, and `write_manifest` makes that worth saying.** The two fields
are defaulted, so a 1.3 bank keeps opening and reads honestly — `None` means "the converter did not
say", which is already what it means on `workspace.Scenario`, and both the CLI and the page print
"map size not recorded" rather than drawing a zero. But `write_manifest` stamps `SCHEMA_VERSION` on
the way out, so **any edit to a 1.3 bank restamps it 1.4 without gaining the numbers.** Only a
re-import gains them. `banks/` is gitignored and disposable, so that is one command per bank.*

*5. **A recorded card is a button again, and single-select.** Step 4 made it a tile because the
only panel behind it compared and replaced by rebuilding a seed. `recordedPanel` is the read-only
sibling of `detailPanel`: the conversion, the pickle it traces back to, the route from start lane
to end lane, the speeds, the waiting, the actors, the lights, the map size, and the step budget as
`3782 · measured off the recording` rather than `earned / cap`. It offers no compare, replace, add,
remove, edit or budget control. `pick()` clears the selection first on a recording, so the two-card
path is unreachable and `/compare` is never called from the page at all.*

*6. **One warning ships untested against real data.** "Mostly stationary" fires on nothing here —
every conversion in all four workspaces records `waiting 0 s, 0 stops` — so its numbers are
constructed in the test rather than measured. The other five are real: the signal note fires on
`junction-1`, "declared and never built" is the `mosque` case (four signals in the lane model, no
phase groups), and "the ego and nothing else" is `mosque`'s two 100 Hz conversions.*

*Also, a smaller one: `actorText` reads `VEHICLE` back as English because a person thinks in
vehicles and pedestrians. Nobody thinks in "road edge boundarys", so map feature types are printed
with the ScenarioNet names exactly as the data spells them — which is what `workspace` prints too.*

### Step 6 — the one-scenario round trip ✅  ⟵ *gate*

An imported bank drives one episode end to end under a diagnostic action. This is what says the two
bank kinds are one runner, before Phase 4 builds on the assumption.

Built as `scenariobank replay` (`src/scenariobank/replay.py`), **a diagnostic and not a runner**:
no result file, no policy loaded by path, no `resolve_options`, and the action is zero throttle and
zero steering. The result *record* is Phase 4 Step 3's, and one invented here to satisfy a single
recording would have designed the frontend contract by accident. What this step owns is the step
loop, which had never existed anywhere in this package — every other MetaDrive call in `src/` is
`reset`-only, and `doctor.probe_simulator` was the closest model.

**Two things this step had to get right, both now measured rather than assumed:**

- **Nothing ends the episode on its own — and `horizon` is the fix, not the step loop alone.**
  Confirmed and sharper than the note above: with `horizon` at `BaseEnv`'s `None` default
  (`base_env.py:84`, and `SCENARIO_ENV_CONFIG` never sets it) the env was still stepping at **6000**
  frames of a 3782-frame recording, neither terminated nor truncated. But `done_function` *does*
  read `horizon` (`scenario_env.py:162`) — it was simply never being set. So the step loop is not
  the only possible terminator: `replay_config` sets `horizon` to the row's own `budget_for` **and**
  `drive` caps its own loop at the same number, which is the belt-and-braces PG already gets.
- **The 19-dimensional observation is a PG fact — and the stored number is 31, not 41 or 161.**
  Measured on `banks/junction-1`: `Box(31,)` at reset and unchanged after the last of 3782 steps.
  The whole 12-wide difference is navigation — `TrajectoryNavigation.get_navigation_info_dim()` is
  `NUM_WAY_POINT * CHECK_POINT_INFO_DIM + 2` = **22** where `NodeNetworkNavigation` reports 10 —
  with the same `StateObservation` and the same `SENSOR_CONFIG` on both. Recorded once, as
  `config.SCENARIO_OBSERVATION_SHAPE` beside `OBSERVATION_SHAPE`, and `OBSERVATION_SHAPE`'s own
  comment now says it is a `MetaDriveEnv` fact. **A policy trained against one bank kind cannot be
  handed the other**, which is a fact Phase 4 has to refuse on and now has a number to refuse
  against.

**Done note (2026-09-07).** Five things this step found or settled:

1. **The third unknown is arithmetic, not a setting.** `--decision-hz` is a stride in `drive`'s own
   loop: replay advances one recorded frame per `env.step`, so `decision_repeat` stays 1 and 20/10/5
   Hz over a 100 Hz recording hold each action for 5/10/20 steps. Measured: 3782 steps at every
   rate, and 3782 / 757 / 379 / 190 actions. The rate moves the action count and nothing else.
2. **The observation width is reported at both ends of the episode**, and `format_episode` warns if
   they differ. One reading cannot make the claim "it was 31 the whole way", and a width that moved
   mid-episode is precisely the failure a single reading at reset cannot see.
3. **`replay` refuses a procedural bank by name**, the mirror of `compare`'s refusal — and refuses
   *before* importing MetaDrive, so a wrong bank is answered off the manifest on a machine with no
   simulator. A test asserts the ordering in the source, because the natural way to write the
   function puts the import first.
4. **`replay_config` is deliberately not `config.base_config()`.** That one pins `start_seed`,
   `num_scenarios`, `horizon: 1000` and `random_spawn_lane_index` for a generated road and calls
   `handedness.install()`, which mirrors MetaDrive's PG lane geometry — a stored map has none to
   mirror and already drives on the side it was recorded on. What the two share is the observation,
   so `agent_observation` and `SENSOR_CONFIG` are imported from `config.py` rather than restated.
5. **Cost, for sizing a batch:** about 2.6 ms per step, so ~10 s of wall time for the whole 3782-
   frame recording. Two full replays is what the length-invariance claim costs in the test suite;
   the slower rates are checked against a 100-step capped run instead.

**Verify alone:** met. `junction-1` route-1 runs to its own length (3782) and stops there with
`max_step` true; `--decision-hz` 20, 10 and 5 each act on the expected stride of a 100 Hz import
and none of them changes the episode length. *The result row carrying the provenance fingerprint
and the attribution string is Phase 4 Step 3's* — there is no result record in this step by
design, and the entry already carries both fields for it to read.

**Done when:** Steps 1-6 are ✅ and `docs/reference/importing.md` is checked in and reproduces from
the reader. **Met** — 546 tests pass, `uv run scenariobank importing` leaves the file unchanged.

---

# Phase 4 — Runner, results schema, reference policies ✅  ⟵ *Step 8's gate met 2026-09-12*

> **The commands in this phase are machine-run.** `run`, `calibrate`, `validate` and `selftest`
> are executed by the container's entrypoint, by the rig agent, and by CI — never typed by a
> person, and **no studio screen is owed for them**. The command blocks below are developer
> verification, and they stay in that form deliberately. See **What a person actually uses**.

**Goal:** the piece the frontend calls.

*(Re-read 2026-09-07 against the code after Phase 3 Step 6. What that pass changed: Step 3's
`horizon` claim was reversed by Step 6's measurement and is corrected; every line reference in
Step 2 had drifted and is re-pinned; the bank the verification blocks named, `pg-bank-2026-08`,
does not exist and every block now names one that does; `obstacles.py`, `actors.py` and the
invariance tests had no step and are Step 4b; the bundled expert's nondeterminism, the `lights`
axis in the tier table, and the X-block trap under Step 5's only bank are each named where they
bite; and every step now has a **Verify alone** command block with an expectation.)*

## Steps — the runner first, the cameras on top of it

Eleven checkpoints, each one testable alone. **They deliberately do not run in the order the
build notes were written in.** The notes lead with the AV3 port because that is the interesting
part; the work leads with `resolve_options` and env construction because a runner that is
reproducible against a two-line `ConstantPolicy` is the thing every later step is debugged
against. Steps 1-5 need no camera, no bridge and no GPU, and they end at **Test 3, the one that
matters**. Steps 6-8 are the port, and they land on a runner that is already known-good — so
when a six-camera run disagrees with a diagnostic one, the disagreement is the rig.

Markers follow the table under **Reading the markers**: ⬜ not started, 🔨 started, ✅ done when
that step's **Verify alone** is met.

**Every step below is written for a procedural bank.** Phase 3 adds a second kind, and the seam
that keeps them one runner is a single function between "an entry and its rows" and "an env plus
a per-row prepare step". Written out for both entry kinds:

| | procedural (`CategoryEntry`) | recorded (`RealWorldEntry`) |
|---|---|---|
| env | `MetaDriveEnv(base_config(map, start_seed, num_scenarios))` | `ScenarioEnv(replay_config(...))` — Phase 3 Step 6's |
| `horizon` | `entry.max_steps` | `entry.max_steps` — both entry kinds carry the field (`bank.py:159`, `:299`) |
| select a row | `reset(seed=row.seed)` | `reset(seed=row.scenario_index)` — both bound an *index*, so `num_scenarios_for` applies to both |
| prepare | `navigation.set_route(env.agent.lane_index, row.destination)` | nothing; the recording already has its route |
| loop cap | `entry.budget_for(row)` | `entry.budget_for(row)` — same name on both (`bank.py:170`, `:302`) |
| options | the six axes, resolved by Step 1 | `no_traffic=False`, `no_light=False`, `reactive_traffic=False`, pinned |
| observation | `OBSERVATION_SHAPE` = 19 | `SCENARIO_OBSERVATION_SHAPE` = 31 |

Above that line the run loop, the result record and the reproducibility diff never learn which
kind they are driving. **The loop already exists**: `replay.drive` (Phase 3 Step 6) is the only
`env.step` in the repo, and it refused procedural banks precisely so it would not become a second
runner. Step 2 moves that loop into `runner.py` and turns `replay` into a caller of it; from then
on `grep -n "= env.step(" src/` returns one site, and that is a **Done when** condition below.

### Step 1 — `resolve_options()`: names in, numerics out ✅

The only piece of this phase with no environment in it, so it goes first and stays unit-tested.

- `resolve_options()` reads `manifest.options` as its defaults, expands tiers, applies explicit
  flag overrides, and returns level names *and* numerics.
- The axis names and levels already exist in `options.py` (schema 1.2, `scenariobank options`);
  `LEVELS`, `TIERS` and this resolver are new. `LEVELS` is Phase 4b's to calibrate — this step
  ships placeholder numerics and the resolver that reads them, not the values. Two shapes of the
  placeholder are pinned anyway, because they are traps and not calibration: `traffic none` is
  exactly `0.0`, and every other traffic numeric is `>= 0.01` (`traffic_manager.py:65-67`
  short-circuits below that, so a smaller "low" is silently "none").
- **The resolver is `options_for(entry, row)`.** Step 10c *named* that signature so per-scenario
  overrides could be added later without touching a call site; it did not build it —
  `options.py` is 51 lines exporting `AXES`, `LEVEL_NAMES` and `Level`. This step builds it.
- Raw values stay reachable: `--traffic medium` and `--traffic-density 0.15` must both work
  (house style in the converter repo is raw values). A raw value resolves to the nearest level
  name *and* records the raw number, so the result still carries both.
- **`lights` is Phase 8's**, and the tier table under **The levels** says `medium` is `lights=low`
  and `hard` is `lights=medium`. Until Phase 8 lands, `resolve_options` refuses `lights` above
  `none` by name — "the Lights axis is Phase 8" — and the shipped `TIERS` carry `lights=none`
  with a note saying which values Phase 8 flips them to. The alternative is a `--tier hard` that
  either silently drops an axis or raises deep inside env construction.
- **The six axes are PG-only.** On a stored scenario (Phase 3) traffic, cones, barriers and lights
  come from the recording, and `ScenarioEnv` offers `no_traffic`, `no_light` and `reactive_traffic`
  instead. `resolve_options` must not assume an axis name means the same thing on both kinds; the
  resolved record says which kind it resolved for (`kind: "pg"` | `"recorded"`).

**Verify alone** — unit tests only, no simulator:

```bash
uv run pytest tests/unit/test_options.py -q
```
**Expect**, against `banks/curve`'s pinned block (traffic=low, cones=medium, barriers=high,
pedestrians=low): no flags resolves to exactly those names; `--tier hard` to
high/medium/medium/medium/low/none; `--tier hard --traffic low` to low, and hard everywhere else;
every record carries `kind: "pg"`, the six names and the six numerics; `traffic none` is `0.0` and
every other traffic numeric is `>= 0.01`; `--lights low` is refused with a sentence naming Phase 8;
a `source: "osm-scenario"` manifest resolves to `kind: "recorded"` with the three replay flags and
no axes.

**Built 2026-09-07** as `options.py` (51 → ~330 lines) and `tests/unit/test_options.py` (39
tests, none of them needing a simulator). No command gained a flag: `run` is Step 3's, and the
`--tier` / `--traffic` / `--traffic-density` spellings above are what `run` will map onto the
resolver's `tier=`, `levels=` and `raw=` arguments. What the step settled beyond the bullets:

1. **The record is a model, `ResolvedOptions`**, with `extra="forbid"` like every other block a
   result will carry: `kind`, `tier`, and per axis `levels` (name), `values` (the number the env
   gets), `origin` (`manifest` | `tier` | `flag` | `raw`), plus `raw` (the numbers given directly)
   and `replay` (the three switches, on a recorded bank). `origin` is what makes an override
   visible in a result without the manifest beside it, which is the promise Step 10c made.
2. **A raw value is the number the env gets; its level name is the nearest one, ties to the
   weaker.** Weaker because a raw value is somebody choosing to sit between two calibrated points,
   and calling it the harder of the two would report a run as more demanding than it was.
   Distances are rounded before comparing, because `0.15 - 0.10` is a hair under `0.05` in
   floating point and the tie test found it.
3. **Three raw refusals the bullets did not name.** A traffic density in `(0, 0.01)` is refused
   naming `traffic_manager.py:65-67`, rather than run as `none` under a result that says
   otherwise; a non-integer on a count axis is refused, because there is no half a cone; `lights`
   has no raw form at all. The same axis given as both a level and a raw value is refused as two
   answers, not ordered.
4. **A bank that already pins `lights` above `none` is refused too**, and the sentence names the
   unpin command — `scenariobank options` accepts the level today and nothing stopped it. The
   refusal is by *origin*, so it fires the same way from the manifest, a tier, or a flag.
5. **`REPLAY_FLAGS` is one dict**, defined here and spread into `replay_config`, so the env config
   and the result record cannot say different things about the same three switches. `replay.py`
   is the one file outside `options.py` and its test that changed.
6. **`options_for(resolved, entry, row)` exists and returns its first argument.** The signature
   Step 10c named, with the resolved bank options in front because the runner has them in hand
   per batch and the seam is per row. `entry` and `row` are deleted on entry, on purpose.

**Verify alone:** met — `uv run pytest tests/unit/test_options.py -q` → 39 passed, including the
on-disk read of `banks/curve` (skips by name where that bank is not checked out); full suite 585.

### Step 2 — env construction from a bank, and the one loop ✅

Build the env the manifest describes, behind the seam in the table above, and prove the three
config keys that silently do nothing when they are wrong. **A bank is a recipe, not a saved
scenario** — see Phase 2's **Scope** and **Stored normalized, applied at run time** — so this step
is that recipe's reader. The procedural half is new; the recorded half is `replay_config`, already
built. What is PG-only is the map build, `set_route`, and the six axes — *not* `horizon` and not
seed selection, which the table shows both kinds have.

**Already built — reuse it, do not restate it.** Three pieces already exist, with the reasoning in
their own docstrings:

- **`num_scenarios_for(seeds)` — `bank.py:412`**, whose docstring already carries the
  `base_env.py:926` argument: `num_scenarios` reads as a count and is not one, it bounds an
  *index*, and sizing it `len(seeds)` raises `scenario_index (seed) should be in [0:N)` the moment
  the seeds are not contiguous. That is live rather than hypothetical — `banks/curve` holds seeds
  `[30, 1, 2, 3, 22]`, so its `num_scenarios` is 31 for five scenarios, and
  `banks/t-junction-left-intersection`'s `t_junction` holds `[0, 28, 2, 4]`.
- **Grouping and `start_seed` — `bank.py:563`.** `generate` already groups by
  `(block_seq, seeds)` through `_by_block_seq`; the runner groups by **entry** instead, because
  what it needs one env for is one `horizon`.
- **`base_config()` — `config.py:59`**, with `_PER_RUN_KEYS` (`bank.py:85`) already naming `map`,
  `start_seed` and `num_scenarios` as the three a caller supplies.
- **`replay_config()` and `replay.drive()` — `replay.py`**, the recorded half of the seam and the
  loop, both from Phase 3 Step 6.

**New here:**

- **`env.py`** — `build_env(entry, options)` returning the env plus a `prepare(row)` callable, one
  branch per entry kind. `runner.py` takes the step loop from `replay.py`; `replay` becomes a
  caller of it, lifts its procedural refusal, and drives both kinds — `--decision-hz` keeps its
  Phase 3 meaning on both (a stride counted in our loop, never a MetaDrive key).
- **`horizon` = the entry's `max_steps`.** `base_config` pins `horizon: 1000` (`config.py:101`)
  and nothing maps an entry onto it yet, which matters — `t_junction` is 320 and `CCS_only` 1320.
  **`horizon` is the config key** (`metadrive_env.py:58`); `max_step` is a `TerminationState` field
  and setting it does nothing. On the recorded kind `replay_config` already does this.
- **The step loop enforces `budget_for(row)`** (`bank.py:170`, `:302`), which is a *per-row* cap:
  a row's own `max_steps` when it declares one, else its entry's. One env cannot carry two
  horizons, so these two are not the same number — `horizon` is a coarse env-level guard, and the
  loop cap is what actually bounds the episode and what the result records. It is also what makes
  a `horizon` that failed to take a bounded run rather than a silent 1000-step one. Belt and
  braces on both kinds, identically.
- **Pin the destination *after* the reset**, with `navigation.set_route(env.agent.lane_index,
  node)` as `bank.py:1299` and `variety.py:124` already do — **not** through
  `vehicle_config["destination"]`, which is read at construction. Destinations vary *inside* a
  category: `banks/t-junction`'s `t_junction` is `1T0_1_` at seeds 0 and 4 and `1T2_1_` at 2 and 3,
  because `StdTInterSection` exposes a different arm per seed. One env per entry and a
  construction-time destination cannot both hold. *(Step 5 made it one env per row, for a
  reason of its own; the argument here only strengthens.)* `set_route` is reproducibility-safe for the
  reason Phase 2 recorded — `auto_assign_task` draws its throwaway destination from
  `get_np_random(random_seed)`, a *fresh* generator rather than a manager's stream — and it raises
  on an unreachable node (`bank.py:1271`), which is the failure you want.
  **`bank.py:10-12` says the opposite and must be corrected with this step**: the module docstring
  tells the runner to pin `vehicle_config["destination"]`, which is where this step's wording came
  from. Still there, confirmed 2026-09-07.
- **Collisions are counted on the rising edge**, here, because the loop is here. `crash_vehicle`
  and its siblings are per-step booleans (`base_vehicle.py:43-45`, set at `:788-794`) and
  `contact_results` is a set of *type names* (`:61`, `:798`), so "once per vehicle per episode"
  needs an identity MetaDrive does not hand over. Counting each flag's low-to-high transitions is
  the cheap correct thing and is what the converter's complaint — a per-step count "reports one
  collision as thirty, and the number describes the frame rate" — is actually about. Per-vehicle
  identity via the `contactTest` nodes (`:755-760`) is a Step 3 check to attempt, not a promise.

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

**Verify alone** — drive with a zero action and no result file, which is what `replay` is for:

```bash
uv run scenariobank replay --bank banks/curve                                     # seed 30: no start_index assert
uv run scenariobank replay --bank banks/t-junction --scenario t_junction_0000     # ends at 320, not 1000
uv run scenariobank replay --bank banks/t-junction --scenario CCS_only_0000       # ends at 1320
uv run scenariobank replay --bank banks/t-junction-left-intersection --scenario t_junction_0001 --json \
  | jq '{seed, destination, steps, ended_by, observation_shape}'                # seed 28 -> 1T0_1_; _0002 is seed 2 -> 1T2_1_
cp -r banks/t-junction /tmp/scratch-tj && uv run scenariobank budget --bank /tmp/scratch-tj \
  --scenario t_junction_0000 --max-steps 100 && uv run scenariobank replay --bank /tmp/scratch-tj \
  --scenario t_junction_0000                                                     # ends at 100
uv run scenariobank replay --bank banks/junction-1                                # unchanged: 3782, [31]
uv run pytest tests/unit/test_env.py tests/unit/test_replay.py -q
```
**Expect:** every seed in every PG manifest resets; a zero action on `t_junction` ends at 320 with
`max_step`; `CCS_only` at 1320; the scratch row at 100 while its sibling rows still end at 320;
`Episode.destination` (new field, read off `navigation` after `set_route`) equals the manifest
row's on both `t_junction` rows whose destinations differ; `observation_shape` is `[19]` on every
PG bank and `[31]` on `junction-1`, asserted against the two `config.py` constants by kind; the
Phase 3 Step 6 tests pass unchanged. `test_env.py` pins offline that a PG entry's config has
`horizon == entry.max_steps`, `num_scenarios == num_scenarios_for(seeds)` and `start_seed ==
min(seeds)`, and that a recorded entry's config is `replay_config`'s dict.

**Built 2026-09-08** as `env.py` (the seam, ~230 lines), `runner.py` (the loop, ~150 lines),
`replay.py` cut to a caller of both, and `tests/unit/test_env.py` (16 tests) plus
`tests/unit/test_runner.py` (9 tests, a fake env, no simulator). Every command in the block above
was run against the real banks and gave the expected number: `curve` seed 30 resets and runs 1200;
`t_junction_0000` ends at 320 and `CCS_only_0000` at 1320, both by `max_step`; the scratch row at
100 with its siblings still at 320; `junction-1` unchanged at 3782 and `[31]`; every PG drive
`[19]` at both ends. What the step settled beyond the bullets:

1. **The bank's `t_junction_0001` is seed 28 → `1T0_1_`, not `1T2_1_`** as the comment above
   said; `_0002` (seed 2) is the `1T2_1_` row. Both are driven and both read back off
   `navigation.final_road.end_node` equal to the manifest. Comment corrected.
2. **A row budget below its entry's `horizon` is a third ending.** `horizon` is the entry's
   320 and the scratch row's cap is 100, so the env never says `max_step` and the loop stops
   first. That is `replay.BUDGETED` ("hit its own budget"), distinct from `CAPPED` (`--steps`)
   and `STILL_DRIVING` — the belt-and-braces bullet, visible in the report rather than read as
   "still driving".
3. **The step rate is per kind and known off the manifest.** `env.step_hz_for` is the
   recording's `step_hz` or 10 Hz on a road (`base_env.py:190-191`, the two keys `base_config`
   does not touch — pinned by a test so the number cannot drift from the env). So
   `--decision-hz 20` on a PG bank is refused before anything is built, naming "the env's 10 Hz";
   Step 7's 20 Hz AV3 decisions on a road need `physics_world_step_size` set there, which is
   Step 7's to do and now has a number to refuse against.
4. **Only `traffic` reaches the config.** `build_config` maps the resolved traffic value onto
   `traffic_density`; the four count axes have no config key until Step 4b's managers exist.
   `banks/curve` pins traffic=low, so its drive is the first with cars in it — zero rising-edge
   collisions under a zero action, which is the counter's first live reading.
5. **`replay_config` moved to `env.py`** and is re-exported from `replay` so Phase 3 Step 6's
   tests import it unchanged; `build_config` on a recorded entry is the same dict sized for the
   whole entry (`num_scenarios_for` over the indices, `horizon = entry.max_steps`), equal to
   `replay_config`'s on the one-row entries every bank here holds.
6. **Two Step 6 tests could not pass unchanged**: the ones that asserted the procedural refusal
   (`recorded()` and the command's "procedural bank" sentence). The refusal is lifted, so they
   now assert the lift and the road's decision-rate ceiling; the hand-built `Episode` gained
   `kind="recorded"`. The other 30 pass as they were.
7. **The one-loop check matches the call, not the word.** `grep "env.step("` also finds
   `categories.py:175`'s comment, so the test (`test_runner.py`) and the Done-when below match
   `= env.step(`.

**Verify alone:** met — every command above, plus `test_env.py` 16, `test_runner.py` 9,
`test_replay.py` 35 (33 unchanged); full suite 609 passed, ruff clean, `commands.md`
regenerated with `replay` describing both kinds.

### Step 3 — the result record, and a batch that never aborts ✅

The schema everything downstream reads. Built before any policy worth scoring, so the record is
designed once rather than grown around whatever the first run happened to emit. **The fields are
named here**, because Steps 4-5, Phase 5 Step 4 and Phase 7 all `jq` them and this plan had been
inventing them at each site:

- **top level:** `schema_version`, `started_utc`, `finished_utc`, `job_id` (`str | None`: the
  queue message's id or the studio-minted one; `null` from the CLI), `attempt` (`int | None`:
  the lease's `attempts`, so a redelivered job's second result says it is one — these two are
  the only queue facts a result carries, and the queue is otherwise invisible below the
  agent), `stopped` (`bool`: the batch was told to stop, so the agent can tell a
  partial run from a whole one without scanning rows),
  `bank` (`path`, `id`, `source`, `schema_version` — `schema` is a pydantic name — and
  for a recorded bank `provenance` and `attribution` — the entry already carries both),
  `policy`, `options` (Step 1's record, names and numerics, with its `kind`), `env`
  (`observation_shape_before`, `observation_shape_after`, `step_hz`, `decision_hz`, `stride`,
  `metadrive_commit`), `results[]`, `summary` (`n`, `success_rate`, `by_failure_reason`,
  `by_status`).
- **per result:** `scenario_id`, `category`, `seed` or `scenario_index`, `destination`, `status` (`ok` |
  `error`), `success` (`info["arrive_dest"]`), `failure_reason`, `steps`, `actions`, `reward`,
  `cost`, `wall_time_s`, `collisions` (`{vehicle, object, human, building, sidewalk}`, rising-edge
  counts from Step 2), `route_completion`, `actions_digest` (`fingerprint.sha256_hex` over the
  per-decision action stream — what Step 8 diffs), and `traceback` on an error.
- **`failure_reason` is a string, not a boolean**, taken from the `TerminationState` keys
  *actually present in `info`* in a fixed precedence. Measured, not read off the enum: `idle` is
  defined (`constants.py:34`) but nothing in `metadrive_env.py`, `base_env.py` or
  `scenario_env.py` ever writes it; `crash` (the aggregate) and `env_seed` *are* written.
  `replay.ENDINGS` is the measured list; promote it.
- `--save-trajectories` optional (off by default; the only large artifact).
- **Never abort the batch**: catch per-episode, record `status: "error"` + traceback, continue.
  And **never wait for the end to write**: each scenario's result is written to
  `<out>/results/<scenario_id>.json` the moment it ends, and `results.json` is assembled from
  those files last. That per-scenario file is the progress signal Phase 7 Step 1 reads (its
  "record directory and exit-code file"), and it is why a run killed at 30 of 35 is a scored
  partial run rather than a lost one. *(Added 2026-09-08, for the queue: a lease is a clock and
  the agent extends it off progress it can see.)*
- **Stoppable.** `run_bank` installs a SIGTERM/SIGINT handler that sets a flag; the loop reads it
  through `run_episode(stop=...)` once per step (an additive parameter on Step 2's loop, with a
  `stopped: bool` on `Drive` so a stopped episode is not misreported as capped or budgeted); the
  episode ends with `failure_reason: "stopped"`, the batch writes what it has, exits 0, and
  `env.close()` runs on the normal path. **Never let `KeyboardInterrupt` raise into
  `env.close()`.** It unwinds panda3d's GL context and bullet's world; that segfaulted and wedged
  the GPU until a reboot. `tools/drive.py` is the precedent — it keeps its exit handler armed
  until teardown returns. This belongs here, with the loop, not in the container entrypoint
  (Phase 7 Step 1 is a caller). A cancelled run that still writes its results is a scored partial
  run; one that does not is a lost one, so the container's stop timeout is budgeted rather than
  left at Docker's 10 s default. A wedged GPU blocks CARLA too. `replay` never passes `stop`.
- **The job is a model too.** `results.py` defines `Job` beside `Results`: `{schema_version,
  job_id, bank: {id, path}, scenarios: [scenario_id] | null, options: {tier, levels, raw},
  policy, checkpoint_path | null, decision_hz | null, save_trajectories}`, `extra="forbid"`.
  `run_bank(job, out)` is the one entry point; the CLI's `run` flags build a `Job`, the
  container's entrypoint reads one from a file (`run --job FILE --out DIR`, every other flag
  refused beside it), and **the queue message payload is a `Job`** — which is what unblocks
  Phase 2c Step 12 and gives Phase 6 its second schema. *(Built with one more field than
  listed: `attempt: int | None`. The container reads nothing but the job, so the lease's
  attempt count has to travel in it to reach the result.)*
- **`horizon` and the loop cap are the same belt and braces on both kinds.** Phase 3 Step 6
  measured that `ScenarioEnv.done_function` reads `horizon` (`scenario_env.py:162`); `replay_config`
  sets it to the row's budget. Unset, the env runs past the end of the recording in silence —
  6000 frames of a 3782-frame scenario, neither terminated nor truncated — which is why the loop
  cap stays as well.
- ~~`replay.Episode` is subsumed: `replay --json` prints one result of this shape.~~ *Decided
  against at build time (2026-09-08): `Episode` is a report — `ended_by` as a phrase, `budget`,
  `ms_per_step`, the `CHANGED to` warning — and a result is a record; folding one into the
  other loses the phrasing or bloats the schema. What the two share is `results.TERMINATIONS`:
  `replay.ENDINGS` is built from it, so the diagnostic and the record cannot rank an ending
  differently, and a test pins the two lists equal.*

*(Amended 2026-09-10 — **a relative `--out` lands under `out/`.** By then fourteen verify-run
directories from Steps 3-5c sat in the repository root and had been committed with it. `out/` is
the path `compose.yaml` mounts writable at `/out` and the one `.gitignore` covers, so `--out easy1`
from a terminal and `--out /out/easy1` from the container are now the same place; `--out out/easy1`
is not doubled and an absolute path goes where it says. **And a tier is a subdirectory**: the
same day, `--out film` run at `easy`, `medium` and `hard` had overwritten itself three times, so
`--out film --tier hard` now writes `out/film/hard/` and a run without a tier writes the directory
itself. `cli.under_out`; pinned in `test_results.py`. The verify blocks below still name
`--out hard1`, and read back `out/hard1/hard/results.json` -- the name predates the rule.)*

**Verify alone:** *(old test 4, plus the recorded kind)*

```bash
uv run scenariobank run --bank banks/t-junction-left-intersection --categories t_junction \
  --policy scenariobank.policies:RaisingPolicy --out raising; echo "exit=$?"
ls out/raising/results | wc -l                                                      # 4, one file per row
jq '.summary.by_status, (.results[0] | {scenario_id, status, traceback})' out/raising/results.json
uv run scenariobank run --bank banks/junction-1 \
  --policy scenariobank.policies:ConstantPolicy --out stored
jq '(.results[] | {scenario_id, steps, failure_reason, status}), .env.observation_shape_after, .bank.attribution' out/stored/results.json
uv run scenariobank run --bank banks/curve --policy scenariobank.policies:ConstantPolicy --out stopped & \
  PID=$!; sleep 5; kill -TERM $PID; wait $PID; echo "exit=$?"                    # stopped mid-episode
jq '.stopped, (.results[] | {scenario_id, failure_reason, steps})' out/stopped/results.json
uv run pytest tests/unit/test_results.py tests/unit/test_policies.py tests/unit/test_runner.py -q
```
**Expect:** exit 0 every time, `results.json` present every time, and `job_id`/`attempt` `null` in
all of them (the CLI is not the queue); `by_status` is `{"error": 4}` and each traceback names
`RaisingPolicy`; the recorded run has one result at `steps: 3782`, `failure_reason: "max_step"`,
`status: "ok"`, shape `[31]`, and `attribution` copied from the entry; the killed run has its
interrupted row at `failure_reason: "stopped"` with `steps` below 1200, every row before it
scored, no row after it, and no traceback from `env.close()`. Offline: `Results` and `Job` both
round-trip through pydantic with `extra="forbid"` and refuse an unknown field; `failure_reason`
precedence is pinned against a hand-built `info` carrying `crash` and `env_seed`; a collision flag
held high for thirty steps counts once; `run_episode(stop=...)` against `FakeEnv` returns
`steps == 7, stopped=True` when the flag goes up at 7, and the nine existing `test_runner.py`
tests pass untouched.

**Built 2026-09-08** as `results.py` (the two records, ~320 lines), `policies.py` (`load_policy`,
`ConstantPolicy`, `RaisingPolicy`, ~80 lines), `run_bank` and the stop flag in `runner.py`
(145 → ~550 lines), `run` in `cli.py`, and `tests/unit/test_results.py` (41 tests) plus
`tests/unit/test_policies.py` (10) and two more in `test_runner.py` (11; the nine untouched).
None of the new tests opens a simulator: the bank is a manifest in `tmp_path` and the env is a
scripted fake handed in through `runner.build_env`, which is what lets the batch's promises —
one file per row the moment it ends, an error is a row, a stop lands between two steps and the
env is still closed — be checked on every machine. Every command in the block above was run
against the real banks: `RaisingPolicy` on the four `t_junction` rows gave `by_status: {error:
4}` with each traceback naming it, exit 0; `junction-1` gave `steps: 3782`, `max_step`, `[31]`
at both ends, the attribution and the whole provenance block copied from the entry; SIGTERM
into `banks/curve` landed in `curve_0004` at step 1022, the four rows before it scored, exit 0,
no traceback, `stopped: true`. What the step settled beyond the bullets:

1. **The stop is a flag, and the flag is the loop's only contact with the outside.**
   `stop_on_signals()` installs SIGTERM/SIGINT handlers that set it, for the length of the batch
   *including* `env.close()`, and puts the old handlers back after. `run_episode(stop=...)` reads
   it before each step, so a stop lands between two steps and never inside one. A test really
   sends SIGTERM to the pytest process; another checks Ctrl-C becomes a flag rather than a
   `KeyboardInterrupt`. Off the main thread — where Python refuses handlers — the flag comes
   back unarmed and the caller's own `stop` is the way to end a batch, which is how every test
   ends one.
2. **Three things the loop had to start returning**, additively, with defaults so Step 2's tests
   held: `reward` and `cost` summed (the env's `cost` is written only by `MetaDriveEnv`, on an
   out-of-road or a crash; a recording reads 0), and `issued_actions`, one per decision, which is
   what `actions_digest` hashes and `--save-trajectories` writes. `stride_for` moved here from
   `replay` (re-exported, so Step 2's tests import it unchanged).
3. **A `sleep 4` is too early for `banks/curve`.** The five rows run at about 0.8 s each after a
   ~2 s import, so the kill landed in the fifth row at 5 s and the block above says so. On a
   bank that finishes before the signal arrives there is nothing to interrupt, and the test of
   the mechanism is the offline one, not the timing.
4. **An entry whose env will not build is one error row per scenario, and the next entry runs.**
   "Never abort" as written was per episode; a `build_env` failure is per entry and would have
   been an abort, so it is caught at that level too, with the traceback in every row. *(Since
   Step 5 the env is built per row, and a build that fails is that row's error row.)*
5. **Every refusal comes before the simulator**: the wrong bank at the path (by id), an unknown
   scenario id (by name, all of them), a policy that will not load (which part of the spec), a
   level or raw value the resolver refuses, a decision rate the env cannot step at, and a bank
   whose entries step at two rates (one run holds one stride). A test asserts no env was built
   after any of them.
6. **`--categories` and `--scenarios` intersect at the CLI**, and a `Job` carries ids only. The
   CLI reads the manifest anyway (for the bank id), so expanding categories to ids there keeps
   the payload one list — what the queue, the studio and the container all hand over.
7. **`ConstantPolicy` defaults to `(0, 0)`**, the same idle action `replay` drives with, so the
   stop test's rows run their full 1200 steps rather than leaving the road in a second. Step 4
   chooses the floor's action; the class takes one.

**Verify alone:** met — every command above, `test_results.py` 41, `test_policies.py` 10,
`test_runner.py` 11; full suite 663 passed, ruff clean, `commands.md` regenerated with `run` in
a group of its own.

### Step 4 — the reference policies: floor and ceiling ✅

- **Diagnostic policies keep the old signature.** `load_policy("pkg.mod:Name")` still instantiates
  and checks callability, and `ConstantPolicy` / `ExpertPolicy` still take
  `(observation: np.ndarray) -> Sequence[float]`. They are CLI-only floor and ceiling checks,
  never the thing under evaluation.
- Two are shipped: `ConstantPolicy` (a fixed action) and `ExpertPolicy`, a wrapper around
  MetaDrive's bundled PPO expert (`metadrive/examples/ppo_expert/`). Per the
  **No lidar** section, it holds the env and ignores its `observation` argument.
- **`ExpertPolicy` calls `expert(vehicle, deterministic=True)`.** The default is
  `deterministic=False` (`numpy_expert.py:39`), and on that path the action is
  `np.random.normal(mean, std)` from the **global** numpy RNG (`:75`) — nothing MetaDrive seeds.
  Left at the default, Step 5's reproducibility diff is non-empty by construction, and the error it
  produces there looks like a runner bug. So "two expert runs diff empty" is this step's own check.
- **`ExpertPolicy` sees a left-side bank in a mirror.** *(added 2026-09-08)* The expert was trained
  on MetaDrive's right-side roads; measured, as shipped it arrives on nine of nine unmirrored
  roads of this bank and on none of the nine mirrored ones, leaving the road inside 24 steps. The
  mirror is exact (**Handedness**), so a left-side road *is* the road it was trained on, seen in a
  mirror: on a left-side process (`handedness.drive_side()`, read when the policy is bound) it
  takes the expert's own 275-wide observation, reflects it with `mirror_expert_observation` --
  every entry computed in the vehicle's frame reads `1 - x`, every entry read off a lane frame
  stays, the 240 lidar points reverse about the first -- runs the same network on it, and
  negates the steering. Which entries reflect was *measured*, not derived: the same row driven
  on both maps with mirrored actions tracks to the millimetre, and each entry classifies as
  equal or flipped. On straight lanes every one did at once; on arcs the lane frame's lateral
  axis came out inverted, which became Step 4a. With 4a in, every entry classifies the same on
  arcs, and the mirrored expert arrives with the original's step counts.
- **The policy protocol gains one optional half: `bind(env)`.** A policy with that method is
  handed each env the batch builds, before that env's rows run. It is how the expert reaches the
  agent (`env.agent`, read at every call, since the agent is respawned at every reset) and how
  Step 7's policy will reach the camera rig; the loop itself still passes only the observation.
- The runner records the observation shape *after* the last expert episode and **fails the run if
  it moved**: `numpy_expert.py:49` admits its config restore is incomplete. The check is *"the
  shape did not move during this run"*, **not** a literal 19: measured (Phase 3 Step 6), the same
  `agent_observation` on a stored scenario observes **31**, because `ScenarioEnv` gives the ego a
  `TrajectoryNavigation` reporting 22 scalars where `NodeNetworkNavigation` reports 10. Both
  numbers are in `config.py`; neither is written anywhere else.

**Verify alone:** *(old tests 1, 2 and 2b, plus determinism)*

```bash
uv run pytest tests/unit/test_policies.py tests/unit/test_results.py -q   # the protocol, the mirror's arithmetic, bind, the shape check; two live tests
B=banks/t-junction-left-intersection
# 1. Floor: a constant-action policy should mostly fail
uv run scenariobank run --bank $B --categories intersection_left --policy scenariobank.policies:ConstantPolicy --out floor
# 2. Ceiling: the bundled PPO expert should mostly pass -- twice
uv run scenariobank run --bank $B --categories intersection_left --policy scenariobank.policies:ExpertPolicy --out ceiling
uv run scenariobank run --bank $B --categories intersection_left --policy scenariobank.policies:ExpertPolicy --out ceiling2
jq -s '[.[0].summary.success_rate, .[1].summary.success_rate]' floor/results.json ceiling/results.json
jq -c '.summary.by_failure_reason' floor/results.json ceiling/results.json
# 2b. The expert must not leak lidar back into the env config
jq -c '[.env.observation_shape_before, .env.observation_shape_after]' ceiling/results.json
diff <(jq 'del(.started_utc, .finished_utc) | del(.results[].wall_time_s)' ceiling/results.json) \
     <(jq 'del(.started_utc, .finished_utc) | del(.results[].wall_time_s)' ceiling2/results.json)
```
**Expect:** floor near 0 with `by_failure_reason` dominated by `out_of_road` / `max_step`; ceiling
substantially above it — if floor is approximately ceiling, the runner is not actually feeding
actions to the env, which is the bug this test exists to catch; both shapes `[19]` — a fact about
`MetaDriveEnv` with our `agent_observation`, and the assertion is that it did not move; **the two
expert runs diff empty**, or `ExpertPolicy` is not passing `deterministic=True`.

**Built 2026-09-08** as `ExpertPolicy`, `bundled_expert`, `expert_weights`, `expert_forward` and
`mirror_expert_observation` in `policies.py` (+180 lines), `bind_policy` and the shape check in
`runner.py`, seven tests in `test_policies.py` (two live) and two in `test_results.py`. What
was learned:

1. **The numpy expert by name, not `metadrive.examples.ppo_expert.expert`.** That package picks
   the torch expert whenever torch is importable, which the rig has and this machine does not,
   and a ceiling that is one arithmetic here and another there is not one ceiling. Same weights
   file either way; only the numpy path is the same everywhere. A live test pins it.
2. **The expert as shipped scores 0 of 9 on this bank, and it is not the runner.** Floor and
   ceiling both read 0.0 on `intersection_left`, which is the "not feeding actions" signature
   this step exists to catch -- except the same expert, on the same rows with
   `handedness.install` made a no-op, arrives on every one in 137-148 steps. The bank drives on
   the left and the expert learned the right. Hence the mirror, above.
3. **The mirror was derived, then measured, and the measurement won twice.** Derived: every
   lateral number flips. Measured (the same row, both maps, mirrored scripted actions, 60 steps,
   positions mirrored to 1.5e-4 m): the border distances and the in-lane offset are *equal*,
   not flipped -- they are read off the lane frame, which `handedness` mirrors -- and only the
   vehicle-frame entries flip. Corrected. Measured again through a curve (`banks/curve`, 200
   steps mirrored to 5 mm, 140 of them on arcs): on arcs the in-lane offset *flips* and the
   border distances match neither way. That became Step 4a; the wrapper did not paper over it.
4. **The shape check is the batch's, after the write.** `run_bank` writes `results.json` with
   both shapes and then raises `RunError` naming them, so the evidence is on disk and the exit
   code says the run is not to be trusted. Measured `[19]` / `[19]` on every expert run here.
5. **One placeholder in the observation is not a lateral.** Before the navigation has updated once,
   its two checkpoint blocks are zeros, which the expert's own `obs_correction` turns into
   `(0, 1, 0, 0, 0)`. The mirror leaves a block that reads exactly that alone; flipping it would
   hand the mirrored expert a first step the original never sees.

**Verify alone: met, once Step 4a was in.** Before it: `test_policies.py` 17 and
`test_results.py` 43 passed, the two expert runs diffed empty, both shapes `[19]`, floor 0.0 with
`max_step` x5 -- and the ceiling read 0.0 too, `crash_sidewalk` x5, all in the turn, an arc. The
right-hand control run was what said the runner is feeding actions. After 4a, the same block:
floor 0.0, **ceiling 1.0**, the five rows in 139, 138, 138, 137, 137 steps -- the right-hand
control run's counts exactly -- diff empty, shapes `[19]`; the whole bank 9 of 9. Full suite 672
passed, ruff clean, `commands.md` regenerated.

### Step 4a — the mirror is exact for lateral coordinates too ✅  ⟵ *found and fixed 2026-09-08*

**Handedness** says the mirror is exact "lane by lane", and its test samples every lane's
centreline (`lane.position(s, 0)`) and length. Both hold. What does not hold is the in-lane
lateral coordinate on arcs, and it was found by driving the same row on both maps with mirrored
actions and comparing the expert's observation entry by entry (Step 4, note 3):

- On a **straight** lane, `lane.local_coordinates(p)` on the mirrored map returns the same
  lateral as on the original -- the true mirror, because change 1 negates `direction_lateral`.
- On an **arc**, it returns the *negated* lateral. `CircularLane.position(lon, lat)` is
  `center + (radius + lat * direction) * (cos phi, sin phi)` (`circular_lane.py:57-61`), and
  change 2 inverts `clockwise`, hence `direction`, to sweep the other way -- which also inverts
  the sign of the lateral term. The centreline mirrors exactly; the lane's lateral axis does not.

Three things read that axis, so three things are wrong on every arc of a left-side map, and none
of them shows in a picture:

1. **The 19-wide observation** the bank is defined against: entries 0-1 (distance to the left
   and right road border, `base_vehicle.py:527-536`) and 8 (offset in the lane) are sign-consistent
   with a right-side road on straights and inverted on arcs. A submitted policy sees a road whose
   lateral sense changes at every bend.
2. **Navigation checkpoints** are placed at `ref_lane.position(length, later_middle)`
   (`node_network_navigation.py:303-304`); on an arc `later_middle` lands on the wrong side of
   the centreline, so the checkpoint the ego steers toward sits half a road off (measured: the
   second checkpoint's projections differ by up to 0.07 normalised while the next lane is an arc).
3. **Out-of-road bounds** use the same two border distances; on a multi-lane arc the asymmetric
   range (`get_current_lateral_range`) is applied from the wrong edge.

The lane *surfaces* are unaffected -- a lane polygon samples both edges -- which is why a
mirrored drive tracked to the millimetre through 140 arc steps while staying in lane. **The
sidewalks are not.** A sidewalk is built off one edge of the outermost lane
(`pg_block.py:303-323`), so on every arc it sits on the wrong side: measured before the fix,
4 of 12 sidewalk polygons on `CC`, 4 of 14 on `X` and 2 of 11 on `T` were not mirrors of the
original's. That is the `crash_sidewalk` the ceiling was reading in every turn, and it is a
fourth consequence: the physical road was wrong on arcs, not only the numbers about it. Lane
lines, built the same way, were on the wrong side of every arc too.

**The fix belongs in `handedness.py`, not in any consumer:** make the mirrored `CircularLane`
carry a true-mirror lateral axis -- negate the lateral in `position` and `local_coordinates`
for the mirrored class, leaving `heading_theta_at` and the sweep as change 2 made them. Extend
`test_the_mirror_is_an_exact_reflection_lane_by_lane` to sample both lane edges, ask each lane
for the coordinates of a reflected probe point, and compare every sidewalk polygon, so the
exactness claim covers the whole map and not only the line down the middle of it. Route lengths
and fingerprints are centreline facts and must not move; `test_handedness.py`,
`test_categories.py` and the bank's stored `route_length_m` are the regression.

**Built 2026-09-08** as ten lines in `_mirror_circular_lanes` (two wrappers), the Handedness
docstring's change 2 rewritten, and the exactness test extended. What was learned:

1. **Nothing else had to be re-derived.** The expectation was that change 3 and
   `_mirrored_create_bend_straight` compensated for the inverted axis and would move with it.
   Read, neither touches it: sibling and opposite arcs are built from explicit radii and
   phases, never from `position(lon, lat != 0)`, and a bend's `previous_lane` is always the
   straight that follows the last bend. The only construction that reads an arc's lateral is
   the sidewalk, which was wrong and is now right.
2. **Measured, whole map, before and after.** Before: every arc's `+w/2` edge was the mirror of
   the original's `-w/2` edge (12 of 12 on `CC`, 24 of 24 on `X`, 12 of 12 on `T`), and 10 of
   37 sidewalk polygons were not mirrors. After: every edge and every polygon is, and every
   centreline and length is unchanged.
3. **Measured from the driver's seat, after.** The same `banks/curve` row, both maps, mirrored
   actions: 199 steps to 5 mm, 120 on arcs, and all nineteen state entries classify the same on
   arcs as on straights. Both drives now end the same way at the same step.
4. **Fingerprints and thumbnails are centreline facts** (`fingerprint.lane_geometry_digest`
   and `figures.render_route` both sample `position(s, 0)`), so no bank id moved, no stored
   picture is stale, and no bank needs rebuilding. What changed for an existing bank is the
   road a run drives on: arc sidewalks and lane lines are now where the mirror puts them.

**Verify alone: met.** `test_handedness.py` and `test_categories.py` 87 passed; `test_policies.py`
live tests pass; Step 4's ceiling block reads 1.0 with the right-hand control's step counts;
full suite 672 passed, ruff clean.

**Verify alone:**

```bash
uv run pytest tests/unit/test_handedness.py tests/unit/test_categories.py -q
# the observation probe from Step 4, note 3: every entry EQUAL or FLIP on arcs as on straights
uv run pytest tests/unit/test_policies.py -q
# then Step 4's ceiling block, unchanged
B=banks/t-junction-left-intersection
uv run scenariobank run --bank $B --categories intersection_left --policy scenariobank.policies:ExpertPolicy --out ceiling
jq -c '[.summary.success_rate, .summary.by_failure_reason, [.results[].steps]]' ceiling/results.json
```
**Expect:** the mirrored expert arrives on `intersection_left` the way the unmirrored one does
(137-139 steps per row on the right-hand control run), and Step 4's "ceiling substantially above
floor" is met without touching `policies.py`. *Measured: 139, 138, 138, 137, 137 -- identical.*

### Step 4b — the option managers: `obstacles.py` and `actors.py` ✅  ⟵ *built 2026-09-08*

*(Added 2026-09-07. Both modules are specified in full under **New modules**; the Target layout
lists them; Step 2 referred to `VRUManager` as if it existed; and Step 5's `--tier hard` cannot
place anything without them. No step owned them. This one does, sitting after the policies
because Steps 3-4 do not need a manager and before the gate because the gate cannot pass without
one.)*

- **`obstacles.py`** — the `TrafficObjectManager` subclass from **New modules**: override
  `reset()` to choose `prohibit_scene` (cones) or `barrier_scene` (barriers) per axis, placement
  maths reused whole, registered only when the axis is above `none` the way
  `metadrive_env.py:296-300` already registers the stock one. Carry in the two facts: the
  breakdown scene spawns a vehicle, and nothing is placed on `X`, `T` or `O` blocks
  (`object_manager.py:51-53`).
- **`actors.py`** — `VRUManager`: spawns in `reset()` from `self.np_random`, patrols between two
  fixed endpoints via `set_velocity` in `after_step()`, consumes no randomness after reset. Wire
  `crash_human_penalty` / `crash_human_cost` mirroring `crash_object`'s 5.0 / 1.0
  (`metadrive_env.py:75`, `:83`) — moved here from Step 2, since nothing crashes into a human
  before this step. `grep -rn crash_human src/` returns nothing today.
- **`tests/unit/test_invariance.py`** — Phase 2b's two tests, unwritten since 2026-09-01
  ("neither test is written"), and required green by Phase 4b and Phase 8. Their stated job is
  guarding exactly these two managers, so they ship with them: `test_option_levels_do_not_move_
  the_map_or_route` and `test_random_traffic_breaks_invariance`, as Phase 2b specifies.
- **The actor determinism test** from **New modules** ships here too, and each result carries the
  actor-layout digest.
- **`placed`** — a per-result count of placed objects by class, read off `engine.get_objects()`
  after reset. It exists so that "the manager is registered but placing nothing" is visible in the
  result instead of inferred from a success rate.

**Verify alone:**

```bash
uv run pytest tests/unit/test_invariance.py tests/unit/test_obstacles.py tests/unit/test_actors.py -q
uv run scenariobank run --bank banks/curve --cones high --policy scenariobank.policies:ConstantPolicy --out cones
uv run scenariobank run --bank banks/t-junction-left-intersection --categories intersection_left \
  --cones high --policy scenariobank.policies:ConstantPolicy --out cones-x
jq -c '.results[0].placed' out/cones/results.json out/cones-x/results.json
```
**Expect:** the invariance test green — `lane_geometry_digest` and `navigation.checkpoints`
identical across levels, `assert_array_equal` not `allclose`; `test_random_traffic_breaks_
invariance` fails the same comparison, as it must, or a green run proved nothing; two envs at one
seed and level place identical actor spawns and patrol endpoints; `placed` shows cones on `curve`
(a `CC` road) and **zero** on `intersection_left` (an `X`) — the documented trap, as a test rather
than a footnote.

**Built 2026-09-08.** Seven things the spec above did not say, all measured:

1. **The env grew a subclass, made inside a function.** MetaDrive's `Config` refuses a
   constructor key it was not told about in `default_config` (`base_env.py:293`), and managers
   are registered in `setup_engine`, which only a subclass can extend. `env.procedural_env_class()`
   is that subclass, cached: the four counts (`env.COUNT_AXES`) and the crash-human pair as config
   keys, each manager registered only when its axis is above zero, `reward_function` /
   `cost_function` slotting `crash_human` in behind `crash_object` at 5.0 / 1.0. It is built
   inside a function because its base is the simulator's, so `env.py` stays importable without
   it. `build_config` writes the four counts; `build_env` builds the subclass. `bank`,
   `variety` and `destinations` still build a stock `MetaDriveEnv` off `base_config`, which is
   why the counts are not in `base_config`.
2. **The push goes to the physics body, not through `set_velocity`.** Any write to an actor's
   transform — `set_heading_theta`, `standup`, and so the stock `set_velocity`, which calls
   `standup` — costs the next physics substep: an actor pushed that way every step moves
   `(decision_repeat - 1) / decision_repeat` of its speed, 0.096 m per step at 1.2 m/s and
   MetaDrive's five substeps, and **nothing at all at `decision_repeat = 1`**. Writing the body's
   linear velocity directly costs nothing. So `actors._push` does that and rewrites the heading
   only when it has turned by more than 0.1 rad. Pedestrians then walk 0.120 m per step exactly;
   a cyclist on an arc rides 0.394 of its 0.400, the rewrite every fourteen steps. *(Since
   Step 5c the push is skipped while a vehicle touches the actor, and every actor carries the
   lane it is on; see there for why.)*
3. **Cyclists drawn onto one lane get disjoint stretches of it.** Four cyclists on `curve`, three
   of them on one 106 m arc, rode head-on into each other and spent the episode stalled (0.14 to
   0.32 m per step of 0.40, with steps of zero). `actors._stretches` splits a shared lane's run
   into equal shares 3 m apart, *after* every cyclist's draws, so the split cannot move a later
   draw. A layout collision is the layout's fault, not the ego's.
4. **The layout is drawn off the map, not the route.** The route is pinned *after* the reset
   (`env._prepare_procedural`, `navigation.set_route`), so at `reset()` `navigation.checkpoints`
   is not yet the row's. Candidates are every positive road of every block after the first — the
   first is the ego's — and a road is picked per actor from `self.np_random`.
5. **No broken-down car, ever.** The stock barrier branch spawns a vehicle half the time;
   `ObstacleManager` uses `prohibit_scene` and `barrier_scene` only. Measured: `curve` at
   `traffic=none, cones=medium, barriers=medium` places 36 cones, 2 barriers and one
   `DefaultVehicle`. The first fact in the bullet above is therefore a fact about stock
   MetaDrive, not about this bank.
6. **The results schema stays at 1.** `ScenarioResult` gains `placed` and `actor_layout_digest`;
   the bump rule in `results.py` is for a record somebody reads, and until Phase 6 hands it over
   the only readers are this repo's tests.
7. **The `X` trap, in numbers.** `cones-x` at `--cones high`: `placed` is `{"DefaultVehicle": 1}`
   with the manager registered and `scenes == []`. `cones` on `curve`: 72 cones — six corridors
   of twelve at a 3.5 m lane — beside the manifest's pinned `barriers=medium` (4), `pedestrians=low`
   (1) and traffic. And a first look past the gate: the expert at `--tier hard` on
   `intersection_left` arrives 5/5 in 176-247 steps against 137-139 at easy, every row carrying
   its own actor-layout digest.

**Verify alone: met.** 16 tests across the three files, green in 7 s; `placed` as in note 7; the
invariance ladder (`none`, traffic alone, everything `high`) leaves the map digest and the
checkpoints identical on both banks while `placed` moves; `random_traffic=True` gives two
different traffic layouts at one seed where `False` gives one, on both banks. Full suite 691
passed, ruff clean.

### Step 5 — reproducibility, and options that do something ✅  ⟵ *gate, met 2026-09-09*

Nothing after this step is worth debugging until this step passes, which is why the AV3 port
starts on the other side of it.

Two banks, not one, because `intersection_left` is an `X` block and the obstacle axes place
nothing there (Step 4b): on that bank "hard is lower than easy" rests on traffic and the actors
alone. `banks/curve` is where cones and barriers bite.

**Verify alone:** *(old tests 3 and 6)*

```bash
B=banks/t-junction-left-intersection; P=scenariobank.policies:ExpertPolicy
# 3. Reproducibility -- the real acceptance test, at the hard tier so it covers the managers and not just the map
for t in easy hard; do for i in 1 2; do
  uv run scenariobank run --bank $B --categories intersection_left --tier $t --policy $P --out $t$i
done; done
diff <(jq 'del(.started_utc, .finished_utc) | del(.results[].wall_time_s)' out/hard1/hard/results.json) \
     <(jq 'del(.started_utc, .finished_utc) | del(.results[].wall_time_s)' out/hard2/hard/results.json)
# 3b. The same, on the bank where cones and barriers are placed -- the one that failed first
for t in easy hard; do for i in 1 2; do
  uv run scenariobank run --bank banks/curve --tier $t --policy $P --out c-$t$i
done; done
diff <(jq 'del(.started_utc, .finished_utc) | del(.results[].wall_time_s)' out/c-hard1/hard/results.json) \
     <(jq 'del(.started_utc, .finished_utc) | del(.results[].wall_time_s)' out/c-hard2/hard/results.json)
# 3c. Another process with another environment block, and a job naming one row
PYTHONHASHSEED=8 uv run scenariobank run --bank banks/curve --tier hard --policy $P --out c-hard-h8
diff <(jq 'del(.started_utc, .finished_utc) | del(.results[].wall_time_s)' out/c-hard1/hard/results.json) \
     <(jq 'del(.started_utc, .finished_utc) | del(.results[].wall_time_s)' out/c-hard-h8/hard/results.json)
uv run scenariobank run --bank banks/curve --tier hard --policy $P --scenarios curve_0003 --out c-0003
jq -c '.results[] | select(.scenario_id=="curve_0003") | [.steps,.reward,.actions_digest]' \
  out/c-0003/hard/results.json out/c-hard1/hard/results.json
# 6. Options actually do something
jq -s '[.[0].summary.success_rate, .[1].summary.success_rate]' out/easy1/easy/results.json out/hard1/hard/results.json
jq -s '[.[0].summary.success_rate, .[1].summary.success_rate]' out/c-easy1/easy/results.json out/c-hard1/hard/results.json
jq -c '[.results[].steps]' out/easy1/easy/results.json out/hard1/hard/results.json
jq '.results[0].placed' out/c-hard1/hard/results.json out/hard1/hard/results.json
```
**Expect: every diff empty** — identical steps, reward, cost, `failure_reason` and `actions_digest`
for every scenario, with the option managers on, in one process or two, and the one-row job's
row identical to the same row of the whole bank. Hard below easy on `curve`; on
`intersection_left` the expert arrives at both tiers and hard is slower. `placed` on `curve`'s
hard run shows cones and barriers; on `intersection_left`'s it shows only actors. If the rates
match on `curve`, a manager is registered but placing nothing — the same class of bug as floor
equals ceiling, and `placed` says which one.

**Built 2026-09-09.** The gate failed the first time it was run, on `curve` at `hard`, and it
failed twice over. Everything below was measured; the block above is the block as amended by it
(the original diffed `intersection_left` only, which never failed).

1. **Two runs of one job disagreed, on two of five rows.** `intersection_left` was identical at
   both tiers. `curve` at `hard`: rows 0-2 identical, row 3 at 300 steps in one process and 296
   in another, row 4 at a reward of 73.474806 against 73.546124 — and the same process gave the
   same answer every time. Two carriers, found separately.
2. **A row's score depended on which rows ran before it in the same env.** `curve_0003` alone:
   339 steps. After `curve_0002`: 218. After rows 0-2: 300 or 296. The actor layout digest was
   the same every time and the expert's actions parted at step two (steering 0.06035200 against
   0.06035198), so this is physics state carried across `reset`, not a layout or a draw. The
   object pool is not it: `force_destroy=True` moved every number (row 3 to 470 after row 2, 389
   in the sequence) and kept the dependence. What carries it was not found. **The fix is one env
   per row**, built and closed around `run_episode`: every row then scores its alone value in
   any company, in any order, in any process, and a job naming a subset of the rows scores them
   exactly as the whole bank does — which Phase 7's "a job may name a subset of rows" requires.
   Cost: five `curve` rows at `hard` take 22.9 s against 19.8 s, under a second per row.
3. **A row alone, in a fresh process, still came out two ways: 339 steps or 348.** Sweeping
   `PYTHONHASHSEED` said what it was not: every value from 1 to 17 gave 348; `0`, `100`, `999999`
   and unset gave 339. Hash randomisation flips per seed; this flipped with the *length of the
   variable*, i.e. with the size of the process's environment block, i.e. with where the heap
   starts. Bisected across the axes: the effect needs the expert *and* traffic *and* cones or
   actors together; traffic alone, cones alone, actors alone and the constant policy were all
   identical. The cause is `Lidar.get_surrounding_objects` (`lidar.py:170`), which returns a
   `set` of objects — iterated by address — that `IDMPolicy.act` hands to
   `FrontBackObjects.get_find_front_back_objs` (`idm_policy.py:83`), where the nearest object
   ahead and behind per lane is kept with a strict comparison. A cone corridor puts cones at one
   longitude by construction, so which cone a traffic car "sees" is the heap layout. **The fix
   is `env.pinned_lidar_class()`**: the stock lidar with both object sets returned as lists in
   `env.object_order` (class, position, heading), registered through the `sensors` config in
   `build_env` on both env kinds, so the IDM policy and the expert's own observation see one
   order. With it, `PYTHONHASHSEED=8` and unset agree on every row.
4. **"Hard below easy on both banks" is false for the expert on the `X` bank, and the block
   said so before it was run.** `intersection_left`: 5/5 arrived at both tiers, steps 133-160
   at easy against 168-247 at hard, `placed` showing three pedestrians, one cyclist and the
   traffic and no cone. `curve`: 0.8 at easy, 0.0 at hard, with 36 cones, 2 barriers, 3
   pedestrians, 1 cyclist and 25-40 traffic vehicles per row, four rows ending in
   `crash_vehicle` and one in `crash_human`. The options do something; on an `X` what they do
   is slow the expert down.
5. **`tests/unit/test_reproducibility.py`** keeps the two measurements as tests: the sort key
   offline; the lidar every env gets is the pinned one and returns sorted lists; `curve_0003`
   scores the same alone, after `curve_0002`, and in a subprocess with `PYTHONHASHSEED=8`; and
   `hard` below `easy` on `curve_0004` with cones in `placed`, slower on `intersection_left_0000`
   with none. `test_results.py`'s two per-entry assertions became per-row ones.

**Verify alone: met.** All five diffs in the block empty (`intersection_left` at both tiers,
`curve` at both tiers, `curve` at `hard` under another environment block); the one-row job's
`curve_0003` identical to the bank run's; rates `[1.0, 1.0]` on the `X` bank with the step
counts apart, `[0.8, 0.0]` on `curve`; `placed` as in note 4. The four gate tests green in 37 s;
full suite 695 passed, ruff clean, `commands` regenerates without a change. *(Step 5c changed
the traffic and the actors and re-ran this block; its numbers are the current ones.)*

### Step 5b — a top-down film of a run, for the eye ✅  ⟵ *added and built 2026-09-09*

*(Added 2026-09-09, at Keith's ask: "mainly just for a visual test". Step 5 proved two runs
of one job agree to the digest; nothing in the plan let a person look at one. Step 6's cameras
feed the AV3 model on a GPU inside the sim container and write nothing to disk; the studio
authors banks and never runs one; Phase 2c Step 12 will only enqueue. This is the missing
thing, and it is small: a switch that films a run, off by default, invisible to the score.)*

**What it is.** `scenariobank run --record-video` writes `<out>/videos/<scenario_id>.mp4` per
row; `scenariobank replay --record-video PATH` writes one file for one drive. The film is
MetaDrive's own top-down view, 800x800 at 5 px/m — a 160 m window that follows the ego, north
up — at the step rate, so a 10 Hz road plays in real time. It runs **locally, under `uv run`,
with no GPU, no display and no container**: the renderer is pygame on the CPU, the same one the
thumbnails already use. Five facts it rests on, all read:

- **The live top-down renderer draws every non-map object** — `TopDownRenderer.render`
  (`engine/top_down_renderer.py:343`) collects `engine.get_objects()` minus the map every frame
  and draws each with its own `top_down_width/length/color`: vehicles, `TrafficCone`,
  `TrafficBarrier`, `Pedestrian`, `Cyclist`. The "map only" note under **No lidar** is about
  `draw_top_down_map`, the thumbnail path; it does not apply here.
- **`window=False` is headless** (`top_down_renderer.py:265`); the only `pygame.init()` sits
  behind `show_agent_name`.
- **`env.render(mode="topdown", **kwargs)`** builds the renderer lazily with those kwargs and
  hands back an RGB array (`to_cv2_image` swaps nothing), so `video.frame` swaps to BGR once.
  `env.reset` clears the renderer (`base_env.py:536-538`); one env per row means one renderer
  per row anyway.
- **The camera follows the ego** when `camera_position` is unset (`top_down_renderer.py:537`).
- **OpenCV is already here**: `metadrive-simulator` requires `opencv-python`, and
  `doctor.COMPANION_PACKAGES` lists it. `cv2.VideoWriter` with `mp4v`. Nothing new in
  `pyproject.toml`; `cv2` is imported inside `Recorder`, so `video.py` imports without it.

**Design.** `runner.run_episode` gains an `observe(env)` hook, asked once after the reset and
prepare (the placed scene, before anything moves) and once after every step, the ending step
included, so a film has `steps + 1` frames and ends on the crash. `run_bank(record_video=True)`
points it at `video.Recorder`, opened before the row and closed in the row's `finally` beside
`env.close()`. **A switch on the run, not a field of the `Job`**: the schema stays, a queue
job cannot ask for it, and the CLI flag is per run — the same shape as `stop` and `progress`.
`replay.drive(record_video=PATH)` does the same for its one drive.

**What it is not.** Not the studio — listing a run's films on the page would be a Phase 2c
step of its own. Not the AV3 cameras — this is a bird's-eye diagram, the right view for
checking that the managers placed what they should and that the actors move. Not a 3D window —
`use_render=True` needs a display and was not asked for. No resolution or decimation flags: one
fixed view until someone needs another.

**Verify alone:**

```bash
P=scenariobank.policies:ExpertPolicy
uv run scenariobank run --bank banks/curve --tier hard --policy $P --out film --record-video
ls -la out/film/hard/videos/                                   # five mp4s, one per row
uv run python -c "import cv2; c=cv2.VideoCapture('out/film/hard/videos/curve_0003.mp4'); \
  print(int(c.get(cv2.CAP_PROP_FRAME_COUNT)), c.get(cv2.CAP_PROP_FPS), int(c.get(3)), int(c.get(4)))"
uv run scenariobank run --bank banks/curve --tier hard --policy $P --out no-film
diff <(jq 'del(.started_utc, .finished_utc) | del(.results[].wall_time_s)' out/film/hard/results.json) \
     <(jq 'del(.started_utc, .finished_utc) | del(.results[].wall_time_s)' out/no-film/hard/results.json)
uv run scenariobank replay --bank banks/t-junction --scenario t_junction_0000 --steps 50 --record-video t.mp4
xdg-open out/film/hard/videos/curve_0003.mp4
uv run pytest tests/unit/test_video.py -q && uv run ruff check src tests && uv run scenariobank commands
```
**Expect:** five files; `curve_0003` has 340 frames at 10 fps, 800x800; **the diff empty** —
the film changed nothing; `t.mp4` has 51 frames; the film shows the corridor of cones, both
barriers, the three pedestrians crossing, the cyclist on the outer lane, the traffic, and ends
on the `crash_vehicle` frame.

**Built 2026-09-09.** Measured on `banks/curve` at `hard`, five rows, 1,211 frames: the run
took 46.8 s filmed against 21.8 s not, so about 20 ms a frame (15 ms to draw, the rest to
encode) — 2 s on the 95-step row, 10 s on the 473-step one. The first frame of a row costs
0.14 s more, the 4000x4000 film of the map drawn once. Files: 0.7 MB for 96 frames to 3.8 MB
for 474. The diff between the filmed and the unfilmed run is empty, and `test_video.py` holds
it as a test on `curve_0004`, `actions_digest` included, beside `steps + 1` frames at the step
rate. One frame, read back off `curve_0003`'s film: the ego centred with its trail, the cone
corridor as a dotted line on the outer lane, a pedestrian beside it, the traffic ahead and
behind. `commands` regenerated with the two new flags.

**Verify alone: met.** Five films, 474/197/104/340/96 frames at 10 fps; the diff empty; the
replay film 51 frames; six tests green; full suite green, ruff clean.

### Step 5c — the traffic drives properly on the mirrored map ✅  ⟵ *found by the film, 2026-09-09*

*(Added 2026-09-09. Keith looked at `curve_0000.mp4` and asked why the other vehicles were
"just driving randomly and not following the lanes". They were: cars sitting inside the bend,
off the road, at odd headings. Not normal, and three causes, found one under the other. Each
was measured on `curve_0000` at `traffic=high` with the expert driving, against stock MetaDrive
on its own unmirrored map in a subprocess as the definition of normal: there, no car leaves the
road, none mounts the sidewalk, 4.0 % of vehicle-steps are more than a metre off the lane
centre, and eleven cars of forty collide in 200 steps -- IDM has no give-way rule.)*

1. **The mirror inverted the traffic's lane-keeping loop.** `IDMPolicy.steering_control`
   (`policy/idm_policy.py:294-302`) feeds `-lat` to its lateral PID; after the mirror a
   positive lateral means the vehicle's left, and the steering is not mirrored, so an offset
   from the centreline was amplified. The heading loop is far stronger, so a car holding its
   heading looked fine -- 0.02 m off centre for 300 steps behind an *idle* ego, which is why
   the first check of exactly this hypothesis came back clean and was wrong to -- and every car
   that changed lanes or was nudged drifted to the kerb: eight of thirty-five mounted the
   sidewalk in 146 steps, 13 % of vehicle-steps more than a metre off centre. **Fix: change 4
   in `handedness.py`**, `lat` negated back by rewriting the method from its own source the way
   change 3 is, with the same loud `HandednessError` if MetaDrive moves. After: no sidewalk,
   2.4 % off centre. `TrajectoryIDMPolicy` keeps its own copy and is not touched.
2. **The traffic drove blind whenever it could see one of our actors.** `IDMPolicy.act`
   (`:236-260`) reads `obj.lane` off every object its lidar sees, inside a bare `except` whose
   fallback is *no front object*. MetaDrive's participants have no `lane` attribute. So with
   actors alone on the row, thirty-one of forty cars carried `crash_vehicle`; with obstacles
   alone, none. **Fix: `VRUManager` sets `actor.lane`** to the lane of its road nearest the
   actor, at spawn and every step -- and the traffic then brakes for a person in its lane and
   changes lane around a cyclist, which is the behaviour the axis wanted anyway.
3. **A walking actor was an unstoppable object.** `_push` wrote the actor's velocity every step
   regardless of contact, so a 70 kg body against a car shoved it with an impulse it never had
   to earn; every car that left its lane with the actors alone carried `crash_human`. **Fix:
   the push is skipped while a vehicle touches the actor** (`contactTest` on its body; a
   vehicle's chassis node is named `MetaDriveType.VEHICLE`), and the names are kept in
   `struck`. Left struck for good instead, an actor lay in the lane and the expert sat behind
   it to the step cap, so it walks on once the car has passed.

With all three: on `curve_0000` at `hard`, no car off the road, no traffic crash, 0.5 % off
centre; with actors alone, eleven crashes in 222 steps -- stock's own number. Three tests hold
it: `test_handedness.py::test_idm_traffic_keeps_its_lane_on_the_mirrored_map` (no sidewalk,
under 7 % off centre, with the expert driving), and in `test_actors.py` the lane every actor
carries with no car off the road and at most two traffic crashes at `hard`, and the touched
actor not driven with no car more than half a lane off centre behind an idle ego.

**Verify alone:**

```bash
uv run pytest tests/unit/test_handedness.py tests/unit/test_actors.py -q
P=scenariobank.policies:ExpertPolicy
uv run scenariobank run --bank banks/curve --tier hard --policy $P --out fixed --record-video
xdg-open out/fixed/hard/videos/curve_0000.mp4          # traffic in its lanes through both bends
# then Step 5's block again, into fresh directories
```
**Expect:** both files green; the film shows the traffic queued in lane through the arcs; every
Step 5 diff still empty.

**Verify alone: met.** 36 tests green; every Step 5 diff empty, the one-row job identical, no
error row; rates `[1.0, 1.0]` on the `X` bank with steps 133-160 at easy against 169-222 at
hard, `[0.8, 0.0]` on `curve`; `curve_0004` at `hard` now runs to the 1200-step cap behind
traffic that brakes for people, which is what hard is. Full suite 703 passed, ruff clean.

### Step 6 — the camera rig: six cameras alive on `DefaultVehicle` ✅  ⟵ *built 2026-09-10*

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

- `tools/camera_rig.py` — `load_rig()`, `CameraRig.sensors/mount/read/image_source`
  → **`src/scenariobank/av3/camera_rig.py`**, every measurement intact, plus `check_frame`,
  `image_buffers` and the report models `scenariobank rig` prints
- `rigs/av3.txt` — the six AV3 cameras, ISO-8855 → CARLA sign rules applied, datum resolved onto
  MetaDrive's `DefaultVehicle` → **`rigs/av3.txt`**, byte-identical; `rigs/README.md` beside it
- `tools/av3_probe.py` + `scripts/av3-probe.sh` — the sign-convention probe → **the rig's half
  only**: `scenariobank rig --check-frame` (the converter's `camera_rig.check_frame`) and
  `scripts/av3-probe.sh`, which runs it and then `replay --camera-rig`. *(Scoped 2026-09-10:
  `av3_probe.py`'s four conversions -- camera order, ego state, route, waypoint sign -- are
  all computed by `av3_model.py` and three of the four are scored against a checkpoint, so
  they cannot be ported ahead of it. They come with Step 7, beside the model, and the script
  says so in its header.)*

`rigs/av3.txt`'s header records two open gaps, and they stay open: fisheye is rendered as an
unwarped pinhole, and 4:3 is rendered then squashed by preprocess, never native 16:9.

The camera rig is selected as a **path, not a registry entry**: `--camera-rig rigs/av3.txt`.

Six things that bite, in the order they will bite:

1. **`image_observation=True` is mandatory, and not for the observation.** `base_env.py:343-346`
   filters **every `BaseCamera` out of `config["sensors"]`** when `use_render` and
   `image_observation` are both false, to save render passes in headless mode. Leave it off and
   the six-camera rig is silently deleted — no error until `env.engine.get_sensor(camera.name)`
   raises inside `CameraRig.mount()`. It stays on purely to keep the cameras alive; the
   observation it would produce is overridden by `agent_observation` anyway (see **No lidar**).
   Set `vehicle_config["image_source"]` to a rig camera via `CameraRig.image_source()` while you
   are there: left at its `"rgb_camera"` default it registers a **seventh** 320x240 camera that
   nothing reads, renders it every step, and spends one of the nine buffers gotcha 6 is rationing.
   A rig requested on a run with `image_observation` off is a **refusal naming `base_env.py:343`**,
   not a `KeyError` from inside `mount()`.
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
6. **`MAX_IMAGE_BUFFERS = 9` is a hard cap** — the converter's constant (`tools/camera_rig.py:118`),
   not MetaDrive's; it does not appear anywhere in the pinned simulator, it was *measured*, and it
   ports with the rig. panda3d fails *intermittently* past it, so a rig one camera over the line
   looks like it works and then fails on a run somebody is relying on.

**Verify alone** — in the sim image, because the cupy gate is only known open there:

```bash
docker run --rm --gpus all -v $PWD:/work:ro metadrive-wingfin-sim:latest bash /work/scripts/av3-probe.sh
# the same two commands by hand, on the host:
uv run scenariobank rig --camera-rig rigs/av3.txt --check-frame --bank banks/curve
uv run scenariobank replay --bank banks/curve --camera-rig rigs/av3.txt --steps 20 --ignore-rig-rate --json \
  | python3 -c "import json,sys; e=json.load(sys.stdin)['env']; print(e['sensors'], e['image_buffers'])"
uv run pytest tests/unit/test_camera_rig.py -q
```
**Expect:** six named sensors and no `rgb_camera` among them; `image_buffers <= 9`; the probe
confirms every sign convention the rig rests on by measurement rather than by reading -- six
`ok` rows. No model, no bridge. `replay` gains `--camera-rig` here because it is the diagnostic
that already exists.

**Built 2026-09-10.** What was measured, and what moved from the notes above:

1. **The cameras are alive, on both machines.** `replay --camera-rig rigs/av3.txt` on
   `banks/curve`: sensors `front_left, front_middle, front_right, lane_line_detector, lidar,
   rear_left, rear_middle, rear_right, side_detector` -- the six, the three ray detectors, no
   `rgb_camera` -- `image_buffers` 6, `image_source front_middle`, every camera returning
   `(384, 512, 3)` uint8 at every one of 21 reads (20 steps plus the reset's), the observation
   `(19,)` at both ends. Host: **30 ms per read** of the whole rig, 38 ms/step. Sim container
   with the GPU: **5.5 ms per read**, 18 ms/step. The frame probe returns the same six rows in
   both, on the *mirrored* road: +y forward, +x right, H+55 left, H-55 right, P+10 up, P-10
   down, each to 0.01. The mirror is on lane geometry and a camera is parented to the vehicle's
   own node, so it never reaches the rig.
2. **What a rig costs is the offscreen window, not the read.** An env with `image_observation`
   on resets in **13 s** on the host against a quarter of a second without (the first build in a
   fresh shader cache took 70 s); the container is faster but the same shape. The live tests
   build exactly two rig envs for that reason; `test_camera_rig.py` runs in about 30 s.
3. **Gotcha 5 bites the AV3 rig on every procedural road, today.** `rigs/av3.txt` declares
   `tick_rate: 0.05` and a road steps at 10 Hz (`env.step_hz_for`, pinned by `test_env.py`), so
   the shortest read interval is 0.1 s and the loader refuses the spec exactly as it should --
   before anything is built, off the manifest. Nothing in this repo can step a PG road at 100 Hz
   yet: `--step-hz` exists for recordings only (`replay_config`, the rate the pickle was written
   at), and on a road it means `physics_world_step_size` / `decision_repeat`, which moves every
   `max_steps` budget and every Step 5 number. That is gotcha 3's `--step-hz 100 --decision-hz 20`
   and it lands with the model in **Step 7**. Until then `--ignore-rig-rate` loads the spec
   with the check deferred and reads the cameras at the road's rate: `replay` prints both rates
   with a `!`, and `run` records both in `env` *(Step 6b gave `run` the switch too, for
   filming; a policy that reads the rig refuses it, which Step 7 pins)*. *(Landed 2026-09-10:
   `--step-hz` on `run`, `replay` and `av3`, with every budget scaled by `env.budget_at`; the
   AV3 rig is read at its own 0.05 s and `AV3Policy` refuses the switch. See Step 7.)*
4. **Where the config is wired.** `env.build_env(..., rig=)` is the one place: the rig's
   cameras join `sensors`, `image_observation` goes on (gotcha 1 -- and `agent_observation`
   still wins, measured: 19 both ends), `vehicle_config["image_source"]` names the first rig
   camera (no seventh buffer), and the returned `prepare` mounts the rig after every reset, so
   no caller can forget. `CameraRig.mount` refuses an env built any other way by name --
   `base_env.py:343` -- before `get_sensor` could raise, and refuses an engine holding more than
   nine buffers. The parse refuses a spec of more than nine cameras. A rig on a recorded entry
   goes through the same three keys; not driven here, `banks/junction-1` being 100 Hz and the
   thing Step 7 wants.
5. **A rig changes nothing a run scores.** `test_camera_rig.py` drives `curve_0000` with the
   expert for 60 steps with the rig mounted and without: `issued_actions` identical, the same
   claim the film gate makes for `--record-video`. The cameras are read off the engine through
   `perceive()` and never through the observation, which is what **No lidar** decided.
   *(Not enough, it turned out: at the bank's pinned levels nothing is placed, and Step 6b's
   hard row parted from the plain one through MetaDrive's `preload_models`. `build_env` now
   turns that off with a rig, and the test that holds the claim runs at `hard`.)*
6. **The cupy gate is open by import and not yet by use.** In the container `base_camera`'s
   `try:` succeeds (cupy 14.2.0, PyOpenGL 3.1.10, cuda-python 12.9.7), and PyOpenGL then logs
   `Failed to load library ( 'libOpenGL.so.0' )` -- the import is lazy and the gate sees only
   the import. Nothing here sets `image_on_cuda`, so nothing here needs it; Step 7, which wants a
   frame that stays on the GPU, should measure `image_on_cuda=True` in the container before
   relying on the gate's word.
7. **A rig env poisons every subprocess started after it, and the seam undoes that.** Found
   by the suite, not by reading: `test_handedness.py`'s reference run -- stock MetaDrive in a
   child process -- died with `Fatal Python error: preconfig_init_utf8_mode: invalid PYTHONUTF8
   environment variable value` only when `test_camera_rig.py` had run first. `asset_loader.py:116`
   writes `os.environ["PYTHONUTF8"] = "on"` when the engine opens an offscreen or onscreen
   window (`engine_core.py:250-252`, so never on a headless run and never before a rig), and
   `on` is not a value CPython accepts at startup. It does nothing for the process that set it.
   `env._with_rig` records the variable before the env is built and puts it back in the
   post-reset `prepare`, where the engine comes to exist; the live test asserts the value and
   starts a child. This would have reached Step 7 as "the bridge subprocess dies after the
   first rig row" and cost a day. Recorded in **Traps**.
8. **Tests: 30 offline, 2 live**, in `test_camera_rig.py`: the spec's six cameras in the
   weights' order with the swap and the flip checked number by number, every camera aiming
   where its name says, fourteen refusals by name, the ceiling at nine, `mount`'s refusal
   naming `base_env.py:343`, the report round-tripping, the commands' refusals, and `replay`
   refusing the AV3 rig on a 10 Hz road before building anything. `docs.GROUPS` places `rig`
   under *Look before you commit*; `commands.md` regenerated.

**Verify alone: met.** `scripts/av3-probe.sh` green in the sim container with the GPU (41 s)
and on the host; six `ok` rows; six sensors, six buffers, no `rgb_camera`; `test_camera_rig.py`
32 passed; full suite 737 passed, ruff clean.

### Step 6b — a film from the car's cameras ✅  ⟵ *added and built 2026-09-10*

*(Added at Keith's ask, once Step 6 was built: "is it possible to record an actual drive from
the point of view of a camera? or at least i can see the pictures that are generated and string
them together into a video myself?" Step 5b films a run top-down; Step 6 reads the rig every
step and throws the frames away. This strings them together, and puts the rig on `run` so the
film is of the expert driving rather than of `replay`'s idle car.)*

**What it is.** `run --camera-rig rigs/av3.txt --record-video` writes, beside the top-down
`<out>/videos/<scenario_id>.mp4`, one mp4 per camera at the spec's size --
`<scenario_id>.<camera>.mp4`, six of them for AV3 -- and `<scenario_id>.rig.mp4`, every view
tiled three across (3x2, 1536x768), all at the step rate so a 10 Hz road plays in real time.
`replay --camera-rig --record-video x.mp4` writes `x.<camera>.mp4` and `x.rig.mp4` the same way.
Three facts it rests on, all read or measured:

- **A frame off the rig is already BGR.** `perceive()` ends in `get_rgb_array_cpu`
  (`image_buffer.py:101-110`): panda3d's `getRamImage` is BGRA, sliced to three channels, so
  the array is what `cv2.VideoWriter` wants and nothing is swapped -- the Step 6 probe's PNG,
  written with `cv2.imwrite`, had a blue sky.
- **One read feeds every film.** `video.CameraFilm.add` calls `rig.read()` once per step and
  writes each camera's file and the mosaic from that dict; `video.mosaic` is a pure tiling
  function, black where the last row runs short, refused by name for tiles of two sizes (the
  per-camera films are written anyway).
- **A film is a look, not a model input**, so `run` takes `--ignore-rig-rate` for it: the AV3
  spec's 0.05 s against the road's 0.1 s is exactly what gotcha 5 refuses for a model, and
  exactly irrelevant to a film that plays at the step rate. The record keeps both:
  `EnvInfo.camera_rig` and `EnvInfo.rig_tick_rate_s` beside `step_hz` and `stride`.

**Built.** `video.Recorder.open(size=)` so a film can be a camera's size; `CameraFilm`;
`chain` moved from `replay.py` into `video.py`, since both callers need it; `run_bank(
camera_rig=, ignore_rig_rate=)` loads the spec before any env is built -- the same refusal
`replay` makes, off the manifest -- hands it to `build_env` per row and opens a `CameraFilm`
beside the `Recorder`; `run --camera-rig --ignore-rig-rate` on the CLI, with the filming example
in `docs.EXAMPLES`. **A filmed row with the rig is the plain row**: `test_camera_rig.py` runs
`curve_0000` with the expert both ways and compares the records minus the clock,
`actions_digest` included, then reads every film back -- six at 512x384, the mosaic at
1536x768, the top-down -- each holding a frame per step plus the reset's at 10 fps. Offline,
`test_video.py` pins the sized recorder, the mosaic's layout with numbered tiles, the film's one
read per step and the mixed-size refusal.

**Verify alone:**

```bash
P=scenariobank.policies:ExpertPolicy
uv run scenariobank run --bank banks/curve --scenarios curve_0000 --tier hard --policy $P \
  --out film --camera-rig rigs/av3.txt --ignore-rig-rate --record-video
ls out/film/hard/videos/          # curve_0000.mp4, six curve_0000.<camera>.mp4, curve_0000.rig.mp4
xdg-open out/film/hard/videos/curve_0000.rig.mp4
uv run pytest tests/unit/test_video.py tests/unit/test_camera_rig.py -q
```
**Expect:** the mosaic plays the six views of the expert driving the hard row in real time,
cones and queued traffic in the front cameras; the row's numbers identical to the same run
without the two flags; both files green.

**And the claim failed when it was first measured, which is what the verify block is for.**
The row filmed with the rig ended at 272 steps against the plain row's 273, with another
`actions_digest`; two rig runs agreed with each other, two plain runs agreed with each other,
and the two drives parted at decision 60 in the seventh decimal of the throttle. The Step 6 test
had compared 60 steps at the bank's pinned levels, where nothing is placed, and passed. Bisected
one config key at a time in fresh processes: not `_fix_offscreen_rendering`'s throwaway engine,
not the threading model, not the mount -- **`preload_models`**. It is on by default and runs only
in a render mode (`base_engine.py:749-763`): at the first reset it spawns a pedestrian, a traffic
light, a barrier and a cone at `[0, 0]`, steps the pedestrian through its speeds, and clears
them back into the object pool. The hard row then reuses those warmed objects where a headless
env builds fresh ones, and the contact physics differs by an ulp. `build_env` sets it off with a
rig; with that, the filmed hard row is the plain hard row field for field. The live test now
runs at `hard`, where the pool is exercised. Recorded in **Traps**.

**Cost, measured on the host.** `curve_0000` at `hard`, 273 steps: 6.9 s plain, 29.4 s with
the rig read and seven films written every step -- **82 ms a step** for the rig read (30 ms) and
the writes -- plus 13 s once to open the offscreen window; 42.6 s in all against 8.2 s. The
mosaic is 15 MB for 27 s of drive, the six single films 2-3 MB each.

**Verify alone: met.** The eight files above; the mosaic plays the expert closing on the queue
and rear-ending it, the front cameras full of traffic, the rear ones showing the bend's
embankment; the record identical to the plain run's; `test_video.py` and `test_camera_rig.py`
green; full suite 744 passed, ruff clean.

### Step 7 — the AV3 model and the openpilot bridge ✅  ⟵ *built 2026-09-10; the row drove as root*

The other half of the port, plus the two things about it that are not a copy:

- `tools/av3_model.py` — `AV3Model.observe/predict_with_navigation`, `FrameHistory`, `preprocess`,
  `ego_state`, `navigation`, `waypoints`
- `tools/openpilot_policy.py` — `BridgeConnection`, `OpenpilotDriver`, `to_metadrive_action`
- `tools/av3_probe.py`'s model half: the camera map,
  the ego state, the navigation block against the route sensor, and the waypoints scored under
  both signs -- all computed by `av3_model.py`, three of them against a checkpoint. It joins
  `scripts/av3-probe.sh`, which already runs the rig's frame probe and a rig replay.
- **`--step-hz` on a procedural road**, which is what lets the AV3 rig be read at its own 0.05 s
  (Step 6, note 3): `physics_world_step_size = 1 / step_hz` with `decision_repeat = 1` on the PG
  config, refused on a recording unless it equals the recording's rate. It changes what a step
  is, so every `max_steps` budget in seconds and every Step 5 number is re-measured under it,
  and `test_env.py`'s pin that the PG config leaves the rate alone becomes a pin on the default.
  And the AV3 policy **refuses `--ignore-rig-rate`**: that switch exists for a film (Step 6b),
  which reads at the step rate whatever the spec says; a model reading a 20 Hz rig at 10 Hz is
  the silently wrong frame rate gotcha 5 is about.
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

**Verify alone** — one scenario, rig on, bridge up:

```bash
bash scripts/bridge.sh start                                  # our copy of the converter's, ported
docker run -d --name av3 --gpus all --network host -v $PWD:/work:ro -v $PWD/../models:/models:ro \
  -v $PWD/out:/out -e HOME=/tmp metadrive-wingfin-sim:latest \
  python -m scenariobank run --bank /work/banks/t-junction --scenarios t_junction_0000 \
  --policy scenariobank.av3:AV3Policy --camera-rig /work/rigs/av3.txt --step-hz 100 --decision-hz 20 \
  --model-config /models/model_dev.yml --checkpoint /models/step_440000_trt_direct_full.ep --out /out/av3
docker wait av3 && docker rm av3
python3 -c "import json; r=json.load(open('out/av3/results.json')); print(r['results'][0], r['env'])"
uv run pytest tests/unit/test_av3_config.py -q
```
*(Detached, because a killed client SIGTERMs the row into a `stopped` record; python instead of
`jq`, which the sim image lacks.)*
**Expect:** `actions == ceil(steps / 5)` at 100 / 20 Hz, with `env.decision_hz` 20 and `stride`
5; the offline test deletes one field from a copy of the submitted `model_dev.yml` and
`load_config` raises naming it rather than defaulting; steering sign matches Step 6's probe.

**Built 2026-09-10.** What landed, what was measured, and where the notes above were wrong:

1. **The port is three package modules and two policies.** `av3/av3_model.py` (`Config`,
   `load_config`, `preprocess`, `FrameHistory`, `ego_state`, `navigation`, `synthetic_route`,
   `modelv2_rows`, `waypoints`, `AV3Model`), `av3/openpilot_policy.py` (the framing,
   `route_points`, `waypoints_from_route`, `bridge_ego`, `to_metadrive_action`,
   `BridgeConnection`, `StubBridge`, `OpenpilotDriver`) and `av3/policy.py`, which is the half
   the converter did not have: `scenariobank.av3:AV3Policy` -- rig, ring, forward pass, bridge,
   pedals -- and `scenariobank.av3:BridgePolicy`, the same path with the model taken out
   (wing-sim's `route_gt.py`), so the bridge can be driven and scored on a machine with no GPU.
   `_PortablePickler`, the HTTP policy server and the path-inserted imports are gone, as
   predicted. The probe's model half is `scenariobank av3` (`av3/probe.py`), and
   `scripts/av3-probe.sh` runs it as its third stage.
2. **The route on a road is built, not recorded.** The converter's model read
   `navigation.reference_trajectory`, which only a recording has. `policy.route_for` walks the
   navigation's checkpoints -- the node sequence `env.py`'s `prepare` pinned -- takes one lane
   per road at the ego's spawn lane position, samples every metre and joins the samples into a
   MetaDrive `PointLane` (`need_lane_localization=False`, no polygon), so the model's block and
   the bridge's route points project one object with MetaDrive's own `local_coordinates` on
   both bank kinds. `av3_model.navigation` takes the trajectory as an argument for that reason.
3. **A policy is told about the run once, before any env.** `load_policy` still makes
   `Name(checkpoint_path=)`; `runner.setup_policy` then hands a `RunSetup` (step rate, stride,
   rig, `ignore_rig_rate`, model config) to a policy with a `setup` hook, and `close_policy`
   ends the batch. Every AV3 refusal lives there -- no `--camera-rig`, `--ignore-rig-rate`,
   no config, no checkpoint, a rig short of a camera `camera_order` names -- so a run that
   cannot work is refused before a 13 s offscreen window opens. `Job` carries `step_hz` and
   `model_config_path` (pydantic reserves `model_config`); `run` takes `--step-hz` and
   `--model-config`. What a `Job` does not carry is read from the environment, the converter's
   convention: `AV3_BRIDGE`, `AV3_TARGET_SPEED_MPS`, `AV3_LONGITUDINAL`, `MODEL_CONFIG`,
   `MODEL_CHECKPOINT`. There is **no default config path**: the file is a contract with one
   set of weights and the wrong one runs and scores.
4. **`--step-hz` on a road, with every budget scaled.** `env.build_config(step_hz=100)` sets
   `physics_world_step_size 0.01` and `decision_repeat 1` -- one physics step per `env.step`,
   the shape a recording is stepped in -- and leaves both keys alone at the default, so
   `test_env.py`'s pin still holds beside its new half. `env.budget_at` scales `max_steps`,
   the cap and `horizon` by `step_hz / 10`, rounded up (`t_junction` 320 -> 3200); a recording
   refuses any rate but its own. `replay` and `av3` take it too. **The expert at 100 Hz
   arrives** on `t_junction_0000` in 1334 steps (13.3 s), so the Step 5 numbers at 10 Hz are
   not this clock's numbers, as predicted; nothing here re-measures them.
5. **The bridge path works, measured three ways.** (a) `StubBridge` on the host:
   `BridgePolicy` drives `t_junction_0000` at 100/20 and **arrives** -- 1211 steps, 243
   actions (`ceil(1211 / 5)`), no collision, 1.7 s of wall clock, the bridge told
   `max_steer_angle 40` and `wheelbase_m 2.46894`; that is `test_av3_policy.py`'s live test.
   (b) The real bridge from the host (`scripts/bridge.sh start`, image `metadrive-wingfin-
   openpilot:prod`): `init` answers `ready`, 640 controls come back, the steer is **positive
   through the left turn on the left-hand road** (+0.13 to +0.23 at decisions 300-540) so the
   two negations cancel as the converter measured, and the car stays on the road -- and it
   **crawls**: `accel_cmd` decays to 0.01 m/s^2 at 3.1 m/s under a 10 m/s target, 78% of the
   route in 32 s, `max_step`. That is exactly the converter's Phase 0 finding for the route-only
   path ("a constant-speed trajectory carries no speed intent"), reproduced, and the reason
   the model's `modelv2` rows exist. (c) The model, below. `to_metadrive_action` defaults to
   the `accel` mode; the converter's `table` mode is not ported, because no pedal map has been
   measured on this bank's car.
6. **The probe, with the checkpoint, in the sim container** (`scenariobank av3` on
   `t_junction_0000`, 100/20, 20 decisions): the engine deserialises in **106 s** (the two
   logged loader failures are the documented non-errors), 20 waypoints x 8; **forward pass
   median 1149 ms** on this card, so about 5 minutes for the row's 267 decisions; the camera
   map is the six by name; ego state to 0.0000 m/s; the navigation block equals the bridge's
   route points to 0.0000 m over 180 points, 6 of 9 samples turning. The waypoints against
   the drive: the model predicts a near-straight line here (0.61 m of lateral at 2 s where the
   expert moved 5.6 m), and over the 22 turning points where it predicted more than 0.25 m its
   sign agreed with the car's on **100%**, off-path 1.09 m as given against 1.51 m negated --
   leaning "as given", conversion 6 unflipped, as the converter found. **The nav-response test
   fails on this road**: a 30 m right-hand arc and a left-hand one move the predicted lateral
   by 0.036 m (+0.010 / -0.026), against the converter's 1.109 m on `junction-1`. Same code,
   same synthetic block; the pictures differ. The probe's own rule is that the drive statistic
   cannot settle the sign without it, and it says so. Read it as a **domain-gap reading, not a
   port defect**: every input the probe can check against an independent computation agrees
   to the last digit, and the one it cannot is a property of the weights on a MetaDrive PG
   scene. Step 8 should re-run the sweep on `banks/curve` and on the imported `junction-1`
   before drawing more from it.
7. **The scored AV3 row is the one thing not yet green.** `run --policy
   scenariobank.av3:AV3Policy` on `t_junction_0000`, 100/20, the sim container with the bridge
   up, run as the host uid with `/etc/passwd` mounted the way `compose.yaml` does: the engine
   loaded (106 s), the policy connected and the bridge answered `ready` -- then no `step` ever
   reached the bridge. Its own recv timed out and it closed the socket (`CLOSE-WAIT` on the
   client's side, one byte unread); the client's main thread was **running**, at 4% of a core
   for four hours, 72 other threads in futex waits and two CUDA threads polling, nothing in the
   socket and nothing in a Python wait -- the shape of a TensorRT synchronise spinning on a
   pass that never returned. The probe in the same image, **as root**, had just completed nine
   passes plus the sweep, and the only differences between the two runs are the uid, the
   `/etc/passwd` and `/out` mounts, and the bridge connection made between the load and the
   first pass. The row was killed after four hours with nothing written. What to do next, in
   order: re-run the row as root (the probe's way) with a hard `timeout`; if it drives, the uid
   is the cause and `compose.yaml`'s `user:` line is what Phase 5 Step 4 has to reconcile with
   `torch_tensorrt`; if it hangs, move the bridge connect before `model.load()` in
   `AV3Policy.start_episode` and try again. *(Re-run 2026-09-10 **as root**, the verify block
   as written: it drove. `status ok`, `max_step`, 3200 steps, 640 actions, 929.5 s wall, one
   decision per 1.45 s -- the 1149 ms pass plus the rig read and the bridge round trip. The
   uid is the cause; resolved in Phase 5 Step 1, 2026-09-13: **the sim container runs as
   root**, and `user:` is the studio's alone.
   The row is not a drive to be proud of: `route_completion` 0.066 in the 32 simulated seconds
   the 100 Hz budget allows, no collision, no out-of-road; the car creeps. That is the model
   and the bridge's longitudinal path on this scene, not the port's -- the probe's inputs all
   agree. Filmed 2026-09-10 (`out/av3-film`): the car rolls two car lengths in its lane and
   stops on the empty road, throttle +0.53 easing to a held brake, because the model predicts a
   near-stationary path on a scene it never trained on; the same bridge on route-only waypoints
   drives the road at 3 m/s. **Keith, 2026-09-13: the model's behaviour, not the runner's.** A
   finding for Phase 7 Step 6's results notes, not work in any phase.)*
   **The heartbeat.** A row at 1.45 s per decision prints nothing for fifteen minutes and reads
   as a hang, which is how the first re-run was nearly killed a second time. `run --heartbeat
   SECONDS` (10 by default, 0 for off; `run_bank(heartbeat_s=)`) is an `observe` hook,
   `runner.Heartbeat`, chained after the films: every interval of wall time it prints the step
   and decision counts, the car's speed, the metres moved since the last line, the route
   completed and the action held, through the same `progress` channel as the row lines, so
   `docker logs -f` on a detached run shows whether the car is moving. It reads the env and
   changes nothing a row records; a host row that ends inside the first interval prints
   nothing. Five offline tests in `test_runner.py`.
8. **What bit, and the traps that did not.** `PYTHONUTF8` (Step 6 note 7) never reached the
   bridge, because the bridge is a container started before the rig env exists. The
   `preload_models` fix (Step 6b) held. Two new ones: the first scored row was ended `stopped`
   at 0 steps with a valid record when the docker client was killed for memory -- the runner's
   SIGTERM path, working as built, and the reason the verify block now runs the container
   detached (`docker run -d`, then wait); and the full test suite must not run beside a
   container holding the 1.2 GB engine on a 16 GB machine.
9. **Tests: 78 offline, 3 live**, in `test_av3_config.py` (the verify block's field-deletion
   test over every required key, no default path, the submitted file pinned to the block the
   test carries), `test_av3_model.py` (`preprocess` pixel-identical to the fork's own
   `modifiers.py`, read as a file; the ring; the mirror; the unflipped output; the model
   without a GPU), `test_openpilot_policy.py` (the framing byte for byte, both negations,
   `accel` by default, every undrivable reply, the stub end to end) and `test_av3_policy.py`
   (every refusal, the hooks, the rate arithmetic, the `Job`, the flags, and the live stub
   drive and 100 Hz config). `docs.GROUPS` places `av3` under *Look before you commit*;
   `commands.md` regenerated; `scripts/bridge.sh` is ours (status, start, stop, logs; no
   build). `pyyaml` joins the core dependencies for the config.

**Verify alone: met.** `test_av3_config.py` green (every field of a copy of the
submitted config deleted in turn and refused by name); the steering sign as the probe and the
real-bridge drive measured; `env.decision_hz` 20 and `stride` 5 in every record at 100/20;
`actions == ceil(steps / 5)` on the stub and real-bridge rows **and on the scored AV3 row**
(note 7: 3200 steps, 640 actions, `env.step_hz` 100, `camera_rig` in the record, the bridge
up throughout). Full suite 823 passed in 6 min 14 s, ruff clean. Run the block with the container **detached** (`docker run -d --name x ...`
then `docker wait x`) and read the record with python rather than `jq`, which the sim image
does not carry. Full suite: see the report below the step.

### Step 8 — an AV3 submission scored end to end ✅  ⟵ *gate, met 2026-09-12 on the rig*

Steps 1-5 with Step 7's policy in place of `ExpertPolicy`. Nothing new is built here; this is the
step that says the two halves are one runner.

Cost, and it drives the ETA model in Phase 7 Step 8. **Measured per-scenario wall time, `t_junction_0000`,
3200 steps / 640 decisions, no video** (Phase 7 Step 8 reads these rather than re-measuring):

| machine | GPU | wall time | per decision | per simulated second |
|---|---|---|---|---|
| the rig (`sim`, 116.12.220.99) | RTX 5080 | **100.0 s, 102.2 s** (two runs) | 0.16 s | 3.2 s |
| the rig, same row with `--record-video` | RTX 5080 | 154.9 s (Keith, 2026-09-11) | 0.24 s | 4.8 s |
| this laptop | RTX 4050 Laptop 6 GB | 912.5 s (`out/av3/`, 2026-09-10) | 1.43 s | 28.5 s |

The decision is one forward pass every 0.05 s of *simulated* time, so a 32 s row is 640 passes
whatever the hardware; what the hardware sets is the wall clock per pass. Price a 35-scenario bank
from the rig row: ~35 × 100 s ≈ 1 hour without video, and the estimate is per rig or it is wrong on
the slower one.

**Two claims, not one.** The AV3 path runs through the openpilot bridge — a real-time control
stack in its own container, over TCP 5558 — and nothing in this plan has established that it
returns the same action twice for the same frame. So the claim is split: the **runner** is
deterministic *given the same actions*, which is Step 5's and is checked through
`actions_digest`; the **policy's** repeatability is measured and written down, not asserted.

**Verify alone** — the same small category run twice:

```bash
diff <(jq '.results[].actions_digest' av3-1.json) <(jq '.results[].actions_digest' av3-2.json)
diff <(jq 'del(.started_utc, .finished_utc) | del(.results[].wall_time_s)' av3-1.json) \
     <(jq 'del(.started_utc, .finished_utc) | del(.results[].wall_time_s)' av3-2.json)
jq '.results[] | {scenario_id, wall_time_s}, .bank.path, .options' av3-1.json
```
**Expect:** if the digests match, the second diff **must** be empty — that is the runner's claim.
If the digests differ, the second diff will too, and how much and why is recorded in this step's
done note as a measurement of the policy, with the runner still proven. Either way the run
records the rig path, the resolved options and a per-scenario wall time, and that wall time is
copied to Phase 7 Step 8.

**Done when:**

- Steps 1-8 ✅, Step 5 first among equals.
- `uv run pytest -q` green with the new files, `uv run ruff check src tests` clean.
- `uv run scenariobank commands` regenerates `docs/reference/commands.md` with `run` grouped —
  `docs.GROUPS` refuses to render until it is.
- `replay` and `run` are two callers of one loop: `grep -n "= env.step(" src/` returns one site,
  in `runner.py` (`test_runner.py` pins it).
- Phase 3 Step 6 and Phase 4 Step 3 say the same thing about `horizon`.

**Done note, 2026-09-12.** Two scored runs of `t_junction_0000` with `scenariobank.av3:AV3Policy`,
`--step-hz 100 --decision-hz 20`, back to back on the rig, the bridge (`metadrive-wingfin-openpilot:prod`)
up throughout. Both files and their heartbeat logs are in `out/av3-rig/` (git-ignored) as `av3-1.json`
and `av3-2.json`; the verify block above was run on them as written.

- **The digests differ** — `3b87f3e1…` against `73ffca67…` — so the second diff is not empty, and
  the plan said what that means: it is the *policy's* measurement, with the runner's claim resting on
  Step 5. How much: the two drives are the same shape (throttle +0.54 at reset, ~1.9 m travelled by
  t+20 s, a stop, then the brake at −0.58 for the rest of the 32 s), and differ by 0.08 percentage
  points of route (`0.06689` against `0.066066`) and 0.13 of reward (`3.63` against `3.50`). Every
  other field — bank path, resolved options, `env` (commit `85e5dad`, stride 5, rig tick 0.05 s),
  steps 3200, actions 640, `max_step` — is identical, and `finished_utc` differs, which the verify
  block's filter now drops alongside `started_utc`.
- **Where the difference comes from is not established** and is not this step's to establish: two
  candidates are the bridge (a real-time stack with its own clocks, over TCP) and the TensorRT
  engine itself (bf16, and `torch_tensorrt` makes no determinism promise). Separating them is one
  more run with `BridgePolicy` (the model taken out) against itself — recorded here as the next
  measurement if the ETA or a leaderboard ever needs the policy's noise floor.
- **The runner's half stands** with no new work: `grep -n "= env.step(" src/` is one site,
  `runner.py`; `uv run scenariobank commands` regenerates `docs/reference/commands.md` unchanged
  with `run` grouped; ruff clean; the suite green (`env -u FORCE_COLOR`, the harness's
  `FORCE_COLOR=3` being the one thing that fails the five typer-help tests); Phase 3 Step 6 and
  Phase 4 Step 3 agree on `horizon` (both: the entry's `max_steps`, set by `replay_config`, with the
  loop cap kept as the second belt).
- **How the runs were made, because it was not the documented way.** An unattended apt upgrade on
  2026-09-12 put NVIDIA's 595.91 user-space libraries under the 595.84 kernel module on *both*
  machines, so `docker run --gpus all` fails (`nvml error: driver/library version mismatch`) on the
  laptop and on the rig alike until each reboots. A container started before the upgrade keeps the
  old libraries bind-mounted, and the rig had one — `metadrive-wingfin-sim-run-a92feea2ca1a`, the
  converter's image, up 6 days, host networking, CUDA still live — so `src/`, `banks/t-junction`
  and `rigs/` were `docker cp`'d into it at `/tmp/pg` and the two runs made with `docker exec` and
  `PYTHONPATH=/tmp/pg/src`. Same image, same lock, same checkpoint as the documented command; only
  the mount path differs, which is why `bank.path` reads `/tmp/pg/banks/t-junction`. After a reboot
  the README's `docker run --gpus all … metadrive-wingfin-sim:latest` form is the one to use.
- **Two things seen on the rig that are not this step's but will bite the next one:** its root
  filesystem is 100 % full (867 G of 915 G, 1.9 G free — docker's data root is on `/mnt/secondary`,
  which is why builds still work), and the checkout there,
  `~/dev-container/workspace-new/metadrive-complete/Metadrive-PG-testing`, is at `9aeb019`, this
  commit.

---

# Phase 4b — Calibrate the levels ✅  ⟵ *measured 2026-09-13 on the laptop*

> **Machine-run.** `calibrate` is a measurement tool; its product is
> `docs/reference/level-calibration.md`, not a screen. See **What a person actually uses**.

**Goal:** replace the provisional numbers with measured ones. Requires the invariance tests green.

The `LEVELS` table is a guess. A `high` traffic setting that makes every intersection unpassable is
not a test point, it is a broken scenario.

```bash
uv run scenariobank calibrate --bank banks/t-junction-left-intersection --axis traffic \
  --values 0,0.05,0.1,0.15,0.2,0.3,0.35,0.4,0.5      # --policy defaults to the expert
```
Pick four values that spread success rate apart; bake them into `options.py`; record the sweep in
`docs/reference/level-calibration.md`. Repeat per axis. *(Built 2026-09-13: `calibration.py`.
The sweep runs `run_bank` once per value with every other axis at `none`, writes one JSON record
per axis and bank under `docs/reference/calibration/`, re-renders the page from every record
there, and prints the four values the spread suggests. `tests/unit/test_calibration.py` holds
the page to the records and `LEVELS` to the swept values.)*

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

**Measured 2026-09-13.** Nine sweeps, the expert driving, every other axis at `none`: traffic on
`t-junction-left-intersection`, `curve` and `t-junction`; cones and barriers on `curve` (the one
bank whose blocks can hold them); pedestrians and cyclists on `curve` and the left-intersection
bank. About a second per row; the whole set took 14 minutes. What went into `options.py`:

| axis | none / low / medium / high | success at the four, where it separates |
|---|---|---|
| traffic | 0 / 0.1 / 0.3 / 0.5 | `t-junction` 1.00, 0.80, 0.40, 0.20; `curve` flat to 0.3 then 0.40 at 0.35, 0.00 at 0.5; the `X` bank 1.00 down to 0.78 |
| cones | 0 / 1 / 4 / 6 | `curve` 1.00, 0.80, 0.40, 0.00 |
| barriers | 0 / 1 / 2 / 3 | `curve` 1.00, 0.20, 0.20, 0.00 |
| pedestrians | 0 / 1 / 3 / 6 | `curve` 1.00, 0.60, 0.40, 0.00; the `X` bank only moves at `high` (0.78) |
| cyclists | 0 / 2 / 3 / 6 | the `X` bank 1.00, 0.89, 0.78, 0.44; `curve` sits at 0.80 from 1 to 8 |

Two things the tables settle. **Barriers cannot be spread into four**: one barrier scene already
holds the expert to 0.20 on `curve` (it waits behind the breakdown vehicle to the step cap,
`max_step`, rather than overtaking), and no count above it can fall further than 0.00; the axis
has one real step and the gate test asks for never-rising rates with a real drop rather than
strictly falling ones, so that is recorded rather than faked. And **the expert is not the
policy under test**: it arrives on the `X` at traffic 0.5 seven times in nine, so `high` there
is a load, not a wall — the intersection levels were chosen off the `T` road where the same
numbers separate cleanly. `tests/unit/test_calibration.py` holds the page to the records under
`docs/reference/calibration/`, every level to a swept value, and every axis to a separating
record; `calibrate --render-only` rewrites the page after an edit to `LEVELS`. What the
command is for, when it is re-run and how is `docs/calibrating-levels.md`.

---

# Phase 5 — Containers ✅  ⟵ *Steps 1–4 met 2026-09-10 → 2026-09-14 on the laptop and one rig; 2.4, the NAS's own `compose up`, waits on Open question 6*

> **Machine-run.** These are entered by CI, by the rig agent and by the studio's own worker, not
> by a person at a terminal — with one exception, `docker compose up studio`, which serves a page a
> person does use. See **What a person actually uses**.

**Goal:** the same numbers on the laptop, in the NAS's studio, and on each rig.

**Compose is the NAS's and the laptop's; the rig runs one line.** *(2026-09-13.)* `docker compose
up studio` is the one command a person types on the NAS. On a rig, Phase 7's agent starts each
job as a sibling container with one `docker run`, and that line is **`scripts/sim-run.sh`**
(Step 1): image, mounts, user, environment, entrypoint, written once and used by the laptop,
the Step 4 gate and the rig alike. Compose's `run` service is the laptop alias of that line; it
is not the rig's launcher. **A rig has this repo and nothing else** — both images are built from
it there (`bridge.sh build`; `sim-image.sh build`, which with no converter checkout beside the
repo builds `docker/Dockerfile` as `scenariobank-sim:latest`), so on a rig
`SIM_IMAGE=scenariobank-sim:latest`, set once in the agent's environment. The laptop keeps the
converter's image as its default. The model on the GPU inside `scenariobank-sim:latest` was
verified on the rig 2026-09-14 (Step 4, checks 4.3 and 4.4).

**Reuse, do not rebuild.** The converter's image already solves the hard parts — `ubuntu:22.04`,
`uv`, `UV_PROJECT_ENVIRONMENT=/opt/venv`, `metadrive.pull_asset`, the panda3d `Config.prc` patch
preferring EGL over GLX, the `glvnd/egl_vendor.d` manifest, `HOME=/tmp` — and it already runs this
repo, measured 2026-09-06:

```
$ docker run --rm -v $PWD:/work:ro metadrive-wingfin-sim:latest python -m scenariobank doctor
commit:        85e5dadc6c7436d324348f6e3d8f8e680c06b4db     requested: 85e5dadc
asset_version: 0.4.3    python: 3.10.21    numpy: 2.2.6
obs_space:     Box(-0.0, 1.0, (19,), float32)               drive_side: left
```

No build, no install, no `PYTHONPATH` — and that output *is* what this phase's acceptance asks for.
So the only image built from scratch here is the studio's; `docker/Dockerfile` is the same recipe
kept as the fallback for a machine with no converter checkout (Step 1).

**Why no `PYTHONPATH`.** The base image's editable install is a single bare path line,
`/work/src`. `site` evaluates it at every interpreter start, so whatever is mounted at `/work` has
its `src/` on `sys.path`; `/work` is also that image's `WORKDIR`, so `banks/curve` resolves the way
it does on the host. The cost is that the mount shadows the converter's own source, so
`import osm_scenario` does not work in there — nothing of ours imports it.

**R1 is untouched.** We import none of that repo's code. We name one of its build products, the way
a lockfile names a wheel.

## Four containers, three images, all built from this repo on a rig

| # | container | image | where | lifetime | GPU |
|---|---|---|---|---|---|
| 1 | studio | `scenariobank-studio` — **here**, `FROM` the sim image plus fastapi, uvicorn, httpx | NAS | always up | no |
| 2 | rig agent (Phase 7) | the sim image, `python -m scenariobank agent` — no fourth image | each rig, one | always up | no |
| 3 | openpilot bridge | `metadrive-wingfin-openpilot:prod` — `bridge.sh build` from `docker/openpilot/`, the fork vendored | each rig, one per *running* simulation | for the job | no |
| 4 | simulator + model | the sim image, `scripts/sim-run.sh run …` | each rig, one per running job | for the job | that card |

The sim image is `metadrive-wingfin-sim:latest` where the converter checkout exists (the laptop)
and `scenariobank-sim:latest` from `docker/Dockerfile` where it does not (a rig); `SIM_IMAGE`
selects, `scripts/sim-image.sh` builds and checks (Step 1). The queue is not a container we run.
A rig with two busy cards runs five containers.

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

### Step 1 — `compose.yaml`, `scripts/sim-run.sh`: our repo, one image, one line ✅  ⟵ *met 2026-09-14: code in, all eight laptop checks pass*

Two compose services over one image, the rig's `docker run` line as a script, plus
`scripts/sim-image.sh`, which is the guard.

- **`scripts/sim-run.sh` — the line a rig executes for a job** *(added 2026-09-13)*.
  `bash scripts/sim-run.sh <scenariobank args>` runs one command in the sim container and exits
  with its code: `${SIM_IMAGE:-metadrive-wingfin-sim:latest}`, `--gpus device=$GPU` (all when
  unset; `NO_GPU=1` for `generate`, `doctor`, `ExpertPolicy`), `--network host` (the bridge is on
  loopback), `$REPO:/work:ro`, `${OUT_DIR:-$REPO/out}:/out`, `${MODELS_DIR:-$REPO/../models}:/models:ro`,
  `/etc/localtime:ro`, `HOME=/tmp`, `MPLCONFIGDIR=/tmp/matplotlib`, entry `python -m scenariobank`;
  the label guard first (`sim-image.sh`'s check), so a stale image says so before a run.
  `GPU` and `BRIDGE_PORT` are its two per-card inputs (`run --bridge-port`, Step 3). Phase 7's
  agent issues exactly this line; compose's `run` service below is its laptop alias.
- **The sim container runs as root** *(2026-09-13, closing Phase 4 Step 7 note 7)*. As the host
  uid, with `/etc/passwd` mounted, the scored AV3 row hung for four hours; as root it drove on the
  laptop and on the rig. The cause is unexplained and root is the documented form. So `user:` and
  the passwd mount move out of compose's shared block into the **`studio` service alone**, which
  writes banks into the repo and must own them; `run` has neither. On a rig, `/out` is read by
  the agent, which then owns the copy to the share, so root-owned results cost nothing.
- **The `agent` service** *(2026-09-13; Phase 7 owns the code, this phase owns the container)*:
  the sim image, no GPU, `/var/run/docker.sock`, the NAS share, and the lock directory --
  which is **`${SIMULATION_ROOT:-$HOME/simulation}`, not the `/var/lock/scenariobank` this
  bullet first said** *(corrected 2026-09-15, Phase 7 Step 2)*: the rig lock is wing-sim's file,
  exclusion is a property of the inode, and a lock directory of our own would exclude nobody
  while looking entirely right. The service also runs **`pid: host`**, without which
  `/proc/locks` reads zero rows for the whole machine;
  environment `SCENARIOBANK_BANKS`, `SCENARIOBANK_MODELS`, `SCENARIOBANK_RESULTS` as **host**
  paths (a sibling's bind mounts are the host's, never the agent's own mount points),
  `WFQUEUE_URL`, `SIM_IMAGE`. The socket mount is acceptable here where Step 2 rejected it for
  the studio: the agent serves no HTTP.
- **`image:` with no `build:` key on the runner** *(Keith's call, 2026-09-12)*. The runner's image
  is the converter's `metadrive-wingfin-sim:latest`, one sim container for both projects, built
  with `docker compose build` in that checkout. Only that direction of sharing is safe: the
  converter's image is a strict superset (its own libraries and `ros` on top of everything we
  need), ours is not. A `docker compose build` in this repo must be unable to produce anything
  under the shared tag — two repos building one tag is the failure the `wingfin.groups` label
  exists to catch. `docker/Dockerfile` (the converter's recipe minus `ros`) is the **fallback**
  for a machine with no converter checkout, built by `sim-image.sh build` under its own tag,
  `scenariobank-sim:latest`, never the converter's; `SIM_IMAGE=scenariobank-sim:latest` selects
  it in compose, the studio's `ARG` and the script. `sim-image.sh build` **delegates**: with the
  converter checkout beside this repo (`CONVERTER_DIR`) it runs the converter's own `docker
  build` under the converter's tag. `test_images.py` pins that `run` has no build key and that
  the fallback build cannot take the shared tag. The bridge is built the same way:
  `docker/openpilot/` carried whole, the openpilot fork vendored under `deps/` (309 MB, 3026
  files, ten symlinks that `bridge.sh build` and `test_images.py` both check). `pyproject.toml`
  carries the `gpu` and `model` groups, opt-in, so the host `.venv` is untouched.
  *(Verified 2026-09-12: `scenariobank-sim:latest` built in ~20 min, 13.1 GB, label `sim gpu
  model`; `doctor` in it prints commit 85e5dadc and `drive_side: left`, the same as the host;
  a `BridgePolicy` row of `t_junction_0000` at 100/20 ran inside it against the live bridge,
  3200 steps / 640 actions, 7.2 s. The bridge image built from the vendored context in ~35 min,
  5.53 GB, and a host row against it gives the converter's own numbers: 640 controls, route
  0.78, `max_step`. Trap for the record: an unattended apt upgrade moved the NVIDIA user-space
  libraries to 595.91 under the running 595.84 kernel module, and from then on `--gpus all`
  failed with `nvml error: driver/library version mismatch` for every image until a reboot. A
  driver upgrade lands silently and breaks every GPU container on the machine; `nvidia-smi` on
  the host is the one-line diagnosis.)*
- **The runner mounts `.:/work:ro`.** Read-only *is* the test: a runner that can rewrite the bank
  it is scoring makes "the same numbers everywhere" uncheckable. `${OUT_DIR:-./out}:/out` is the
  only writable path — and `../models:/models:ro` the second read-only one *(added 2026-09-13;
  compose had no models mount at all, so a scored run could not use it)*.
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
  *(Since 2026-09-12 it also has `build`, and its label check compares the image against
  `docker/Dockerfile`'s own `uv sync` line rather than a hard-coded list.)*

**Verify alone, step by step** *(expanded 2026-09-13; every check runs on the laptop today,
none needs the queue)*. Before any of them:

```bash
nvidia-smi -L                        # the card by name. "Driver/library version mismatch" → reboot the host first
bash scripts/sim-image.sh status     # ends in "ready." — the sim image is present and its label matches the recipe
ls banks/t-junction ../models        # the bank (not in git) and model_dev.yml + step_440000_trt_direct_full.ep
uv run scenariobank doctor | grep -E 'commit|drive_side'   # the host's own answer, kept for 1.2
```

*(2026-09-13: the laptop rebooted; `nvidia-smi -L` lists the RTX 4050 again and the 595.91/595.84
mismatch of the 12th is gone.)*

**1.1 The guard runs before the container.** A missing or stale image must be named, not run.

```bash
SIM_IMAGE=nothing-here:latest bash scripts/sim-run.sh doctor; echo "exit=$?"
```

Expect: a non-zero exit, a line naming `bash scripts/sim-image.sh build`, and no `docker run`
(`docker ps -a --latest` shows nothing new). If a container is created and *then* fails with
"unable to find image", the guard is after the run, not before it.

**1.2 The container's `doctor` equals the host's.** The phase's acceptance in one line.

```bash
NO_GPU=1 bash scripts/sim-run.sh doctor | grep -E 'commit|drive_side'
```

Expect: `commit: 85e5dadc…`, `requested: 85e5dadc` and `drive_side: left`, byte-for-byte the
lines the host printed above. A different commit means `/work` is not the mount `site` reads
`src/` from (the "Why no `PYTHONPATH`" paragraph); a missing `requested:` means the image's own
metadrive is answering, not the pinned one.

**1.3 The read-only mount refuses a write.** Read-only *is* the test.

```bash
NO_GPU=1 bash scripts/sim-run.sh generate --out /work/banks/x --bank-id x -c curve --seeds 0; echo "exit=$?"
ls banks/x 2>&1
```

Expect: exit 1 with `Read-only file system` in the traceback (the `mkdir` at `bank.py:556` is
the first write, so it fails before any rendering), and `ls: cannot access 'banks/x'`. If a
`banks/x` appears, `/work` is mounted `rw` and the numbers-everywhere claim is uncheckable.

**1.4 `/out` is the one writable path, and root owns it.**

```bash
NO_GPU=1 bash scripts/sim-run.sh run --bank /work/banks/t-junction \
    --policy scenariobank.policies:ExpertPolicy --out /out/gate; echo "exit=$?"
ls -ln out/gate/results.json                  # owner uid 0 — expected, the root bullet above
jq '.summary, .env.metadrive_commit, .bank' out/gate/results.json
```

Expect: exit 0; `summary.n` equals the bank's scenario count with `by_status.ok` the same
number; the commit above; `bank.path` reads `/work/banks/t-junction` (the mount's path, the
label Step 4 deletes). This file is also the laptop half of Step 4's gate, so keep it.

**1.5 The clock is the host's.** The trap that makes timestamps hours adrift.

```bash
jq -r '.started_utc' out/gate/results.json; date -u +%FT%TZ
```

Expect: the same minute. Hours apart means `/etc/localtime` is not mounted.

**1.6 The GPU reaches the container, through the script's `--gpus device=$GPU`.**

```bash
GPU=0 bash scripts/sim-run.sh doctor | grep -E 'commit|drive_side'      # not NO_GPU this time
docker run --rm --gpus device=0 --network host -v $PWD:/work:ro -v $PWD/../models:/models:ro \
    -e MODEL_CONFIG=/models/model_dev.yml -e MODEL_CHECKPOINT=/models/step_440000_trt_direct_full.ep \
    ${SIM_IMAGE:-metadrive-wingfin-sim:latest} bash /work/scripts/av3-probe.sh   # the Phase 4 Step 7 probe, all three stages
```

Expect: `doctor` unchanged from 1.2, and the probe's three stages all running — six named
sensors, no `rgb_camera`, `image_buffers <= 9`, six `ok` rows, then the model stage with the
engine loaded and the forward pass running (`forward pass median … ms`). The probe is a shell
script, so it goes through `docker run` directly: `sim-run.sh`'s entry is `python -m
scenariobank` and it runs nothing else — which is why the models mount and the two `MODEL_*`
variables are spelled out here; without them the probe stops before the model stage with `no
model config at ../models/model_dev.yml`. The probe's own closing verdict (`result FAILED` on
the nav-response conversion) is the model's, Phase 4 Step 7's finding, and is not judged here. Failure reads: `nvml error: driver/library version mismatch` → the host's driver
moved under the module, reboot (the 2026-09-12 trap above); `could not select device driver ""
with capabilities: [[gpu]]` → the NVIDIA container runtime is not installed on this host; a probe
that stops after the frame stage → the model stack, Phase 4 Step 7's problem, not this phase's.

**1.7 The bridge answers through the script's `--network host`.** Step 3 proves the bridge
itself; this proves the script's networking reaches it.

```bash
bash scripts/bridge.sh start && bash scripts/bridge.sh status     # "listening" on 127.0.0.1:5558
bash scripts/sim-run.sh run --bank /work/banks/t-junction --scenarios t_junction_0000 \
    --policy scenariobank.av3:BridgePolicy --camera-rig /work/rigs/av3.txt \
    --step-hz 100 --decision-hz 20 --out /out/bridge-check; echo "exit=$?"
jq '.results[0] | {status, steps, route_completion, failure_reason, wall_time_s}' out/bridge-check/results.json
docker logs metadrive-wingfin-openpilot-bridge 2>&1 | tail -3    # not `bridge.sh logs`: that one follows, and never returns
```

Expect: exit 0, the row `status: ok` with the step count and route completion the 2026-09-12
note above records for this row (3200 steps, 640 actions, route 0.78, `max_step`; about 7 s
warm, about 20 s when the bridge has just started and acados compiles on the first `init`),
and the bridge's log still on its planner chatter. A `connection refused` means the container is not on the host's
network (a bridge network with `-p` gives the same symptom); a connect that succeeds and then
times out on `init` means a previous simulation still holds the one connection the bridge serves
(Step 3: `listen(1)`), so `bridge.sh stop && bridge.sh start` and rerun.

**1.8 The scored AV3 row drives, as root, with a heartbeat.** The four-hour hang, closed.

```bash
bash scripts/sim-run.sh run --bank /work/banks/t-junction --scenarios t_junction_0000 \
    --policy scenariobank.av3:AV3Policy --camera-rig /work/rigs/av3.txt --step-hz 100 --decision-hz 20 \
    --model-config /models/model_dev.yml --checkpoint /models/step_440000_trt_direct_full.ep \
    --heartbeat 30 --out /out/av3-check 2>&1 | tee out/av3-check.log
jq '.results[0] | {status, steps, route_completion, failure_reason, wall_time_s}' out/av3-check/results.json
```

Expect: the engine loads (about 106 s; the two logged loader failures are the documented
non-errors), then a heartbeat line every 30 s until the row ends, about five minutes for its
decisions at this card's 1.1 s per pass; `status: ok`. Route completion and `failure_reason`
are the model's result and are not judged here (Phase 4 Step 7 note 7: the car stopping is the
model's behaviour). **Failure:** the engine has loaded, the bridge answered `ready`, and ten
minutes pass with no heartbeat → the hang is back. First thing to read: `docker inspect
--format '{{.Config.User}}' <container>` must print nothing (root); anything else means
`user:` leaked back into the run line.

Add to the failure reads: `torch.AcceleratorError: CUDA error: unspecified launch failure`
mid-drive, and `nvidia-smi` afterwards printing `No devices were found` → the card has fallen
off the bus (`journalctl -k | grep Xid` shows `Xid 79 … GPU has fallen off the bus` and `Xid 154
… Node Reboot Required`). Nothing in the container did that; reboot the host and rerun.

**1.9 The model in `scenariobank-sim:latest`** is the one check this step cannot run on the
laptop with its default image: it is Step 4's 4.3, on a rig, and it is listed there.

**Run 2026-09-13 (evening), on the laptop, converter's image.** `scripts/sim-run.sh` written;
`compose.yaml` changed as the bullets say (`user:` and the passwd mount on `studio` alone, the
models mount on `run`, the `agent` service under the `rig` profile so `docker compose up` never
starts it on the laptop or the NAS); `tests/unit/test_images.py` gained one test pinning all of
that against both files, 7 passed.

| check | result |
|---|---|
| preflight | `ready.`; host `commit 85e5dadc…`, `drive_side: left` |
| 1.1 | exit 1, `NOT PRESENT -- bash scripts/sim-image.sh build`, container count unchanged (22 before and after) |
| 1.2 | `commit`, `requested: 85e5dadc`, `drive_side: left` identical to the host; exit 0 |
| 1.3 | exit 1, `OSError: [Errno 30] Read-only file system: '/work/banks/x'` from `bank.py:556`; no `banks/x` |
| 1.4 | exit 0 in 11 s; `results.json` owner uid 0; `n: 5, by_status.ok: 5, success_rate 1.0`; commit as above; `bank.path: /work/banks/t-junction` — kept as the laptop half of the Step 4 gate |
| 1.5 | `started_utc 2026-09-13T16:07:34Z` against host `16:07:43Z` |
| 1.6 | `doctor` unchanged with `--gpus device=0`; probe: six sensors, six `ok` rows, engine loaded, `forward pass median 1228 ms`; its verdict `result FAILED` on the nav-response conversion is the model's (Phase 4 Step 7). The plan's original probe line lacked the models mount and stopped at `no model config`; fixed above |
| 1.7 | first attempt `Connection refused`: a stopped bridge from seven hours before held the name, exactly the documented reading. `bridge.sh stop && start`, rerun: `status: ok`, 3200 steps, 640 actions, route 0.782, `max_step`, 20.5 s (acados compiling on the fresh bridge's first `init`). `bridge.sh logs` follows the log and never returns; the check now reads `docker logs … | tail` |
| 1.8 | **met 2026-09-14, on the rerun.** `docker inspect` on the running container: `User=[]` (root), `NetworkMode=host`, `/work` ro, `/out` rw, `/models` ro, one gpu request. `status: ok`, 3200 steps, 640 actions, `max_step`, route 0.066 (the host's own earlier run of this row: 0.0659), 815 s; 26 heartbeats at 30 s; `results.json` owned by uid 0; `nvidia-smi -L` lists the card afterwards; no Xid this boot. The first attempt, 2026-09-13, died at step ~113 with `CUDA error: unspecified launch failure` because the card had fallen off the bus after a host OOM (Xid 79, then Xid 154 `Node Reboot Required`) -- not the container's fault; the rerun after the 01:40 reboot on 2026-09-14 passed. |

The bridge container does not survive a reboot: `bridge.sh status` first, `stop; start` if it shows Exited.

So Step 1's code is in and all eight laptop checks pass: the scored AV3 row ran as root, with
heartbeats, to `max_step` on the rerun after the reboot. Nothing committed.

*Trap, for the record:* a laptop with 15 GB running the sim container with the model, the
bridge, a browser and desktop apps can hit the OOM killer, and this driver (595.91 open
kernel module) answers memory pressure by dropping the card off the bus, which only a reboot
brings back. On a rig this is not a concern; on the laptop, close what is not needed before
1.8, and read `nvidia-smi` first when a GPU row dies mid-drive.

### Step 2 — `docker/studio.Dockerfile`: the one image built here ✅  ⟵ *met 2026-09-14 on the laptop; 2.4 is the NAS's, after Open question 6*

`FROM scenariobank-sim:latest`, then the web group. Nothing else — no `pull_asset`, no EGL
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
an interface its own step has not defined. **Revisited 2026-09-15, when Step 3 made that
supervisor real, and the decision stands**: the first two grounds are untouched by its existing,
and `RunSession` needs a card lock and a host-path view of every root, neither of which a studio
running one job in its own container has or wants.

**Verify alone, step by step** *(expanded 2026-09-13)*. The claim under all of it: this image
contains MetaDrive and writes as the host uid.

**2.1 The image builds as one thin layer, and the tests still pin the tags.**

```bash
docker compose build studio
docker images scenariobank-studio --format '{{.Tag}} {{.Size}}'      # one tag, ~13.4 GB: the base plus ~40 MB
docker history scenariobank-studio:85e5dad --format '{{.Size}} {{.CreatedBy}}' | head -3
uv run pytest tests/unit/test_images.py -q                           # 7 passed (Step 1 added one)
```

Expect: the top layer is tens of MB, not 13 GB — a 13 GB top layer is the `chmod -R` trap
above, the venv copied up. The size printed is the whole image and it is fine that it reads as
the base's; what must not happen is a second 13 GB.

**2.2 Compose puts each setting on the one service that needs it.**

```bash
# `config` normalises volumes to the long form, so a grep for `:/work` finds nothing; and the
# agent only appears under its profile. Read the services out of the normalised form instead:
docker compose --profile rig config | awk '/^  [a-z]+:$/{svc=$1} /target: \/work$/{getline n; print svc, "/work", (n ~ /read_only: true/ ? "ro" : "rw")} /target: \/etc\/passwd/{print svc, "passwd"} /^    gpus:/{print svc, "gpus"} /^    user:/{print svc, $2} /docker.sock/{print svc, "docker.sock"}' | sort -u
```

Expect exactly six lines: `agent: docker.sock`; `run: gpus`; `run: /work ro`; `studio: passwd`;
`studio: 1000:1000`; `studio: /work rw`. `user:` under `studio` only; `gpus` under `run` only;
`docker.sock` under `agent` only; `/work:ro` on `run`, `/work` writable on `studio`. Anything on a second service is the leak
1.8 would catch the slow way.

**2.3 The page serves, and a job it launches writes a bank the host uid owns.**

```bash
docker compose up -d studio
curl -s http://127.0.0.1:8770/api/doctor | jq -r '.commit'          # 85e5dadc…, the same commit again
# in a browser: http://127.0.0.1:8770/ → Run tab → generate, bank id "studio-check", category curve, one seed
# -- or the same thing the page does, without the browser:
curl -s -X POST http://127.0.0.1:8770/api/jobs -H 'Content-Type: application/json' \
  -d '{"command":"generate","options":{"--out":"banks/studio-check","--bank-id":"studio-check","--category":["curve"],"--seeds":"1"}}'
curl -s http://127.0.0.1:8770/api/jobs/<job_id> | jq -r '.state'     # finished
ls -ln banks/studio-check/manifest.json                               # owner is YOUR uid, not 0
docker compose down
```

Expect: the page lists the commands, the job finishes in the job log, and the bank is owned by
the host uid — the whole reason `user:` stays on the studio. Root-owned files under `banks/`
mean `user:` is missing; a `KeyError: getpwuid()` in the job log means the `/etc/passwd` mount
went with it. A page that answers but whose job dies importing `metadrive` means the image was
built `FROM` something other than the sim image — check `SIM_IMAGE` at build time.

**2.4 The NAS's own command.** `docker compose up studio` (no `-d`) on the NAS is 2.3 again and
is run there once, after Open question 6 decides how a browser reaches it.

**Run 2026-09-14 (laptop, after Step 1 closed).** `SIM_IMAGE` unset, so the base is the
converter's `metadrive-wingfin-sim:latest`; the job was submitted through `POST /api/jobs`, which
is the route the page's Run tab posts to, so the browser step is the same code path.

| check | result |
|---|---|
| 2.1 | **met.** First build 1.5 s of `uv pip install` on a cached base; `docker images` → one tag, `85e5dad 13.4GB`; `docker history` top layers: `0B WORKDIR /work`, **`10.5MB RUN uv export … uv pip install`**, `93.4kB COPY pyproject.toml uv.lock`. No 13 GB copy-up. `uv export --only-group web` resolved 10 changes out of the lock: `+ starlette 1.6.0`, `+ uvicorn 0.52.4`, `+ idna`, `+ truststore`, and three the base already had, moved to the lock's pins (`pydantic 2.13.4 → 2.13.5`, `pydantic-core 2.46.4 → 2.46.5`, `typing-inspection 0.4.2 → 0.4.4`). So "additive" is exact for the venv as a whole and the lock wins for the packages the web group touches -- a patch bump, and the one the tests and the laptop's `.venv` already run on. `uv run pytest tests/unit/test_images.py -q` → 7 passed. |
| 2.2 | **met.** The awk above prints exactly the six lines. The plan's original grep printed `user: 1000:1000` under `studio` and `gpus:` under `run` but nothing for `/work` -- `config` emits long-form volumes -- and nothing for the agent without `--profile rig`; the check now reads the normalised form. |
| 2.3 | **met.** `docker compose up -d studio` → `scenariobank-studio-1` up in 4 s; `docker inspect`: `User=1000:1000`, `NetworkMode=host`, mounts `/etc/passwd ro`, `/etc/localtime ro`, repo `→ /work rw`. `GET /api/doctor` → `commit 85e5dadc6c7436d324348f6e3d8f8e680c06b4db`; `GET /` → 200, 156 kB of page; `GET /api/commands` lists the groups. `POST /api/jobs` → 201, `argv ['/opt/venv/bin/python', '-m', 'scenariobank', 'generate', '--out', '/work/banks/studio-check', '--bank-id', 'studio-check', '--category', 'curve', '--seeds', '1']`; job `finished`, exit 0, log ends `[1/1] curve_0000 seed 1 -> 2C0_1_ lane 1 340.1 m` / `1 scenarios in 1 categories -> /work/banks/studio-check/manifest.json`. `ls -ln`: `manifest.json`, `thumbs/curve_0000.png` (47 kB) and the `.studio/jobs/<id>` directory all **owned by 1000:1000**; `find banks/studio-check -not -uid 1000` → nothing. `drive_side: left` in the manifest. The job log's `libOpenGL.so.0: cannot open shared object file` line is the same one every Step 1 log carries from the base image; it is not the studio's and the thumbnail rendered anyway. `docker compose down` removed the container and network; the bridge, started by hand, was untouched. |
| 2.4 | **not run here; it is the NAS's.** Same command, on the NAS, once Open question 6 says how a browser reaches it. |

`banks/studio-check` is left in place as the evidence; delete it whenever. Nothing committed.

### Step 3 — the bridge: built here, one per running simulation ✅  ⟵ *met 2026-09-10*

`metadrive-wingfin-openpilot:prod` is built **from this repo** (`bridge.sh build`, the converter's
Dockerfile with the openpilot fork vendored under `docker/openpilot/deps/`, 2026-09-12: 5.53 GB,
~35 min) and a host `BridgePolicy` row against it gives the converter's own numbers (Step 1's
note). A rig needs no converter checkout.

- It mounts nothing and needs no repo: `bridge.sh start` is `docker run -d --network host … python3
  -m zapeta.server`, and the wire protocol is 29 lines of length-prefixed JSON on TCP 5558.
- **One bridge process per running simulation** *(2026-09-13, read off `zapeta/server.py`)*: the
  server is `listen(1)`, one connection handled to completion on one thread, the planners
  rebuilt per connection, and the waypoint grid fixed by the first `init` for the life of the
  process. A second simulation connecting while one runs waits in the backlog and times out.
  So a rig running one simulation per card runs one bridge per card, on `BRIDGE_PORT` = 5558 +
  card index — already honoured by the server and by `bridge.sh`; the client's port becomes
  `run --bridge-port`, and the container name gains the card (Open question 5). The bridge does no
  GPU work and is a resource the agent's worker starts, not a controller.
- **One coupling worth writing down for future users:** `AV3_MPC_MENU="4 16 20 32"` prebuilds one
  acados solver per waypoint count. A model with a count outside that menu still runs — the solver
  is generated and compiled on first use — but the first decision then pays a compile it should not.
- We port the client, not the container: `tools/openpilot_policy.py` → `src/scenariobank/av3/`,
  which Phase 4 Step 7 owns. *(Done 2026-09-10, with `scripts/bridge.sh` -- status, start, stop,
  logs; no build -- as our copy of the converter's script's running half. The image on this
  machine is `metadrive-wingfin-openpilot:prod`, 6.17 GB, image id `b32169b35049`, the same id
  as `wing-sim-openpilot:prod`.)*

**Verify alone:** `bridge.sh status` reports the image present and something listening on
127.0.0.1:5558, and the ported client's `init` handshake gets `ready` back with nothing rebuilt.
*(Met 2026-09-10 from Phase 4 Step 7: `bash scripts/bridge.sh start`, then `BridgePolicy` on
`t_junction_0000` -- `init` answered `ready`, 640 `step`s answered with controls.)*

**To re-run it as a regression** *(2026-09-13)*, on the host, no sim container involved — so a
failure here is the bridge's and not the runner's:

```bash
bash scripts/bridge.sh start && bash scripts/bridge.sh status        # image present, "listening" on 127.0.0.1:5558
uv run scenariobank run --bank banks/t-junction --scenarios t_junction_0000 \
    --policy scenariobank.av3:BridgePolicy --camera-rig rigs/av3.txt --step-hz 100 --decision-hz 20 \
    --out out/bridge-host; echo "exit=$?"
jq '.results[0] | {status, steps, route_completion, failure_reason}' out/bridge-host/results.json
bash scripts/bridge.sh logs | tail -3                                # a clean shutdown, nothing rebuilt
```

Expect: `status: ok`, the converter's own numbers for this row (640 controls, route 0.78,
`max_step`), and a log with no acados compile between `init` and the first `step` — a compile
there means the waypoint count is outside `AV3_MPC_MENU`. The same row through `sim-run.sh` is
Step 1's 1.7; a second bridge on `BRIDGE_PORT=5559` cannot be started yet because
`bridge.sh` fixes the container name (Open question 5), so the two-bridge check is Phase 7's.

### Step 4 — laptop and rig agree ✅  ⟵ *met 2026-09-14 on the rig, with one finding: outcomes agree across machines, action digests do not*

The claim that matters is across machines, through the line the rig will run.

```bash
# on the laptop, then the same line on a rig (banks/t-junction copied over first; banks are not in git)
NO_GPU=1 bash scripts/sim-run.sh run --bank /work/banks/t-junction \
    --policy scenariobank.policies:ExpertPolicy --out /out/gate
diff <(jq 'del(.started_utc,.finished_utc,.bank.path,.results[].wall_time_s)' out/gate/results.json) \
     <(jq 'del(.started_utc,.finished_utc,.bank.path,.results[].wall_time_s)' out/gate-rig/results.json)
```

Those four are the only volatile fields (checked against a real record, `out/sim-check/results.json`);
`bank.path` differs by mount and is a label, not a score. **Expect: empty.** If it is not, the diff
names the field, and that is the finding to write down before anything else — the bank is not
portable and the premise needs revisiting before the studio submits a job.

*(2026-09-14: it was not empty, and the field it named was `actions_digest` on every row, with
`reward` behind it in the seventh significant digit. What that turned out to mean is in the run
below: the bank **is** portable — every outcome field agrees — but two CPUs do not produce the
same floats, so the exact-diff form of this gate is the wrong instrument across machines. The
comparison that holds is the outcome fields; `docs/running-the-application.md` has the jq line.)*

**Verify alone, step by step** *(expanded 2026-09-13; the four marked rig run there — state
the commands before running them, per the rig-access rule)*. A rig has this repo and nothing
else, so the images are built there, never copied.

**4.1 (rig) Build both images from the clone, and the guard passes on them.**

```bash
df -h /mnt/secondary                             # docker's data root; the two builds need ~25 GB free (the root disk is full)
git pull && bash scripts/sim-image.sh build      # no converter checkout beside the repo → docker/Dockerfile, ~20 min, 13.1 GB
bash scripts/bridge.sh build                     # ~35 min, 5.5 GB
SIM_IMAGE=scenariobank-sim:latest bash scripts/sim-image.sh status   # "ready.", label "sim gpu model"
```

Expect: `sim-image.sh build` says it is building the fallback under its own tag, never
`metadrive-wingfin-sim` (`test_images.py` pins this); `status` ends in `ready.`

*(2026-09-14: the first rig had the converter checkout beside the repo after all, so `build`
would have made the converter's image — which it already had, 21.5 GB, from the 2026-09-12
session. The fallback was forced with `CONVERTER_DIR=/nonexistent`. The premise "a rig has this
repo and nothing else" is the design's, not necessarily the machine's.)*

**4.2 (rig) The gate: the same bank, the same numbers.** The laptop half is Step 1's 1.4.

```bash
# laptop → rig, banks are not in git; then prove the copy is the same bank
rsync -a banks/t-junction/ rig:~/dev-container/workspace-new/metadrive-complete/Metadrive-PG-testing/banks/t-junction/
sha256sum banks/t-junction/manifest.json                    # on both machines: one digest
# on the rig, 1.4's line with the rig's image
SIM_IMAGE=scenariobank-sim:latest NO_GPU=1 bash scripts/sim-run.sh run --bank /work/banks/t-junction \
    --policy scenariobank.policies:ExpertPolicy --out /out/gate
# rig → laptop, then the diff above
rsync -a rig:.../out/gate/results.json out/gate-rig/results.json
```

Expect: **the diff is empty.** How to read one that is not, by the field it names:
- `results[].actions_digest` or `actor_layout_digest` — the simulation is not deterministic
  across machines. The finding this step warns about; stop and write it down before anything else.
- `env.metadrive_commit` — the rig's image carries a different MetaDrive; the build did not
  pin what `pyproject.toml` pins.
- `bank.id`, `bank.schema_version` or `results[].seed` — the wrong bank, or a bank rewritten by
  a `replace` on one side since the copy; redo the rsync and the digest.
- `summary` alone, with every row equal — impossible; a row differs above it, read further up.

**4.3 (rig) The model on the GPU inside `scenariobank-sim:latest`** — the one thing no machine
has verified, and the first thing to run after the build.

```bash
docker run --rm --gpus device=0 -v $PWD:/work:ro scenariobank-sim:latest bash /work/scripts/av3-probe.sh
bash scripts/bridge.sh start
SIM_IMAGE=scenariobank-sim:latest MODELS_DIR=<the rig's models dir> GPU=0 bash scripts/sim-run.sh run \
    --bank /work/banks/t-junction --scenarios t_junction_0000 --policy scenariobank.av3:AV3Policy \
    --camera-rig /work/rigs/av3.txt --step-hz 100 --decision-hz 20 \
    --model-config /models/model_dev.yml --checkpoint /models/step_440000_trt_direct_full.ep \
    --heartbeat 30 --out /out/av3-rig
```

Expect: what 1.6 and 1.8 expect, on the rig's card. The engine load and the pass time are the
rig's own numbers; record them, they are what Phase 7's lease `visibility_timeout` and extend
timer are sized from. A failure here that 1.6 did not show on the laptop is in
`docker/Dockerfile`'s model stack, not the runner.

**4.4 (rig) Two cards, two containers, one each.** What Phase 7's worker-per-GPU stands on.

```bash
SIM_IMAGE=scenariobank-sim:latest GPU=0 bash scripts/sim-run.sh replay --bank /work/banks/curve \
    --camera-rig /work/rigs/av3.txt --steps 2000 --ignore-rig-rate --json > /dev/null &
SIM_IMAGE=scenariobank-sim:latest GPU=1 bash scripts/sim-run.sh replay --bank /work/banks/curve \
    --camera-rig /work/rigs/av3.txt --steps 2000 --ignore-rig-rate --json > /dev/null &
sleep 20; nvidia-smi --query-compute-apps=pid,gpu_uuid --format=csv; wait
```

Expect: two rows with two different UUIDs while both run, both exits 0. Both on one UUID means
`--gpus device=$GPU` is not reaching `docker run` and the agent would stack every job on card 0.

*(2026-09-14: the first rig has **one** card, so the two-container form waits for a two-card rig.
The one-card substitute below proves the half that can be proved here: the index the script is
given is the device the container asks for and gets. Note also that a `replay` with cameras shows
**no** process in `nvidia-smi --query-compute-apps` — offscreen rendering is not a compute
context — so the sampling has to catch a CUDA process, the model's, not the renderer's.)*

**Run 2026-09-14 (rig: `116.12.220.99`, one RTX 5080 16 GB, Ryzen 9 9950X3D, 58 GB; laptop:
i7-13700H).** The rig's checkout was two commits behind and nothing was pushed, so the two
commits went over as a `git bundle` and a fast-forward to `5d75340`, the laptop's HEAD — same
SHAs, nothing through GitHub. Banks compared by digest first (`t-junction` 3f5918e1…, `curve`
18a33be6…, equal on both). The laptop half of 4.2 was re-run at the same commit
(`out/gate-head/`) and is byte-identical to the 2026-09-13 run in `out/gate/`.

| check | result | evidence |
|---|---|---|
| 4.1 | **met.** `scenariobank-sim:latest` built from `docker/Dockerfile` on the rig (~35 min; converter checkout present, fallback forced with `CONVERTER_DIR=/nonexistent`); the bridge rebuilt entirely from cache, same image; `status` ends `ready.`, label `sim gpu model` | rig `../build-sim.log`, `../build-bridge.log` |
| 4.2 | **met, with the finding.** 5/5 `ok` on both. The strict diff names `actions_digest` on all five rows and `reward` on four (≤ 1.5e-7 relative); `steps`, `status`, `route_completion`, `cost`, `collisions`, `failure_reason`, `actor_layout_digest` equal on every row. Cause isolated to the CPU, not the image: on the laptop the converter's image and the fallback give byte-identical files; on the rig the fallback and the converter's image give byte-identical files; across machines both pairs differ the same way. The digest hashes each action at six decimals (`runner.py`), so a last-place float difference in the expert's steering flips it and the closed loop carries it forward without changing the outcome. | laptop `out/gate-head/`, `out/gate-fallback/`, `out/gate-rig/results.json`, `out/gate-rig/results-converter.json` |
| 4.3 | **met.** Probe in the fallback image on the rig's card: six cameras, six buffers, engine loaded in **4.0 s**, forward pass median **131 ms** (laptop ~1.1 s), the model's known `result FAILED` at the end. Scored row: `ready` from the bridge, `status: ok`, 3200 steps, `max_step`, route 0.0664 (laptop 0.0659), three heartbeats at 30 s, **100.5 s** wall (laptop 815 s), 1 m 49 s end to end including the engine load; `results.json` root-owned. | rig `../av3-probe-rig.log`, `../av3-rig.log`, `out/av3-rig/` |
| 4.4 | **met, one-card form.** The `sim-run.sh replay` container's `HostConfig.DeviceRequests` is `DeviceIDs ["0"]`, capabilities `gpu`, user root; the replay exits 0 (1200 steps, the bank's cap). A CUDA process under the same `--gpus device=0` shows in `nvidia-smi` on `GPU-3a5ceef2…`, the rig's one card. The two-card claim is open until a rig has two cards. | rig `../card-check.json`, `../card-check.log` |

What the numbers mean for Phase 7: the engine loads in seconds on the rig, and a 3200-step row
takes under two minutes, so a lease `visibility_timeout` of a few minutes with an extend every
30 s (the heartbeat) covers a row with room to spare; the laptop's 815 s was the laptop's card.

What 4.2 changes: the gate's exact diff stays as the **same-machine** regression (it is exact
there, across images too). Across machines the claim is "every outcome field equal", and that
is what the studio can promise about a job scored on any rig. `actions_digest` remains what it
was built for — the same-machine determinism pin in `test_camera_rig.py` — and is not a
cross-machine identity. Nothing in code changes for this; Phase 7 Step 6's results notes say
which fields are comparable across machines. Nothing committed.

**Done when:** the diff is empty between the laptop and a rig (4.2), the read-only mount refuses
a write (1.3), and the studio image serves a page that can launch a job (2.3) — from the NAS's
own command, `docker compose up studio` (2.4). As a checklist:

| checks | proves | where |
|---|---|---|
| 1.1–1.3 | the script is the runner: guard first, same commit, no writes into the bank | laptop |
| 1.4–1.5 | results land, root-owned, with the host's time | laptop |
| 1.6–1.8 | the two per-card inputs work and the hang is gone | laptop |
| 2.1–2.3 | the studio image contains MetaDrive and writes as the host uid | laptop |
| 2.4 | the NAS's one command | NAS, after Open question 6 |
| 3 | the bridge answers on its own, nothing rebuilt | laptop |
| 4.1 | a rig has this repo and nothing else | rig |
| 4.2 | the bank is portable — the premise of submitting a job at all | rig |
| 4.3 | `scenariobank-sim:latest` runs what the converter's image ran | rig |
| 4.4 | a worker per GPU is real | rig |

Steps 1–3 green on the laptop can be re-run any time as a regression; 4.1–4.4 close the gate.
Nothing here touches the queue, the share or the agent: those are Phase 7's, tested against
Phase 7 Step 0's replica.


---

# Phase 6 — ~~`CONTRACT.md` and handoff~~ retired 2026-09-14

*(Retired with Keith, 2026-09-14. Since the studio is the only producer, the `Job` half has no
reader outside this repo; what still crosses to another team is the **result**. The reading rules
are the bullet list in **Phase 7 Step 6**, to become `CONTRACT.md` when the webapp's endpoint and
payload are known (Open question 7); `validate --results` is the agent's check in **Phase 7 Step 5**.
Nothing renumbered.)*

---

# Phase 7 — The rig agent ⬜  ⟵ *the deliverable*

**Goal:** a job the studio puts on the NAS queue is leased by the agent on whichever rig has a free
GPU, run in two containers there, and its results delivered back to the NAS — with nothing of
Tyrone's imported (R1).

*(Shape settled 2026-09-13. Nothing on the NAS dispatches and nothing on a rig listens: the queue
knows messages, not cards, so the only process that can know a card is free is the one holding it,
and that process is on the rig. So: **one agent container per rig, a worker per GPU, that locks a
card and only then leases** — and the queue is asked for last, at Step 7, because Keith has no
access to it yet; everything before runs against Step 0's local replica.)*

**The queue is not a table we write SQL against.** It is `wfqueue`, an HTTP service on the NAS
with lease/ack/nack semantics, documented in `docs/queue-docs/queue-doc-v0.json`, with a 392-line
stdlib-only Python client at `docs/queue-docs/queue-client-v0.py`. **Use that client. Do not
reimplement the HTTP calls** — it already handles leasing, ack/nack, retry backoff and
long-polling, and every one of those is a thing to get subtly wrong.

Read **How this ships** first for the topology and, in particular, for what the five words mean.

---

## Three properties of the queue, and what each one forces

These are not background. Each dictates a specific piece of code, and each is a silent failure if
missed.

**1. Delivery is at-least-once.** The queue doc says it outright: *"Leasing is at-least-once: make
handlers idempotent, or use `dedupe_key` upstream."* If our agent dies mid-run, the lease
expires, the message returns to `ready`, and it is leased again — **while the rig is still
running it**, possibly by the other rig's worker.

> **Forces:** the run is **keyed by job id and idempotent**. A worker that leases a job whose
> `results/<job_id>` already exists on the share acks without running; an agent that restarts adopts any container carrying
> our label for its card before it leases anything. Those two rules are what make redelivery
> harmless instead of a double-booked GPU, and why the agent can be restarted at any moment
> without a reconciliation dance.

**2. A lease is a clock, and our work is longer than it.** `visibility_timeout` defaults to 30 s
(`POST /topics/{topic}/lease`); a 35-scenario bank is minutes, and a 1,000-scenario one is far
longer.

> **Forces:** the worker calls `msg.extend()` on a timer for the whole run, and stops the
> instant the run ends. A missed extend does not lose the job — it *duplicates* it, which is worse,
> and property 1 is the only thing standing between that and two runs on one card.

**3. `nack` is not the same as failure.** A rig that is busy, or whose lock is held by CARLA, has
not failed. Dead-lettering after `max_attempts` is for jobs that are **wrong**, not jobs that were
**unlucky**.

> **Forces:** busy → `msg.nack(retry_after=...)`, so the job returns to `ready` and is tried again,
> on this rig or the other. Only a job that cannot ever run — a bad options file, a missing
> checkpoint, a validation failure — is allowed near the dead-letter pile.
>
> This remains **the single most likely wrong behaviour in this whole phase.**

---

## What the queue knows, and what it does not

Read off `docs/queue-docs/queue-doc-v0.json`, 2026-09-13, because a design was nearly built on the
opposite assumption. The queue knows **messages**: each is `ready`, `leased`, `done` or `dead`; a
lease takes an optional `consumer` label that is recorded on the message; `GET
/topics/{t}/messages?state=leased` lists every job someone holds, with that label. So it can say
what work exists, who has it, and what finished — and with a worker per card leasing one job at
a time and labelling itself `rigA:gpu0`, that listing *is* the "what is running where" view.

It has **no word for GPU, rig, worker, slot or capacity**. It cannot tell anyone a card is free.
A card is free only because the process that asks for work is the one holding it — which is why
the consumer lives on the rig, beside the lock, and not on the NAS. Two limits follow, and both are
about things the queue never sees:

- **CARLA jobs and hand-run scripts take the same cards through no queue of ours.** A card can be
  busy with zero leased messages in our topic. That is the only reason the lock file still exists.
- **A lease is a clock.** A job whose worker stops extending shows `ready` while its container is
  still driving. The extend timer is what keeps the queue's picture honest (property 2).

## The lock is rig-local, and CARLA shares the cards

Confirmed 2026-09-01: CARLA jobs run on these same two rigs. So the flock stays the authority, for
the reason it always was — things outside *both* queues take a card: `deployment/run_local.sh`,
hand-run scripts, and `free_gpu.sh --free`, which his attempt script runs as **root** with
`--pid=host`. Inside that container `id -un` is root, so its "spare a python3 if it is mine" rule
does not apply: **it will terminate a MetaDrive run holding the card.**

The split that follows, and it is the design:

- **There is no GPU map.** The worker holding the card is the only thing that leases.
- **The rig's lock is the truth.** Checked *on the rig*, because it cannot be checked anywhere
  else: `rig/lock.py:43,73-123` excludes by **inode** and reads liveness out of local `/proc/locks`
  plus `/proc/<pid>/stat` starttime. Host-local by construction. On an NFS mount it would not mean
  the same thing — so it never lives on the share — and from the NAS it cannot be seen at all.
- **A worker that cannot take its lock does not lease.** It sleeps and tries again; it never nacks
  for `busy`, because it never took a job. It does not tear anything down, and it never signals a
  holder it did not start.

---

## Files: a NAS share, names in the job, paths on the rig

*(2026-09-13, Keith: option 1 of three — the others were the bank inside the queue payload with a
results topic back, rejected on the first thing that is not small, and HTTP through the studio,
rejected as the most code and a rig that stops when the studio does.)*

| what | size | direction |
|---|---|---|
| a PG bank (`manifest.json` + thumbnails; maps rebuild from seeds at run time) | 200–400 KB | NAS → rig |
| an imported bank (carries the recorded dataset) | ~50 MB | NAS → rig |
| the checkpoint and `model_dev.yml` | GBs, changes rarely | by cron, already decided out of scope |
| `results.json` plus per-scenario records | < 1 MB | rig → NAS |
| films and trajectories, when a job asks for them | hundreds of MB to GBs | rig → NAS |

- **One share, mounted on both rigs, three roots:** `banks/<bank_id>/`, `models/`,
  `results/<job_id>/`. The agent reads them from `SCENARIOBANK_BANKS`, `SCENARIOBANK_MODELS`,
  `SCENARIOBANK_RESULTS` — as **host** paths, because the `docker run` it issues bind-mounts the
  host's filesystem, not the agent's own mount points.
- **A job carries names, never paths:** `bank.id`, a model name, `job_id`. The agent fills
  `bank.path`, `checkpoint_path` and `model_config_path` on the rig, so one job runs on either rig
  whatever its mount points. `Job.bank.id` is already checked against the manifest by the runner
  (Phase 4 Step 3), so the lookup needs no schema change.
- **The studio writes the bank to the share before `put()`** (Phase 2c Step 12), so the bank is on
  disk before the job is visible and no card idles on a transfer once it has leased. The worker
  mounts `banks/<bank_id>` read-only straight into the simulator; nothing is copied. (If the share
  ever proves too slow for an imported bank's 50 MB, a local copy is one `cp` in the worker and
  nothing else changes.)
- **Results: local run → deliver → ack, in that order.** The container writes to the rig's local
  `/out/<job_id>`; the agent then **delivers** — the first implementation copies to
  `results/<job_id>.partial` on the share and renames it to `results/<job_id>`, so a directory
  without `.partial` is always complete — and only then acks. A `results/<job_id>` that already
  exists means "done": ack without running. That is property 1's idempotency, now living here.
- **Two NFS traps.** The lock never lives on the share (above). A root-squashed export turns the
  container's root-owned write into a permission error, which is one more reason the agent, not
  the container, does the copy.
- Not decided: whether the rigs can mount the share, which protocol, and whether the checkpoint
  cron already uses it — Open question 8.

---

## Why the agent holds no state

Nothing calls the agent, so nothing listens. Three properties are
worth having and all three come from the same choice — **the agent holds no state of its own**:

- Every answer it gives is read back off disk: the log file, the exit-code file, and the
  per-scenario record directory. `rig/session.py:458-521` is the reference for this and is worth
  reading before writing it.
- So an agent restarted mid-run still describes that run correctly, and adoption after a restart is
  **the same code path** as a normal poll rather than a special case.
- And a run is owned by the Docker daemon, not by the service: `start_new_session` escapes a
  process group but not a PID namespace or a cgroup, so a 25-minute run must not be a child of
  anything that can be restarted. `rig/session.py:236-258` paid for that lesson.

**An agent that keeps progress in a variable breaks restart recovery silently.** Same reason
`rig/progress.py` has no table: progress is derived, never stored.

---

## Steps

Each is buildable and verifiable on its own, and the order is deliberate: **the run session
first**, because `scenariobank agent --once job.json` drives it from a file long before a queue is
involved; **the real queue last**, at Step 7, because there is no access to it yet. Step 0 comes
first only because the loop in Step 5 cannot be tested on a laptop without it.

### Step 0 — a local `wfqueue` replica ✅  *(added 2026-09-08; made runnable 2026-09-13; built 2026-09-14)*

The NAS is not routable from the development machine (`No route to host` on
`192.168.1.90:9090`, measured 2026-09-08), `wfqueue` is not installed here, and the colleague
shared the client, not the server. So before the agent's loop can be tested at all:

1. **Ask the colleague for the server** — the `wfqueue` package (the client's own docstring says
   `from wfqueue import QueueClient  # or from the installed package`, so one exists), its single
   file, or a compose service. That is the preferred bench: the real code, on localhost,
   `WFQUEUE_URL=http://localhost:9090`.
2. **Until it arrives, `tests/support/fake_wfqueue.py`**: a stdlib `http.server` + `threading`
   double of the *documented* contract only — `put` (with `dedupe_key`), `lease` (visibility
   timeout, `wait`, the `consumer` label), `ack`, `nack` (backoff, `retry_after`, `dead`,
   `max_attempts`), `extend`, `stats`, `list` (with `state=leased` and the label), `requeue`, and
   `409` on a stale `lease_id`. ~250 lines, never imported by `src/`. **Runnable, not only
   importable** *(2026-09-13)*: `python -m tests.support.fake_wfqueue --port 9090`, SQLite-backed
   so a restart keeps its messages, because Keith has no access to the real queue and the studio's
   submit (Phase 2c Step 12) and the agent (Step 5) are developed against it on the laptop with
   `WFQUEUE_URL=http://localhost:9090`. It exists to exercise *our* code, not to stand in for the
   queue in any claim: when the real server disagrees with it, the replica is wrong.
3. **One contract test file, two backends.** `tests/unit/test_queue_contract.py` runs the same
   assertions against the fake by default and against `$WFQUEUE_URL` when it is set (a
   `needs_queue` marker in the house per-file style, no `conftest.py`). The assertions are the
   four Phase 7 properties in miniature: an expired lease is redelivered with `attempts == 2`;
   `ack` with a stale `lease_id` is `409`; `nack(dead=True)` lands in `dead` and `requeue`
   returns it; a second `put` with the same `dedupe_key` returns `duplicate: true` and one
   message. **When the real server disagrees with the fake, the fake is wrong** and is corrected;
   that is the whole discipline, and it is why the file is under `tests/` and not `src/`.

**Verify alone:** `uv run pytest tests/unit/test_queue_contract.py -q` green offline; the same
with `WFQUEUE_URL` pointing at the colleague's server (or at the NAS, from a machine that can
route to it) green with the marker taking effect. Step 7's round trip stays the gate and stays
on the real NAS — and is the **first and only** step that touches it.

**Done 2026-09-14.** Items 2 and 3 are built; item 1 (the colleague's server) is still to ask for,
and until it arrives every "real" assertion below has only ever run against the fake.

- `tests/support/fake_wfqueue.py` (700 lines, more than the ~250 guessed: half of it is the
  input validation the doc's `400` row implies). `FakeQueueServer` is a `ThreadingHTTPServer`
  with a `Store` behind it — one SQLite connection, one lock, one `Condition` that a long-polling
  `lease` waits on and every `put`/`nack`/`requeue`/expiry notifies. Expired leases are swept
  lazily on every read and write (`_reap`), so no timer thread. **Eighteen of the doc's
  twenty-four endpoints**, including `GET /`, `/health`, `/topics`, `/source/client.py`, topic
  delete with `?purge=true`, batch put, and `/admin/reap`; errors are the doc's
  `{"error", "status"}` shape. The six left out are the human-facing ones — the HTML console at
  `/admin` and `/admin/list`, its form posts `/admin/delete` and `/admin/compact`, and key
  management at `/admin/keys` and `/admin/keys/revoke` — which the doc itself calls "for humans,
  not for code" and nothing of ours will call. `python -m
  tests.support.fake_wfqueue --port 9090` serves on a file (default `.studio/fake-wfqueue.sqlite`,
  already ignored); `port=0, db=":memory:"` is what the test uses. `pyproject.toml` gained
  `pythonpath = ["."]` under pytest so the test imports `tests.support` the way the terminal
  runs it.
- `tests/unit/test_queue_contract.py`: nine assertions, each parametrised `[fake]` and `[real]`,
  the `real` half behind `needs_queue` (skips without `WFQUEUE_URL`). Driven through
  `docs/queue-docs/queue-client-v0.py`, loaded by path so the copy stays byte-identical. Each
  test makes and deletes its own `contract-test-<hex>` topic, so a shared server is left as
  found. The four the step asked for (expired lease → `attempts == 2`; stale `lease_id` ack →
  409, and a done message acks 409 too; `nack(dead=True)` → `dead` with `last_error`, `requeue`
  brings it back; same `dedupe_key` → `duplicate: true`, one message, first payload stands) plus
  four the agent's loop rests on: `retry_after` hides the message until then; `extend` outlives
  the original timeout; `list(state="leased")` carries the `consumer` label; a `wait=5` lease
  returns the moment a `put` lands (0.3 s, not 5). The ninth *(added 2026-09-15)* fetches
  `/source/client.py` and executes it, asserting it defines the `QueueClient` this file drives —
  not asserting it byte-identical to ours, because the real server stamps a served copy with its
  own address. Against the real server that is the check that catches our vendored copy having
  drifted from theirs.
- **Where the doc is silent, the fake's choice is a guess**, listed in its docstring for Step 7
  to check against the real server: `attempts` counts every lease including the re-lease after
  expiry; nack backoff `2 ** (attempts - 1)` s capped at an hour; an expired lease with
  `attempts >= max_attempts` goes to `dead`; `requeue` resets `attempts` to 0; the label's key is
  `consumer`; `stats` is `{"topic", "counts", "depth", "oldest_ready_age", "next_available_at"}`;
  **`created_at` and `updated_at` are on every message row and are named nowhere in the doc**, so
  nothing of ours may read them until the real server is seen to send them (the other thirteen
  field names are the doc's own, and the client reads five of them off a leased message);
  and **`lease`/`list`/`stats` on a topic nobody has created answer 404**, the doc's error table
  read literally. That last one reaches Step 5 and Phase 2c Step 12: the agent and the studio
  each call `create_topic("metadrive")` once at startup (idempotent) rather than assume the
  other side went first.

| check | result |
|---|---|
| `uv run pytest tests/unit/test_queue_contract.py -q`, offline | 9 passed, 9 skipped (`needs_queue`), 2.33 s |
| the same with `WFQUEUE_URL` at the standalone fake on a file DB | 18 passed, 4.13 s |
| `GET /source/client.py`, saved and diffed against the vendored copy | `text/x-python`, 15,152 bytes, identical |
| put on `metadrive`, kill the server, restart on the same file | topic and message still there; leased with `attempts == 1`; acked → `done` |
| `lease` on a topic never created | `404 {"error": "no such topic: …"}` — the guess above, so the agent creates its topic first |
| `ruff check` on both files | clean |
| the by-hand round trip, on a database already holding a stale `ready` message | `purge`, `put`, `lease`, `ack` → `done: 1` |

The worked round trip is written up in `docs/running-the-application.md`, "The queue replica",
with the trap that cost a session: **a lease returns the oldest `ready` message, not the one just
enqueued**, so both the id and the `lease_id` must come from the lease reply. An ack naming any
other message is `409`. This is at-least-once delivery seen from the outside, and it is what
Step 5's loop has to be written against.

### Step 1 — the container image and entrypoint ✅  *(built 2026-09-15)*

Extends Phase 5. The container reads a `Job` file (Phase 4 Step 3's model: the same JSON the
queue carries), calls `run_bank()`, writes `results.json`, exits 0. Four additions, all so a supervisor never has to parse prose:

**Where the image comes from** *(Keith, 2026-09-13)*: **the rig clones this repo and builds both
images from it** — `bridge.sh build` for the bridge, `sim-image.sh build`
for the sim image, which with no converter checkout beside the repo builds `docker/Dockerfile` as
`scenariobank-sim:latest`; `SIM_IMAGE=scenariobank-sim:latest` in the agent's environment. No
converter checkout, no `docker save`. The line the agent runs is `scripts/sim-run.sh`'s (Phase 5
Step 1). The model on the GPU in that image was verified 2026-09-14 on the first rig (Phase 5
Step 4, checks 4.3 and 4.4): probe, then a scored AV3 row, `status: ok`; and the rig is the
one Phase 5 measured, so the numbers there are what the lease timer is sized from.

- **Structured JSON lines on stdout.** One object per event. No regexes — his `rig/progress.py`
  scrapes four prose patterns out of CARLA's log because it has no choice; we do not.
- **A per-scenario record directory and its exit-code file**, written when each scenario starts and
  ends. These two files are the progress signal, so a bar moves without anything reading the log.
- **Teardown inside the launched script**, not the supervisor — a dead runner must still bring the
  stack down and still record exit codes.
- **The stop is `run_bank`'s, not the entrypoint's.** SIGTERM from `docker stop` reaches the
  flag Phase 4 Step 3 installs; the run ends `stopped`, writes what it scored, and closes the env
  on the normal path. The `KeyboardInterrupt`-into-`env.close()` wedge and the stop-timeout
  budget are described there *(moved 2026-09-08)*. The entrypoint adds nothing but the file read.

**Verify alone:** `sim-run.sh` it by hand with a one-scenario `Job` file; get a `results.json`;
then the probe inside `scenariobank-sim:latest` on a rig's GPU.

**Done 2026-09-15.** All four additions are in, and the first finding is that **the image did not
change at all**: `docker/Dockerfile` is untouched. The image carries the environment and this
repo is mounted at `/work`, whose `src/` is the venv's one path line, so the entrypoint is code
and code arrives with the mount. "The container reads a `Job` file" was already
`run --job` (Phase 4 Step 3); what this step built is what it *writes* while it does.

- `src/scenariobank/events.py` (319 lines, new): six events — `run.started`, `batch.started`,
  `scenario.started`, `scenario.finished`, `heartbeat`, `run.finished` — as pydantic models with
  `extra="forbid"` and a `schema_version` on every line, beside `Emitter` (append to a file,
  print to a stream, or both), `write_exit_code`/`read_exit_code` and `read_events`. It imports
  no simulator, no runner and no CLI, so a rig agent reads a run's events with `scenariobank`
  installed and nothing else. Field order is pydantic's rather than sorted, so `event` is the
  second key a `tail` shows; `dump_json`'s sorted, indented shape is for the files.
- `runner.run_bank(on_event=…)`: the same four moments, with the job's id and attempt stamped on
  every event in one place — `emit()` — because they are the job's and `run_bank` is what holds
  it. `<out>/batch.json` and `<out>/starts/<id>.json` are the first two events written as files,
  **whether or not a sink is given**, because they are the record and not the stream.
  `<out>/results/<id>.json` has been the third since Phase 4 Step 3. So the bar is the directory:
  denominator `batch.json`'s `n`, numerator the file count in `results/`, and the row running now
  is the one in `starts/` with no result beside it. Nothing is held in memory, which is the
  property an agent restarted mid-run rests on.
- **The heartbeat is one reading said twice.** `runner.Heartbeat` now makes an
  `events.Heartbeat` and renders the prose *from it*, so the line a person reads and the object
  a supervisor parses cannot disagree about how fast the car was going — which is the whole
  complaint against scraping the prose. The prose format is unchanged, to the space.
- `cli.run --events` prints the stream on stdout and takes the summary line off it; the file is
  written either way. The two **process** files — `events.jsonl` and `exit_code` — live in the
  directory `--out` named, while everything the **batch** writes goes under the job's tier
  subdirectory when it names one (`--out out/j7` + `"tier": "hard"` → `out/j7/hard/results.json`).
  A supervisor must find an exit code without knowing the job's contents; Step 3 reads
  `<job dir>/exit_code` and `cli.under_out(out, job.options.tier)` for the rest.
- **The exit code is written whatever happened**, staged and renamed like wing-sim's launch
  script, so a half-written file can never be read as a `0`: `0` ran (a stopped run included),
  `1` failed, `2` the command line was wrong (click's own code, and the file agrees with the
  process). The directory is created and the emitter opened *before* any validation, so a job
  file that will not parse still leaves an exit code where the agent looks for one. **No file at
  all is the answer "killed outright"** — SIGKILL, the OOM killer, the machine going down — which
  is wing-sim's `VANISHED` and is why `read_exit_code` returns `None` rather than raising.
- **`run.finished` carries the one judgement a supervisor acts on**: `permanent: true` means
  nothing ran and nothing this machine does will change that (the bank at that path is a
  different bank, a scenario id is not in it, the policy will not import), so the job is
  dead-lettered rather than spending its attempts. It is not a guess — the emitter has seen
  whether `batch.started` went by. A failure after the first env was built is not permanent: it
  may be the card, the driver or the bridge, and those are worth another rig.
- **The third bullet, teardown, is Step 3's and not this step's.** There is no compose stack
  inside our container — the container *is* the run — so the teardown that must not live in the
  supervisor is the entrypoint's own `finally` (the event, then the exit code) plus `--rm`.
  The launched-script half arrives with the session that starts a bridge beside a simulator.
- Where the doc is silent about nothing, but two choices are ours and are worth naming for Step 5
  to agree with: `events.jsonl` is **appended, never truncated**, so a second attempt landing in
  the directory the first one used keeps both streams and every line carries its `attempt`; and a
  heartbeat's `speed_mps` is `null` and never `nan`, because `json.dumps` writes that as the bare
  token `NaN`, which is not JSON and which a strict reader refuses the whole line over — the line
  it would refuse being the one that says the car has stopped.

| check | result |
|---|---|
| one-scenario `Job` through `sim-run.sh`, `NO_GPU=1`, on the laptop | exit 0; five events on stdout; seven files under `out/j7` |
| the same run's `results.json` | `t_junction_0000`, arrived, 139 steps, 0.788 s |
| `pid` on the `run.started` line | `1` — the run is the container's init, so `docker stop`'s SIGTERM reaches it |
| `docker stop -t 30` on a five-scenario job, after the second row started | both rows kept, second `failure_reason: "stopped"`, `success_rate: 0.5`, `"stopped": true`, **exit 0**, `exit_code` file `0` |
| a refusal (`--scenarios nope`) | `run.started` then `run.finished` `permanent: true`; no `batch.json`; no env built; `exit_code` `1` |
| a job file that will not parse | `run.finished` alone, `permanent: true`, `exit_code` `1` |
| a failure after the batch started (observation shape moved) | `permanent: false`, `results.json` still named and present |
| `--job` with `--bank` (a usage error) | process `2`, file `2`, `permanent: true` |
| `tests/unit/test_events.py` (new, 14), six added to `test_results.py` with two there extended, two added to `test_runner.py` | green offline, no simulator |
| `ruff check`, and `docs/reference/commands.md` regenerated | clean; `--events` in the reference |
| the probe inside `scenariobank-sim:latest` on the rig's RTX 5080 (`GPU=0`), 2026-09-15 | commit `85e5dad`, obs `Box(-0.0, 1.0, (19,))`, `drive_side: left` |
| the same one-scenario job on that card | five events, the same seven files, `exit_code` `0`, `t_junction_0000` arrived in 139 steps (0.331 s, against the laptop's 0.788 s) |
| the rig's scored row against the laptop's | equal in every field but `actions_digest`, `reward` (sixth decimal) and `wall_time_s` — Phase 5 Step 4's cross-machine finding, not this step's |

**Done on the rig too, 2026-09-15.** Committed as `90a361d`, pulled on the first rig
(`116.12.220.99`, one RTX 5080, checkout clean at `5d75340` before the pull, no compute process on
the card), then the probe and the same job file on `GPU=0`. The entrypoint behaves there exactly
as here: the same five events, the same seven files, `exit_code` `0`. The image was untouched by
this step and needed no rebuild, which the probe reporting the same commit confirms.

The worked round trip, the file table and the two traps are in
`docs/running-the-application.md`, "One job, one container".

### Step 2 — the lock helper (R1: our own, same paths) ✅  *(built 2026-09-15)*

Advisory `flock`, **exclusion by inode**, one lock file per GPU. ~150 lines.

- **Name it per device** — `.wing-sim.gpu<N>.lock` — and record, for Tyrone, that wing-sim's
  single `.wing-sim.gpu.lock` serialises a whole rig. *Open question for Tyrone; do not assume he
  will change it.* Until he does, a two-GPU rig behaves as a one-GPU rig whenever CARLA is running.
- **Publish holder identity as a separate file.** Atomic replacement is a rename, and a rename gives
  the path a new inode, voiding every outstanding lock — so never write into the lock file itself.
- Confirm acquisition by finding the launched process in `/proc/locks`, rather than trusting a
  return value.

**Verify alone:** hold it from a shell (`bash wing-sim/deployment/with_rig_lock.sh sleep 60 &`),
confirm the helper reports it foreign and refuses.

**Done 2026-09-15.** `src/scenariobank/agent/lock.py`, and with it the `agent/` package. It
imports `scenariobank.events` (for the one timestamp format every record here is stamped with)
and nothing else of ours, so the dependency runs one way: the agent launches runs, and must keep
working when the run it launched has died.

**The finding that shaped the file: the rig lock is taken SHARED.** Two GPUs on a rig are two
resources and the rig is one, and both are true at the same time -- a CARLA evaluation takes the
whole machine, one of our runs takes one card. That is a reader-writer relation, so a run holds
**two** locks: `.wing-sim.gpu.lock` shared and `.wing-sim.gpu<N>.lock` exclusive, rig first, card
second, the same order everywhere. Measured rather than assumed: with one shared holder, an
exclusive `flock -n` is refused and a second `flock -s -n` is granted. This is what makes the
per-device name *work* rather than merely exist -- a per-card lock alone would let us start
beside a live CARLA stack, which is worse than serialising. And if Tyrone ever does name his per
device, he takes `.wing-sim.gpu<N>.lock`, this exact path, and our shared rig lock keeps working
untouched. **The ask for him is one sentence:** take `.wing-sim.gpu<N>.lock` for the card you are
using, and keep taking `.wing-sim.gpu.lock` (exclusive) for anything that needs the machine.

- **`flock(2)` is the authority and `/proc/locks` is a witness -- and in a container a partly
  blind one.** `locks_show()` skips every row whose pid it cannot translate into the reader's
  namespace, so **a container sees the locks taken inside it and none of the host's**. Measured
  on the rig with `scenariobank-sim:latest`, against a host holding 18 locks: without `--pid
  host` it is refused correctly, reads **0 rows** before locking and **2** after -- its own, both
  naming pid `1`; with `--pid host` it reads 23 then 25, naming its real host pid. *(The first
  version of this bullet said "zero rows for the whole machine". That was the before-state only,
  and the rig disproved it -- the confirmation passes in a namespace because our own row is
  always there.)* So **acquisition is always an attempt and never a look**, which is answered
  correctly across namespaces, and **the agent's container needs `--pid host`** -- not to prevent
  a double-booking, which `flock` prevents anyway, but so that a **foreign** holder can be named
  at all. `compose.yaml`'s `agent` service carries `pid: host`. The device:inode key is identical
  on both sides of the mount, because a bind mount shares the superblock -- that part was the
  risk, and it is measured, not argued.
- **Confirming the lock has three outcomes, not two.** Our row is in `/proc/locks` (confirmed);
  no rows at all (blind -- keep the lock, `confirmed=False`, and a note naming `--pid host`);
  rows but not ours (a contradiction -- `LockError`, both locks dropped, and the message asks
  whether the lock directory is on a network mount, which is the likeliest cause since `flock(2)`
  on NFS or SMB is emulated and excludes nobody). A blind witness must not refuse a run: the
  `flock` that prevents the double-booking works there anyway.
- **`Busy` is not a failure.** It carries a `Holding` with three verdicts and no fourth -- `ours`
  (a live record of ours matches the pid holding it), `foreign` (we can see, and it is not ours),
  `unknown` (we cannot see). Nothing was taken, so there is nothing to release, nothing to nack
  and nothing to report upstream: the worker sleeps and asks again (Step 5). And a card refused
  **gives the rig lock straight back**, because holding it alone locks CARLA out of a machine we
  are not using.
- **It never blocks.** No `WAIT_FOR_GPU`, no `flock -w`. A worker waiting on a lock is a second
  queue -- not FIFO, invisible to the real one, and able to overtake it -- so a held lock is a
  sleep in the worker's own loop where the wait can be seen.
- **The holder record is a separate file** (`.scenariobank.gpu<N>.holder.json`), written with
  mkstemp + fsync + rename and chmod 0644 because the rig is shared. The rename is the whole
  reason it is not the lock file: it gives the path a new inode, and a lock file that is replaced
  excludes nobody, in complete silence. Named ours rather than `.wing-sim.*` -- the lock paths are
  shared, the record format is private, and a file that looks like his but parses like ours is an
  afternoon of somebody's time. It is published at acquisition with `job_id: null` (the card is
  taken *before* the queue is asked for work), republished when the job is leased, and deleted on
  release: a record with no lock is history.
- **A dead pid in `/proc/locks` is never a stale lock.** The kernel records the pid that created
  the open file description, and a child that inherited the descriptor keeps the lock alive long
  after that pid exits -- `flock -n 9` in a sourced shell leaves exactly such a row. The only safe
  reading of a row is "still locked", and nothing here deletes, steals or breaks a lock. A record
  is believed only when the pid holds the lock, on this boot, for a process created at the
  recorded moment; anything less is `foreign`, which is a perfectly good answer to wait on.

| check | result |
|---|---|
| `bash wing-sim/deployment/with_rig_lock.sh sleep 60 &`, then `acquire()` | refused: scope `rig`, verdict `foreign`, naming his pid; nothing published, card untouched |
| we hold card 0, then `with_rig_lock.sh true` | his exit **99**, *"the GPU is in use — this run did NOT start"*, naming our pid |
| card 1 while card 0 is held | taken -- two cards at once, CARLA still shut out of both |
| both released, then `with_rig_lock.sh true` | exit 0 |
| `scenariobank-sim:latest`, lock held on the host, no `--pid host` | `flock` refused (correct); `/proc/locks` **0 rows for the machine** |
| the same with `--pid host` | refused; 436 rows, ours among them; same `103:03:9460376` key as the host |
| the agent image (`--pid host`, `SIMULATION_ROOT=/simulation`) while the host held the rig lock | refused, scope `rig`, verdict `foreign`, naming the **host's** pid |
| the same container holding card 0, then `with_rig_lock.sh` on the host | exit 99. It printed no pid: our container is root and his `fuser` cannot read another user's descriptors, so the holder file is what names us -- worth knowing on a shared rig |
| **on the rig** (`sim`, RTX 5080, `$HOME` on ext4), `test_lock.py` inside `scenariobank-sim:latest` | 20 passed, 2.02 s |
| a container there holding card 0 (`--pid host`), probed from the rig's own shell | exclusive on the rig lock **1** (their GitLab job's syscall), shared on it **0**, exclusive on card 0 **1**; after release both **0** and the record gone |
| the host's `/proc/locks` during that hold | `FLOCK ADVISORY READ 619155 103:02:37879864` and `FLOCK ADVISORY WRITE 619155 103:02:37879865` -- the container's own pid, both inodes. The cross-boundary check the laptop cannot make |
| the record the container wrote onto the rig's disk | `job_id: rig-check`, `pid` 619155, mode 0644; the lock files 0666 |
| the same container **without** `--pid host` | 0 of the host's 18 rows, its own 2 after locking (pid `1`), `confirmed=True`, and a second taker still refused |
| `tests/unit/test_lock.py` (new, 20) | green offline -- no simulator, no GPU, no docker; every exclusion assertion made from a second process |
| the offline suite | 843 passed, 9 skipped, 457.15s |
| `ruff check src tests scripts` | clean |

`docs/running-the-application.md` gained **The two locks, and the rig you share**: the table, the
two things an operator sees on a rig, the by-hand "who has card 0", and `--pid host`.
`compose.yaml`'s `agent` service was corrected with it -- it mounted a lock directory of its own,
`/var/lock/scenariobank`, which excludes nobody; it now mounts the rig's `SIMULATION_ROOT` and
runs `pid: host`, both pinned by `test_images.py` so neither can drift back.

**Done on the rig too, 2026-09-15.** Committed as `2d78bf5` and pulled on the first rig, which
turned out to be a **GitLab runner host for wing-sim** (`runner-czkjgi56y-project-84835806-…`,
their `wing-sim-*:prod` images) with no wing-sim checkout and no `~/simulation` at all -- so the
lock directory was created there by us, and the exclusive taker in the checks above is `flock -n`
making the same syscall on the same inode their CI job makes. The one finding is the corrected
bullet above: a container is not blind to its own locks, only to the host's.

### Step 3 — the run session, driven by a job file (R1: our own) ✅  *(built 2026-09-15)*

Takes the lock, starts this card's bridge if it is not up, launches the run as a sibling container,
supervises, delivers, tears down. ~300 lines. His `rig/session.py` is the reference, but it takes
`presets=` and emits CARLA compose commands, so this is a sibling rather than a reuse.
**Testable with no queue at all**: `scenariobank agent --once job.json` runs one job from a file
through the whole session on the laptop (`NO_GPU=1`, `ExpertPolicy`), which is how the rig half is
proven before Step 5 wraps it in a loop.

- **Sibling container, not a detached child** — see above.
- **Never inherit the environment wholesale.** His `rig/compose.py::child_environment` returns only
  `HOME/PATH/HEADLESS/QUALITY/COMPOSE_MENU`, and the reason is that a developer's exported setting
  otherwise silently changes what a model is scored on.
- **Own compose project name, container prefix and labels**, so a stray-container sweep on either
  side can never reach the other. Label with the job id and the attempt, as he does — that is what
  makes a sweep able to tell whose container it found.
- **One bridge per running simulation** (Phase 5 Step 3: the server holds one connection). The
  worker starts `bridge-gpu<N>` on `BRIDGE_PORT` = **5600 + N** — gpu0 on 5600, gpu1 on 5601
  *(Keith, 2026-09-15)* — and passes it to the simulator as `run --bridge-port`. The zapeta
  bridge listens on 5558 in wing-sim's stack too and both use host networking, so **the base port
  is deliberately not 5558**: a collision with his is then an error rather than a wrong number,
  and on a shared rig the two stacks can run at once on different cards. `bridge.sh` needs no
  change — it reads `BRIDGE_PORT` and `BRIDGE_NAME` already.
- **The bank root is a plain directory** *(Keith, 2026-09-15)*: `SCENARIOBANK_BANKS`, as
  `compose.yaml` declares it. Whether that path is a mounted share or a local directory is the
  deployment's business and not this step's (Open question 8), which is what makes
  `agent --once job.json` runnable on the laptop and on a rig with nothing mounted.
- **The lock directory is a deployment prerequisite, not code.** `~/simulation` on the first rig
  is owned by `metadrive`, mode 755 *(measured 2026-09-15)*. wing-sim's script creates the rig
  lock with `umask 000`, but it still needs write permission on the **directory** — so if their
  GitLab runner runs as another user, one of the two sides cannot create the file at all. That
  directory wants mode 777, or an agreed owner, before Step 5 runs unattended.
- **Validation before the card**: the payload parses as a `Job`, the bank id is under the bank
  root and its manifest carries that id, exactly one checkpoint under the models root with a matching
  suffix, and any uploaded `modifiers.py` **parsed to AST and never imported**. A job that fails
  this can never run and is dead-lettered (Step 5), never retried.

**Verify alone: met.** One scenario end to end from `agent --once job.json`: the lock taken, the
bridge up on its card's port, the container run, `results/<job_id>` renamed into place, the lock
released.
No queue anywhere yet.

**Done 2026-09-15.** `scenariobank agent --once job.json` is the whole session: validate, take
the card, start that card's bridge if the job needs one, launch the run as a sibling container,
follow it, deliver, release. Two modules, one command, five opt-in inputs on `sim-run.sh`, and
one latent bug in `compose.yaml` found by building it.

- `src/scenariobank/agent/jobs.py` (330 lines, new) — everything that happens **while the card is
  still free**. `Roots` (the four directories a rig works in), `resolve()` (the checks, and the
  rewrite into the container's own paths), `JobRefused` (a verdict: this job cannot run here or
  on the other rig either). `Resolved` keeps the container's paths as `str` and the host's as
  `Path`, deliberately: the failure they cause is silent — a bind mount the daemon cannot resolve
  creates an empty directory and the run dies on a missing manifest, four minutes and one card
  later.
- `src/scenariobank/agent/session.py` (600 lines, new) — `RunSession`: `adopt` / `ensure_bridge` /
  `launch` / `supervise` / `harvest` / `deliver` / `stop`, plus `Progress` (derived, stored
  nowhere) and `Outcome` (six verdicts). Every subprocess goes through one seam, `Commands`, so
  the tests drive the real code against a dictionary for a daemon.
- `cli.agent --once` — the command, and the exit codes a queue worker will read: **0** ran (a
  stopped run included), **1** the run or the rig failed, **2** the command line, **3** refused
  and to be dead-lettered, **4** the card is busy. `agent` with no `--once` refuses and names
  Step 5, so the compose service says what is missing rather than crashing.
- `scripts/sim-run.sh` — five new environment inputs, every one a no-op when unset, so the line
  a person runs is byte-for-byte what Phase 5 verified: `REPO_DIR` and `BANK_DIR` (host paths),
  `DETACH`, and `JOB_ID`/`ATTEMPT` for the labels. Every container it starts now carries
  `scenariobank.managed-by`, hand-run ones included — a sweep for strays should find those too.

**Three views of every path, and two of them are not this process's.** The agent starts siblings
through the docker socket, so a sibling's `-v` is resolved by the **daemon on the host**; the
agent must also **read** those same directories itself, to validate a bank and to copy the
results; and the run sees a third set (`/bank`, `/out`, `/models`). The share and the local out
directory are therefore mounted into the agent container **at their own host paths**, which
collapses the first two into one variable — wing-sim's trick with its simulation root
(`rig/session.py:296`), for the same reason. The repo cannot be: it must be at `/work`, because
the image's editable install is the single path line `/work/src`. So its host path travels in the
environment as `SCENARIOBANK_REPO` and `sim-run.sh` reads it as `REPO_DIR`.

**And that is how two real bugs in `compose.yaml` were found: the `agent` service could never have
started, twice over.** It mounted the socket, the share and the lock directory, and not the repo —
so `python -m scenariobank agent` in that container would have failed on `import scenariobank`,
because the image carries the environment and this repo carries the code. And **neither sim image
has the docker CLI** (measured 2026-09-15 on both), so the agent had a socket it could not speak
to and `sim-run.sh` would have died on its own first line. Declared in Phase 5, never started, and
nothing would have said so until a rig tried it.

The second one moves an image decision. `docker/Dockerfile` now installs a pinned **static docker
client** — client only, no daemon, ~35 MB — and the `agent` service names
`${AGENT_IMAGE:-scenariobank-sim:latest}` instead of `${SIM_IMAGE}`. That is the one place the two
part company, and the reason is ownership: the agent needs the client, the converter's image does
not have it, and that recipe is not ours to change while `docker/Dockerfile` is. `SIM_IMAGE` still
selects what the **runs** use — the agent passes it through to `sim-run.sh` — so a rig can run the
converter's image for the work and ours for the supervisor. Still no fourth image: a rig builds
this one and the bridge, exactly as Step 1 says. The agent also refuses at startup, by name, when
there is no client on its PATH, because inside a container the remedy is "rebuild the image" and
not the one `sim-run.sh` prints for a person on a host. A second label, `wingfin.tools`, records
what is in there beside the venv.

**The card is held by the agent, not by the container, and that is a deliberate difference from
the reference.** wing-sim makes `flock` the container's own command (`rig/session.py:307`), so the
lock dies exactly when the run does. We cannot: `flock file python …` leaves `flock` as pid 1, and
pid 1 is what makes `docker stop` reach the batch's flag and turn a killed run into a **scored
partial** one (Step 1). That property is worth more than closing the window, and the window is
what the rest of the design already covers — measured on the laptop: kill the agent and the run
keeps driving with the card free, and the restarted agent **adopts it by label** rather than
starting a second. `restart: always` makes the gap seconds; what can still slip into it is a
CARLA job taking the rig lock while a run of ours is driving, which is a two-line note for
Tyrone's side of Open question 10 and not a thing this code can fix alone.

**Detached must not be `--rm`.** An exited container is how an agent that was down when the run
*ended* finds out that it ended, and where its stdout still is. Measured both ways: killed while
the run was on row 1, the restarted agent picked it up at 3/5 and delivered at 5/5; killed and
restarted after the run had finished, it read the exited container's code, kept its log and
delivered. Exactly one `run.started` in both — one per container, which is the number to check.
`harvest()` is the only thing that removes a run container.

**The gap between the exit code and the container, closed.** The run writes `exit_code` and then
exits, so the two happen in that order — but the supervisor's read and its liveness check do not,
and a run that finishes between them leaves a file that exists and a container that has stopped.
wing-sim reported a job **failed** with "lock released with no exit code recorded" after every one
of its presets passed. The fix is one more read before concluding anything, and a test that makes
the container stop *during* the check.

**Validation before the card, and what it refuses:** no `job_id` (the results directory, the
container and the redelivery guard all need one, and a rig cannot mint one — two rigs would mint
two); no `bank.id`; a bank id that resolves outside the banks root; a bank that is not on this
share; a manifest that disagrees about which bank it is; a scenario id the bank does not hold; a
policy that is not a `pkg.mod:Name`; and a checkpoint or config name that matches **zero or more
than one** file under the models root. The policy is checked for shape only — importing it to see
whether it loads would pull torch and a CUDA context into the supervisor, and the run already
reports `permanent: true` when it will not.

Three more notes, so nothing here is silent:

- **The `modifiers.py` bullet has no input today.** `Job` carries no such field — the AV3
  preprocessing is ported into `src/scenariobank/av3/av3_model.py` (Phase 4 Step 7) rather than
  uploaded — so there is nothing to parse to AST. What stands in its place is stronger: the agent
  imports nothing from the models root at all, and resolves names under it to exactly one file.
  If a submission ever carries code, this is the line that must grow the AST check.
- **A job carries names; `JobBank.path` is still required by the model.** A submitter must write
  *something* there and the agent ignores it, reading `bank.id` and rewriting the path to
  `/bank`. Making the field optional is a one-line widening of the schema and belongs with the
  studio's submit (Phase 2c Step 12), not here.
- **The local out directory is never swept.** The container writes as root, so `out/<job_id>`
  outlives the job and, on a laptop, cannot even be removed by the user who started it. One
  directory per job on the rig's own disk, and films are hundreds of MB — a sweep (by age, and
  only for a `job_id` already delivered) belongs in **Step 5**, where the loop that creates them
  lives. Added to that step's list.

| check (laptop, `scenariobank-sim:latest`, `--no-gpu`) | result |
|---|---|
| one scenario, `agent --once` | `completed`, `1/1`, delivered, 7.0 s |
| what was delivered | 8 files, including the resolved `job.json` and the container's log |
| the same job again | `already delivered`, exit 0, no card taken, nothing run |
| a scenario id the bank does not hold | exit **3**, named, and **no holder record** — the card was never taken |
| the card held from another shell (`flock -x`) | exit **4**, naming the foreign pid; card 1 still free |
| killed mid-run, restarted | adopted at 3/5, delivered 5/5, **1** `run.started` |
| killed after the run finished, restarted | adopted the exited container, delivered, **1** `run.started` |
| SIGTERM to the agent, two rows in | `stopped`, 3 rows kept, `failure_reason: "stopped"`, exit_code 0, delivered, no container left |
| `scenariobank.av3:BridgePolicy` | `bridge-gpu0` started on **5600**, 320 steps / 320 actions through it, delivered |
| the same again | the same bridge container reused, not restarted |
| wing-sim's 5558 bridge, throughout | up and untouched — which is the whole reason the base port is 5600 |
| two agents, `--gpu 0` and `--gpu 1` | both cards `ours`, two containers, two results |
| a job with `"tier": "hard"` | batch under `hard/`, `events.jsonl` and `exit_code` above it |
| `agent` with no `--once` | refuses, naming Step 5 |
| `tests/unit/test_agent_jobs.py` (new, 26) + `test_session.py` (new, 37) | 63 passed |
| `tests/unit/test_images.py` (3 assertions added, 12 for `sim-run.sh`) | 7 passed |
| `ruff check src tests scripts`, `docs/reference/commands.md` regenerated | clean |
| the offline suite, minus the two files of Open question 12 | **907 passed, 9 skipped, 440.64 s** (843 before this step) |
| the new `docker/Dockerfile` layer, built on its own | 38 MB, `Docker version 27.3.1`, and `docker ps` through the mounted socket |

**The image itself was built on the rig and not here**, which is the standing rule and was also
forced: a full rebuild on the laptop is a 4 GB wheel resync (`COPY src` invalidates the sync
layer, so any source change costs one), and this 16 GB machine ran out of memory 50 minutes in
with the other projects' containers up — the same constraint Phase 4 Step 7 note 9 recorded.
What was verified here instead is the only thing the recipe gained: the docker client layer,
built alone on `ubuntu:22.04`, run, and pointed at the host's socket.

One thing the verification itself taught: **`uv run` wraps the agent**, so killing the pid that
`uv run scenariobank agent` returns kills the wrapper and leaves the agent holding the card. The
lock's own holder record is what names the real pid. In the container the agent is pid 1 and there
is no wrapper; on a laptop, read the record.

**Done on the rig too, 2026-09-15**, and this is where the container shape was first exercised:
the agent in its own container (`docker compose --profile rig run --rm agent`), starting a sibling
on the RTX 5080 through the mounted socket. The image was rebuilt there first (15 minutes) for the
docker client. `SCENARIOBANK_SHARE=$HOME/scenariobank/share` with the bank copied into
`banks/t-junction` — a plain directory standing in for the NAS share, which is what Open question
8 not blocking this step means in practice.

| check (rig `sim`, one RTX 5080, real GPU) | result |
|---|---|
| the four agent test files inside `scenariobank-sim:latest` | 87 passed, 2.62 s |
| `docker compose --profile rig config` | every root a host path, and equal inside: the same-path mounts |
| one scenario, the agent in its container | `completed`, delivered to the share, 20.5 s |
| the row | `t_junction_0000`, 139 steps, 0.332 s — the same numbers Step 1 measured there |
| `host` on `run.started` | `sim` — the rig's own name, through `--network host` |
| the whole bank, five rows | `5/5`, delivered, no container left, `holder.json` gone |
| while it held card 0: rig lock **exclusive** (CARLA's) | refused, **exit 1** |
| the same moment: rig lock **shared** (ours) | granted, **exit 0** — a second card of ours could start |
| the same moment: card 0 / card 1 | 1 / 0 |
| after it released: rig lock exclusive | **0** — CARLA can run again |
| the same job a second time | `already delivered`, exit 0, before the lock is even attempted |
| a hand-held rig lock, fresh job, card 1 | exit **4**, naming the holder's **host** pid — which only `pid: host` makes possible |
| a card this rig does not have (`--gpus device=1`) | exit **1**, `launch_failed`, nothing left behind, the driver's message kept |
| that job again, after the failure | it **runs** — a failure is not a delivery (see below) |

**And two bugs the rig found that the laptop could not**, both from the same job: a card this
rig does not have.

1. Asking a one-card rig for `--gpus device=1` fails — correctly — but `docker run --detach` has
   **created** the container by then, and it sits there exited 128. The next attempt adopted it
   and reported a run that *vanished*: two wrong answers, since the run never started and the
   reason was in a log nobody kept. `run()` now harvests after a failed launch — the log beside
   the results, the container removed. A worker configured for a card the rig does not have is a
   deployment error, and it now fails in about a second with the driver's own message on disk.
2. **The bigger one, and it would have silently broken every retry in Step 5.** Delivery happens
   whatever the outcome — the evidence of a failure is worth more than the disk — but it was
   delivering *under the job's own name*, and `results/<job_id>` existing **is** the redelivery
   guard. So that vanished run was delivered, and the next attempt at the same job reported
   `already delivered` and exited 0 without running. On the queue that is an ack for a job that
   never ran. Fixed by `RunSession.delivery_name`: **only a run that RAN — `completed` or
   `stopped` — takes the job's name**; a failure, a refusal or a vanishing goes to
   `results/<job_id>.attempt<N>`, a sibling that is never mistaken for the result and that the
   next attempt lands beside rather than on top of. Step 5's own bullet 8 had the same flaw
   written into it (`results/<job_id>/invalid/`) and is corrected there too.

The worked round trip, the five environment variables, the exit-code table and the two recovery
measurements are in `docs/running-the-application.md`, "One job, start to finish: `agent --once`".

### Step 4 — ~~`metadrive-runner`: the service on each rig~~ retired 2026-09-13

There is no HTTP service on the rig. It existed so a NAS orchestrator could ask a rig for a card
and be told `busy`; with the worker leasing only when it already holds the card, the queue **is**
the API, and nothing calls the rig. What this step listed survives elsewhere: idempotency by
`job_id` is the `results/<job_id>` check (**Files**); validation before the card is in Step 3; the
`/health` facts — per-card lock state, disk, image label, agent version — are a **status file per
card** the worker writes beside the results (Step 6), so the studio's "what is running where" is a
file read and no port is open on a rig. The bearer token went with the port.

### Step 5 — the rig agent: lock, lease, run, deliver, ack ✅  *(built 2026-09-15)*

*(Keith's shape, 2026-09-13: one container per rig — `docker compose up agent`, the sim image, no
GPU — with one worker per card, each polling its card's availability and only then the queue for a
job.)*
`QueueClient` from `docs/queue-docs/`, one topic (`metadrive`), long-polled. `WFQUEUE_URL` and, if
the server is ever started with one, `WFQUEUE_TOKEN` come from the environment. Per worker:

0. **On start, adopt before leasing anything.** Any container carrying our label for this card is
   a run in flight: wait on it and deliver its results as if this worker had started it. That is
   the recovery path and it is the same code path as a normal run, not a special case.
1. **Take this card's lock** (Step 2). Held — by us, by CARLA, by a hand-run script — → sleep and
   retry. Never nack: no job was taken.
2. `consume("metadrive", poll_interval=0, wait=20, consumer=f"{host}:gpu{n}")`, one message at a
   time. `poll_interval=0` matters: the client's default is a 60 s sleep after every empty poll.
3. **Validate** (Step 3's list). A payload that can never run → `nack(dead=True)`, the lock
   released, back to 1.
4. `results/<job_id>` already on the share → `ack`, release, back to 1 (the redelivery guard).
5. Resolve names → paths: `bank.path` = the share's `banks/<bank_id>` mounted read-only, no copy;
   `checkpoint_path` and `model_config_path` under the models root.
6. Start `bridge-gpu<n>` if not up; `sim-run.sh run … --bridge-port` on card *n*, results to the
   rig's local `/out/<job_id>`.
7. **Extend the lease on a timer** while the container runs; stop the instant it exits. The client
   has no timer of its own.
8. **Deliver, then ack, then release.** `deliver(job_id, result_dir)` is one function behind one
   interface, because where results finally live is not decided — most likely a database on the
   NAS, shape unknown (Open question 7). The first implementation is the share copy (**Files**); when
   the database exists `deliver` writes to it as well or instead and the loop does not change.
   Ack only after delivery succeeds, so a failed delivery is a retried job, never a lost result.
   Before delivering, `scenariobank validate --results <dir>/results.json` (moved here from the
   retired Phase 6): the file parses against `Results`, every `failure_reason` is in the enum,
   the row count matches the job's scenario list. A file that fails is delivered anyway, into
   **`results/<job_id>.attempt<N>/`** and never inside `results/<job_id>/`, and the job is nacked
   — evidence first, then the retry. *(Corrected 2026-09-15, from Step 3: `results/<job_id>`
   existing IS the redelivery guard, so anything delivered under that name says "done, ack
   without running". Only a run that ran may take the job's own name; `RunSession.delivery_name`
   is the one place that decides it.)*
8b. `ack` raising `QueueHTTPError` with `409` after a run completed → `GET /messages/{id}`;
   `state == "done"` means the first ack landed and the client's own retry is noise. Anything else
   is a real lease loss: the job was redelivered, and step 4 on the other worker is what keeps it
   from running twice.
9. Any other failure → `nack(retry_after=…)`, release. Throughout, write a **status file per
   card** beside the results: job id, scenario progress (derived from the record directory, never
   stored), lock holder, disk, image label, agent version.
10. **Sweep the local out directory** *(added 2026-09-15, from Step 3)*. Every job leaves
   `SCENARIOBANK_OUT/<job_id>` on the rig's own disk, written by a container running as root, and
   films are hundreds of MB. Delete by age, and **only** where `results/<job_id>` exists on the
   share — the local copy is the evidence until the delivered one is real. Nothing else in the
   agent removes it, deliberately: it is what an adopted run is read from.

Steps 1, 3, 5 and 6 of that list are `RunSession` (Step 3) called in order —
`adopt()`, `ensure_bridge()`, `launch()`, `supervise(on_tick=…)`, `harvest()`, `deliver()` — so
what this step adds is the queue around them: the lease, the extend timer on `on_tick`, and the
ack/nack mapping of `Outcome` (`refused` → `nack(dead=True)`, `completed`/`stopped` → ack after
delivery, everything else → `nack(retry_after=…)`).

**Verify alone:** against Step 0's replica, on the laptop, `NO_GPU=1`, `ExpertPolicy`, two fake
cards: `put()` two jobs and watch each worker take one; hold one card from a shell and watch
only the other worker lease; kill the agent mid-run and restart it — it adopts, delivers, acks,
and no second container appears.

**Done 2026-09-15.** `src/scenariobank/agent/worker.py` (~760 lines, half of it the
docstrings that say why), and `scenariobank agent` with no `--once` is the loop: one process per
rig, one `Worker` thread per card, each one `RunSession` with the lease before it, the extend
timer on `on_tick`, and the ack or nack after. `--gpu` is now repeatable (`SCENARIOBANK_GPUS=0,1`
in the compose file), `--queue`, `--topic` and `--max-jobs` join it, and `--once` is unchanged.
The queue's own client runs as `src/scenariobank/agent/wfqueue_client.py`, a byte-identical copy
of the vendored one that `test_worker.py` asserts equal and ruff is told not to touch, for the
same reason the docs copy is excluded. 25 tests in `tests/unit/test_worker.py`, against the
Step 0 replica over real HTTP and a fake daemon (`tests/support/fake_docker.py`, moved out of
`test_session.py` so both can drive it), with real `flock`s under `tmp_path` for the card.

Five things differ from the list above, each for a reason found while building it:

1. **The lock is held for milliseconds between jobs, not for the 20 s long-poll.** Item 2 had
   `consume(wait=20)` with the card held. The rig lock is shared between our cards and exclusive
   for CARLA, so a worker long-polling on it would deny the machine to everybody else for as long
   as our queue was empty -- and with two workers alternating it would never be free at all. The
   worker takes the card, asks for one message with `wait=0`, and on nothing gives the card
   straight back and sleeps five seconds outside it. Measured on the laptop: an exclusive
   `flock -n` from a shell succeeds while the worker is polling. Latency to pick up a job is
   the sleep; the cost is one request per five seconds per card.
2. **Adoption keeps the lease, and stopping is a handover.** Item 0 said "deliver as if this
   worker had started it"; on its own that leaves the dead agent's lease to expire and the
   message to be redelivered -- possibly to the other rig, while this one is still driving it,
   and property 1's guard only helps once the result is on the share. So the holder record beside
   the card lock now carries `message_id` and `lease_id` (**lock schema 2**), the adopting worker
   calls `extend` -- accepted for as long as the lease is alive -- and acks the job itself when
   the run ends. And SIGTERM to the agent is the mirror image: a worker mid-run extends its lease
   once more (ten minutes), leaves the record, closes its descriptors (`Held.abandon()`, which
   is `release()` without the unlink) and exits with the container still driving. That is what
   `docker stop` on the agent container does, and it is the opposite of `--once`'s Ctrl-C on
   purpose: a rig's agent is restarted by redeploys and reboots, and none of those may cost a
   twenty-minute drive. Measured both ways on the laptop with the five-scenario bank; the rig
   table below has the container version.
3. **The queue's `attempts` is the attempt.** The payload's `attempt` is the submitter's guess;
   the container name, the holder record and a failed run's `<job_id>.attempt<N>` all follow the
   count the queue keeps, so the second delivery of a message lands beside the first.
4. **A rig that cannot start the run still delivers what there is.** `SessionError` out of
   `launch()` or `ensure_bridge()` used to be a nack and nothing else; now whatever the run
   directory holds (the job file, the driver's own message in `container.log`) goes to the share
   as `<job_id>.attempt<N>` first, then the nack. Evidence first, then the retry, as bullet 8
   already said for a run that ran.
5. **The sweep found its own trap in a test.** On the laptop the results root is *inside* the
   out root (`out/results`), so a delivered job called `results` would have made the share
   itself look like an old, delivered run directory -- and the sweep deleted it, in `tmp_path`.
   The share is never a run, whatever it is called; the guard is by path, not by name.

Bullet 8's `scenariobank validate --results` before delivery is **not** in: the runner writes
`results.json` through the same pydantic model the validator would read it with, and a row count
that disagrees with the job is `run.finished`'s business (Phase 4 Step 3). It stays a Step 6
question, where a store rather than a directory is what would refuse a bad file.

| check (laptop, `--no-gpu`, the Step 0 replica on 9091, real containers) | result |
|---|---|
| `tests/unit/test_worker.py` | 25 passed, 4.0 s |
| the four agent test files together | 108 passed |
| two jobs put, `agent --gpu 0 --gpu 1 --max-jobs 1` | each worker took one, both `done` in 7 s, `consumer` `<host>:gpu0` / `:gpu1` |
| the two results | `t_junction_0000`, 139 steps each, `job.json` beside them, one `run.started` each, no holder file, no container |
| `kill -9` the agent mid-run (pid from the holder record), restart | `adopting … lease=live`, then `ack … completed, 5/5`; message 3 `done`, `attempts` 1 |
| SIGTERM the agent mid-run | `handing over` logged, agent exited in under a second, container `Up`, record kept, lease extended to 600 s |
| restart after that | adopted, `ack … completed, 5/5`, one `run.started`, no container left |
| card 0 held by `flock -x` from a shell for 8 s, a job waiting | message stayed `ready`, `attempts` 0; status file `busy` naming the holder's pid; leased and acked the moment the hold ended |
| a job naming a scenario the bank lacks | `dead` after one lease, `last_error` is the refusal, nothing launched, no holder file |
| the topic's counts at the end | `done: 5, dead: 1`, nothing `ready` or `leased` |

The worked commands, the ack/dead/retry table and the handover are written up in
`docs/running-the-application.md`, "The loop: `agent`".

### Step 6 — results storage on the NAS ⬜

**The agent delivers, the NAS stores** *(2026-09-13)*. `deliver()` on the agent is the only writer.
Its first target is `results/<job_id>/` on the share (**Files**); its intended target is a database
on the NAS whose shape is not yet decided (Open question 7). Whatever that database is, the rules
below are the requirements on it, and the studio is what reads it — including the **push to
Tyrone's webapp**, a studio background task that takes each newly complete result from the NAS
store and POSTs it (endpoint, auth and payload unknown, Open question 7). Downstream of delivery;
never the agent's job. Our own SQLite plus a results tree until then. Not his schema, and no
mapping — the shape is ours.

**What the webapp must be told about a result** *(moved here from Phase 6, retired 2026-09-14;
written as a document — `CONTRACT.md` at the repo root, plus `scenariobank schema --results` if
the payload is the results document — the day the endpoint and payload are known. Until then this
list is the document, and every finding elsewhere in the plan that says "for the results notes"
lands here.)*
- **The camera-only statement first.** A state-vector policy cannot perceive traffic, cones,
  barriers, pedestrians, cyclists or lights. A collision under one is not a model defect.
- **The bank is left-side traffic** (right-hand-drive market): the ego keeps left, roundabouts
  circulate clockwise, on-ramps join from the left, and the unprotected turn is the **right**
  turn. A model trained for right-side traffic fails this bank for reasons that are not defects.
- **Seeds vary geometry and route; options vary difficulty.** Options are echoed **expanded** in
  results, level name and resolved numeric, so a result says which difficulty it was.
- **`scenario_id` is the key to store. Seeds are ours and may change between banks.**
- **`metadrive.commit` is a label**, echoed so an old result can be read later; nothing refuses on it.
- **Success rates are over 5 scenarios per category — 20% granularity.** `0.6` is three of five,
  not 60% ± 1.
- **`failure_reason` is a closed enum** — list every value (with `crash_human`, and
  `run_red_light` once Phase 8 lands, schema v1.1) so a grouping UI can be built.
- **`status: "error"` is distinct from `success: false`.** An error means we learned nothing;
  `skipped` is distinct from both.
- **Across machines, compare outcome fields only** — `status`, `steps`, `route_completion`,
  `cost`, `collisions`, `failure_reason`, `actor_layout_digest`. `actions_digest` and the last
  digits of `reward` differ between CPUs (Phase 5 Step 4, 2026-09-14) and are not defects.
- **What the evaluated model observes**: the six AV3 cameras off the rig; the 19-number state
  vector is only the CLI diagnostic policies'. The ScenarioNet `.pkl` replay path is rejected
  because it observes 31 numbers and changes the task.
- **The model's known behaviours that are not runner defects**: the car stopping on an empty
  road (Phase 4 Step 7 note 7); the traffic lights a state-vector policy cannot see (Phase 8);
  the camera framing findings in Open question 1.
- **Exit codes**: 0 = ran, 2 = integrity refusal, 1 = internal.

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
2. A real message on the real queue — **the first and only use of it**, the one step waiting on
   access (Open question 9) — leased by the agent on one real rig.
3. Results delivered to the NAS, renamed into place, message acked; the studio's push picks it up.

Then wire the real bank behind it. **A green round-trip against a stub is worth more than a correct
bank nothing can run**, and here it also proves the routing before either side is finished.

### Step 8 — `GET /options` and the ETA ⬜

- **The six axes served as data**, so a frontend renders the form from the schema instead of
  hard-coding it. This is what keeps the picker in step when an axis is recalibrated in Phase 4b.
  One consumer: our studio's submit screen (Phase 2c, Step 12), the only producer onto the queue
  since 2026-09-13; the wing-sim webapp reads results, not options.
- **The ETA.** Bootstrap from measured per-category wall time — Phase 4b's calibration runs produce
  it for free — keyed on `(category, tier)`, then replace it with a **median** of the last N real
  runs. Median, not mean: one degraded run is a 5x outlier that poisons a mean for weeks. At the
  measured 100 s per 32 s scenario on the rig (Phase 4 Step 8's table; 9x that on the laptop) a
  35-scenario run is an hour, long enough that an absent estimate is a visible gap.
  Two rigs means the estimate is per rig, or it is wrong on the slower one.

---

## How you test it

Four failures that are all **silent**, which is why each gets an explicit test rather than a hope.

**A double-lease must not double-run** — property 1, and the one that costs a GPU:
```bash
# lease with a short visibility_timeout and then do nothing; let it expire mid-run
python3 -c "import os; from client import QueueClient; QueueClient(os.environ['WFQUEUE_URL']).lease('metadrive', visibility_timeout=5)"
docker ps --filter label=scenariobank.job=<id> | wc -l   # on the rig: expect 1, still 1 after redelivery
```

**A busy rig must not fail a job** — hold the card from outside both queues:
```bash
bash wing-sim/deployment/with_rig_lock.sh sleep 120 &      # on rig A, not a queued job
curl -s $WFQUEUE_URL/topics/metadrive/stats | jq '.dead'   # expect 0 throughout
curl -s $WFQUEUE_URL/topics/metadrive/messages?state=leased | jq '.[].consumer'   # rigB:gpu0 — the other rig took it
```

**The two stacks must not interfere** — queue a CARLA job and a MetaDrive job together; each is
leased by its own consumer only, and the rig lock serialises them on a shared card.

**Restart recovery** — kill the agent mid-run and restart it. It must adopt the run rather than
orphan it, deliver and ack it, and no second container may appear.

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
in Phase 7 Step 6's results notes beside the axis — though the camera-only statement at the top of
that list already covers it.

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
  `pwd.getpwuid()` raises for a host uid with no passwd entry — `import torch_tensorrt` calls it
  at module scope). The sim container runs as root (Phase 5 Step 1); the mounts are the studio's.

**Docs**
- The sibling repo has no root `CONTRACT.md`; its convention would be `docs/reference/<topic>.md`
  with a 1-3 line trap summary in `CLAUDE.md` pointing at it. When the results notes become a
  file (Phase 7 Step 6, after Open question 7), keep `CONTRACT.md` at the root because it is
  cross-team and the repo is standalone, and *also* add the trap lines to `CLAUDE.md`.
- If you write a `CLAUDE.md` here: **hard budget under 30 KB**, traps only (1-3 lines + pointer),
  measurements live in `docs/reference/`. The sibling's grew to 223 KB by appending before it had
  to be split.
- Two standing rules worth carrying over: **never quote a measured figure from a doc, re-measure
  it** — the `LEVELS` table cites `level-calibration.md`, not the other way round — and **blast
  radius is an acceptance criterion**: a fix that changes
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
  — the provenance of `src/scenariobank/av3/` (ported in Phase 4); ours is the source of truth now.
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

---

## Open questions

1. **The two rig fidelity gaps** recorded in `rigs/av3.txt`'s header — fisheye rendered as an
   unwarped pinhole, and 4:3 rendered then squashed by preprocess rather than native 16:9. Both
   are known, neither is fixed, and both belong in Phase 7 Step 6's results notes so nobody reads
   their effect as a model defect.
2. ~~`max_steps` per category~~ **resolved**: `step_budget()` from the measured route length
   (`categories.py:185`).
3. ~~Which exit for `t_junction` and `roundabout`~~ **resolved**: `docs/reference/destinations.md`
   records the exit and its reason per category.
4. **Bank size beyond 5 seeds.** 35 is the shipping bank. `--count` is a flag, so growing it is one
   regeneration away — but the results notes state the 20% granularity, so growing it later changes
   what a success rate means to the webapp. Decide before the push is written, not after.
5. ~~One bridge per running simulation~~ **resolved 2026-09-15**: the base port is **5600**, so
   gpu0's bridge is 5600 and gpu1's is 5601 — clear of wing-sim's 5558/5559, which makes a
   collision with their bridge an error rather than our simulator driving against their planner.
   The container name per card needs no change to `bridge.sh`: it already reads `BRIDGE_PORT`
   and `BRIDGE_NAME` from the environment (`scripts/bridge.sh:38`, `:40`), so the agent passes
   both. `run --bridge-port` on the client stays as written. The rest — one bridge per *running*
   simulation, because the server holds one connection — is Phase 5 Step 3's finding, and it was
   built in Phase 7 Step 3 and measured there: `bridge-gpu0` on 5600 with wing-sim's own bridge
   still up on 5558 beside it.
6. **How the NAS exposes the studio to browsers** *(2026-09-13)*. `cli.py` binds loopback only, by
   design, because its routes run subprocesses that write into the repo with no authentication.
   On the NAS someone other than localhost must reach it: a reverse proxy, a tunnel, or a
   deliberate loosening with auth. Decide before the NAS deploy.
7. **Where results finally live, and how they reach Tyrone's webapp** *(2026-09-13)*. Most likely a
   database on the NAS; its shape, and the webapp's endpoint, auth and payload, are all unknown.
   The agent's `deliver()` is the seam: the share copy now, the database when it exists. Decide
   the database before Phase 7 Step 6; ask about the webapp before the push is written.
8. **The NAS share** *(2026-09-13)*. Protocol, mountable on both rigs, root-squash or not, and
   whether the checkpoint cron already uses it. **It no longer blocks Step 3** *(2026-09-15)*:
   Step 3 reads `SCENARIOBANK_BANKS` as a plain directory, which is what `compose.yaml` already
   declares, so `agent --once job.json` runs on the laptop and on a rig with nothing mounted.
   Mounting the share is then a path change and not a code change. Still to decide for Step 6,
   which delivers into it.
9. **Queue access, and the queue's own server** *(2026-09-13)*. None yet, on either count.
   Everything up to Phase 7 Step 7 runs against the Step 0 replica
   (`tests/support/fake_wfqueue.py`), and Step 7 waits on a key from the colleague. The second
   half is Step 0's item 1, still unasked: the `wfqueue` **server** -- the package its own client
   docstring implies, a single file, or a compose service -- so that the agent's loop can be
   developed against the real code on localhost instead of against our double. Until it arrives
   every "real" assertion in `test_queue_contract.py` has only ever run against the replica, and
   when the two disagree the replica is what is wrong.

10. **wing-sim's lock is one file for a whole rig** *(2026-09-15, Phase 7 Step 2)*. Ours are per
    card, `.wing-sim.gpu<N>.lock`, and we take his `.wing-sim.gpu.lock` **shared**, so our two
    cards run together while CARLA is still shut out of both. Nothing is unsafe and nothing is
    blocked; the cost is that a two-GPU rig behaves as a one-GPU rig for as long as CARLA runs.
    **The ask for Tyrone is one sentence:** take `.wing-sim.gpu<N>.lock` for the card you are
    using, and keep taking `.wing-sim.gpu.lock`, exclusive, for anything that needs the machine.
    Do not assume he will change it -- this is written so that the day he does, nothing of ours
    has to move.

11. **Who owns the lock directory on each rig** *(2026-09-15, measured)*. `~/simulation` on the
    first rig is owned by `metadrive`, mode 755, and did not exist at all until Step 2's check
    created it. `flock(2)` needs only read access, and both sides create their lock files 0666 --
    but creating one needs write permission on the **directory**. If their GitLab runner runs as
    another user, whichever side gets there second cannot make the file. Decide before Step 5
    runs unattended: mode 777 on that directory, or an agreed owner. The second rig has none yet.

12. **Four tests fail on this laptop, and they are not Phase 7's** *(confirmed 2026-09-15)*.
    Three in `tests/unit/test_importing.py`, one in `tests/unit/test_workspace.py`. The assertion
    is `report.last_conversion["dataset_dir"] == "scenarionet-100hz"` and what is there is
    `scenarionet-10hz`: the converter workspace this machine has is not the one those pages were
    measured on (the two-checkout trap in **Reference checkouts**). Every offline suite run
    recorded in this document therefore excludes those two files -- 843 passed, 9 skipped, is
    with them excluded. Decide which is true: re-pin the tests to the current workspace, or treat
    the 100 Hz conversion as the one that counts and fix the workspace.

*The rest of this document points at these by number -- "Open question 7" -- so a resolved item
keeps its number and is struck through rather than removed. Add new ones at the end.*
