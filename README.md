# Plywood Cut-Layout Optimizer

Takes a STEP AP242 assembly exported from Onshape, works out which solids are
plywood panels and how thick each one is, and lays them out on 4x8 sheets with a
cut order you can actually follow at a track saw.


## Running it

```bash
source .venv/bin/activate && uvicorn app.main:app --reload --port 8000
```

Then open <http://localhost:8000> and choose a STEP file — nothing loads on its
own. If a `.step` file is sitting in the project folder, a one-click **Load
sample** button appears; otherwise use the file picker or drop a file anywhere on
the window.

If the virtualenv ever needs rebuilding:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

`cadquery-ocp` is a ~400MB wheel, so that first install takes a few minutes.

## What it does

**Track saw = guillotine cuts.** Every cut runs edge-to-edge across the whole
piece, so the layout is built as a recursive split tree rather than free-form
nesting. A consequence worth knowing: every blank is a rectangle, so the
optimizer packs rectangles. Curved parts get a rectangular blank and you profile
them afterwards — which is what you would do anyway, since a track saw cannot cut
a curve. The true part outline is still drawn inside each blank so panels stay
recognizable.

- **Thickness auto-detection.** Each solid is measured with an *oriented*
  bounding box, so parts sitting at an angle in the assembly still measure
  correctly. The smallest dimension is the thickness; those get clustered, and
  each cluster becomes its own stock of sheets.
- **Kerf.** Adjustable in the GUI, default 0.087" (a typical track-saw blade).
  Cutting a piece to size costs one blade width off the offcut — except when the
  part fills the dimension exactly, in which case no cut is made and no kerf is
  lost.
- **Grain.** Parts rotate 90° freely by default. Tick *lock grain* on any part to
  pin its orientation.
- **Non-plywood parts** are auto-excluded (blocky proportions, implausible
  thickness, bigger than a sheet) and listed with a reason so you can override.
- **Naming** is path-qualified and shortened: `Top Stretcher` stays short, but the
  three drawers' back panels become `Drawer 2 / Back` because `Back` alone is
  ambiguous. Onshape writes repeated instances as `Drawer <2>`; the tool reads
  those as `Drawer 2`. Click any name to rename it.
- **The search animates.** Improved layouts are pushed to the browser as they are
  found, up to four times a second, with a progress card showing which stock is
  being optimised, how many layouts have been tried, and the best result so far.
  Turn it off with *Animate the search* — the answer is identical either way.
- **The optimiser is an iterated local search.** It perturbs the best solution
  found so far rather than reshuffling from scratch. That matters at scale: with
  100+ parts a random shuffle essentially never beats a size-sorted order, so a
  restart-only search finds everything in the first few milliseconds and then
  stalls. Effort is **Fast 2s / Normal 15s / Thorough 2 min**, and Thorough is the
  default. Those are ceilings, not durations — a task that converges hands its
  remaining time back, and a second pass re-spends it where it might still help.
- **It searches in three spaces, because no one of them can reach everything.**
  *Ordering* moves reshuffle the global part order and repack; only these
  reliably collapse the sheet count. *Assignment* moves relocate individual parts
  between existing sheets, which is the only way to bring a stray part back
  beside the rest of its cabinet — reordering can never carry a part to a
  different sheet. And *dissolving* empties one sheet onto the others: a layout
  that has settled on N sheets stays on N under either of the other two, because
  neither ever removes one. Dissolving packs for real rather than guessing —
  every orphan is offered to each surviving sheet's live free rectangles — so it
  either comes back with a layout known to fit or reports that the sheet cannot
  be dissolved.
- **Parts that measure the same are kept together.** Every cut offset is some
  part's dimension, so a sheet holding six different dimensions needs six stop
  settings. Parts are grouped into *families* by the dimension they share with
  the most other parts; families seed the search, whole families move as a block
  during it, and a placement heuristic prefers free rectangles whose cuts land on
  a measurement that sheet has already been set to — or need no cut at all. None
  of that runs during the floor pass: gathering parts that measure alike is a
  fine instinct for the saw and a poor one for density, and it was costing a
  sheet.
- **Placement decides which saw a cut lands on.** Seating a part takes a cut to
  width and a cut to length, and how far each has to reach — and so which saw
  makes it — depends on which comes first and how the part is turned. The `CHOP`
  heuristic scores a seat by exactly that: how many *track* cuts it adds. Nothing
  else in the packer has any reason to care.
- **Two ways to choose the sheet, because they are good at opposite things.**
  First fit takes the first sheet that will have the part, which fills sheets
  before opening the next and is what drives the count down. Best fit asks every
  open sheet and takes the best seat going, which spreads the parts — it costs a
  sheet on a tight job, so it is only offered when the ceiling is above the area
  floor, and the ranked score rejects it the moment it needs an extra sheet.
- **It keeps improving in the background.** Once the first answer is on screen the
  search carries on, and the layout is quietly replaced whenever something better
  turns up. The app stays fully usable throughout. Uncheck *Keep improving in the
  background* to stop at the first answer.
- **Two bills of materials**, toggled in the Materials pane. *Pieces* is every
  part to cut, grouped by size. *Plywood* is what to buy: sheets per thickness,
  price per sheet, and the total. Prices are editable and remembered (defaults
  $48.98 for 1/2" and $68.88 for 3/4", CAD); a stock with no price set is left
  out of the total rather than costed at a made-up number.

## Ranked priorities

There are no modes. You rank what matters and the solver obeys, comparing layouts
lexicographically: rank 1 always wins, the rest break ties.

| Criterion | Meaning |
|---|---|
| Track saw stop changes | how many times you move the stop on the parallel guide |
| Track saw cuts | passes with the track saw — the awkward ones |
| Saw changes | trips between the track saw and the miter saw |
| Mitre saw cuts | passes with the chop saw — the easy ones |
| Largest offcut | biggest single reusable piece |
| Cuts to sort by cabinet | cuts that must happen before parts can be piled up per cabinet |

Plywood is not on the list. It is a **hard cap**, set from the proven floor unless
you raise it, so no ranking can quietly spend a sheet. The search finds the
tightest pack it can first, then optimises whatever you ranked while holding that
sheet count.

### Finding the floor

Searching on sheet count alone does not work, and the reason is worth stating
plainly: almost every candidate layout ties with the incumbent on the count, so
the count tells the search nothing about which candidate is *closer* to needing
one sheet fewer. The tiebreaker underneath it is the entire gradient, and the
choice of tiebreaker decides whether the floor is found at all — not how fast.

Two are used, because neither dominates:

- **Emptiest sheet.** A sheet disappears when its parts fit elsewhere, so keep
  making the least full sheet emptier still. Every move that shifts work off it
  is rewarded, right up to the moment it empties and the count drops. Read to the
  nearest twentieth of a sheet, because a raw fill fraction never ties and a term
  that never ties makes everything under it dead weight.
- **Largest offcut.** Gather the waste into one rectangle rather than scattering
  it. Weaker on big jobs — a tidy layout is precisely what cannot shed a sheet —
  but it wins on small ones, where there are only two sheets and consolidating
  one of them *is* emptying the other.

The opening pass runs both and keeps whichever packed tighter. Two half-length
searches under the right gradient beat one full-length search under the wrong one
by a whole sheet: on the sample kitchen's 151-part half-inch stock, emptiest-sheet
reaches the floor from every seed tried in under four seconds, while largest-offcut
alone reached it on one run in ten and looked, from any single run, like the floor
simply was not there.

**Underneath both sits the track-cut count, and it matters more than a tiebreak
usually does.** Many layouts use the same number of sheets and leave the same
sheet equally empty. They are not equally good to cut, and the search has to
settle on one of them for the ranked stages to start from. Left to pick
arbitrarily it lands on layouts needing 25 more track cuts than they had to --
and the ranked stages cannot undo it. At the sheet floor the sheets are 92% full,
so almost nothing can move: of 20,000 legal moves measured at the floor, *not one*
reduced the track-cut count. Whatever structure the floor pass hands over is the
structure you get, so the choice is made there, on the one criterion that is cheap
to measure and tells sane structure from a jumble.

For the same reason the floor pass is given only the three classic area fits.
`STOP` and `CHOP` each trade area for something it has no use for yet, and
offering them there costs both the sheet and the structure -- on that same stock,
the search that lands on 12 sheets and 138 track cuts with three heuristics lands
on 196 with all five. They earn their place in the ranked stages, where the thing
they optimise is the objective.

That last point is why `tests/test_sheets.py` checks the floor is reached **from
every seed** rather than from the default one. A search that only wins on one
random seed is not a working search, and the difference is invisible until
something unrelated — an extra placement heuristic, one more kind of move —
consumes the random stream differently and quietly costs a sheet of plywood.

One ranking is worth knowing about before you try it: **keep track saw cuts above
saw changes.** Saw changes are a real cost, but the cheapest way to never walk
between two saws is to use only one — and the one that can make every cut is the
track saw. Rank saw changes first and you will get a layout that never touches the
chop saw and does all the easy crosscuts the hard way. The shipped order avoids
that; the trade-off is pinned by a test rather than left as advice.

## The cutting order

Stop changes are the headline goal, and getting them right is most of what this
tool does. Four facts about a track saw with parallel stops shape it:

- **The stop is one number** — the width of the strip the saw takes off the
  reference edge. Two cuts share a setting whenever their offsets match, whatever
  axis they run along. A 7-3/8" rip and a 7-3/8" crosscut buried in some strip are
  the *same* setting; turning the work under the saw is free.
- **Runs carry across sheets.** Finishing one sheet at 7-3/8" and opening the next
  at 7-3/8" costs nothing — and across *stocks* too, since the stop does not care
  whether it is cutting half-inch or three-quarter. So which sheet you break down
  first is a real decision, and so is which column of it you rip first.
- **The miter saw never touches the stop.** A trip to the chop saw can sit
  anywhere in the sequence without breaking a run, so chop cuts are placed for saw
  changes alone — put off to the end of each sheet.
- **Same-axis runs are permutable.** Every cut in a run of same-axis cuts spans
  its piece completely, so the sections are parallel bands: the same parts, the
  same cuts, in whatever order you like — including which band is left as the
  remainder and so never cut at all.

That last point matters more than it sounds. The order the packer happens to emit
bands in is arbitrary, so *stop changes are not a property of a layout* until you
also say what order you cut it in. Measuring the raw tree therefore overstates
every layout, and by a variable amount — which is exactly the way to mis-rank
them. `app/schedule.py` scores a layout by the best cutting order it can find
*for* it, and rewrites the tree to match once a layout is chosen. Sequencing is
free: it never moves a part, buys a sheet or adds a cut. Applying it is checked
against that on every solve.

The model of the shop is one sheet at a time: everything that came off a sheet is
finished before the next is opened. Within that, the order is free — and the whole
job is sequenced together, so the *Constant widths* panel shows which settings
carry straight into the next sheet, and the cut list marks the sheets you pick up
with the stop already where you want it.

### Where the remaining cost is

The report shows both figures against a certified floor, so it is clear which half
of the problem is still costing you:

- The **stop-change floor** counts, per sheet, the settings that sheet's cuts
  cannot be made without, and subtracts one per sheet boundary (a run can serve
  the end of one sheet and the start of the next, but only there). Meeting it
  proves the *cut order* is optimal for that layout — not that no better layout
  exists.
- The **saw-change floor** is the same argument for tool trips: which saw makes a
  cut is fixed by how far it has to reach, so a sheet holding both kinds needs two
  blocks of work and one trip between them.

On the sample kitchen the gap to the stop-change floor runs about 10–15%, and most
of what is left sits in the layout rather than the order: a sheet whose parts
measure six different widths needs six settings however you sequence it. That is
what the shared-dimension seeds and moves below are for.

The two goals pull against each other, and the cap is where you decide between
them. On the sample kitchen:

| Max sheets | sheets | track cuts | stop changes | saw changes |
|---|---|---|---|---|
| floor (default) | 16 | ~195 | ~115 | ~20 |
| floor + 1 | 17 | ~163 | ~95 | ~19 |

The 16-sheet pack is 92% full on its half-inch stock, and a sheet that full has to
nest parts inside each other: every crosscut then spans something wide, which
means the track saw. Give it one more sheet and the same parts fall into narrow
strips whose crosscuts are bench chops — about 30 fewer track-saw setups and 20
fewer stop moves, for one sheet of plywood.

Sheets first is the default because plywood costs money and the cut order does
not. But this really is a trade, not a bug, and **Max sheets** is the dial: set it
to the floor plus one and the saw work drops sharply.

### The identity behind the cut counts

A guillotine layout is a tree, so exactly:

```
cuts = parts + scrap pieces + trim cuts - sheets
```

Minimising cuts is therefore *exactly* minimising the number of separate scrap
pieces — not scrap area. Every extra offcut costs one more pass. (The trim term is
for cuts whose remainder is narrower than the blade: the cut happens, but the
material becomes sawdust, so it adds a cut without adding a piece.)

It also gives a certified floor: `cuts >= parts - sheets`. Track cuts and mitre
cuts partition that total: which saw makes a cut is decided by how far it has to
reach, not chosen, so the split is a property of the layout and not of the order.

## Certified bounds and the prover

Every criterion is shown against a provable lower bound. Meeting one is a proof of
optimality; missing one shows the exact remaining gap and claims nothing more.

Two kinds of bound appear, and they claim different things. Some are properties of
the *parts* — the area bound on sheets, the crosscuts no layout can move onto the
chop saw — and hold against every layout there is. The stop-change and saw-change
floors are properties of *the layout in hand*: meeting one proves the cut order is
the best available for that layout, and says nothing about whether a better layout
exists. The report is explicit about which is which rather than blurring them.

**Prove optimality** runs an exhaustive branch-and-bound per sheet. It explores the
whole space but abandons branches a bound proves cannot win, so a completed search
is a real proof — "everything not tried was provably worse". Sheets of up to about
eight parts finish and are proven; larger ones time out and are reported as
unproven rather than dressed up as a proof. Its pruning is checked against literal
enumeration on instances small enough to enumerate.

**What is not claimed.** For a job the size of a kitchen there is no proof of
global optimality on offer, and this tool does not pretend otherwise. Guillotine
cutting is NP-hard, and 200 parts have 2^200 orientation combinations before any
question of arrangement — beyond any hardware. What the tool does claim is
narrower and true: the cut *order* is measured, not estimated, and it is shown
against a floor no order can beat; the plywood cap is never exceeded; and the
number in the report is the number the cut list will give you.

## Blade and offcuts

Offcuts thinner than the saw blade are kept — there is no minimum-offcut rule. Set
**Min offcut** above 0 to discard slivers below a width instead. A remainder
*smaller* than the kerf cannot survive whatever the setting: the blade consumes
that material.

## Using the window

- **Zoom and pan the layout** with the trackpad: scroll (or pinch) over the sheets
  to zoom at the cursor, drag to pan, **Fit** to reset. The page itself never
  zooms or scrolls. Cut-number badges and line weights stop growing once they
  reach a readable size, so zooming in to read a part label no longer buries it
  under giant discs.
- **Drag the pane dividers** to resize the sidebar, the instructions panel, and
  the split between the cut list and the BOM. Sizes are remembered.

## Onshape export settings

Keep using **STEP AP242** — it carries the assembly tree *and* the part names,
and both are needed to tell three drawers' `Back` panels apart. Export the
assembly, and do not flatten it.

Onshape cannot give repeated instances of one part distinct names — they all come
through as `Drawer <1>`, `<2>`, `<3>`. The tool normalises those to `Drawer 1/2/3`
so the labels read `Drawer 1 / Back`. To name them something else entirely
(`Top Drawer`), click the name in the parts table and type over it.

## Layout

```
app/
  step_parser.py   STEP AP242 -> panels, via the XCAF (XDE) layer
  geometry.py      oriented bounding box, thickness clustering, outline projection
  nesting.py       guillotine packer with kerf, and the layout search
  staged.py        rip-first layouts: two 1D bin packings instead of one 2D
  grain.py         a cabinet's faces cut from one continuous run of grain
  schedule.py      the cutting order: sheet order, band order, stop runs
  regroup.py       single-sheet door into schedule.py
  objective.py     the ranked criteria, all measured on the real order
  bounds.py        certified floors, per criterion
  cuts.py          split tree + chosen order -> measurable cut list
  exact.py         branch-and-bound prover, per sheet
  validate.py      invariants asserted after every solve
  solver.py        orchestration: group by thickness, nest, sequence, report
  bom.py, units.py, models.py, main.py
static/            vanilla JS + SVG, no build step
tools/make_fixture.py   generates a synthetic STEP covering awkward cases
tests/
```

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

204 tests covering kerf arithmetic, the guillotine invariants, instance-name
normalisation, the streaming search, every ranked criterion, the separating-cut
metric, background refinement, and the HTTP API. Hard expectations against
the real cabinet: 21 panels, three distinct drawers, no collapsed duplicate names,
and a unit-scale sanity check (the file declares metres and OpenCascade must
normalise it to millimetres — a scale slip here would silently produce a layout at
the wrong size). Every intermediate layout the animation shows is validated too,
so a half-finished search can never draw something impossible. Two tests guard
responsiveness directly: no gap between animation frames may exceed 0.75s, and
the search must still be finding improvements well after the first half-second on
a 144-part assembly.

`tests/test_sheets.py` guards the plywood floor, and guards it the only way that
stays guarded: by checking the floor is reached **from every seed**, not from the
default one. A search that wins on one random seed is not a working search, and
the difference is invisible right up until something unrelated consumes the random
stream differently. That is exactly how a sheet went missing once — see *Finding
the floor* above. The achieved layout is also re-checked from the API output
rather than the solver's own invariants, so a tighter pack cannot buy its sheet
back by overlapping two parts.

`tests/test_schedule.py` is where the cutting order is pinned down, and it is
built around the one claim everything else rests on — that sequencing is free.
Every test that reorders also checks the layout survived: the same parts at the
same sizes, the same cut count per sheet, still valid. A "saving" that quietly
moved a part or lost a cut is not a saving. It also checks the certified floor
holds on randomly generated layouts rather than only the ones it was written
against, that the figure in the report is the one you can count off the cut list,
and that a grain-matched run is never permuted however much a reorder would save.

Regenerate the synthetic fixture with:

```bash
.venv/bin/python tools/make_fixture.py
```

## Not in this draft

3D viewer, drag-to-adjust placement, offcut inventory carried between projects,
PDF export beyond browser print (Cmd-P is styled for the shop), saved projects.
