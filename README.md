# scenariobank

A bank of procedurally generated MetaDrive scenarios with fixed seeds, forced destinations, and
run-time difficulty options — plus the runner that scores a camera model against it.

**The studio is the way in.** `scenariobank studio` serves a local web page: pick a scenario type,
build a bank, look at every scenario in it, and swap a poor seed. That is the product;
[`docs/web-setup.md`](docs/web-setup.md) is how to install and run it.

The CLI underneath is how the studio executes rather than a second product. MetaDrive's engine is a
per-process singleton, so every simulation has to be a subprocess, and that subprocess is this
package's own command line — which is also why the studio's forms and its job validation are
generated from the CLI's flags and cannot describe a different program.

`IMPLEMENTATION_PLAN.md` is the source of truth for what gets built and in what order.
`CONTRACT.md` (Phase 6) is what the bank will promise its consumers.
[`docs/reference/commands.md`](docs/reference/commands.md) is the full flag reference --
generated from the CLI, so it cannot drift. This file explains *why* each command exists;
that one lists every flag and every value they accept.
[`docs/importing-scenarionet.md`](docs/importing-scenarionet.md) is how to bring a converter
workspace in as a bank — which rate to choose, and what `import` refuses.

All scenarios are **left-side traffic** (right-hand-drive market). See
[Which side of the road](#which-side-of-the-road) — it is not a MetaDrive setting, and it is the
first thing to check if a figure ever looks wrong.

**Built so far:** Phase 0 (skeleton, environment truth), Phase 1 (categories, forced
destinations, left-side traffic) and Phase 2 (`generate` — scenarios and a manifest on disk).
`run` and the rest arrive in later phases.

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

Flags: [`docs/reference/commands.md`](docs/reference/commands.md#doctor).

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

Lists the eleven categories with block sequence, exit rule, step budget and description. The only
command that does not need the `sim` group.

**Writes:** nothing.

### `sockets` — which exits does a block offer, and which way do they turn

The discovery command. It is how each category's destination was chosen, and it is what to re-run
when MetaDrive changes shape.

Flags: [`docs/reference/commands.md`](docs/reference/commands.md#sockets).

```bash
uv run scenariobank sockets --block-seq X --seed 0
uv run scenariobank sockets --category roundabout --seed 3 --json
```

Prints one row per exit — socket index, destination node, **two** angles and the turn they imply —
then what each `ExitRule` would resolve to, **including the rules that resolve to nothing**:
"nothing here turns right. The closest is `1T1_1_`, which carries straight on" is the useful half
of the answer when a road cannot carry a category. Refusals are worded for a reader who has not
used MetaDrive, because the studio's card shows them verbatim. Angles are
counter-clockwise-positive, so **positive is a left turn**.

`--json` emits one document — `block_seq`, `seed`, `sockets` and `rules` — rather than a bare list
of sockets, because the rules are what turn an exit into a destination and a list has nowhere to
carry them. Each socket carries `angle_deg`, `turn_deg` and `entry_heading_deg`; the rules carry
`angle_deg` and `turn_deg` beside the node they picked. The rules are in `ExitRule`'s own order, so
the page and the terminal print them the same way round. This is what **Read the exits** on the
studio's **Build** tab reads, and what fills the exit dropdown under a picked scenario.

**`turn` is measured from where the car enters the last block; `from spawn` from where it set
off.** They are the same number on a one-block road, and differ by exactly the rotation the road
applied on the way in — printed above the table when it is not zero. `ExitRule` matches **`turn`**,
because that is the turn a driver makes:

```
CSX seed 0
  the road turns the car +115.5 deg before its last block, so the two angles differ
  socket           node            turn  from spawn   which way
  3X-socket0       3X0_1_         +90.0      -154.5   left
  3X-socket1       3X1_1_          -0.0      +115.5   straight
  3X-socket2       3X2_1_         -90.0       +25.5   right
```

The curve in front of the crossroads swings the car +115.5°, so from the spawn its three arms read
−154.5 / +115.5 / +25.5. Matching those, `right` found nothing inside its 45° tolerance and refused
on a road that plainly has a right turn, and `left` answered `3X1_1_` — the arm the driver goes
*straight* through. From the junction they are the +90 / 0 / −90 a crossroads has.

Only single-block roads and rule `only` are used by the eleven shipped categories, so this
correction leaves `docs/reference/destinations.md` byte-for-byte unchanged. It bites on composed
roads, which is what the studio's road builder makes.

**Neither angle is how far the ego turns in total.** Both fold at ±180: `curve` seed 0 sweeps
+239.5° and shows here as −120.5°, half the rotation and the wrong direction. For total rotation
use `destinations`, which reports both.

Expect one exit near `+90`, one near `-90` and one near `0` for a four-way junction. **Two exits
of the same sign and similar magnitude mean the block is not what you think it is** — stop and
look before pinning anything to it.

**The arm the ego drives in through is not listed.** A block's sockets are the connections it
offers onward; the one behind it belongs to the block before. Measured across all fifteen block
ids: none of the twelve that build alone marks an entry. `SocketReading.is_entry` and the filter
every rule applies stay — they guard a case MetaDrive does not currently produce — but nothing
composed from these blocks fills that column in.

**Three of the fifteen do not build on their own,** and for two different reasons. `f` and `F`
are broken — MetaDrive refuses both at every seed in its own words. `P` is not: a parking lot
needs the road in front of it to be one lane in each direction, and a road starts at three. Only
the lane merge `y` narrows one, so `SP` is refused at every seed and `SyyP` builds at all of
them. That condition is declared once, on `categories.BLOCKS`, and read by the CLI's refusal, the
studio's palette and the command reference alike.

**Writes:** nothing.

### `inspect` — see the route rather than trust it

Flags: [`docs/reference/commands.md`](docs/reference/commands.md#inspect).

```bash
uv run scenariobank inspect --category intersection_left --seed 0
uv run scenariobank inspect -c t_junction -s 2 -o /tmp/t2.png
uv run scenariobank inspect --block-seq CC --seed 22 --rule only    # any sequence, any seed
uv run scenariobank inspect -b CCX --rule left --json -o /tmp/ccx.png  # what was drawn, as data
```

`--block-seq` is also what the studio's road builder runs: the card under the gallery is a palette
of the fifteen blocks, and **Draw** is this command with `--json`, whose output adds
`earned_max_steps` — the budget a category on that road would be given — and `composed_name`, the
category the road would be filed under if it were added. Drawing reaches no bank; `add
--block-seq` is what does. A road MetaDrive will not build (`fS`, say) is refused in MetaDrive's
own words; a road that cannot build because of what a block needs in front of it (`SPS`, say) is
refused before a simulator starts, and names the sequence that would build instead.

Draws the road network in grey with the **pinned route in red**, a blue arrow at the spawn pose
and a green star at the destination. Headless — no display, no window, no image buffer. The route
comes from the navigation module after the destination is pinned, so the picture shows what the
runner will actually drive.

**Which way is the ego facing?** Always **due east** — rightward in every figure this repo draws,
with `+y` up. What moves with the seed is where that spawn lands in the frame: `curve` seeds 2 and 3
start at the *top* and bend downward, while 0, 1 and 4 start at the bottom and bend up. A road
picture read from the wrong end reverses every turn in it, which is why the spawn arrow is drawn and
why `net_rotation_deg` is recorded per scenario. Trust the arrow, not the shape.

**Writes:** one PNG, at `--out` or under `docs/reference/figures/`.

### `destinations` — regenerate the reference document

Flags: [`docs/reference/commands.md`](docs/reference/commands.md#destinations).

```bash
uv run scenariobank destinations
```

Resolves every category at every seed, proves each destination is reachable by running the
shortest path, measures the route, and fingerprints each block sequence's drivable surface. Two
env builds per category per seed plus one per sequence — about 20 seconds, reported as it goes:
twenty `[n/m]` lines, nine block sequences then eleven categories, in the shape the studio's
progress bar already reads.

The document it writes has seven sections: the resolved destination and angle per category and
seed; route length against the earned step budget; the turn actually taken; the **spawn lane**;
the **curve direction pairs**; **distinct roads per block sequence** — `X` builds one identical
road at all five seeds, `T` builds two, and the other seven build five each, so **38 distinct
roads across the 55 scenarios** — and **how alike the closest two are**, which is the section that
stops the first number being read as more than it is.

**38 flatters the bank.** It counts roads that are not *identical*, and a hash cannot see a
near-twin. Measured by shape instead, the closest pair of **every** sequence is a near-duplicate:
`CC` seeds 0 and 4 are 7% apart, `$S` and `YS` 4%, `yS` 3%, `rS` and `RS` 2%, and `O`, `T` and `X`
not measurably apart at all. Only `CC` has real spread available. **The bank's variety is in the scene the Phase 4 options build, not in the
road** — the position already accepted for `X`, and true of the bank as a whole. Use `seeds` to
find a seed that would add more.

Those `X` seeds are still not five identical runs. `random_spawn_lane_index` is left on
deliberately, so the ego starts in lane `0, 1, 0, 1, 1` — the only thing separating them until
Phase 4's options arrive, and invisible in `route_length`, which is measured on a reference lane.
The curve section records the other property that holds by luck: `CC` draws each block's direction
independently, and seeds 0–4 happen to cover all four of `LL`, `LR`, `RR`, `RL`. Both are asserted
by tests so a MetaDrive bump cannot quietly take them away.

Regenerate it after any MetaDrive bump. That is how a change in block geometry becomes visible
instead of silently changing what the bank means.

**Writes:** `docs/reference/destinations.md` (or `--out`). Overwrites in place.

### `add` — one more scenario, or a road of your own

```bash
uv run scenariobank add --bank ./banks/b -c t_junction -s 7
uv run scenariobank add --bank ./banks/b -b CCX --rule sharpest -s 0
```

Appends one scenario to an existing bank. With `--category` the road and the rule come from the
bank's own manifest, so a bank grows the way it was built. With `--block-seq` and `--rule` the
road is composed on the spot and **its category is named after it** — `CCX` at `sharpest` is
`CCX_sharpest`, always, so the same road never arrives twice under two names and nothing has to
be invented for a one-off. A category created that way is capped at what its first route earns.

The new id is one past the highest, never the row count: removing leaves a gap, and re-using an
id would make every result already keyed on it ambiguous.

**Writes:** the bank's `manifest.json`, and one thumbnail unless `--no-thumbnails`.

### `generate` — write a bank

Flags: [`docs/reference/commands.md`](docs/reference/commands.md#generate).

```bash
uv run scenariobank generate --out ./banks/pg-bank-2026-08 --bank-id pg-bank-2026-08
uv run scenariobank generate -o /tmp/b --bank-id b -c curve --seeds 0,7,9 --no-thumbnails
uv run scenariobank generate -o ./banks/b --bank-id b \
    --seeds 0,1,2,3,4 --seeds curve=0,1,2,3,22       # one category on its own seeds
```

Builds every category at every seed and writes `manifest.json` plus one PNG per scenario. The
full 55-scenario bank takes about **7 seconds** (a little more on the first run after an install,
which also builds matplotlib's font cache). Progress goes to stderr, one line per scenario.

A thumbnail is the same route figure `inspect` draws (`figures.render_route`): road in grey, the
driven route in red, a blue arrow at the spawn, a green star at the destination. It is **per
scenario, not per map** — the three `X` categories share a road and a seed, and a picture of that
road alone is the same picture three times, with nothing in it to say which of the three routes it
belongs to. The manifest carries `net_rotation_deg` and `turn_pairs` for the same reason: the
direction should be readable from the data, not only from the image.

**A bank is a disposable, per-batch artifact.** Regenerate it whenever you want scenarios; nothing
checks that a road matches a previous run's, and if it comes out different that is a different
batch. What makes one batch self-consistent is the container pinning one MetaDrive commit — run
`doctor` to see which. The manifest records that commit as *information*; nothing refuses on it.

The manifest is the runner's input, not an audit trail. Per scenario it carries the resolved
`destination` and the drawn `spawn_lane_index`, so the runner pins `vehicle_config["destination"]`
and `auto_assign_task` never draws a random one. `destination` is **per scenario and not per
category**: `t_junction` resolves to `1T0_1_` on seeds 0, 1 and 4 and `1T2_1_` on 2 and 3, because
the arm the junction exposes changes with the seed.

One check runs during generation and it is not a reproducibility check: every map is **measured**
to be left-side traffic. If `handedness.install` fails to take, every field in the manifest stays
correct and every thumbnail still looks like a road, and the only symptom is a right-hand-drive
model failing everything for reasons no result explains.

`manifest.json` is written **last and atomically**, so an interrupted run leaves a directory with
no manifest — which reads as "no bank here" rather than as a bank quietly missing rows.

**Writes:** `<out>/manifest.json` and `<out>/thumbs/*.png`.

### `seeds` — which seeds are worth using

Flags: [`docs/reference/commands.md`](docs/reference/commands.md#seeds).

```bash
uv run scenariobank seeds -c curve --keep 0,1,2,3 --scan 0-30
```

**A seed is not automatically a scenario.** Two seeds of one block sequence can draw roads a few
percent apart — near enough that their thumbnails are the same picture. `curve` seeds 0 and 4 are
7% apart and `roundabout` seeds 0 and 4 are not measurably apart at all, yet
`docs/reference/destinations.md` calls both pairs distinct, because that count is hash equality
and a hash cannot see a near-twin.

So this measures each candidate against its **nearest kept seed** with `fingerprint.shape_gap` and
ranks by the gap, largest first. It costs one reset per scanned seed. The workflow is: scan, look at
the top few with `inspect --block-seq`, then commit what you like with `generate --seeds` or
`replace`.

**Writes:** nothing.

### `replace` — correct one scenario in a bank

Flags: [`docs/reference/commands.md`](docs/reference/commands.md#replace).

```bash
uv run scenariobank replace --bank ./banks/b --scenario curve_0004 --seed 22
```

Rebuilds one scenario at a different seed rather than regenerating all fifty-five. **The bank never
changes size and never renumbers** — the scenario keeps its id and its position, and only the seed
and what was measured from it change. A seed already used in that category is refused, and a route
that would overrun the category's `max_steps` is written with a warning on stderr.

`block_seq` and `exit_rule` come from the manifest's own category entry rather than from
`categories.py`, so a bank generated before a code change is still correctable afterwards.

**Writes:** `<bank>/manifest.json`, and that scenario's thumbnail.

### `options` — pin the levels runs of this bank use

Flags: [`docs/reference/commands.md`](docs/reference/commands.md#options).

```bash
uv run scenariobank options --bank ./banks/b --traffic medium --pedestrians low
uv run scenariobank options --bank ./banks/b --show
```

Six axes — `traffic`, `cones`, `barriers`, `pedestrians`, `cyclists`, `lights` — each at `none`,
`low`, `medium` or `high`. **They are applied when a run happens, not when the bank was built.**
The map is generated before any object is placed and a thumbnail draws lanes rather than objects,
so the roads, the routes and the pictures are identical at every level. What is stored here is
*declared intent*, and changing it is a manifest edit in the same class as `budget` — nothing is
rebuilt, no scenario row moves, and `base_config` keeps recording what generation actually used
(`traffic_density: 0.0`, `accident_prob: 0.0`).

That separation is the whole point. Pinned at generation time instead, changing a traffic level
would mean regenerating every scenario in the bank. Pinned as intent, it is one field.

The pin is a **default, not a lock**: a run flag still overrides it, and the result records the
levels actually used, so an override stays visible in the artifact afterwards.

**Writes:** `<bank>/manifest.json`. Nothing else.

### `scripts/bank-check.sh` — the one command CI and a human both run

```bash
./scripts/bank-check.sh
```

`ruff check` (the gate — `ruff format --check` deliberately is not), then `pytest`. There is no
per-bank step: `scenariobank verify` was cut on 2026-08-31 with the durable-bank premise it
enforced, since a bank is regenerated per batch and there is nothing to check it against.

**Writes:** `.ruff_cache/`, `.pytest_cache/` — both gitignored.

### `rig` and `replay --camera-rig` — the cameras a model reads, alive on the ego

```bash
uv run scenariobank rig --camera-rig rigs/av3.txt                                   # offline
uv run scenariobank rig --camera-rig rigs/av3.txt --check-frame --bank banks/curve  # measures
uv run scenariobank replay --bank banks/curve --camera-rig rigs/av3.txt --steps 20 --ignore-rig-rate
bash scripts/av3-probe.sh                                                           # both, host or container
```

`rigs/av3.txt` is the AV3 model's six-camera rig in CARLA's frame; `rigs/README.md` says where it
came from. `rig` converts it into MetaDrive's vehicle frame — an x/y swap and a sign flip on yaw,
not a rename — and prints each camera's mount beside the direction it aims in words, so a camera
named `front_left` that looks right is visible. `--check-frame` re-measures the frame itself on a
real car, six rows. `replay --camera-rig` mounts the rig and reads every camera at every decision:
MetaDrive silently deletes cameras from a headless env unless `image_observation` is on
(`base_env.py:343`), so "the sensors are there" is measured, never assumed. The cameras never
enter the observation, which stays 19 wide, and the expert's actions are identical with the rig
on and off (`tests/unit/test_camera_rig.py`).

The AV3 rig declares 0.05 s and a road steps at 10 Hz; nothing resamples, so the loader refuses
the mismatch. `--step-hz 100 --decision-hz 20` steps the road at the rig's own rate (below), and
`--ignore-rig-rate` is the switch for looking anyway at 10 Hz, on `replay` and on `run`. A model
that reads the rig refuses that switch.

**A film from the cameras** is `run --camera-rig --record-video`:

```bash
uv run scenariobank run --bank banks/curve --tier hard --policy scenariobank.policies:ExpertPolicy \
  --out film --camera-rig rigs/av3.txt --ignore-rig-rate --record-video
xdg-open out/film/hard/videos/curve_0000.rig.mp4
```

Beside the top-down `videos/<scenario_id>.mp4` it writes one mp4 per camera,
`<scenario_id>.<camera>.mp4` at the spec's size, and `<scenario_id>.rig.mp4`, every view tiled
three across, at the step rate so a 10 Hz road plays in real time. The pictures are the frames a
model would read, straight off the rig; the row's numbers are the numbers without the film.

**Writes:** `rig` nothing; `run` its record under `out/`, plus the films with `--record-video`.

### `av3`, `run --policy scenariobank.av3:AV3Policy` and `scripts/bridge.sh` — the model on the car

```bash
bash scripts/bridge.sh start                                # openpilot's planner and controller, TCP 5558
uv run scenariobank av3 --bank banks/t-junction --camera-rig rigs/av3.txt \
  --model-config ../models/model_dev.yml --no-model --step-hz 100 --decision-hz 20   # host, no torch
uv run scenariobank run --bank banks/t-junction --policy scenariobank.av3:BridgePolicy \
  --step-hz 100 --decision-hz 20 --out bridge                # the bridge alone, no model, no GPU
docker run --rm --gpus all --network host -v $PWD:/work:ro -v $PWD/../models:/models:ro \
  -e HOME=/tmp metadrive-wingfin-sim:latest python -m scenariobank run \
  --bank /work/banks/t-junction --policy scenariobank.av3:AV3Policy --camera-rig /work/rigs/av3.txt \
  --step-hz 100 --decision-hz 20 --model-config /models/model_dev.yml \
  --checkpoint /models/step_440000_trt_direct_full.ep --out /tmp/av3   # the submission
```

The AV3 submission is a TensorRT checkpoint that reads the six cameras and predicts twenty
waypoints two seconds ahead; openpilot's planner and controller, in the bridge container, turn
those into pedals. `scenariobank.av3:AV3Policy` is that path on a scored run: the rig read at
every decision, the model's ring and ego state fed, the forward pass against the route's
navigation block, the waypoints and their `modelv2` rows sent to the bridge, its reply negated
into MetaDrive's action. `scenariobank.av3:BridgePolicy` is the same path with the model taken
out — the bank's route resampled at the car's speed, wing-sim's `route_gt.py` — so the bridge,
the frame and both negations can be driven on a machine with no GPU. A scored row with the model
on the car takes about 1.5 s per decision, so `run` prints a heartbeat line every 10 s
(`--heartbeat SECONDS`, 0 for off): step, decision, speed, metres moved, route completed, the
action held. `moved 0.0 m` line after line is a stuck car; no lines at all is a hung run.

**The clock is `--step-hz 100 --decision-hz 20`.** The rig declares 0.05 s and the bridge is
written for 0.05 s; a road steps at 10 Hz by default, so a run for the AV3 stack steps it at
100 Hz with one physics step per `env.step` and decides every fifth, and every step budget is
scaled with it. `AV3Policy` refuses `--ignore-rig-rate`: a model reading a 20 Hz rig at 10 Hz is
the silently wrong frame rate.

**Six conversions stand between the model and the car and none of them raises when it is
wrong**, so `scenariobank av3` measures each before a run: the camera map by name, the ego
state against the car's own speed, the route block against the bridge's route points, the
predicted waypoints against where the car went under both sign conventions, and the model's
answer to a synthetic bend right and then left. `--no-model` checks the three that need no
forward pass, on the host. `--model-config` is the submission's `model_dev.yml`, every field
required and none defaulted; `--checkpoint` its `.ep`. `MODEL_CONFIG`, `MODEL_CHECKPOINT` and
`AV3_BRIDGE` (`host:port`) in the environment stand in for the flags.

**Writes:** `av3` nothing; `bridge.sh start` a container named `metadrive-wingfin-openpilot-bridge`.

## What gets generated, and where

| path | written by | in git? |
|---|---|---|
| `docs/reference/destinations.md` | `destinations` | yes — it is the Phase 1 deliverable |
| `docs/reference/commands.md` | `commands` | yes — every command and flag, read off the CLI |
| `docs/reference/importing.md` | `importing` | yes — what an import of a converter workspace must carry over |
| `docs/reference/figures/*.png` | `inspect` | yes — one per category at seed 0 |
| `banks/<bank-name>/` | `generate` | **no** — regenerate it; it is a per-batch artifact |
| `banks/<bank-name>/` | `import` | **no** — re-import it; the dataset is copied in, 50 MB at 100 Hz |
| `out/<name>/[<tier>/]` | `run` | **no** — a run's record; a relative `--out` lands here, a `--tier` is a subdirectory, and the container writes here |
| `.venv/`, `.ruff_cache/`, `.pytest_cache/` | tooling | no |

Nothing writes outside the repo, and no command writes to `$HOME`.

## Tests

```bash
uv run pytest        # 546 tests, ~90s with the sim group installed
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
