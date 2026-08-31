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
- **The optimiser is an iterated local search.** It perturbs the best ordering
  found so far rather than reshuffling from scratch. That matters at scale: with
  100+ parts a random shuffle essentially never beats a size-sorted order, so a
  restart-only search finds everything in the first few milliseconds and then
  stalls. Effort is **Fast 2s / Normal 15s / Thorough 2 min**, and Thorough is the
  default. Those are ceilings, not durations — a task that converges hands its
  remaining time back, and a second pass re-spends it where it might still help.
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
| Plywood sheets | how many sheets you buy |
| Cuts to sort by cabinet | cuts that must happen before parts can be piled up per cabinet |
| Total cuts | saw passes |
| Cutting stages | how many times the cut direction changes; 2 is rips-then-crosscuts |
| Largest offcut | biggest single reusable piece |

Ranking *Plywood sheets* first never costs a sheet: the search first finds the
tightest pack it can using a density-guided surrogate, then optimises whatever you
ranked next while holding that sheet count.

### The identity behind "Total cuts"

A guillotine layout is a tree, so exactly:

```
cuts = parts + scrap pieces + trim cuts - sheets
```

Minimising cuts is therefore *exactly* minimising the number of separate scrap
pieces — not scrap area. Every extra offcut costs one more pass. (The trim term is
for cuts whose remainder is narrower than the blade: the cut happens, but the
material becomes sawdust, so it adds a cut without adding a piece.)

It also gives a certified floor: `cuts >= parts - sheets`.

## Certified bounds and the prover

Every criterion is shown against a provable lower bound. Meeting one is a proof of
optimality; missing one shows the exact remaining gap and claims nothing more.

**Prove optimality** runs an exhaustive branch-and-bound per sheet. It explores the
whole space but abandons branches a bound proves cannot win, so a completed search
is a real proof — "everything not tried was provably worse". Sheets of up to about
eight parts finish and are proven; larger ones time out and are reported as
unproven rather than dressed up as a proof. Its pruning is checked against literal
enumeration on instances small enough to enumerate.

Naive brute force is not on the table at real sizes: 138 parts have 2^138 ~ 3.5e41
orientation combinations alone, which is beyond any hardware.

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
  nesting.py       guillotine packer with kerf + randomised-restart search
  cuts.py          split tree -> ordered, measurable cut list
  validate.py      invariants asserted after every solve
  solver.py        orchestration: group by thickness, nest, stream progress, report
  bom.py, units.py, models.py, main.py
static/            vanilla JS + SVG, no build step
tools/make_fixture.py   generates a synthetic STEP covering awkward cases
tests/
```

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

110 tests covering kerf arithmetic, the guillotine invariants, instance-name
normalisation, the streaming search, all three goals, the separating-cut metric,
background refinement, and the HTTP API. Hard expectations against
the real cabinet: 21 panels, three distinct drawers, no collapsed duplicate names,
and a unit-scale sanity check (the file declares metres and OpenCascade must
normalise it to millimetres — a scale slip here would silently produce a layout at
the wrong size). Every intermediate layout the animation shows is validated too,
so a half-finished search can never draw something impossible. Two tests guard
responsiveness directly: no gap between animation frames may exceed 0.75s, and
the search must still be finding improvements well after the first half-second on
a 144-part assembly.

Regenerate the synthetic fixture with:

```bash
.venv/bin/python tools/make_fixture.py
```

## Not in this draft

3D viewer, drag-to-adjust placement, offcut inventory carried between projects,
PDF export beyond browser print (Cmd-P is styled for the shop), saved projects.
