# Running the application

Three machines, one product. The **studio** -- the web page where a bank is authored and, later,
a run submitted -- is the only thing a person starts. Everything else is started by the studio
(a job is a subprocess of it) or by the rig agent (a container per job). This page is what to
type on each machine, and how to tell that it worked.

`docs/web-setup.md` is what the page does once it is up, and how to run it from a laptop venv
without Docker. `README.md` is why each command exists. `IMPLEMENTATION_PLAN.md` Phase 5 and
Phase 7 are the design behind the layout below. **This page is hand-written**, because what to
run, where, is not something the CLI can be asked.

---

## Where things run

| machine | runs | image(s) | GPU |
|---|---|---|---|
| **the NAS** | the studio, always up | `scenariobank-studio:85e5dad` -- the one image built in this repo, on top of the sim image | no |
| **a laptop** | the studio; one-off runs; the openpilot bridge for an AV3 run | the sim image, the bridge image, the studio image | yes, for an AV3 run |
| **each rig** | the rig agent (Phase 7, **not yet written**); per running job, one bridge and one sim container | `scenariobank-sim:latest`, the bridge image | one card per job |

The sim image is `metadrive-wingfin-sim:latest` where the converter checkout sits beside this
repo (the laptop) and `scenariobank-sim:latest` where it does not (a rig). `SIM_IMAGE` selects;
every script and `compose.yaml` read it.

## What every machine needs first

- **docker**, with compose v2 (`docker compose version` answers).
- **The NVIDIA driver and the NVIDIA container toolkit**, only where a GPU run happens: the
  laptop and the rigs. The NAS runs no GPU work and needs neither.
- **`uv`**, for the host-side commands (`uv run pytest`, `uv run scenariobank …`).
- **`git clone` of this repo** -- clone, not rsync or a zip. Ten paths in the vendored openpilot
  fork are symlinks, and a flattening transport makes the bridge build die on a missing
  SConscript.
- **`../models/` beside the repo**, holding `model_dev.yml` and `step_440000_trt_direct_full.ep`,
  on any machine that runs the AV3 model. The engine in the `.ep` is compiled for one GPU
  architecture; on another it fails to deserialize, and rebuilding it is the converter's job.

## Build the images

From the repo root, once per machine:

```bash
bash scripts/sim-image.sh build      # the sim image, ~20 min
bash scripts/sim-image.sh status     # is it here, and does its label cover what we need; ends "ready."
bash scripts/bridge.sh build         # the openpilot bridge, from docker/openpilot/, ~30 min
docker compose build studio          # the studio, seconds: ~10 MB on top of the sim image
```

`sim-image.sh build` runs the converter's own build when the converter checkout is beside the
repo and produces `metadrive-wingfin-sim:latest`. With no checkout it builds `docker/Dockerfile`
instead, as `scenariobank-sim:latest` -- the same thing minus the converter's parts. On such a
machine set `SIM_IMAGE=scenariobank-sim:latest` in the environment before anything else, or the
scripts look for the converter's tag and stop.

`sim-image.sh status` also prints a line for the studio image. `not built yet` there is
information, not a failure: the status ends in `ready.` when the sim image is right.

There is no registry. An image that is not on the machine is built on the machine, and compose's
own error for a missing one, `pull access denied`, is wrong -- run the build above.

## On the NAS -- the studio

The NAS is the producer: banks are authored here and, in Phase 7, runs are submitted from here.
It runs one container.

```bash
git clone <this repo> && cd metadrive-PG
bash scripts/sim-image.sh build                                   # once
docker compose build studio                                       # once, and again after pyproject.toml or uv.lock changes
DOCKER_UID=$(id -u) DOCKER_GID=$(id -g) docker compose up -d studio
docker compose logs -f studio                                     # "studio on http://127.0.0.1:8770/  (banks: banks)"
docker compose down                                               # stop it
```

**`DOCKER_UID` and `DOCKER_GID` are your uid and gid.** The studio writes banks and thumbnails
into the checkout, and they must belong to whoever runs it, not to root -- so `compose.yaml`
runs the studio as `${DOCKER_UID:-1000}:${DOCKER_GID:-1000}`. The default is right on a machine
whose first user is 1000; on any other, set the two as above, or put them in a `.env` file beside
`compose.yaml`:

```
DOCKER_UID=1001
DOCKER_GID=1001
```

Files owned by the wrong uid under `banks/` mean this was skipped.

**The studio binds 127.0.0.1 and nothing else, by design.** Its routes run subprocesses that
write into the repository and there is no login, so the server refuses any other address. On the
NAS that means a browser on another machine cannot reach it directly. The way that works today,
with nothing loosened, is an SSH tunnel from your own machine:

```bash
ssh -L 8770:127.0.0.1:8770 <nas>      # then open http://127.0.0.1:8770/ on your machine
```

Whether the NAS should instead front it with a reverse proxy, a tunnel of its own, or a
deliberate loosening behind authentication is undecided (`IMPLEMENTATION_PLAN.md`, Open question 6).
Until it is, the tunnel is the answer.

**After a NAS reboot, start it again by hand.** The studio service carries no restart policy
today, so `docker compose up -d studio` is the command after every reboot. (The `agent` service
does restart on its own; the studio does not, and that is a compose change to raise separately
rather than something to assume.)

**What it writes, and where:** `banks/<bank>/` and `.studio/` (the job log, the queue, candidate
seed pictures) inside the checkout, owned by `DOCKER_UID`. Nothing outside the checkout.

## On a laptop

The studio is the same three lines as the NAS's, and `docs/web-setup.md` has the venv way
(`uv run scenariobank studio`) when Docker is more than you want.

**One-off runs go through `scripts/sim-run.sh`**, which is the line a rig executes for a job --
image, mounts, user, environment, entrypoint, written once. The arguments after the command name
are written in the container's terms: this repo is `/work` (read-only), results go to `/out`,
the model is under `/models`.

```bash
bash scripts/sim-run.sh doctor
NO_GPU=1 bash scripts/sim-run.sh run --bank /work/banks/t-junction \
    --policy scenariobank.policies:ExpertPolicy --out /out/gate
GPU=0 BRIDGE_PORT=5558 bash scripts/sim-run.sh run --bank /work/banks/t-junction \
    --policy scenariobank.av3:AV3Policy --camera-rig /work/rigs/av3.txt \
    --model-config /models/model_dev.yml --checkpoint /models/step_440000_trt_direct_full.ep \
    --out /out/av3-check
```

| variable | when unset | |
|---|---|---|
| `SIM_IMAGE` | `metadrive-wingfin-sim:latest` | the image; `scenariobank-sim:latest` on a machine with no converter checkout |
| `GPU` | all cards | one card by index, `GPU=1` |
| `NO_GPU` | -- | set to anything for a run that needs no card (`generate`, the expert) |
| `BRIDGE_PORT` | the policy's own default, 5558 | where the openpilot bridge listens; exported as `AV3_BRIDGE` |
| `OUT_DIR` | `./out` | the host directory mounted at `/out` |
| `MODELS_DIR` | `../models` | the host directory mounted at `/models`, read-only |
| `NAME` | unnamed | the container's name, for `docker ps` and `docker logs` |

The sim container runs **as root**, deliberately: as the host uid the scored AV3 row hung for
four hours, as root it drives. So results under `out/` come out root-owned; `sudo rm -r out/<name>`
when clearing them. The studio is the service that runs as you, because it writes into the repo.

`docker compose run --rm run doctor` is the same container through compose -- the `run` service
is the laptop alias of the script, kept in step with it by hand.

**An AV3 run needs the bridge up first.** It is a second container, openpilot's real planner and
controller behind TCP 5558, and the runner talks to it over host networking:

```bash
bash scripts/bridge.sh status      # up, and listening?
bash scripts/bridge.sh start       # on 5558, host networking
bash scripts/bridge.sh stop        # remove the container; the image stays
docker logs metadrive-wingfin-openpilot-bridge 2>&1 | tail -3     # `bridge.sh logs` follows and never returns
```

The bridge container does not survive a reboot: `status` shows it `Exited (255)`, and the runner
reports `cannot reach the bridge at 127.0.0.1:5558 -- ConnectionRefusedError`. `stop`, then
`start`. The first `init` after a fresh start takes about 20 s while acados compiles; later ones
about 7 s.

**The laptop trap.** A laptop with 15 GB of RAM running the sim container with the model, the
bridge, a browser and desktop apps can hit the kernel's OOM killer, and the NVIDIA open kernel
module (595.91 as of this writing) answers that memory pressure by dropping the card off the bus.
The container then dies with `torch.AcceleratorError: CUDA error: unspecified launch failure`,
`nvidia-smi` says `No devices were found`, and only a reboot brings the card back. Close what is
not needed before an AV3 run. A rig has the memory; this is the laptop's problem.

## The queue replica

The real queue, `wfqueue`, runs on the NAS and is not routable from a development machine, so
this repo carries a double of it: `tests/support/fake_wfqueue.py`, a stdlib HTTP server over
SQLite that answers the contract in `docs/queue-docs/queue-doc-v0.json`. It exists to exercise
our code. **When the real server disagrees with it, the replica is wrong**, and where the
documentation is silent the replica had to guess. Its docstring lists every guess.

Nothing runs it by default. The contract tests build their own copy in memory on a random port:

```bash
uv run pytest tests/unit/test_queue_contract.py -q            # 9 passed, 9 skipped
```

The nine skips are the same assertions aimed at a real server. They need `WFQUEUE_URL`, so to
run all eighteen, start the replica in one terminal:

```bash
uv run python -m tests.support.fake_wfqueue --port 9090       # from the repo root
```

and in another:

```bash
WFQUEUE_URL=http://127.0.0.1:9090 uv run pytest tests/unit/test_queue_contract.py -q
```

### Driving it by hand

A full round trip. **Take both the message id and the lease id from the `lease` reply**, never
from the `put`: a queue hands out the oldest ready message, which is not necessarily the one you
just enqueued, and an ack that names the wrong message is refused with `409`.

```bash
Q=http://127.0.0.1:9090

# start clean: drop every message in the topic
curl -s -X POST $Q/topics/metadrive/purge -H 'Content-Type: application/json' -d '{}'

# put one
curl -s -X POST $Q/topics/metadrive/messages -H 'Content-Type: application/json' \
  -d '{"payload": {"job_id": "j2"}}'

# lease it, and keep the reply: the id and the lease_id both come from HERE
curl -s -X POST $Q/topics/metadrive/lease -H 'Content-Type: application/json' \
  -d '{"visibility_timeout": 30, "consumer": "laptop"}' | tee /tmp/lease.json

ID=$(python3 -c 'import json; print(json.load(open("/tmp/lease.json"))["messages"][0]["id"])')
LEASE=$(python3 -c 'import json; print(json.load(open("/tmp/lease.json"))["messages"][0]["lease_id"])')

curl -s -X POST $Q/messages/$ID/ack -H 'Content-Type: application/json' -d "{\"lease_id\": \"$LEASE\"}"
curl -s $Q/topics
```

The last line ends at `ready: 0, leased: 0, done: 1`.

`GET /topics` is names and per-state counts, which is all the queue's own documentation promises
of it. The fifteen-field record of a message is on `GET /topics/metadrive/messages`, and
`GET /topics/metadrive/stats` adds the visible depth and the age of the oldest backlog. The
`consumer` label survives the ack, so a `done` message still records which rig and which card ran
it; that is the field Phase 7 Step 6 reads when it writes the results notes.

**Three things that will catch you out.**

- **The database persists.** It is `.studio/fake-wfqueue.sqlite` by default, already ignored by
  git, and it keeps its messages across a restart on purpose, because the studio and the agent
  are developed against it. Ids therefore keep climbing and never restart at 1. Reset with the
  `purge` call above, or stop the server and delete the file.
- **A running server holds the code it started with.** After editing the replica, stop it and
  start it again, or you are testing yesterday's copy.
- **An unacked lease comes back by itself.** That is the point of a visibility timeout, and it is
  the queue's at-least-once delivery working. The message returns to `ready` with `attempts`
  incremented and is handed out again, so a leftover from an earlier session is the message your
  next `lease` receives.

## On a rig

Today a rig runs the laptop's commands with the fallback image. Phase 7's agent, which will lease
jobs from the queue and run them without a person, is declared in `compose.yaml` as the `agent`
service but its code does not exist yet (`IMPLEMENTATION_PLAN.md`, Phase 7 Step 5); `docker
compose --profile rig up -d agent` will be the rig's one line when it does.

```bash
git clone <this repo> && cd metadrive-PG            # and ../models/ beside it
export SIM_IMAGE=scenariobank-sim:latest             # no converter checkout here; put it in the shell profile
bash scripts/sim-image.sh build && bash scripts/bridge.sh build
bash scripts/bridge.sh start
GPU=0 BRIDGE_PORT=5558 bash scripts/sim-run.sh run …  # the AV3 line above
```

A rig that *does* have the converter checkout beside the repo -- the first one did, on
2026-09-14 -- gets the converter's image from `sim-image.sh build`, not this repo's. To build
what a bare rig would build, point the script away from it: `CONVERTER_DIR=/nonexistent bash
scripts/sim-image.sh build`. Both images give the same results on the same machine (checked
on the laptop and on the rig, byte for byte); the choice is about what the rig has to clone,
not about the numbers.

The rig has no studio and no compose service to start today. A rig with two busy cards will run
five containers -- the agent, two bridges, two simulators -- and nothing on it listens on a port
other than the bridges on 127.0.0.1.

## The two locks, and the rig you share

A rig's cards are taken with `flock`, in `~/simulation` (`SIMULATION_ROOT`, the same variable
wing-sim's `deployment/with_rig_lock.sh` reads). There are two lock files and they are not the
same kind of lock:

| file | who | how |
|---|---|---|
| `.wing-sim.gpu.lock` | the whole rig. wing-sim's, by name and by path | we take it **shared**, everyone else takes it exclusive |
| `.wing-sim.gpu<N>.lock` | one card. ours | **exclusive** |

A CARLA evaluation takes the whole machine, so it must not run beside us; two of our own cards
are two resources, so they must run beside each other. A shared rig lock is exactly that
relation. What you see on a rig:

- while any card of ours is running, `with_rig_lock.sh` exits **99** -- its own words are
  *"the GPU is in use — this run did NOT start"*. It names our pid when it can; our container
  runs as root, so its `fuser` often cannot read our descriptors and prints the refusal with no
  pid at all. The holder file below is then the thing to read;
- while CARLA or a hand-run script holds the rig lock, **every** card of ours waits. Until
  wing-sim names its lock per device, a two-GPU rig behaves as a one-GPU rig for as long as
  CARLA is running, and not a second longer.

Nothing of ours ever deletes, renames, steals or breaks a lock. Exclusion is a property of the
inode, so a file that is replaced excludes nobody, silently -- which is also why who-holds-it is
published in a *separate* file, `.scenariobank.gpu<N>.holder.json`, beside the lock. Read it to
find the job, the attempt, the container and the host. It is advisory: believe it only while the
lock is held.

To ask who has a card, from the repo on the rig:

```bash
uv run python -c "
from scenariobank.agent.lock import CardLock
lock = CardLock(gpu=0)
for scope in ('rig', 'card'):
    holding = lock.look(scope)
    print(holding.sentence() if holding else f'{scope}: free')
"
```

**The agent container must run with `--pid host`** (`pid: host` in `compose.yaml`).
`/proc/locks` is filtered by PID namespace: a container sees the locks taken inside it and none
of the host's. Measured on the rig with this repo's sim image -- 0 rows against a host holding
18, then its own 2 once it locked. Nothing can be double-booked without the flag, because `flock`
answers correctly across namespaces, and the lock still confirms itself. What is lost is the
answer to *who* has a card: a holder outside the container becomes invisible, and the helper then
says "held, by whom this process cannot see" rather than "free". For a separate reason the lock
directory must be local disk: on an NFS or SMB mount `flock(2)` is emulated and excludes nobody.

## One job, one container

The line a rig's agent will issue (Phase 7 Step 5) is the line you can issue by hand today. The
container reads a `Job` file -- the same JSON the studio submits and the queue carries -- runs
it, and writes everything a supervisor needs into `--out`. Nobody parses a printed line.

```bash
mkdir -p out/j7
cat > out/j7/job.json <<'JSON'
{
  "schema_version": 1,
  "job_id": "j7",
  "attempt": 1,
  "bank": {"id": "t-junction", "path": "/work/banks/t-junction"},
  "scenarios": ["t_junction_0000"],
  "policy": "scenariobank.policies:ExpertPolicy"
}
JSON

NO_GPU=1 SIM_IMAGE=scenariobank-sim:latest NAME=j7 \
    bash scripts/sim-run.sh run --job /out/j7/job.json --out /out/j7 --events

cat out/j7/exit_code
find out/j7 -type f | sort
```

**Every path in the job file is the container's**, not the host's: `/work` is this repo mounted
read-only, `/out` is `OUT_DIR` (`./out` unless you set it), `/models` is the checkpoint
directory. So the job above names `/work/banks/t-junction` and is itself read from
`/out/j7/job.json` -- write it into `out/`, which is the one directory the container can read
*and* you can write. A rig's agent writes the same file into that rig's local `/out/<job_id>`.

That run, on the laptop, prints five lines and leaves seven files:

```
{"schema_version": 1, "event": "run.started", …, "out": "/out/j7", "policy": "scenariobank.policies:ExpertPolicy", "pid": 1, "host": "keith-82y7"}
{"schema_version": 1, "event": "batch.started", …, "n": 1, "scenarios": ["t_junction_0000"], "step_hz": 10.0, "stride": 1}
{"schema_version": 1, "event": "scenario.started", …, "scenario_id": "t_junction_0000", "index": 1, "n": 1, "max_steps": 320}
{"schema_version": 1, "event": "scenario.finished", …, "status": "ok", "success": true, "steps": 139, "wall_time_s": 0.788}
{"schema_version": 1, "event": "run.finished", …, "outcome": "ok", "exit_code": 0, "stopped": false, "n": 1, "success_rate": 1.0}
```

| what | when it appears | what it is for |
|---|---|---|
| `batch.json` | every refusal has passed, before the simulator opens | the bar's denominator: `n` and the scenario ids in order |
| `starts/<id>.json` | each scenario is about to be built | which row is running, and that row's own `max_steps` |
| `results/<id>.json` | each scenario ends | the scored row, whatever ended it |
| `results.json` | the batch ends | the record everything downstream reads |
| `events.jsonl` | throughout | the five lines above, whether or not you passed `--events` |
| `exit_code` | last, always | `0` ran, `1` failed, `2` the command line was wrong |

**A progress bar needs none of the log**: the denominator is `batch.json`'s `n`, the numerator is
the number of files in `results/`, and the row running now is the one in `starts/` with no result
beside it yet. That is what lets an agent restarted mid-run describe the run correctly -- it
kept nothing in memory, so there is nothing to have lost.

**`--events` puts the stream on stdout and takes the summary line off it.** Without the flag you
get the usual `results written: …` line and the same events in the file. A container's stdout
also carries panda3d's and torch's own chatter, so a reader keeps the lines that parse as JSON
and drops the rest; `events.jsonl` holds only ours.

**`docker stop` is a scored partial run, not a lost one.** The SIGTERM reaches the batch's flag,
the scenario it lands in ends with `failure_reason: "stopped"`, every row so far is written, the
env is closed on the normal path and the process exits **0** -- `run.finished` says
`"stopped": true`. Measured on 2026-09-14: a five-scenario job stopped after the second started
kept both rows and ended `success_rate: 0.5`, `exit_code: 0`.

**`exit_code` is the one file to look for, and its absence is an answer.** It is written whatever
happened -- a job file that will not parse leaves one too -- staged and renamed, so a half-written
file can never be read as a `0`. No file at all means the container was killed outright: SIGKILL,
the OOM killer, or the machine going down.

**`run.finished` carries the one judgement a supervisor acts on.** `"permanent": true` means
nothing ran and nothing this machine does will change that -- the bank at that path is a
different bank, a scenario id is not in it, the policy will not import -- so the job is
dead-lettered rather than spending its remaining attempts. A failure after `batch.started` is
not permanent: it may be the card, the driver or the bridge, and those are worth another rig.

Two things to know before reading a directory twice. **An attempt's results overwrite and its
events do not**: `events.jsonl` is appended, so a second attempt landing in the directory the
first one used keeps both streams, and every line carries its `attempt`. And **a job that names a
tier writes its batch into a subdirectory** (`--out out/j7` with `"tier": "hard"` writes
`out/j7/hard/results.json`), while `events.jsonl` and `exit_code` stay in the directory you
named: they belong to the process, not to the batch.

## Did it work?

Three checks, in the order the machines come up.

**The sim container runs this repo**, on any machine:

```bash
bash scripts/sim-run.sh doctor
# commit:            85e5dadc6c7436d324348f6e3d8f8e680c06b4db
# requested:         85e5dadc
# …
# drive_side:        left
```

The commit and the requested commit agree, and `drive_side` is `left`. Anything else is the wrong
image.

**Two machines agree on a bank** -- the check `IMPLEMENTATION_PLAN.md` Phase 5 Step 4 calls the
gate. Run the expert on the same bank on both, copy one `results.json` beside the other, and
compare the outcome fields. Do not compare `actions_digest` or `reward` across machines: on
2026-09-14 an Intel laptop and an AMD rig agreed on every step count, status, route completion,
cost and collision, and disagreed on every action digest and on rewards in the seventh
significant digit. The digest hashes actions at six decimals, and two CPUs do not round the
same way. On one machine the digest is exact, and the two images give identical files.

```bash
FIELDS='.results[] | {scenario_id, status, steps, route_completion, cost, collisions, failure_reason, actor_layout_digest}'
diff <(jq "$FIELDS" out/gate/results.json) <(jq "$FIELDS" out/gate-rig/results.json) && echo "the two machines agree"
```

**The studio answers**, on the NAS and the laptop, with the same commit:

```bash
curl -s http://127.0.0.1:8770/api/doctor | jq -r '.commit'
# 85e5dadc6c7436d324348f6e3d8f8e680c06b4db
```

**A bank from the page belongs to you.** On the page, Run tab, `generate`: bank id
`studio-check`, output `banks/studio-check`, category `curve`, seeds `1`. When the job log says
`1 scenarios in 1 categories -> /work/banks/studio-check/manifest.json`:

```bash
ls -ln banks/studio-check/manifest.json banks/studio-check/thumbs/
# -rw-r--r-- 1 1000 1000 … manifest.json          <- your uid and gid, not 0 0
```

## When it does not

| what you see | what it means | what to do |
|---|---|---|
| `pull access denied for metadrive-wingfin-sim` | the image is not on this machine, and there is no registry | `bash scripts/sim-image.sh build` |
| `sim image … is not ready` from `sim-run.sh` | the image is missing, or was built before a group was added | the build command it prints |
| `bridge.sh status` shows `Exited (255)` | the bridge did not survive a reboot | `bash scripts/bridge.sh stop && bash scripts/bridge.sh start` |
| `cannot reach the bridge at 127.0.0.1:5558 -- ConnectionRefusedError` | no bridge is listening | `bridge.sh status`, then `start` |
| files under `banks/` owned by `0` or by a uid that is not yours | the studio ran as the wrong user | set `DOCKER_UID` and `DOCKER_GID`; `docker compose down` and up again |
| `KeyError: getpwuid(): uid not found` in a job log | the `/etc/passwd` mount is missing from the studio service | restore it in `compose.yaml`; the tests pin it |
| the page answers but every job dies at `import metadrive` | the studio was built `FROM` something other than the sim image | check `SIM_IMAGE` at build time; `docker compose build studio` |
| `CUDA error: unspecified launch failure`, then `nvidia-smi` says `No devices were found` | the card fell off the bus after memory pressure | reboot; close the browser and desktop apps before the next AV3 run |
| `started_utc` hours adrift of the host's clock | `/etc/localtime` is not mounted | the scripts and compose mount it; use them rather than a bare `docker run` |
