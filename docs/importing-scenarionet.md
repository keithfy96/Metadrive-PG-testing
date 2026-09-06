# Importing ScenarioNet recordings from the converter

`scenariobank import` turns one converter workspace into one bank under `banks/`, beside the
procedurally generated ones. A bank built this way holds a recording rather than a road built from
a seed, and the runner replays it instead of simulating traffic.

`docs/reference/commands.md` is the generated flag reference and `docs/reference/importing.md` is
the generated checklist of what an import carries over. **This page is hand-written**: which
workspace to point at, which rate to choose, and what to do when the command refuses is not
something the CLI can be asked.

---

## Before you start

The converter lives beside this repo:

```
metadrive-complete/
  metadrive-PG/                          <- you are here
  wingfin-osm-scenarionet-converter/
    workspaces/
      junction-1/
      junction-1a/
      mosque/
      mosque-1/
```

Every path in this page is written relative to `metadrive-PG/`, so
`../wingfin-osm-scenarionet-converter/workspaces/<name>` is the workspace to import.

> **There is more than one converter checkout on this machine, and the workspace names repeat.**
> The sibling checkout above is the current one. A workspace of the same name elsewhere holds
> different contents and will import a different drive.

`import` builds no environment and imports no simulator, so it runs on a plain `uv sync`. You do
not need the `sim` group for it.

A workspace is importable only if it has been through stage 6 and holds a dataset directory.
`junction-1a` has not, and the command says so rather than writing an empty bank.

## The three commands, in order

### 1. Look at the workspace first

```bash
uv run scenariobank workspace -p ../wingfin-osm-scenarionet-converter/workspaces/junction-1
```

This reads the workspace and prints every conversion in it: the rate, the frame count, the route,
the actors, the lights, and the picture each one has. It opens no simulator and writes nothing.

Read this before importing. A workspace can hold several conversions of the same map that are
**different drives**, not different views of one, and the actor counts differ between them.

### 2. Read the checklist, if you want it

```bash
uv run scenariobank importing
```

This regenerates `docs/reference/importing.md`: field by field, what an import carries over and
what it leaves behind. The importer and this checklist are one module, so the page cannot describe
a file the command does not copy.

### 3. Import into `banks/`

```bash
uv run scenariobank import \
  -p ../wingfin-osm-scenarionet-converter/workspaces/junction-1 \
  -o banks/junction-1
```

The bank lands at `banks/junction-1/`. `banks/` is git-ignored — a bank is a per-batch artifact,
and an imported one is 50 MB, so re-import it rather than commit it.

The command names every file as it copies it, so a multi-minute copy is never silent:

```
copied  dataset/dataset_summary.pkl  (0.0 MB)
copied  dataset/dataset_mapping.pkl  (0.0 MB)
copied  dataset/sd_osm-scenario_v1_mosque-1-b1fe3e0cc9bc58ec-t.pkl  (1.1 MB)
copied  source/manifest.json
copied  reports/scenario-conversion.json
absent  no stage-6 picture in mosque-1; its rows carry no thumbnail
1 scenario from scenarionet (1.1 MB) -> banks/mosque-1/manifest.json
1 scenario at 10 Hz -> banks/mosque-1/manifest.json
```

## Flags

| flag | | default | what it does |
|---|---|---|---|
| `--path`, `-p` | **required** | | The converter workspace to import, the directory holding `source/manifest.json`. Not the dataset directory inside it — point at the workspace and let the command choose the conversion. |
| `--out`, `-o` | **required** | | The bank directory to write. Created if it does not exist. Use `banks/<name>` so the studio and the runner find it. |
| `--bank-id` | optional | the workspace name | The name recorded inside the manifest. This is what results are attributed to, and it is independent of the directory name. Set it when the directory name and the identity you want recorded differ. |
| `--rate` | optional | `100.0` | Which conversion to take, in Hz. See below — this is the one choice here you cannot undo. |

### `--rate`, and why 100 is the default

The rate is how many recorded frames per second the pickle holds. It is fixed when the converter
writes the file, and the import copies the file, so **the rate is fixed in the bank forever**.

It matters because ScenarioNet replay advances one recorded frame per environment step. The
recording's rate *is* the simulation rate, and the runner sets the physics step from it:
`physics_world_step_size = 1 / step_hz` with `decision_repeat = 1`.

The asymmetry is the whole argument for the default:

- `--decision-hz` is a stride in the runner's own loop. It is never written into a bank, so it
  stays adjustable on every future run.
- `step_hz` is baked in. A 100 Hz import keeps every decision rate that divides 100 available. A
  10 Hz import caps every future run at 10 Hz, and undoing it means going back to the converter.

So a 10 Hz import buys about a tenth of the disk and gives up every decision rate above 10. Import
at 100 unless you have a reason.

`--rate` also does not name a conversion on its own. `junction-1` holds two 100 Hz conversions of
different drives. The rate narrows the field, and the converter's stage 6 record breaks the tie by
naming the conversion it produced last. When stage 6 names none of the survivors, the highest rate
wins and the last directory name settles it.

## What the four workspaces hold

Measured with `scenariobank workspace` on 2026-09-07:

| workspace | conversions | at `--rate 100` you get | at `--rate 10` you get |
|---|---|---|---|
| `junction-1` | 100 Hz ego-only, 100 Hz full, 10 Hz full | 3782 frames, 151 actors, 8 lights, **50 MB** | 379 frames, same actors, **5.5 MB** |
| `mosque` | 100 Hz ego-only ×2, 10 Hz | 4175 frames, ego only | 414 frames, 4 actors |
| `mosque-1` | 10 Hz only | refused, and the error names 10 Hz | 418 frames, ego only, **1.3 MB** |
| `junction-1a` | none | refused — no dataset directory | refused |

Size follows the actors, not the map. Every actor stores a position, a heading and a velocity for
every frame, so `junction-1` at 100 Hz stores 151 tracks across 3782 frames while `mosque-1` stores
one track across 418. `mosque-1` has more map features than `junction-1` and is forty times
smaller.

Note that `mosque`'s actors are in its **10 Hz** conversion, not its 100 Hz ones. Which conversion
carries traffic is a property of that conversion, so run `workspace` before choosing a rate.

## Examples

Import the full `junction-1` drive at the default rate:

```bash
uv run scenariobank import \
  -p ../wingfin-osm-scenarionet-converter/workspaces/junction-1 \
  -o banks/junction-1
```

Import the same workspace small, for a quick check where 10 Hz is enough:

```bash
uv run scenariobank import \
  -p ../wingfin-osm-scenarionet-converter/workspaces/junction-1 \
  -o banks/junction-1-10hz \
  --rate 10
```

Import `mosque` at the rate that actually carries traffic:

```bash
uv run scenariobank import \
  -p ../wingfin-osm-scenarionet-converter/workspaces/mosque \
  -o banks/mosque \
  --rate 10
```

Record a different name in the manifest than the directory carries:

```bash
uv run scenariobank import \
  -p ../wingfin-osm-scenarionet-converter/workspaces/junction-1 \
  -o banks/kl-junction \
  --bank-id kl-junction-baseline
```

Change your mind about the rate. Re-importing over an imported bank replaces it, so this is one
command and not a delete first:

```bash
uv run scenariobank import -p ../wingfin-osm-scenarionet-converter/workspaces/junction-1 -o banks/junction-1 --rate 10
uv run scenariobank import -p ../wingfin-osm-scenarionet-converter/workspaces/junction-1 -o banks/junction-1
```

Nothing of the 10 Hz conversion is left behind after the second command.

## What lands in the bank

```
banks/junction-1/
  manifest.json                          schema 1.3, source "osm-scenario"
  dataset/                               the summary, the mapping, and the sd_*.pkl recording
  reports/scenario-conversion-100hz.json this conversion's own report, not another's
  source/manifest.json                   the converter's manifest, for provenance
  thumbs/junction-1.png                  from the workspace's stage 6 map picture
```

**The dataset is copied in, not referenced.** A bank is mounted into a container and shipped to a
rig, and a path into somebody's home directory is not. That is what makes an imported bank
expensive to move, and it is deliberate.

A workspace with no stage 6 picture imports without a thumbnail rather than failing.

## What the command refuses

Each of these fails before a byte is copied, and the message says which one it is:

- **Stage 5 did not pass.** Stage 5 is the converter's validation of the reviewed lane model. A
  bank built from something that failed it would claim a scenario nobody checked.
- **The workspace drives on the right.** This project is defined left-side. The declared side is
  checked, not a measured one, because an import builds no map to measure. A right-side workspace
  is refused rather than mirrored.
- **The workspace holds no conversion at `--rate`.** The error lists the rates it does hold. The
  fix is another conversion in the converter, not another flag here.
- **`--out` already holds a procedurally generated bank.** Those took minutes of map building, so
  the import stops rather than replacing roads with a recording. An existing *imported* bank is
  replaced, which is what makes the re-import above a single command.
- **The workspace holds no converted scenario at all.** Nothing has been through stage 6 yet.

## What an imported bank cannot do

- **`review` refuses it by name.** Every measure review makes is built on a seed, a block sequence
  and a resolved exit. A recording has none of them, and a review reporting "0 duplicates" from a
  computation that never ran is worse than a refusal.
- **The five editing commands refuse it the same way.** `replace`, `add` and the rest edit a bank
  by rebuilding a seed. There is no seed here to rebuild.
- **`options` cannot pin levels on it.** The six difficulty axes are contents of the recording, not
  settings applied at run time, so the manifest raises rather than storing a promise nothing keeps.
- **The manifest records no `base_config` and no simulator.** MetaDrive did not build this bank,
  the converter did. Which simulator replays it is a property of the run, and the runner records
  that.

## Verify an import

```bash
ls -R banks/junction-1 | head -20
du -sh banks/junction-1
python -c "import json;m=json.load(open('banks/junction-1/manifest.json'));print(m['schema_version'],m['source'])"
```

You should see `1.3 osm-scenario`, a `dataset/` directory with three files, and a size near 50 MB
at the default rate.

Then confirm the import read the workspace without touching it:

```bash
cd ../wingfin-osm-scenarionet-converter && git status --short && cd -
```

The workspace should be unchanged. A test pins this, and so does the whole suite:

```bash
uv run pytest tests/unit/test_import.py -v    # 23 tests
uv run pytest                                  # 474 tests
```

Three of the import tests need this converter checkout and skip without it.
