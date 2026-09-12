# calibrating-levels

What `scenariobank calibrate` does, when it has to be run, and how. Phase 4b of
`IMPLEMENTATION_PLAN.md`; the measured result is `docs/reference/level-calibration.md`.

**Short version.** A bank pins difficulty by *name*: `traffic=medium`, `pedestrians=high`. A run
turns each name into a number through the `LEVELS` table in `src/scenariobank/options.py`.
Calibration is how those numbers were chosen: sweep one axis at a time with a known reference
driver, read where its success rate falls, pick four values that sit apart. It was run once,
on 2026-09-13, and is run again only when the simulator or our own scene managers change. It is
not part of building or running a bank, and it is not run per bank.

## What it does

### The problem it solves

The four level names are the contract. The studio's Bank tab offers them, the wing-sim form
will offer them, every queue job carries them and every result records them. A name is only
worth carrying if the number behind it produces a scene that is measurably different from the
next name's. Before calibration the numbers were guesses, and the sweep showed three of them
were empty:

- traffic `medium` was 0.15, a density at which the expert arrived 100% of the time on every
  bank -- `medium` was `none` with a different label;
- cyclists `low` and `medium` were 1 and 2, and both scored 1.00 on the intersection bank --
  two names for one level;
- barriers turned out to be almost binary: a single barrier scene drops the expert from 1.00
  to 0.20, because it waits behind the breakdown vehicle to the step cap rather than overtake.

A model scored against those levels would have produced conclusions about the model that were
really facts about the constants.

### What one sweep is

One axis, one bank, a list of raw values:

1. **Every other axis is forced to `none`**, whatever the bank pins, so the only thing that
   differs between runs is the swept axis.
2. **The whole bank is driven once per value** by MetaDrive's bundled expert
   (`scenariobank.policies:ExpertPolicy`, deterministic), through the ordinary `run_bank`
   loop: one fresh env per scenario, the same record a `run` writes. A calibration point is
   exactly what `run --traffic-density 0.3` would have scored.
3. **Arrivals are counted.** Success rate per value is arrivals over scenarios. Each row also
   records why it ended, what the ego hit, and what was actually placed on the road.
4. **The table is written down**: one JSON record per axis and bank under
   `docs/reference/calibration/`, and `docs/reference/level-calibration.md` is re-rendered from
   every record there.
5. **Four values are suggested.** `none` is 0. `high` is the smallest value at the lowest
   rate, since going past it buys nothing but wall time. `low` and `medium` are the values whose
   rates sit nearest one third and two thirds of the way down from the rate at 0, in increasing
   order. The suggestion is printed, not applied.

Worked example, traffic on `banks/t-junction`:

| density | 0 | 0.05 | 0.1 | 0.15 | 0.2 | 0.3 | 0.35 | 0.4 | 0.5 |
|---|---|---|---|---|---|---|---|---|---|
| success | 1.00 | 1.00 | 0.80 | 0.80 | 0.60 | 0.40 | 0.60 | 0.40 | 0.20 |

The drop is 1.00 to 0.20, so the targets are 0.73 and 0.47, which land on 0.1 and 0.3. The
pick is 0 / 0.1 / 0.3 / 0.5, reading 1.00, 0.80, 0.40, 0.20.

### What it produces

- **`LEVELS` in `options.py`** -- the only consumer that matters. Every run from then on
  resolves level names through it. Baking the numbers in is a person's edit, made after
  reading every bank's table for that axis, not the command's.
- **`docs/reference/level-calibration.md`** -- the evidence. If anyone asks why `high` traffic
  is 0.5, the answer is a table.
- **Three tests in `tests/unit/test_calibration.py`** that hold the two together: the
  checked-in page must be the render of the checked-in records; every non-`none` level of a
  swept axis must be a value some sweep actually ran; and every axis must show never-rising
  rates with a real drop on at least one bank. Change a number by hand without a sweep behind
  it and the suite fails.

### What it is not

- **Not a per-bank check.** One table serves every bank on purpose: `hard` has to mean the
  same scene everywhere or results across banks are not comparable. Whether an axis does
  anything on a *particular* bank is a property of the road, and it is already in every
  result: the `placed` field of each row lists what was on the road after the reset. Cones and
  barriers land only on `Straight` and `Curve` blocks, so on an `X`, `T` or `O` road they
  place nothing at any level. Ask `placed`, not `calibrate`:
  ```bash
  jq '.results[] | {scenario_id, placed}' out/<run>/results.json
  ```
- **Not a score of the model.** The driver is the expert, the ceiling. Its curve is the ruler
  the model's curve is read against; it says nothing about AV3.
- **Not a GUI feature.** The Bank tab's six dropdowns are unchanged; only the numbers behind
  the names moved. Nothing in the studio runs a sweep.

## When to do it

Run once, then only on these triggers:

| trigger | why the numbers may have moved |
|---|---|
| **A MetaDrive bump** (the pinned commit in `pyproject.toml` changes) | the traffic model, the IDM policies, the expert or the block geometry may differ; the same density may now mean a different scene. Same rule as `destinations`. |
| **A change to our own managers** (`obstacles.py`, `actors.py`, and Phase 8's `lights.py`) | the count behind `medium` pedestrians is only meaningful for the placement code that was swept |
| **A new kind of road** on which an axis might behave differently from the bank it was measured on | cones and barriers were measured on `curve`; a bank of long straights would be worth one sweep to confirm the numbers still separate. A fifth T-junction bank would not. |

Not a trigger: adding a bank of an existing road type, editing a bank's seeds or destinations,
pinning levels, running or submitting a model, a change to the studio.

Expect to want it when a result surprises you and you need to know how hard that level really
is for a good driver -- that is a reason to read the page, not to re-measure.

## How to do it

About fifteen minutes on a laptop; roughly a second per scenario per value on the host, no
container needed. `--policy` defaults to the expert.

### 1. Sweep every axis on a bank where it can act

```bash
T=0,0.05,0.1,0.15,0.2,0.3,0.35,0.4,0.5     # densities; anything in (0, 0.01) is refused
C=0,1,2,3,4,6,8                             # counts; whole numbers only

uv run scenariobank calibrate --bank banks/t-junction-left-intersection --axis traffic --values $T
uv run scenariobank calibrate --bank banks/curve      --axis traffic     --values $T
uv run scenariobank calibrate --bank banks/t-junction --axis traffic     --values $T
uv run scenariobank calibrate --bank banks/curve      --axis cones       --values $C
uv run scenariobank calibrate --bank banks/curve      --axis barriers    --values $C
uv run scenariobank calibrate --bank banks/curve      --axis pedestrians --values $C
uv run scenariobank calibrate --bank banks/t-junction-left-intersection --axis pedestrians --values $C
uv run scenariobank calibrate --bank banks/curve      --axis cyclists    --values $C
uv run scenariobank calibrate --bank banks/t-junction-left-intersection --axis cyclists    --values $C
```

Cones and barriers only on `curve`, because only its blocks can hold them. Traffic on three
banks, because the `X` road barely moves before 0.5 while `curve` collapses at 0.35, and the
pick has to be read across them. `--categories` narrows a bank to some of its categories;
`--values` repeats or takes a comma list. Each command prints one line per value as it lands,
then the table, then the suggested four, then where the record and the page were written. The
runs themselves are under `out/calibrate/<axis>/<bank>/<axis>=<value>/`, one `results.json`
each, git-ignored.

To try the command without touching the checked-in records, point it elsewhere:

```bash
uv run scenariobank calibrate --bank banks/t-junction --axis traffic --values 0,0.2 \
    --record /tmp/cal --doc /tmp/cal/page.md
```

### 2. Read the tables and pick

Open `docs/reference/level-calibration.md`. Per axis, look for four values whose rates sit
apart on the bank where the axis bites, and check the other banks do not contradict them. The
printed suggestion is a starting point; it is computed per bank and cannot weigh three banks
against each other. Three things to read the tables with:

- **ego collisions** are the ego's own hits. A traffic car hitting another traffic car is not
  counted; it shows as a `max_step` row, the ego stuck behind the wreck. Read the collisions
  column for what the ego did and the `ended by` column for what the scene did.
- **placed** is what was on the road after the reset. It is how you see that an axis is inert
  on a road, and that the cones axis' breakdown scene puts a vehicle on the road at
  `traffic=0`.
- An axis that cannot be spread into four is recorded, not forced. Barriers today has one real
  step in it and the page says so.

### 3. Bake the numbers and re-render

Edit `LEVELS` in `src/scenariobank/options.py`, then rewrite the page so it quotes the new
table next to the measurements:

```bash
uv run scenariobank calibrate --render-only
```

It also names any level whose number no sweep ran.

### 4. Let the tests hold it

```bash
env -u FORCE_COLOR uv run pytest -q tests/unit/test_calibration.py tests/unit/test_options.py
env -u FORCE_COLOR uv run pytest -q tests/unit           # ~7 min; the tier tests drive the sim
```

Tests that were written against a particular scene pin raw numbers rather than level names
(`tests/unit/test_actors.py`, `scene(..., raw={...})`), so they do not move when the table
does. A test that names a level and asserts its number should read the number off `LEVELS`.

### 5. Record it

Note the date and the bank per axis in the Phase 4b section of `IMPLEMENTATION_PLAN.md`, the
way 2026-09-13 is. Commit the records, the page, `options.py` and the tests together.

## Where to read the code

- `src/scenariobank/calibration.py` -- the sweep, the record, the picker (`suggest`), the
  renderer and the `levels_match` gate. The module docstring carries the three caveats above.
- `src/scenariobank/options.py` -- `LEVELS`, with a one-line reading of each axis' table in the
  comment above it.
- `src/scenariobank/cli.py`, `calibrate` -- the flags, `--render-only`, and the printed table.
- `docs/reference/calibration/*.json` -- the records; `docs/reference/level-calibration.md` --
  the page rendered from them.
- `tests/unit/test_calibration.py` -- the fold, the picker, the refusals and the three gates.
