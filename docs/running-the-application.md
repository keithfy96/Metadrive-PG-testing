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
deliberate loosening behind authentication is undecided (`IMPLEMENTATION_PLAN.md`, Still open 6).
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
