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

Bank and scenario names are checked **on the shape of the name**, before either becomes a path:
letters, digits, dot, dash, underscore, and not a leading dot. A name that cannot hold a separator
and cannot begin with a dot is not a traversal that gets filtered out — it is one that cannot be
spelled. Same rule as `/api/examples/{category}.png`.

## Running a command from the page

The **Run** tab is the escape hatch — everything the purpose-built screens do not cover yet. Pick a
command, fill in the flags, click **Run**, and watch the output arrive. Start with `categories`: it
takes no flags, needs no simulator, and finishes in under a second, so it is the quickest way to
confirm a fresh install works end to end.

The form is **generated from the CLI's own flags** — the same data
`docs/reference/commands.md` is written from. It cannot offer a flag the command does not take,
`--category` and `--rule` are dropdowns filled from `categories.py`, and a submission the CLI would
reject comes back as a sentence about one flag rather than a traceback in the log.

Three things it will not do:

- **One job at a time.** A second Run is refused, naming the job that holds the slot. Two
  `generate`s into one bank directory is a corrupt manifest.
- **No writing outside this checkout.** A path flag that resolves outside the directory the studio
  was started in is refused.
- **It will not run `studio`.** A studio inside a job would bind another port and serve this same
  page.

A job's state lives in two files — `.studio/jobs/<id>/log` and `.studio/jobs/<id>/exit` — and is
read back off disk every time it is asked for. Reloading the page mid-job reattaches to it;
restarting the studio does not orphan it.

## What it will and will not do to your files

- **Reads** `--banks-root` for directories holding a `manifest.json`, and serves thumbnails from
  inside them.
- **Writes** under `.studio/` (job logs, scratch figures), and into whatever a command you ran was
  told to write — `generate -o ./banks/b` writes a bank, exactly as it would from a terminal.
  `.studio/` is gitignored and disposable: a job is re-runnable, so nothing in it is worth keeping.
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
| a bank is listed as `unreadable` | its `manifest.json` does not parse or does not validate — opening it names the reason. A bank written by an older schema reads exactly like this |
| **Generate** is greyed out and says banks live outside the working directory | the studio was started with a `--banks-root` outside its own checkout; no job may write out there. Restart it inside the directory you want the bank in |

## What exists today

Built one step at a time; the Bank tab lists the same steps and strikes through what is
done. Currently live:

1. the shell, and `doctor` in the header
2. `categories` and `commands` — the reference tab
3. the job engine — **every command is runnable from the Run tab**, which is scaffolding: it
   is deleted once the purpose-built screens replace it
4. the gallery — the **Build** tab, one example picture per scenario type
5. generation from that selection — how many of each type, and a progress bar
6. the dataset — the **Bank** tab, every scenario in a bank as the picture of it

Still to come are the surfaces that read one scenario back: the panel saying what generated a
picture, and the swap. See **Phase 2c** in `IMPLEMENTATION_PLAN.md`.
