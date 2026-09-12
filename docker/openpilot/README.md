# The openpilot bridge container

The second of the two containers an AV3 drive needs (Phase 4 Step 7). `docker/Dockerfile`
builds the sim image — MetaDrive, scenariobank and the AV3 model. This one builds the
**controller**:
openpilot's `plannerd` + `controlsd` behind a length-prefixed JSON/TCP server on port 5558.

They cannot be one image. The fork is Python 3.8 (its own deps pin it — `casadi==3.5.5` ships no
cp310 wheel — and it carries cp38 compiled extensions), and torch 2.8, which the AV3 model needs,
has no 3.8 wheel. So there are two, and `network_mode: host` is what lets them find each other on
`127.0.0.1`.

## Building it

```bash
bash scripts/bridge.sh build       # budget half an hour the first time
bash scripts/bridge.sh start       # runs it as `metadrive-wingfin-openpilot-bridge` on port 5558
bash scripts/bridge.sh status      # is it up?
```

*Carried whole from `../wingfin-osm-scenarionet-converter/docker/openpilot/` on 2026-09-12 --
Dockerfile, `bridge/`, `pull.sh` and the vendored fork byte for byte; only this README's head
was reworded. The rest of it is the converter's account and still true here.*

**That is the whole prerequisite.** Clone this repo, run those two commands, and tier 4 works —
no fork to fetch, no SSH keys, no submodules, no LFS. That is the point of the arrangement below.

Budget half an hour with nothing cached. The apt + pyenv + poetry base dominates it and is the
part not timed here; then comes the scons compile of cereal, boardd and the two MPC libraries, and
the acados solver prebuild, which took under 5 s. The image is 5.5 GB. A later build that touches
only `bridge/` takes seconds — the Dockerfile copies it in last on purpose.

## Everything is here, including the fork

| | size | what it is |
|---|---|---|
| `Dockerfile` | 5 KB | carried verbatim from wing-sim's `docker/Dockerfile.openpilot` |
| `bridge/` | 100 KB | 11 files, 1649 lines — the zapeta bridge server itself |
| `deps/openpilot/` | **309 MB** | the openpilot fork, **vendored** at a pinned commit |
| `pull.sh` | 4 KB | re-vendoring only; `bridge.sh build` does not run it |

**`deps/openpilot/` is a flat copy, not a checkout.** `git@github.com:zapetaai/openpilot.git` at
`c767ace8`, with its `.git` and its seven submodule gitdir pointers removed — git will not track
a directory containing one. `deps/openpilot/VENDORED.md` records the commit, every submodule SHA,
and the two edits made to it.

It is carried rather than fetched because fetching it needs SSH access to a private org, and that
is access nobody can grant a rig remotely. A drive on a fresh machine used to die on
`ConnectionRefusedError` a minute into a terrain build, from a component the machine had no way
to obtain. It costs a 309 MB clone once; it cost setup access every time.

**Two things about it that are easy to get wrong:**

- **`git add -f`, not `git add`.** The fork ships 75 of its own `.gitignore` files, and they
  exclude the prebuilt binaries the build links against — `third_party/acados/x86_64/lib/*.so`
  among them, which is what the lateral and longitudinal MPC load. A plain `git add` silently
  skips them and the tree looks complete.
- **It must arrive by `git clone`, not by `rsync` or a zip.** Ten paths in it are symlinks
  (mode 120000): `rednose`, `laika`, `tinygrad`, `selfdrive/hardware` and six under
  `third_party`. A transport that flattens them makes scons die on
  `Missing SConscript 'rednose/SConscript'`, which reads like a broken Dockerfile and is not.
  `bridge.sh build` checks all ten before it starts and refuses with that explanation.

## Carrying the built image instead

Still the right move for a machine with no network, or one where 40 minutes of build matters
more than 5.5 GB of transfer:

```bash
cd scripts && ./bridge.sh save /tmp/bridge-image.tar.gz
# then, on the other machine
gunzip < bridge-image.tar.gz | docker load
```

## Two things about the build

**The context is this directory, not the repo root.** `COPY deps/openpilot/` and `COPY bridge/`
in the Dockerfile are relative to it, and the layout here mirrors the one the recipe was written
for — which is why not a line of it changed in the move. `bridge.sh build` passes the right
context; a hand-rolled `docker build` from the repo root will not find anything.

**`docker/openpilot/deps` is in the root `.dockerignore`**, so `docker compose build` for the
`sim` image does not ship these 309 MB to the daemon for nothing. That line matters more now
that the directory is tracked rather than ignored.

There is no `.dockerignore` *here*, deliberately: the whole 309 MB is the bridge build's context.
The `git config --global --add safe.directory` after the COPY is upstream's own line and not a
sign that anything reads git — `system/version.py` shells out to it but degrades to defaults, and
no SConscript touches it. That is why stripping `.git` was safe, and the build is what proved it.
