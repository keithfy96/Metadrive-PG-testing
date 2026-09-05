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

The bar counts the CLI's own per-scenario progress lines. `generate` prints `[3/55]` to stderr as
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

## Composing a road by hand

Under the gallery is a second card: a palette of the fifteen blocks MetaDrive can build — the
letter, then what it is (`$` toll gate, `y` lane merge, `O` roundabout) — a text box the palette
appends to and you can type in directly, an exit rule, a seed, and **Draw**. The line under the
row is the command it runs:

```
scenariobank inspect --block-seq CCX --rule sharpest --seed 0
```

plus `--json` and an `--out` under `.studio/looks/`, spelled the same way the seed swap draws its
candidates. The picture comes back beside the facts the drawing measured — the exit the rule
chose, its angle, the net rotation, the route length, and **would earn**, the step budget a
category on this road would be given from the same `step_budget` formula as the eleven shipped
ones. That last number is the one to have in hand when deciding whether a road drawn here
deserves to become a twelfth type.

**Drawing writes nothing. Adding does.** Under the facts is a bank to add to and an **Add to
bank** button, which runs the same `add` a terminal would:

```
scenariobank add --bank banks/pg-bank-2026-09 --block-seq CCX --rule sharpest --seed 0
```

**The category is named after the road and the rule**, never typed: `CCX` driven to the
`sharpest` exit is `CCX_sharpest`, always. That is what keeps "no invented names for one-offs"
true while still letting a road in — the same road composed twice by two people is one category
rather than two spellings of one. The toll gate's `$` is spelled `toll`, because a name is also a
directory entry and a URL segment, and dropping the character would make `$S` and `S` the same
category. `categories.composed_name` owns that spelling and `inspect --json` reports it, so the
card never guesses at it.

**One press adds one scenario**, the seed on screen. The category it creates is capped at what
that first route earns — the *would earn* number, from `step_budget` — and after that it is an
ordinary category: it appears in the bank, in `review`, in the comparison panel, and the Bank
tab's **Add another** grows it a seed at a time. Two things it does not get, both because
`categories.py` has never heard of it: a picture in the Build gallery, and a seed ranking. **Find
a better seed** says so rather than ranking against the wrong road.

One caution the `tollgate` category already records: `step_budget` assumes 6 m/s, and a toll
plaza holds its lanes to 3 m/s. A composed `$S` is therefore capped optimistically, and its own
cap can be raised on the Bank tab without rebuilding anything.

Removing a composed category's last scenario removes the category, and this build cannot re-create
it from `categories.py`. Composing the same road again is the way back, and lands under the same
name — which is the point of deriving it.

It is still the escape hatch for everything the eleven shipped types left out — a fork, a parking
lot, a two-way road, a crossroads with a U-turn — and the place to watch them fail honestly.

**Failing honestly** means the command's own words, not a spinner. Three kinds of refusal reach
the card. The rule can find no exit: `T` at rule `right` is refused with `nothing here turns
right. The closest is 1T1_1_, which carries straight on`, because a T junction at that seed offers
a left and a straight and nothing else — try `sharpest`, or another seed, where the arm on offer
flips. MetaDrive can refuse the road itself. Type `fS` and draw it:

```
inspect failed: seed 0 does not build for block sequence 'fS': Bug exists in this block, Recommend to use Ramp
```

That sentence is the simulator's, quoted through `sockets.explain_build_failure`. Map layout is a
backtracking search, so a sequence can fail at one seed and build at the next; a block MetaDrive
names as broken is broken at every seed. **Both forks are broken, not just `f`** — measured at
seeds 0–4, `f` and `F` refuse identically. The palette lists them anyway: a palette that quietly
dropped a block would be asserting something about the simulator that only the simulator can say.

And a block can need a particular road *in front of it*, which is the third kind and the one the
first two used to be mistaken for.

### What a block needs before it — the note under the palette

`P` was the case that made this necessary. Clicking `S`, `P`, `S` and pressing **Draw** produced
`seed 0 does not build for block sequence 'SPS': Lane number of previous block must be 1 in each
direction` — MetaDrive's internal `assert` (`parking_lot.py:30`), which names no block, offers no
remedy, and arrives behind a sentence of ours that blames the seed. Going to seed 1 returned a
byte-identical error, because the seed had nothing to do with it.

`ParkingLot` needs the road feeding it to be one lane in each direction, and a road starts at
three. Measured at seeds 0–4: **only the lane merge `y` narrows a road and only the lane split
`Y` widens one** — every other block passes the count through. One merge drops one or two lanes
depending on the seed (`Merge` draws `DiscreteSpace(min=1, max=2)` floored at `max(1, …)`), so
`yP` builds at seeds 0, 1 and 4 and not at 2 and 3, while `yyP` builds at all five.

So three of the fifteen blocks carry a `categories.BlockNeeds` — `f`, `F` and `P` — and the
studio shows it **under the palette, the moment the block lands in the box**, before anything is
pressed. For `P` it also offers the repair: type `SPS` and the note says *your sequence would
build as `SyyPS`*. The panel holds no copy of the rule. `after_any` (which blocks in front would
help) and `insert` (what to put there) are served with each block by `GET /api/blocks`, and they
are the same fields `categories.validate_block_seq` refuses on — so pressing **Draw** anyway
refuses in under a second, before an env is built, and suggests the same sequence the panel did.

The two are split on purpose. `f` and `F` fail *themselves*, and that judgement stays MetaDrive's
to make and to word, so nothing pre-empts it — their `after_any` is empty and `unmet_need`
ignores them. `P` fails because of what is *before* it, which is a property of the sequence, and
checking sequence properties is what `validate_block_seq` was already for. Lane arithmetic is
deliberately not modelled: `yYP` narrows and then widens again, and MetaDrive refuses that one,
because a second implementation of a simulator's geometry is a second thing to be wrong.

The palette is served by `GET /api/blocks` from `categories.BLOCKS`, the table
`validate_block_seq` checks sequences against; a test asserts every letter and class name against
MetaDrive's own registry — and a `needs_sim` test asserts the parking-lot rule the same way, that
`SP` really is refused at every seed and that the `SyyP` the refusal suggests really builds. So
the palette cannot offer a block the CLI would refuse, name one differently from the reference,
or claim a condition the simulator does not impose.

### Read the exits — the same road, the other question

A refused rule tells you the rule failed. It does not tell you which rule would work. **Read the
exits** does, on the sequence already in the box — the second button on the same form, running
`scenariobank sockets` where **Draw** runs `inspect`. That is why the two share one card: asking
this from anywhere else would mean typing the road twice.

One table comes back, written for someone who has never opened MetaDrive: **which setting sends
the car which way**. All five values of the `exit` dropdown, what each one does, and where it
lands — **including the ones that cannot be used here**, in the command's own words:

```
T at seed 0
This road ends at a T junction, so the car has 2 ways to leave it.

  setting     what it does                                      goes to
  only        can't be used here — this junction has 2 ways
              out (1T0_1_, 1T1_1_), and "only" means "take
              the single way out". Choose left, right,
              straight or sharpest instead.                            —
  left        turns left                                        1T0_1_
  right       can't be used here — nothing here turns right.
              The closest is 1T1_1_, which carries straight
              on. Try a different exit setting, or a
              different seed.                                          —
  straight    carries straight on                               1T1_1_
  sharpest    takes the biggest turn on offer, whichever
              way it goes                                       1T0_1_
```

**The exit name is a label, not something you type.** `1T0_1_` names that piece of road *at this
seed*; the next seed's road may have no such name. You set `exit` to a direction and the studio
finds the road that matches.

A second table, one row per way out, sat above this one until it was measured away. No block
MetaDrive builds offers more than three arms, and their turns are always −90 / 0 / +90, so `left`,
`right` and `straight` between them already name every arm there is — the arm table was this one
rearranged. The one thing it carried that this does not is each exit's lane count, which matters
on `y` (leaves on 1 lane), `Y` (5) and `B` (1) and nowhere else; it can come back as a column here
if it is wanted.

Under the table, **show the raw measurements** opens the numbers the plain view leaves out: every
way out of the last block, MetaDrive's own socket index for it, and two angles.

**`turn`** is measured from the heading the car arrives on, so it is what the driver does, and it
is what the settings match. **`from spawn`** is the same arm measured from where the car set off —
the angle the drawing's title reports. On a one-block road they are the same number. They come
apart the moment the road turns the car before its last block, and the disclosure says by how much:

```
CSX at seed 0 — this road turns the car +115.5° before it reaches the last block

  socket        node        turn   from spawn
  3X-socket0    3X0_1_     +90.0       -154.5
  3X-socket1    3X1_1_      -0.0       +115.5
  3X-socket2    3X2_1_     -90.0        +25.5
```

The curve in front of the crossroads swings the car +115.5°, so from the spawn the three arms sit
at −154.5 / +115.5 / +25.5 — no two of which look like a crossroads. From the junction they are the
+90 / 0 / −90 that they plainly are in the drawing. **Positive is a left turn**
(`straight_lane.py:56`). Two exits of the same sign and similar size mean the block is not the
shape you think it is.

The settings used to match `from spawn`, which is why `CSX` with rule `right` was refused on a road
with an obvious right turn, and why rule `left` answered `3X1_1_` — the arm the drawing goes
*straight* up. None of the eleven shipped scenario types was affected: they are single-block roads,
or they use rule `only`, which ignores angles, and `docs/reference/destinations.md` re-measures
byte-for-byte identical. It was only ever composed roads — the ones this card exists for — that
read wrong.

**The way the car came in is never listed.** A block's sockets are the connections it offers
onward, and the one behind it belongs to the block before — so `X` reads as exactly three exits.
Measured across all fifteen block ids: of the twelve that build alone, not one marks an entry.
(The three that do not are `f` and `F`, which MetaDrive names as broken, and `P`, which is not
broken — it needs a road narrowed to one lane first, and `yyP` reads back one exit.)
`SocketReading.is_entry` and the filter every rule applies stay, guarding a case MetaDrive does not
currently produce, but nothing composed from these blocks fills that column in.

So the `T` refusal above resolves in one press: `right` never will at seed 0, `sharpest` gives
`1T0_1_`. It is also where a scenario type's exit setting can be justified — `t_junction` uses
`sharpest` because the arm on offer flips with the seed, and reading `T` at seed 0 (`right`
refused) and at seed 2 (`left` refused) is the evidence for that.

Both buttons are held while either is running: the studio runs one job at a time, so a second
click would be a refusal rather than a second answer.

This is *inspection only*. Nothing here changes an existing scenario's exit — that is the Bank
tab's edit panel, under **Editing, adding and removing an item** below.

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
| `POST /api/banks/{bank}/scenarios/{id}/budget` | set or clear one scenario's own `max_steps` — one of the two writes here that are not jobs |
| `POST /api/banks/{bank}/options` | pin some of the bank's option levels — the other |

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

## The option levels, above the scenarios

The first card on an open bank is six dropdowns: `traffic`, `cones`, `barriers`, `pedestrians`,
`cyclists`, `lights`, each at `none`, `low`, `medium` or `high`. They save the moment you change
one, and the equivalent `scenariobank options` line is printed underneath, as every other screen
here prints the command it is a way of typing.

**Nothing on this screen changes when you change one**, and that is not a bug in the page. These
are applied when a run happens, not when the bank was built: the map is generated before any
object is placed, and a thumbnail draws lanes rather than objects, so the pictures and the routes
are identical at every level. What is stored is *declared intent*, which is why setting it writes
one field of the manifest and starts no job — the same class of edit as a step budget, and for the
same reason. Pinned at generation time instead, changing a traffic level would mean rebuilding
every scenario in the bank.

The card's heading carries the sentence `review.py` writes — *runs at traffic=medium,
pedestrians=low, everything else none* — rather than one this page composes, so `scenariobank
review` and the studio cannot describe one bank two different ways. The six axes and their four
levels are read off the `options` command's own flags through `/api/commands`, so the page cannot
offer an axis the CLI does not have.

It is a **default, not a lock**: a run flag still overrides it.

## What is actually in a bank

Under each scenario type is a row of counts, and the first one is the point: **`2 distinct of 5`**.

A bank of 55 rows is not automatically 55 scenarios. `intersection_left` resolves every seed to the
same destination, the same 111.7 m route and the same +90.0° turn — MetaDrive's `X` junction does
not vary with the seed — so five seeds draw **two** scenarios, one per spawn lane, and the other
three are padding. Nothing said so until this screen, and a bank that is 60% repetition would have
reached the frontend team looking like 55.

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

## Editing, adding and removing an item

The swap above re-rolls a seed and **deliberately** keeps the bank's size, its ids and its
positions. Under it, in the same panel, are the controls that change those, plus the two fields a
re-roll never touches.

| control | what happens |
|---|---|
| **seed** + **Rebuild at this seed** | `replace --seed`, for when you already know the seed you want and do not need a thirty-one seed ranking |
| **exit** + **Rebuild to this exit** | `replace --exit-rule` / `--destination` / `--inherit-exit`: same seed, a different destination |
| **List this seed's exits** | runs `sockets` on **this bank's** road at **this row's** seed, and fills the dropdown with the exits it actually has |
| **step budget** + **Save** | writes `max_steps` straight into the manifest. No job, no simulator, no waiting |
| **add** + **Add another _type_** | `add`: one more scenario of that type, at the seed you give |
| **Remove this one** | `remove`, after asking twice. The scenario and its picture go |

**One distinction decides all of it: `max_steps` is declared, everything else is measured.** A
route length, a rotation, a destination and a turn pair are all read off a road that was built, so
changing any of them means building that road again — a job, like everything else here. A budget is
a cap somebody chose. So it is the one control on the panel that saves instantly, through the one
endpoint in this API with no subprocess behind it. It is refused while a job is running, because a
`replace` holds the manifest in memory and would write over the edit when it finished.

### Removing leaves a gap

`scenario_id(name, index)` puts the position in the id, so `curve_0004` **is** position 4. Delete
`curve_0002` and the ids after it do not move:

```
curve_0000  curve_0001  curve_0003  curve_0004        and the next one added is curve_0005
```

Renumbering would change the id of a scenario nobody touched, and an id is how a run refers to a
scenario — Phase 5's results are keyed on it. So the position is left empty, `add` numbers one past
the **highest id** rather than counting rows, and an id stops being a row number. Nothing in
`bank.py` minds: `_locate` always searched by id.

Removing a category's last scenario removes the category with it. The bank's last scenario is
refused, on the panel, in the CLI's own words:

```
remove failed: curve_0004 is the only scenario in this bank, and a bank with nothing in it is
a manifest describing no scenarios. Delete the directory instead, or generate a new bank over it.
```

A category you removed can be added back: `add` re-creates a missing entry from this build's
`categories.py`, which is what keeps that removal an edit you can undo.

### Schema 1.1

This is the first change to touch the manifest, so the version moved. Three optional fields
appeared on a scenario row, all `null` on a row that follows its category:

```json
{ "scenario_id": "t_junction_0005", "seed": 7, "destination": "1T1_1_",
  "exit_rule": null, "exit_node": "1T1_1_", "max_steps": null }
```

- `exit_rule` — this row's own rule, instead of the category's.
- `exit_node` — an exact exit, pinned rather than resolved. Only meaningful at this row's seed:
  `StdTInterSection` offers a different arm on seeds 2 and 3, so **a rebuild at a new seed drops
  the pin** and says so.
- `max_steps` — this row's own cap.

An overridden row keeps its category name. The manifest's rule is "declare the intent, store the
fact", and an override *is* the declared intent for that row; a bank that silently reclassified a
scenario would be the manifest failing to explain itself.

**1.0 banks still open**, because 1.1 only added optional fields. The reverse does not hold — a 1.0
reader forbids extra keys — which is why the number moved rather than the fields being slipped in
quietly. A 1.0 bank that this build edits is written back as 1.1: the version describes the shape
of the file, not the history of the bank.

### Schema 1.2

`options` joined the manifest at its top level — six axes, all `none` on a bank that pins nothing:

```json
{ "options": { "traffic": "medium", "cones": "none", "barriers": "none",
               "pedestrians": "low", "cyclists": "none", "lights": "none" } }
```

It sits beside `base_config` rather than inside it, and the difference between the two is the
design: `base_config` records what generation **used** — `traffic_density: 0.0`,
`accident_prob: 0.0`, and they stay at zero — while `options` records what runs of this bank
should **default to**. Changing the first would mean rebuilding the bank; changing the second is
one field.

**1.1 banks still open**, and read as every axis at `none` — the floor rather than an absence, so
"never set" and "set to zero" are one state. A 1.1 bank this build edits comes back stamped 1.2,
for the reason 1.0 banks come back 1.1.

## The destinations reference

On the **Reference** tab, above the command reference, is `docs/reference/destinations.md` — where
every scenario type's route ends, at every seed, with the route lengths, the turns taken, the
spawn lanes and how alike the closest two roads of each sequence are. It is checked in, so the
card names the file and when it was last measured; **Show the document** renders it in place. It
is collapsed by default because this tab's own job is the command reference, and an always-open
document would push the index below the fold on every visit.

**Re-measure** rewrites it, in place. There is no path box: the page always rewrites the one copy
anything reads. It is twenty units of work — nine block sequences fingerprinted, then eleven
categories resolved and driven at five seeds each, roughly a hundred resets — and reports as it
goes in the same `[n/m]` lines the build bar reads. One count across both passes, so the bar does
not refill halfway and read as a job starting over. `destinations` in `README.md` has the measured
wall-clock figure.

**Re-measure after a MetaDrive bump.** Every figure in that file comes from a reset on the
simulator `doctor` reports, and regenerating it is how a change in block geometry becomes visible.
On an unchanged simulator the file comes back byte-identical, so `git diff` is the check: an empty
diff means nothing moved.

The document is served as text by `GET /api/reference/destinations` and rendered on the page by
`renderMarkdown`, which handles the three blocks `destinations.render` emits — headings,
paragraphs and pipe tables — reusing the same table and inline-markdown helpers the command
reference uses. Text rather than a parsed structure, because the generator is the authority on
that file's shape and re-parsing it in the API would be a second opinion about it.

## What it will and will not do to your files

- **Reads** `--banks-root` for directories holding a `manifest.json`, and serves thumbnails from
  inside them.
- **Writes** under `.studio/` (job logs, and the candidate seeds **Look** draws), and into
  whatever a command you ran was told to write — `generate -o ./banks/b` writes a bank, exactly as
  it would from a terminal, **Use this seed** rewrites one row of one manifest and redraws its
  thumbnail, **Add to bank** appends one scenario to the bank you picked from the list, and the
  option dropdowns rewrite one field of one manifest and rebuild nothing. **Re-measure** on the
  Reference tab rewrites `docs/reference/destinations.md` — the one thing this page writes that is
  not under `.studio/` and not in a bank. `.studio/` is gitignored and disposable: a job is
  re-runnable, so nothing in it is worth keeping.
- **Deletes** exactly two things, both of them yours to ask for: **Remove this one** deletes a
  scenario's thumbnail with its row, and a rebuild deletes a thumbnail it did not redraw. A picture
  of a scenario the manifest no longer describes is wrong, not merely stale.
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
| **Save** on a step budget says a command is still running | an edit written beside a running job would be overwritten when that job finished. Wait for it |
| the exit dropdown offers no nodes | the exits are only known once **List this seed's exits** has run — it is a simulator job, so it is not run for every card you click |
| a pinned exit disappeared after a rebuild | the seed changed. An exit node names an arm of *that* seed's road, so it cannot outlive it; the category's rule resolved the new one |
| removing is refused, naming the scenario | it is the last one in the bank. Delete the directory instead |
| a comparison says `incomparable` | the two cards are different scenario types. There is no gap between categories, only between scenarios of one — the fields are still shown side by side |
| a bank is listed as `unreadable` | its `manifest.json` does not parse or does not validate — opening it names the reason. A bank written by an older schema reads exactly like this |
| the destinations card on the Reference tab says no reference is on disk | the studio was started outside the checkout, so `docs/reference/destinations.md` is not where it looks. **Re-measure** writes one where it is |
| **Re-measure** changed `destinations.md` | the simulator is not the one the file was measured on. That is the command working: read the diff |
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
10. the edit — an exact seed, another exit, a scenario's own step budget, and adding or removing
    one; manifest schema 1.1
11. four more scenario types — `off_ramp_hold`, `lane_merge`, `lane_split` and `tollgate`, so the
    gallery is eleven cards and a full bank is 55 scenarios
12. the road builder — any block sequence, an exit rule and a seed, drawn and measured, and
    added to a bank under a name derived from the road and the rule
13. the road utilities — **Read the exits** beside the road builder, which says what each rule
    picks from a road and why the ones that fail fail; and the destinations reference on the
    **Reference** tab, shown as the document it is and re-measured on demand

Still to come is submitting a run to the queue, which is blocked on the queue itself. See
**Phase 2c** in `IMPLEMENTATION_PLAN.md`.
