"""Ranked optimisation criteria.

Instead of discrete modes, the user ranks what matters and layouts are compared
lexicographically in that order. Every criterion is "lower is better".

The `cuts` criterion rests on an exact identity for guillotine trees:

    cuts = parts + scrap pieces + trim cuts - sheets

Leaves are parts plus scrap, and a *binary* node has one more leaf below it than
it has internal nodes. The correction term is for cuts whose offcut is narrower
than the blade: the cut still happens but yields only sawdust, so that node has a
single child and adds a cut without adding a leaf.

So minimising cuts is exactly minimising the number of *separate scrap pieces*
plus sawdust-only trims -- not scrap area. Every extra offcut costs precisely one
more pass with the saw.
"""
from __future__ import annotations

from . import schedule as sched
from .nesting import SheetLayout, separating_cuts

SHEETS = "sheets"
GROUPING = "grouping"
CUTS = "cuts"
STAGED = "staged"
STOPCHANGES = "stopchanges"
TRACKCUTS = "trackcuts"
MITERCUTS = "mitercuts"
OFFCUT = "offcut"

# Order used to break ties among criteria the user did not rank.
# Matches the ranking the UI ships with: get the sheet count down, force the
# rip-first pattern, then push work off the track saw and onto the chop saw.
# Sheet count is not rankable: it is a hard cap set from the proven floor
# rather than something traded against the rest.
DEFAULT_ORDER = (STOPCHANGES, TRACKCUTS, STAGED, MITERCUTS, OFFCUT, GROUPING)
CRITERIA = frozenset(DEFAULT_ORDER)

LABELS = {
    SHEETS: "Plywood sheets",
    GROUPING: "Cuts to sort by cabinet",
    STAGED: "Saw changes",
    STOPCHANGES: "Track saw stop changes",
    TRACKCUTS: "Track saw cuts",
    MITERCUTS: "Mitre saw cuts",
    OFFCUT: "Largest offcut",
}


def stage_depth(sheet: SheetLayout,
                miter_capacity: float = 304.8) -> int:
    """How many saws the worst path through this sheet's cut tree touches.

    Two is the shape worth aiming for: break everything down on the track saw,
    put it away, then chop the rest. Switching cut *axis* costs nothing with a
    track saw, so it does not count -- only picking up a different tool does.

    This is a property of the tree, and a floor on what any order can do with
    that sheet, so it is a useful thing to assert about a layout. The ranked
    criterion is not this: `saw_changes` counts the trips you actually make,
    over the whole job, in the order the cut list gives.
    """
    saws = cut_saws(sheet, miter_capacity)

    def walk(pid: int, last: str | None, runs: int) -> int:
        piece = sheet.pieces[pid]
        if piece.cut is None:
            return runs
        saw = saws[piece.id]
        here = runs + (1 if saw != last else 0)
        return max((walk(c, saw, here) for c in piece.children), default=here)

    return walk(0, None, 0)


DEFAULT_MITER_MM = 304.8       # 12" -- a common sliding miter saw


def cut_saws(sheet: SheetLayout,
             miter_capacity: float = DEFAULT_MITER_MM) -> dict[int, str]:
    """Which saw makes each cut, keyed by the piece the cut divides.

    Only two kinds exist: a cut short enough to fit the miter saw, which is the
    easy one, and everything else, which means the track saw.
    """
    saws: dict[int, str] = {}
    for piece in sheet.pieces.values():
        if piece.cut is None:
            continue
        span = piece.w if piece.cut.axis == "H" else piece.h
        saws[piece.id] = "miter" if span <= miter_capacity + 1e-6 else "track"
    return saws


def cut_workload(sheet: SheetLayout,
                 miter_capacity: float = DEFAULT_MITER_MM) -> tuple[int, int]:
    """Counts of (miter cuts, track-saw cuts) on one sheet."""
    saws = cut_saws(sheet, miter_capacity)
    return (sum(1 for v in saws.values() if v == "miter"),
            sum(1 for v in saws.values() if v == "track"))


def cut_offset(sheet: SheetLayout, pid: int) -> float:
    """How far from the piece's reference edge the cut lands.

    This is the number you set the stop to, which is why it -- not the position
    on the sheet -- is what has to stay constant to avoid re-measuring.
    """
    piece = sheet.pieces[pid]
    return (piece.cut.pos - piece.x) if piece.cut.axis == "V" else (piece.cut.pos - piece.y)


def schedule_cuts(sheet: SheetLayout,
                  miter_capacity: float = 304.8,
                  start_width: float | None = None) -> list[int]:
    """One sheet's cuts in an order that keeps the stop still, as piece ids.

    A thin wrapper over `schedule`, kept because it is the natural unit for a
    single sheet: the real work -- and the freedom to reorder the bands a run
    divides a piece into -- lives there.
    """
    plan = sched.plan_sheet(sheet, miter_capacity, start_width)
    sched.apply_plan([sheet], plan, miter_capacity=miter_capacity)
    return [step.pid for step in plan.steps]


def stop_runs(sheets: list[SheetLayout],
              miter_capacity: float = 304.8) -> list[tuple[float, int]]:
    """Consecutive track cuts sharing a stop, as (width, how many).

    Runs carry across sheets: finishing one sheet on a 20" stop and starting the
    next on 20" costs nothing, because the saw has not been touched. Axis does
    not matter either -- the stop is one number, so a rip and a crosscut at the
    same measurement are the same setting.
    """
    return sched.quick_runs(sheets, miter_capacity)


def stop_changes(sheets: list[SheetLayout],
                 miter_capacity: float = 304.8) -> int:
    """How many times the stop has to move on the track saw.

    Counted from a real schedule, not from how many distinct widths appear.
    Those differ, in both directions. Counting distinct widths undercounts,
    because precedence can force a width to be revisited: two columns of one
    width either side of a third can only be cut consecutively if nothing has to
    happen between them. Counting the tree as the packer emitted it *over*counts,
    because the order of the bands a run divides a piece into is arbitrary --
    that is the freedom `schedule` spends.
    """
    return sched.score_job(sheets, miter_capacity)[0]


def saw_changes(sheets: list[SheetLayout],
                miter_capacity: float = DEFAULT_MITER_MM) -> int:
    """How many times you walk between the two saws, over the whole job.

    A property of the cutting order, not of the tree: chop cuts are put off to
    the end of each sheet, because a chop cut never disturbs the stop and so
    costs nothing to defer. Two saw changes per sheet is the shape to expect --
    break it down on the track saw, then chop what came off.
    """
    return sched.score_job(sheets, miter_capacity)[1]


def biggest_offcut(sheets: list[SheetLayout]) -> float:
    """Area of the largest single reusable piece anywhere in the job."""
    biggest = 0.0
    for sheet in sheets:
        off = sheet.largest_offcut()
        if off is not None:
            biggest = max(biggest, off.area)
    return biggest


def emptiest_first(sheet_area: float):
    """Sheet count, then how empty the least-full sheet is. Lower is better.

    Sheet count on its own is a hopeless thing to search on: nearly every
    candidate ties with the incumbent, so the search cannot tell which of them is
    *closer* to needing one sheet fewer. The tiebreaker is the whole gradient,
    and which one it is decides whether the floor gets found at all.

    A sheet disappears when its parts fit elsewhere, so the way to lose one is to
    keep making the least full sheet emptier still: every move that shifts work
    off it is rewarded, right up to the moment it empties and the count drops.

    With a single sheet there is nothing to shed and nowhere to shed it to, so
    the term is switched off rather than left arguing for a half-empty sheet.
    """
    def rate(sheets: list[SheetLayout]) -> tuple:
        emptiest = (min(s.used_area() for s in sheets) / sheet_area
                    if len(sheets) > 1 else 0.0)
        return (len(sheets), emptiest, -biggest_offcut(sheets))
    return rate


def consolidate_first(sheet_area: float):
    """Sheet count, then the largest single piece of free material.

    The other gradient: gather the waste into one rectangle instead of scattering
    it, and eventually everything fits on one sheet fewer. It is much the weaker
    of the two on big jobs -- a tidy layout is precisely what cannot shed a sheet
    -- but it wins on small ones, where there are only two sheets and
    consolidating one of them *is* emptying the other.
    """
    def rate(sheets: list[SheetLayout]) -> tuple:
        return (len(sheets), -biggest_offcut(sheets),
                sum(s.total_cut_length() for s in sheets))
    return rate


def sheet_surrogates(sheet_area: float) -> tuple:
    """Both gradients toward one sheet fewer.

    Neither dominates the other, and plywood is the one number nothing else is
    allowed to trade away, so the opening pass runs both and keeps whichever
    packed tighter. Two half-length searches beat one full-length search under
    the wrong gradient by a whole sheet.
    """
    return (emptiest_first(sheet_area), consolidate_first(sheet_area))


def scrap_pieces(sheet: SheetLayout) -> int:
    """Leaves holding no part: every one of them costs a cut."""
    return sum(1 for p in sheet.pieces.values()
               if p.part_id is None and not p.children)


def trim_cuts(sheet: SheetLayout) -> int:
    """Cuts whose offcut was thinner than the blade, so nothing usable survived.

    These nodes have a single child, which is why the leaf-counting identity
    needs them as a correction term.
    """
    return sum(1 for p in sheet.pieces.values()
               if p.cut is not None and len(p.children) < 2)


def measure(sheets: list[SheetLayout],
            cabinets: dict[str, str] | None = None,
            miter_capacity: float = DEFAULT_MITER_MM) -> dict[str, float]:
    """Every criterion's value for a layout, all lower-is-better."""
    biggest = 0.0
    for sheet in sheets:
        off = sheet.largest_offcut()
        if off is not None:
            biggest = max(biggest, off.area)

    # One schedule answers both sequence questions, and building it is the
    # expensive part of scoring a layout.
    stops, saws = sched.score_job(sheets, miter_capacity)
    miter = track = 0
    for sheet in sheets:
        m, t = cut_workload(sheet, miter_capacity)
        miter += m
        track += t

    return {
        SHEETS: len(sheets),
        GROUPING: (sum(separating_cuts(s, cabinets) for s in sheets)
                   if cabinets else 0),
        STAGED: saws,
        STOPCHANGES: stops,
        TRACKCUTS: track,
        MITERCUTS: miter,
        OFFCUT: -biggest,
    }


def resolve_order(priorities) -> tuple[str, ...]:
    """The user's ranking, with any unranked criteria appended as tiebreakers."""
    ranked = [c for c in (priorities or ()) if c in CRITERIA]
    seen = set(ranked)
    return tuple(ranked) + tuple(c for c in DEFAULT_ORDER if c not in seen)


def score(sheets: list[SheetLayout], priorities,
          cabinets: dict[str, str] | None = None,
          miter_capacity: float = DEFAULT_MITER_MM) -> tuple:
    """Lexicographic score in the user's ranked order."""
    values = measure(sheets, cabinets, miter_capacity)
    return tuple(values[c] for c in resolve_order(priorities))


def verify_cut_identity(sheets: list[SheetLayout]) -> bool:
    """cuts == parts + scrap + trims - sheets, for any guillotine tree."""
    parts = sum(len(s.placed) for s in sheets)
    scrap = sum(scrap_pieces(s) for s in sheets)
    trims = sum(trim_cuts(s) for s in sheets)
    cuts = sum(s.cut_count() for s in sheets)
    return cuts == parts + scrap + trims - len(sheets)
