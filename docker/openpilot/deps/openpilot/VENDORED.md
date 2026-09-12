# Vendored openpilot fork — provenance

This is not a git checkout any more. It is a **flat copy** of `zapetaai/openpilot`, committed
into this repo so a machine with no access to that private org can still build the bridge image.
Its `.git` and its seven submodule gitdir pointers were removed to make that possible: git will
not track a directory that contains one.

| | |
|---|---|
| origin | `git@github.com:zapetaai/openpilot.git` |
| commit | `c767ace885e64015fe58a7e2074ef51f79085a7b` |
| dated | 2026-07-09 |
| subject | `av3 dense lateral MPC: per-waypoint grid via AV3_MPC_N + solver menu` |
| branch | tip of `av3-dense-lat-mpc` |

Submodules, at the SHAs this copy was taken at:

```
body          e1805f65ee75fab4454c21eda8b42b49d4bdc48f  (c433da94~10)
cereal        e949a56030368912354c5c5f451cacbb180c2e2b  (v0.1.1-1-ge949a56)
laika_repo    e932f32ab921ed09ba6e304990574693d8ca5199
opendbc       03a1df1fd38d520ab13b6976f3f9e930ed0f7023  (v0.1.1-6-g03a1df1)
panda         0317a569602c54ec5a95ae9afd5190e14e8449f2  (v0.1.1)
rednose_repo  3b6bd703b7a7667e4f82d0b81ef9a454819b94bd
tinygrad_repo d8dda2af3afcef0bb772fff580cfa8b3eabf7f69  (v0.4.0-462-gd8dda2af3)
```

**The commit must match the environment the AV3 `.ep` checkpoint was compiled against.** That is
the reason it was pinned rather than tracked, and the reason moving it is not a routine update.

## Updating it

There is no `git pull` here. Re-fetch into a scratch directory with `pull.sh` (set `REF` for a
different commit), strip the git metadata the same way, and replace this tree:

```bash
REF=<sha> bash docker/openpilot/pull.sh          # clones to docker/openpilot/deps/openpilot
find docker/openpilot/deps/openpilot -maxdepth 2 -name .git -exec rm -rf {} +
```

Then re-apply the change below, which is what `pull.sh` does not do for you.

## What was changed from upstream

Two `.gitattributes` files had their `filter=lfs` lines commented out — `.gitattributes` and
`third_party/mapbox-gl-native-qt/.gitattributes`. Left alone, git in *this* repo would try to push
`selfdrive/modeld/models/*.onnx` and `*.dlc` (82 MB, five files) through an LFS server this repo
does not have. They are ordinary files here.

Nothing else. The tree is otherwise byte-for-byte what `c767ace8` checks out.

## What the build actually uses

`docker/openpilot/Dockerfile` runs scons over `cereal/`, `common/`, `opendbc/can/`,
`selfdrive/boardd/` and the two MPC libraries, then prebuilds the acados lateral-MPC solver menu.
Most of what is here — `body/`, `laika_repo/`, `tools/`, the modeld models — is never touched.
It is carried whole anyway, because trimming a tree whose SConscripts cross-reference each other
is how you buy a build failure on a machine you cannot debug on.
