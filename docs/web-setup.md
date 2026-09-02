# Setting up the studio

The studio is how you author a bank: the thumbnail grid, the seed rankings, the swap. It is the
product, and the CLI underneath is how it executes — MetaDrive's engine is a per-process singleton,
so every simulation runs as a subprocess.

`docs/reference/commands.md` is the exhaustive flag reference for that worker and is generated;
**this page is hand-written**, because how to install and run a thing is not something the CLI can
be asked.

It is a **local authoring tool**. It is not the Phase 7 orchestrator, it has no queue, no auth and
no network surface, and it must never be exposed on one.

---

## Install

Two dependency groups, both opt-in so that `uv sync` stays fast for anything that needs neither:

```bash
cd metadrive-PG
uv sync --group web --group sim
```

| group | brings | needed for |
|---|---|---|
| `web` | `fastapi`, `uvicorn`, `httpx2` | serving the page at all |
| `sim` | `metadrive-simulator` (pinned to commit `85e5dadc`), `matplotlib` | every command the page runs on your behalf |

**Sync both.** The studio process itself needs only `web` — it never imports MetaDrive — but every
button on the page runs a `scenariobank` subprocess that does. With `sim` missing, the page loads,
the header says `not installed`, and every action fails at the moment you click it rather than at
the moment you start.

`httpx2` is there for `starlette.testclient`, so `tests/unit/test_web.py` runs. starlette ≥1.6
prefers `httpx2` over plain `httpx` and warns if you give it the latter.

## Run

```bash
uv run scenariobank studio
# studio on http://127.0.0.1:8770/  (banks: banks)
```

Then open <http://127.0.0.1:8770/>. Stop it with Ctrl-C.

| flag | default | |
|---|---|---|
| `--banks-root` | `banks` | where the bank list is read from |
| `--port` | `8770` | |
| `--host` | `127.0.0.1` | loopback only — see below |

Point it somewhere else without moving anything:

```bash
uv run scenariobank studio --banks-root /tmp --port 9000
```

If the `web` group is missing you get a plain sentence rather than a traceback:

```
the studio needs FastAPI and uvicorn. Install the web group: `uv sync --group web`
```

## It binds loopback, and will not be argued with

```bash
$ uv run scenariobank studio --host 0.0.0.0
Invalid value for --host: the studio binds loopback only, and '0.0.0.0' is
not. It runs commands that write into this repository and it has no
authentication.
```

`127.0.0.1`, `::1` and `localhost` are accepted; nothing else is. This is deliberate and not a
config gap. The endpoints run subprocesses that write into your checkout, and there is no token, no
login and no per-user anything — the only safe listener is one nothing else can reach.

To reach it from another machine, forward the port over SSH rather than changing the bind:

```bash
ssh -N -L 8770:127.0.0.1:8770 you@this-machine
```

## Building a bank from the page

The **Build** tab is the landing view: one card per scenario type, showing the example picture
`scenariobank examples` drew at seed 0. Click the types you want, check the bank name — it is
filled in as `bank-YYYY-MM-DD-hhmm` and editable — say how many of each type you want, and click
**Generate**.

**per type** is how many scenarios of each ticked type to build. It starts at the size of a bank
the CLI would have built on its own and is then yours: one for a quick look, thirty when you are
choosing between seeds. A scenario *is* a seed, so the line under the button always names the
seeds it is about to build — `each type at seeds 0,1,2` — rather than only the count. Leaving the
box alone asks for exactly the seeds `generate` uses by default.

The bar counts the CLI's own per-scenario progress lines. `generate` prints `[3/35]` to stderr as
each scenario lands, and the page reads those; the total it shows *before* the first one arrives is
the types times the count. Importing MetaDrive is twenty silent seconds, and `0 / 10` during them
is the difference between waiting and wondering.

Banks are written under `--banks-root`, and only if that is inside the directory the studio was
started in — a job may not write outside the checkout. `GET /api/studio` is where the page learns
both, rather than assuming `banks/`.

Underneath, this is the same `POST /api/jobs` every other command goes through: the selection
becomes one repeated `--category` per type and one `--seeds 0,1,2`, spelled exactly as you would
type them, and `invoke.py` checks every flag against the CLI's own parameters. There is no second
way of calling the CLI. A seed list that is not a simple count — `--seeds curve=0,1,2,3,22`, one
category built at its own seeds — is still the **Run** tab's, or the terminal's.

When a build finishes, **Open the bank** takes you straight to it.

## Looking at a bank

The **Bank** tab is what replaces opening the thumbnail directory in an image viewer. Pick a bank
from the list — newest first, by what its manifest says it was built at, each row carrying its
scenario types and its size — and every scenario in it appears as the picture of it, grouped by
type.

The banks are **a list, not a dropdown**: with a dozen of them you want to see them all at once and
read their sizes against each other, which is the one thing a `<select>` hides until you open it.
**find** filters the list — by name, or by scenario type, so "the roundabout ones" is as answerable
as "the one called `b`".

**The picture is the unit.** This is not a table with a thumbnail column: what you are judging is
an image, so the card is mostly image, with the `scenario_id`, the seed, and the destination and
route length underneath. Those last two are the fields that say *why* two cards look alike. Two
seeds that drew the same road is the thing this screen exists to make visible — `curve` seeds 0
and 4 are a few percent apart in route length and their thumbnails are the same picture, which is
a fact about the bank you cannot get from the manifest by reading it.

Cards appear in the order the manifest lists them, so a card's position is the position its
`scenario_id` names: `curve_0004` is the fifth curve card.

Three endpoints behind it, and nothing else:

| endpoint | what it is |
|---|---|
| `GET /api/banks` | directories under `--banks-root` holding a `manifest.json`, summarised: name, `bank_id`, when it was built, its categories and its scenario count |
| `GET /api/banks/{bank}` | that bank's manifest, **as it is on disk** |
| `GET /api/banks/{bank}/thumbs/{name}.png` | one scenario's picture |
| `GET /api/banks/{bank}/review` | what is in it: duplicates, coverage, step budgets and spread — computed, never stored |
| `GET /api/banks/{bank}/compare?left=&right=` | two of its scenarios read against each other |
| `GET /api/looks/{name}.png` | a candidate seed drawn by `inspect`, before it is committed to anything |

The manifest is returned unshaped. It was written to explain itself — "declare the intent, store
the fact" — so a studio that reformatted it here would be inventing a second description of a bank
for the page to drift away from.

Two states that are not errors, and say so:

- **A directory with no `manifest.json` is not a bank.** Generation writes the manifest last, so
  an interrupted run leaves exactly that, and it is left out of the list rather than half-listed.
- **A manifest this studio cannot parse stays in the list, carrying its error**, and opening it
  says why. A bank silently missing from the list is the one failure you cannot debug from the
  page, and a schema bump is when it would happen.

`generate --no-thumbnails` is a supported way to build a bank, so a card with no picture says
"built without a thumbnail" rather than showing a broken image.

## What is actually in a bank

Under each scenario type is a row of counts, and the first one is the point: **`2 distinct of 5`**.

A bank of 35 rows is not automatically 35 scenarios. `intersection_left` resolves every seed to the
same destination, the same 111.7 m route and the same +90.0° turn — MetaDrive's `X` junction does
not vary with the seed — so five seeds draw **two** scenarios, one per spawn lane, and the other
three are padding. Nothing said so until this screen, and a bank that is 60% repetition would have
reached the frontend team looking like 35.

Duplicates are marked on the cards too, where you are already looking:

| mark | means |
|---|---|
| `≡ 0002` | identical: every measured field matches, spawn lane included |
| `≈ 0004 · 7%` | near-duplicate: close enough that the two thumbnails are the same picture |
| `lane only` | the same drive from a different starting lane — the weakest real difference |

The rest of the chips answer *what does this bank fail to test*: turn pairs covered against the
four possible, destinations and spawn lanes with counts, the left/right balance, the route-length
range, and `budget 1140/1200` — the largest step budget any route earns against the category's cap.
That last one is a **correctness** check rather than a quality one: over the cap means a policy runs
out of steps before reaching the destination.

The warnings under the chips are sentences written by `review.py`, not assembled by the page, so
the studio and the CLI cannot describe the same bank two different ways.

`GET /api/banks/{bank}/review` serves it, and `scenariobank review --bank ./banks/b` prints the same
thing in a terminal (`--json` for a machine). **No simulator is involved** — every field the
comparison needs is already in the manifest, so it answers in milliseconds, runs on a machine with
no MetaDrive, and works on banks generated before the feature existed.

One measure was deliberately not reused: `variety.shape_gap` compares the *road*, and for `X` and
`T` the road is identical across every seed by construction, so it would score 0% for every pair
and say nothing exactly where the trouble is. What a thumbnail shows, and what actually differs, is
the **route**.

Bank and scenario names are checked **on the shape of the name**, before either becomes a path:
letters, digits, dot, dash, underscore, and not a leading dot. A name that cannot hold a separator
and cannot begin with a dot is not a traversal that gets filtered out — it is one that cannot be
spelled. Same rule as `/api/examples/{category}.png`.

## One card, or two against each other

**Click a card and it is selected.** One selected shows the whole row; a second shows how the two
differ; a third drops the oldest, so a run of "and how about this one" chains without a clear step
in between. `Esc` or **clear** lets go. The panel pins itself to the bottom of the window rather
than sitting above the grid, because the cards you are comparing are the ones you have just
scrolled to.

**Nothing in the one-card panel is computed.** Seed, destination, spawn lane, route length, net
rotation, turn pairs, road, exit rule and the MetaDrive commit are all in the manifest already —
it was written to explain itself, and this is the first thing in the product that reads it back to
a person. The one number it does not read verbatim is the step budget, which comes from the review
rather than being derived here: `step_budget` rounds, the rule for how belongs to `categories.py`,
and a page dividing metres by a constant would be a second rounding rule to keep in step.

The two-card panel is the same measure the review uses, asked about one pair:

| verdict | means |
|---|---|
| `identical` | every measured field matches, spawn lane included — one drive stored twice |
| `same-drive` | only the spawn lane differs |
| `near-duplicate` | under 10% apart |
| `distinct` | two scenarios |
| `incomparable` | different scenario types — see below |

Under it, every field side by side, with the ones that agree dimmed rather than dropped: "these two
share a destination" is half the answer, and a table showing only differences would hide it.

**Two scenarios of different types are answered, not refused.** A gap is only defined inside a
category — two categories differ by *declaration*, a different road and a different exit rule — so
the comparison says there is no number and why, and still lays the fields out. This is not
pedantry: `intersection_left_0000` and `t_junction_0000` have the same 111.7 m route, the same
+90.0° turn, the same spawn lane and the same step budget, on completely different roads. Any
measure that scored them would call them identical.

The comparison is computed **on the server**, by `review.compare`. The page holds every field
already; what it must not invent is what the fields mean together, and a copy of `gap` and
`verdict` in JavaScript would be a second measure that drifts the first time either changes.

## Replacing the seed behind a picture

A scenario **is** a seed and what was measured from it, so "this picture is a poor draw" is
answered by swapping the seed. Select one card and the panel offers **Find a better seed**.

It runs `scenariobank seeds` over seeds 0–30 and ranks them by how unlike the ones you are
**keeping** each one draws — the other scenarios of that type, never the one you are replacing. A
candidate earns its place by being unlike what stays in the bank; ranking it against the draw you
are throwing away would score it on the wrong thing. Roughly a minute, one line per seed on the
bar.

Each row carries the route, the rotation, the turn pairs, the exit and the gap, with the
near-duplicates flagged. Two buttons per row:

- **Look** draws that seed with `inspect` and shows it beside what is in the bank now. That is the
  question in one picture: a different road, or the same corner again? Nothing drawn this way is in
  any bank — it goes under `.studio/looks/`, which is scratch.
- **Use this seed** runs `replace`. The bank never changes size and never renumbers: the scenario
  keeps its id and its position, and only the seed and what was measured from it change. The card
  redraws and the whole bank is re-read, so the statistics band above it moves too.

**Use this seed** is offered on every row, kept seeds included, and the refusal is the CLI's:

```
replace failed: seed 0 is already used by curve: each seed builds one scenario.
curve currently holds seeds [0, 1, 2, 3, 22].
```

`bank.replace_scenario` owns that rule. The page shows the sentence rather than knowing the rule,
because a copy of it here would be a second rule to keep in step — the same reason the gap column
is flagged by `variety.SeedReading` and not by a comparison in JavaScript.

`seeds --json` is what the page reads, and it is the same ranking in the same order the aligned
table prints. If those two could disagree, the seed the page recommends would not be the seed the
command recommends.

**One caveat.** The ranking is measured on the road `categories.py` declares today; a replacement
is built on the road the *manifest* records. They are the same road unless a category has been
edited since the bank was generated — and when they differ the panel says so and will not scan.

### There is no Run tab

There was, until this step: a form generated over the CLI's flags, so that nothing was unreachable
while the real screens were built. Picking, building, looking and swapping now have screens of
their own, and keeping a generated form beside them would leave two ways to do the same job, one of
them worse.

The job engine underneath is unchanged and permanent — every simulator command still runs as a
subprocess of this same CLI, one at a time. What changed is only where a job reports: the screen
that started it. A failed build shows the tail of its log under the bar, a refused swap shows the
CLI's sentence under the table.

Three things a job still will not do:

- **One at a time.** A second is refused, naming the job that holds the slot. Two `generate`s into
  one bank directory is a corrupt manifest.
- **No writing outside this checkout.** A path flag that resolves outside the directory the studio
  was started in is refused.
- **It will not run `studio`.** A studio inside a job would bind another port and serve this same
  page.

A job's state lives in two files — `.studio/jobs/<id>/log` and `.studio/jobs/<id>/exit` — and is
read back off disk every time it is asked for. Reloading the page mid-job reattaches to it;
restarting the studio does not orphan it. Anything the page cannot show you is still there, in
`.studio/jobs/`.

## What it will and will not do to your files

- **Reads** `--banks-root` for directories holding a `manifest.json`, and serves thumbnails from
  inside them.
- **Writes** under `.studio/` (job logs, and the candidate seeds **Look** draws), and into
  whatever a command you ran was told to write — `generate -o ./banks/b` writes a bank, exactly as
  it would from a terminal, and **Use this seed** rewrites one row of one manifest and redraws its
  thumbnail. `.studio/` is gitignored and disposable: a job is re-runnable, so nothing in it is
  worth keeping.
- **Never** touches a bank you did not name, and never regenerates one you did not ask it to.

## How it works, in one paragraph

**The studio process never imports MetaDrive.** `BaseEngine.singleton`
(`engine/engine_utils.py:36-59`) is one engine per *process*: a server that built an env would hold
it for its lifetime, could serve exactly one simulator request, and would die with it — a panda3d
fault is a segfault, not an exception a handler can catch. So every simulator command runs as a
subprocess of this same CLI. That costs a second or two of import per job and buys three things:
a crash kills a job rather than the studio, there is no engine contention to get wrong, and the CLI
keeps describing what runs, so the page and `docs/reference/commands.md` describe the same
program.

## Development

```bash
./scripts/bank-check.sh            # ruff, then the full suite including tests/unit/test_web.py
```

`tests/unit/test_web.py` drives the app through `TestClient` against a temp directory: no server, no
port, no simulator. It skips itself with a clear reason if the `web` group is absent, so a machine
that only runs the CLI is not broken by it.

The frontend is **one file** — `src/scenariobank/web/static/index.html` — with no build step, no
node, and no bundler. Edit it and reload the page. It loads nothing from a CDN, so it works offline.

The API is `src/scenariobank/web/api.py`, and `create_app(banks_root=…, state_dir=…)` takes its
roots as arguments rather than reading globals, which is what lets the tests point it at a temp
directory.

### Adding an endpoint

Keep routes thin. If a route is doing arithmetic, that arithmetic belongs in a module the CLI also
imports — otherwise the two front doors will eventually disagree, which is the failure this whole
design is arranged to prevent. Existing examples of the pattern: `/api/doctor` returns
`doctor.collect(probe=False)` unchanged, `/api/commands` returns `docs.reference()` unchanged, and
`/api/categories` returns `docs.category_rows()` unchanged.

## Troubleshooting

| symptom | cause |
|---|---|
| header reads `metadrive not installed` | the `sim` group is missing: `uv sync --group sim` |
| `the studio needs FastAPI and uvicorn` | the `web` group is missing: `uv sync --group web` |
| `Address already in use` | another studio is running; `pkill -f 'bin/scenariobank studio'` or use `--port` |
| the reference tab says it failed to load | look at the terminal — a `ValueError` from `docs.reference()` means a CLI command was added without a group or without examples in `docs.py` |
| a job says `lost` | its process ended without recording an exit code — the studio was killed while it ran |
| the **Bank** tab says there are no banks | no directory under `--banks-root` holds a `manifest.json`; generate one from the **Build** tab, or with `scenariobank generate -o ./banks/b --bank-id b` |
| a category says `2 distinct of 5` | not a fault: `X` and `T` junctions do not vary with the seed, so the spawn-lane count is the ceiling however many seeds you build. Use a category whose road varies, or accept the smaller set |
| clicking a card does nothing | the studio is older than the panel — restart it. The routes are fixed when the process starts, so a studio started before `/api/banks/{bank}/compare` existed serves a 404 for it |
| **Find a better seed** is greyed out | the bank records a different road or exit rule for that type than this build declares, so a ranking would be measured on one road and the replacement built on another |
| **Find a better seed** does nothing, or a swap 404s | the studio is older than the feature — restart it. Routes are fixed when the process starts |
| a swap is refused naming the seeds the category holds | a bank does not build one seed twice. Pick another row, or free that seed first |
| a comparison says `incomparable` | the two cards are different scenario types. There is no gap between categories, only between scenarios of one — the fields are still shown side by side |
| a bank is listed as `unreadable` | its `manifest.json` does not parse or does not validate — opening it names the reason. A bank written by an older schema reads exactly like this |
| **Generate** is greyed out and says banks live outside the working directory | the studio was started with a `--banks-root` outside its own checkout; no job may write out there. Restart it inside the directory you want the bank in |

## What exists today

Built one step at a time; the Bank tab lists the same steps and strikes through what is
done. Currently live:

1. the shell, and `doctor` in the header
2. `categories` and `commands` — the reference tab
3. the job engine — every simulator command runs as a subprocess, one at a time, reporting on
   the screen that started it
4. the gallery — the **Build** tab, one example picture per scenario type
5. generation from that selection — how many of each type, and a progress bar
6. the dataset — the **Bank** tab, every scenario in a bank as the picture of it
7. the review — how many of those scenarios are actually different
8. the panel — click a card for the row behind the picture, two for how they differ
9. the swap — rank candidate seeds for a scenario, look at one, and replace it

Still to come is editing, adding and removing an item, four more scenario types, and submitting a
run to the queue. See **Phase 2c** in `IMPLEMENTATION_PLAN.md`.
