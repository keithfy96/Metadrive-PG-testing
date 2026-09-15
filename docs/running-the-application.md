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
seed pictures, and `results.sqlite`, the index of what the rigs delivered) inside the checkout,
owned by `DOCKER_UID`. Nothing outside the checkout, and nothing on the share: the studio reads
`results/` there and writes nothing into it (see "Results: the store").

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

Today a rig runs the laptop's commands with the fallback image, plus `scenariobank agent --once`
for a whole job at a time (below). What does not exist yet is the **loop**: leasing from the
queue without a person (`IMPLEMENTATION_PLAN.md`, Phase 7 Step 5). `docker compose --profile rig
up -d agent` will be the rig's one line when it does; until then the `agent` service starts and
refuses, naming that step.

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

This is the line the agent issues for you, and the one to issue by hand when you want to watch a
single container rather than a whole session. The
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

## One job, start to finish: `agent --once`

Everything above, as one command: take the card, start that card's bridge if the job needs one,
run the container, follow it, deliver the results, release. No queue is involved -- the job comes
from a file -- which is how the rig half is provable long before there is a queue to prove it
against (Phase 7 Step 3).

```bash
cat > /tmp/job.json <<'JSON'
{
  "schema_version": 1,
  "job_id": "j7",
  "attempt": 1,
  "bank": {"id": "t-junction", "path": "ignored on a rig"},
  "scenarios": ["t_junction_0000"],
  "policy": "scenariobank.policies:ExpertPolicy"
}
JSON

uv run scenariobank agent --once /tmp/job.json --gpu 0 --no-gpu     # laptop: a card's lock, no card
uv run scenariobank agent --once /tmp/job.json --gpu 0              # a rig: card 0, bridge on 5600
```

**A job carries names and the rig supplies the paths.** `bank.id` is looked up under
`SCENARIOBANK_BANKS`, a checkpoint *file name* under `SCENARIOBANK_MODELS`, and `bank.path` as
submitted is the submitter's own machine's and is never read -- the agent rewrites it to `/bank`,
the one bank it bind-mounts into the container. That is what lets one job run on either rig
whatever each has mounted where.

| variable | what it is | with none of them set |
|---|---|---|
| `SCENARIOBANK_SHARE` | the NAS share; the three below default to `banks/`, `models/`, `results/` under it | — |
| `SCENARIOBANK_BANKS` | where `bank.id` is looked up | this checkout's `banks/` |
| `SCENARIOBANK_MODELS` | where a checkpoint name is looked up | `../models`, beside the repo |
| `SCENARIOBANK_RESULTS` | where finished jobs are delivered | `out/results/` |
| `SCENARIOBANK_OUT` | this rig's local disk, where the container writes | `out/` |

So a fresh clone runs the command above with nothing mounted at all, and a rig sets one variable.

**Refused, busy and failed are three different answers**, and the exit code says which:

| exit | meaning | what a queue worker does with it (Step 5) |
|---|---|---|
| 0 | ran -- a stopped run included | ack |
| 1 | the run failed, or the rig could not start it | nack, retry |
| 2 | the command line was wrong | — |
| 3 | the job can never run here or anywhere | nack, **dead-letter** |
| 4 | the card is busy | nothing: no job was taken |

**Everything that can refuse a job happens before the card is taken.** A bank id that is not on
this share, a manifest that disagrees about which bank it is, a scenario id the bank does not
hold, a checkpoint name that matches two files -- each is exit 3 with the GPU still free, because
taking a card to find that out is a card idle for the length of a docker pull and, on the queue,
an attempt spent for nothing.

**The run is a sibling container and outlives the agent.** Restarting the agent -- or killing it
outright -- leaves the run driving, and running the same command again **adopts** it rather than
starting a second: the container carries `scenariobank.job-id` and `scenariobank.gpu` labels, and
that query is the recovery path. Measured on the laptop, both ways: killed while the run was on
its first row, the restarted agent picked it up at 3/5 and delivered at 5/5; killed and restarted
after the run had already finished, it read the exited container's exit code and delivered that.
Exactly one `run.started` event in both, which is the thing to check -- it is one per container.

**Ctrl-C is a scored partial run, not a lost one.** SIGINT or SIGTERM to the agent stops the
container with a 30-second grace rather than dying and leaving it driving; the row it lands in
ends `failure_reason: "stopped"`, every row already scored is kept, and the partial result is
delivered like any other. The outcome says `stopped`, so nobody has to infer it from an exit code
of 0.

**Delivery is a copy and a rename.** The container writes to `SCENARIOBANK_OUT/<job_id>` on local
disk; the agent copies that to `<results>/<name>.partial` and renames it into
`<results>/<name>`. So a directory without `.partial` is always complete, and a
`results/<job_id>` that already exists means *done* -- running the same job again prints
`already delivered` and exits 0 without touching a card. Delete that directory to run it again.

**A failed run is delivered too, but never under the job's own name.** `completed` and `stopped`
land at `results/<job_id>`; a failure, a refusal or a vanishing lands at
`results/<job_id>.attempt<N>` beside it. The evidence is kept either way -- what changes is which
name means *finished*, because that name is the redelivery guard, and a failure delivered under it
would tell the next worker to ack a job that has to be tried again.

What one finished job looks like -- the run's own seven files, plus two the agent adds:

```
out/results/j7/job.json          <- the job as the container was given it, paths and all
out/results/j7/container.log     <- the container's stdout, kept before it was removed
out/results/j7/events.jsonl      <- the process's stream
out/results/j7/exit_code         <- 0
out/results/j7/batch.json  starts/  results/  results.json
```

A job that names a tier puts the last four under it (`out/results/j7/hard/results.json`) and
leaves `events.jsonl` and `exit_code` where they are: those two belong to the process, not the
batch.

**The bridge is one per card, on 5600 + the card index** -- gpu0 on 5600, gpu1 on 5601 -- and is
started only for the two policies that talk to one (`scenariobank.av3:AV3Policy` and
`:BridgePolicy`). It is deliberately **not** 5558: wing-sim's zapeta bridge listens there on the
same rig with the same host networking, so a collision with theirs is an error rather than our
simulator driving against their planner. A bridge already up is reused, and it is left running
when the job ends -- it is the card's, not the job's. `BRIDGE_IMAGE` picks the tag if this
machine built it under another name.

**Two cards run at once.** Two `agent --once` on `--gpu 0` and `--gpu 1` hold both cards
together, run two containers, and deliver two results -- which is the shared rig lock working
(above). A third on either card exits 4.

## The loop: `agent`

`agent` with no `--once` is the rig's service (Phase 7 Step 5): one process, one worker per card,
each doing what `--once` does with a queue message where the file was -- take the card, lease one
job, run it, deliver, ack, release, again. The queue is the replica above until the real one is
reachable (Open question 9), and the whole loop runs on a laptop with `--no-gpu`.

```bash
uv run python -m tests.support.fake_wfqueue --port 9091          # one terminal, from the repo root

uv run python - <<'EOF'                                          # put two jobs
from scenariobank.agent.wfqueue_client import QueueClient
q = QueueClient("http://127.0.0.1:9091")
q.create_topic("metadrive")
for job_id in ("loop-1", "loop-2"):
    q.put("metadrive", {"schema_version": 1, "job_id": job_id,
                        "bank": {"id": "t-junction", "path": "ignored on a rig"},
                        "scenarios": ["t_junction_0000"],
                        "policy": "scenariobank.policies:ExpertPolicy"}, dedupe_key=job_id)
EOF

uv run scenariobank agent --gpu 0 --gpu 1 --no-gpu --queue http://127.0.0.1:9091 --max-jobs 1
```

Two workers, one job each, two containers at once, two directories under `out/results/`, and
the queue's `GET /topics/metadrive/messages` shows both `done` with `consumer` `<host>:gpu0`
and `<host>:gpu1` -- that label is the "what is running where" view, and it survives the ack.
`--max-jobs 1` stops each worker after one settled job; a rig runs without it, forever. On a rig
the same thing is `docker compose --profile rig up -d agent`, with `SCENARIOBANK_GPUS=0,1`
naming the cards and `WFQUEUE_URL` the queue.

**Lock first, then lease, and the lock is held for milliseconds between jobs.** A worker takes
its card, asks the queue for one message with no wait, and if there is none gives the card
straight back and sleeps five seconds outside it. So an empty queue is a rig CARLA can have, and
a card somebody else holds costs the queue nothing: the message stays `ready` with `attempts`
0 until the card is free, and the status file says who has it.

```bash
flock -x -n ~/simulation/.wing-sim.gpu0.lock sleep 8 &            # somebody else's card, for 8 s
uv run scenariobank agent --gpu 0 --no-gpu --queue http://127.0.0.1:9091 --max-jobs 1
cat out/results/status/$(hostname)-gpu0.json                      # "busy", naming the holder's pid
```

**What the queue is told, and when.** Every answer is after delivery, so a lost copy is a retried
job and never a lost result:

| the run | delivered to | the message |
|---|---|---|
| `completed` or `stopped` | `results/<job_id>` | `ack` |
| refused -- `permanent: true`, or a job the share cannot satisfy | `results/<job_id>.attempt<N>` (or nothing, if it never launched) | `nack(dead=True)`, with the reason as `last_error` |
| failed, vanished, or the rig could not start it | `results/<job_id>.attempt<N>` | `nack(retry_after=60)`: back to `ready` for this rig or the other |
| already at `results/<job_id>` | -- | `ack` without running |

`<N>` is the queue's own `attempts`, not the payload's `attempt`: the container name, the holder
record and the delivery name all follow the count the queue keeps.

**Stopping the agent leaves the run driving, and the restarted agent finishes it.** This is the
opposite of `--once`, on purpose: a rig's agent is restarted by `docker stop`, by a redeploy, by
the machine, and none of those may cost a twenty-minute drive. On SIGTERM a worker mid-run
extends its lease once more (ten minutes), leaves the holder record beside the card lock -- it
carries the message id and the lease id -- and exits. The next agent finds the container by its
labels before it leases anything, picks the lease up from the record (`extend` on a live lease
is accepted), supervises the run to its end, delivers it and **acks it itself**. Measured both
ways on the laptop with the five-scenario bank: `kill -9` on the agent mid-run, and SIGTERM
mid-run; each restart logged `adopting … lease=live` and then `ack … completed, 5/5`, with
exactly one `run.started` in the delivered `events.jsonl` and no container left behind. If the
lease has expired by the time the agent is back, the run is still finished and delivered, and
the redelivered message meets `results/<job_id>` and is acked without a run.

**A lease is a clock.** The worker asks for a three-minute lease and extends it every thirty
seconds for as long as the container runs; a lease lost mid-run (`409` on the extend) does not
stop the run -- that would turn a duplicate into a loss -- and the ack afterwards asks the queue
what became of the message: `done` means the first ack landed.

**Two files the loop keeps.** `results/status/<host>-gpu<N>.json` on the share is rewritten
whenever the card's state changes and every ten seconds while a run is up: the state (`idle`,
`busy`, `running`, `handing over`, `stopped`), the job and its progress read off the record
directory, the holder when busy, disk free on the rig's out root, the agent version. And the
rig's own `SCENARIOBANK_OUT/<job_id>` is swept a day after delivery -- only where
`results/<job_id>` exists on the share, and never while a container of ours names it.

## Results: the store

The agent delivers into `results/` on the share -- `results/<job_id>/` for a run that ran, and
`results/<job_id>.attempt<N>/` for one that did not -- and that tree is the truth. The studio
reads it and keeps an index of it in `.studio/results.sqlite`, **on its own disk and never on the
share**: SQLite over NFS or SMB corrupts, its own documentation says so, so the file on the share
is never the database. The index is a cache. Delete it and the next request rebuilds it from the
tree.

```bash
uv run scenariobank results                       # the tree SCENARIOBANK_SHARE names, or out/results
# /mnt/scenariobank/results: added 6, skipped 0, invalid 0; 6 in .studio/results.sqlite
#   name        status    bank         n  success  delivered             host  policy
#   rig-step5-1 complete  t-junction   1     1.00  2026-09-15T04:10:22Z  sim   scenariobank.policies:ExpertPolicy
#   …
#   rig sim:gpu0: idle  (updated 2026-09-15T04:31:02Z)
uv run scenariobank results                       # again: added 0, skipped 6
uv run scenariobank results --rebuild             # start the index over
uv run scenariobank results --json | jq '.jobs[0]'
```

The same store answers on the page: `GET /api/results` lists what was delivered and indexes any
new directory on the way (a listdir and a set difference -- a delivered directory never changes,
so one already indexed is skipped by name; `?rebuild=true` starts the index over),
`GET /api/results/<name>` is one delivery's job and its per-scenario rows, and `GET /api/rigs`
is what each card is doing, read off the status files the agents write under `results/status/`.
No port is open on a rig for any of it.

What the index holds is decided by what is comparable across machines: per row, `status`,
`success`, `steps`, `route_completion`, `cost`, `collisions`, `failure_reason`, `wall_time_s`
and `actor_layout_digest`, keyed by job **and** scenario, because the same scenario legitimately
scores under many jobs. `actions_digest` and `reward` are not columns -- two CPUs disagree on
them and that is not a defect ("Did it work?", below).

Three things the listing shows on purpose rather than hiding:

- **`invalid`**: a `results.json` that does not validate against the record's own model, with
  the error on the next line. The file is listed, never skipped -- a result silently missing is
  the one failure nobody can debug from a page. This is the check on results Phase 7 Step 5
  deferred to the store.
- **`failed`**, kind `attempt`: a `<job_id>.attempt<N>` directory, a run that did not run --
  its exit code and whether a `container.log` exists, and no rows. The job's own name is still
  free, so the next attempt lands beside it.
- **`stopped`**: a batch the agent was told to end; the rows present ran, the rest did not.

**Deploying it on the NAS is one variable.** The studio resolves the tree the way the agent does,
from `SCENARIOBANK_SHARE`; `compose.yaml` passes it through and mounts the share at its own
path, the way the agent's service does. Unset, both read `out/results` in the checkout, which is
where a laptop's `agent --once` delivers. The day the mount exists (`IMPLEMENTATION_PLAN.md`,
Open question 8):

```bash
SCENARIOBANK_SHARE=/mnt/scenariobank DOCKER_UID=$(id -u) DOCKER_GID=$(id -g) docker compose up -d studio
docker compose logs studio | tail -1     # "… (banks: banks, results: /mnt/scenariobank/results)"
```

The index carries a version (SQLite's `user_version`). An index of another version is dropped
and read again from the tree the next time it is opened -- a schema change costs one rescan and
never a migration, which is what a cache is for.

## The options and the estimate

Two more routes the submit screen (Phase 2c Step 12) is drawn from, both served by the same
studio and needing no simulator:

- `GET /api/options` is the six option axes as data: each axis's label, its four level names
  with the number behind each, whether a raw number may be given instead and what shape it must
  take, and `choices`, the levels that run today -- `lights` offers `none` alone until Phase 8
  and says why in `restricted`. The tiers are there too, as the six names each expands to. A
  form drawn from this cannot drift from the resolver, because the resolver reads the same
  tables (`options.describe()`).
- `GET /api/eta?bank=<name>&policy=<path>[&tier=hard][&traffic=high…][&scenarios=a,b][&host=sim]`
  is how long a run would take, in seconds. The levels are resolved the way `run` resolves them
  (the bank's pinned block, then the tier, then any axis named), so a level that would be
  refused at run time is refused here too, as a 400.

The estimate is a **median** of the newest scored rows in the results index -- median, not
mean, because one degraded run is a 5x outlier that would poison a mean for weeks -- for the
same policy on the same road. Wall time depends first on the policy (the camera model through
the bridge is two orders slower than the bundled expert), then on the rig, then on the
difficulty, so the rows are narrowed to the named `host` and the resolved levels when there are
enough of them (three), and widened one step at a time when there are not; each category in
the answer says which subset it came from (`host`, `levels_matched`, `n`, `note`). Before any
run of a policy has been delivered there are two measured stand-ins, and each answer names the
one it used in `source`:

- `calibration`: the sweeps under `docs/reference/calibration/`, for the expert only. They are
  the expert driving one axis at a time with the rest at `none`, so for a difficulty that sets
  several the slowest axis is taken rather than the sum.
- `measured`: `docs/reference/wall-times.json`, the camera model's per-scenario time as
  measured on the rig (and, not by default, on the laptop) -- one road's figure, copied there
  with its provenance rather than re-measured, because a bank of that model is an hour and an
  absent estimate is a visible gap. With no `host` named, the rigs' figure is what a submit
  screen shows; a laptop's never is.

`complete` is false when a category has no number at all, and `seconds` is then a floor;
`missing` names the categories. The first delivered run of a policy on a road replaces every
stand-in for it. The rig that scored a delivery is read off its `events.jsonl`
(`run.started.host`), which is why `scenariobank results` now shows a `host` column and why
the per-rig narrowing works without the agent writing anything new.

```bash
uv run --group web scenariobank studio
curl -s 'http://127.0.0.1:8770/api/options' | jq '.axes[] | {name, choices}'
curl -s 'http://127.0.0.1:8770/api/eta?bank=t-junction&policy=scenariobank.policies:ExpertPolicy' \
  | jq '{seconds, complete, per_category}'
```

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
