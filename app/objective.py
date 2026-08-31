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
    """How many times you swap saws on the worst path through the sheet.

    Two stages is the pattern worth aiming for: break everything down on the
    track saw, put it away, then chop the rest. Switching cut *axis* costs
    nothing with a track saw, so it does not count -- only picking up a
    different tool does.

    One stage is possible but usually undesirable: it means never touching the
    miter saw, which is the easy work. Rank track cuts above stages to stop the
    search reaching for it.
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
    """Order a sheet's cuts so equal track-saw widths land back to back.

    Any order works as long as a piece is cut before its children, which leaves
    real freedom: several cuts are usually available at once. This spends that
    freedom on keeping the stop where it is -- take another cut at the current
    width if one is available, otherwise clear the free miter cuts, and only
    then move the stop, jumping to whichever width has the most work waiting.

    Ready cuts are bucketed by width so the common case is a dict lookup rather
    than a scan of everything available. The scan only happens when the stop
    actually has to move, which is exactly what we are trying to make rare.
    """
    saws = cut_saws(sheet, miter_capacity)
    widths: dict[int, float] = {}
    by_width: dict[float, list[int]] = {}
    free_miter: list[int] = []

    def offer(pid: int) -> None:
        piece = sheet.pieces[pid]
        if piece.cut is None:
            return
        if saws[pid] == "miter":
            free_miter.append(pid)
            return
        width = round(cut_offset(sheet, pid), 1)
        widths[pid] = width
        by_width.setdefault(width, []).append(pid)

    offer(0)
    order: list[int] = []
    current = start_width

    while by_width or free_miter:
        pick = None
        bucket = by_width.get(current) if current is not None else None
        if bucket:
            pick = bucket.pop()
            if not bucket:
                del by_width[current]
        elif free_miter:
            pick = free_miter.pop()
        elif by_width:
            # The stop has to move: go where the most work is waiting.
            current = max(by_width, key=lambda w: (len(by_width[w]), -w))
            bucket = by_width[current]
            pick = bucket.pop()
            if not bucket:
                del by_width[current]

        if pick is None:
            break
        order.append(pick)
        for child in sheet.pieces[pick].children:
            offer(child)
    return order


def stop_runs(sheets: list[SheetLayout],
              miter_capacity: float = 304.8) -> list[tuple[float, int]]:
    """Consecutive track cuts sharing a stop, as (width, how many).

    Runs carry across sheets: finishing one sheet on a 20" stop and starting the
    next on 20" costs nothing, because the saw has not been touched. Uses the
    real schedule, so this is what the cut list will actually say.
    """
    runs: list[tuple[float, int]] = []
    current: float | None = None
    for sheet in sheets:
        saws = cut_saws(sheet, miter_capacity)
        for pid in schedule_cuts(sheet, miter_capacity, current):
            if saws[pid] != "track":
                continue
            width = round(cut_offset(sheet, pid), 1)
            if runs and width == current:
                runs[-1] = (width, runs[-1][1] + 1)
            else:
                runs.append((width, 1))
            current = width
    return runs


def stop_changes(sheets: list[SheetLayout],
                 miter_capacity: float = 304.8) -> int:
    """How many times the stop has to move on the track saw.

    Counted from the real schedule, not from how many distinct widths appear.
    Those differ: two columns of equal width sitting either side of a third can
    only be cut consecutively if nothing has to happen between them, and here
    something does -- the middle column's rip, at another measurement. Counting
    distinct widths called that free and undercounted by a fifth, so the search
    had no reason to put equal widths next to each other.
    """
    return max(0, len(stop_runs(sheets, miter_capacity)) - 1)


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

    return {
        SHEETS: len(sheets),
        GROUPING: (sum(separating_cuts(s, cabinets) for s in sheets)
                   if cabinets else 0),
        STAGED: max((stage_depth(s, miter_capacity) for s in sheets), default=0),
        STOPCHANGES: stop_changes(sheets, miter_capacity),
        TRACKCUTS: sum(cut_workload(s, miter_capacity)[1] for s in sheets),
        MITERCUTS: sum(cut_workload(s, miter_capacity)[0] for s in sheets),
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
