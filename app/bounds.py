"""Certified lower bounds.

These are what let the tool say "proven optimal" as a fact rather than a hope.
Each bound is a value no layout can beat; when a result meets its bound it is
optimal, and when it does not the difference is the exact remaining headroom.

2D guillotine cutting is NP-hard, so these are lower bounds, not achievability
proofs -- a gap does not mean a better layout exists, only that none has been
ruled out.
"""
from __future__ import annotations

from collections import defaultdict
from math import ceil

from . import schedule as sched
from .nesting import PartSpec, SheetLayout
from .objective import (CUTS, GROUPING, MITERCUTS, OFFCUT, SHEETS, STAGED,
                        TRACKCUTS)


def sheet_bound(specs: list[PartSpec], usable_w: float, usable_l: float) -> int:
    """Area bound: parts cannot occupy less room than their own area."""
    if not specs:
        return 0
    area = sum(s.w * s.h for s in specs)
    return max(1, ceil(area / (usable_w * usable_l) - 1e-9))


def cut_bound(part_count: int, sheets: int) -> int:
    """At `sheets` sheets, no layout uses fewer than parts - sheets cuts.

    From cuts = parts + scrap + trims - sheets, with scrap and trims both >= 0.
    A sheet holding p parts needs at least p - 1 cuts to separate them, and in
    practice one more because something is always left over -- but only the
    unconditional form is certified here.
    """
    return max(0, part_count - max(sheets, 1))


def grouping_bound(specs: list[PartSpec], usable_w: float, usable_l: float,
                   sheets: int) -> int:
    """Cabinets that cannot fit on one sheet must appear on several.

    Each (cabinet, sheet) incidence beyond the first on a sheet costs a cut, so
    total grouping cuts >= (total incidences) - (number of sheets).
    """
    if not specs:
        return 0
    area = usable_w * usable_l
    per: dict[str, float] = defaultdict(float)
    for spec in specs:
        per[spec.group_key] += spec.w * spec.h
    incidences = sum(max(1, ceil(a / area - 1e-9)) for a in per.values())
    return max(0, incidences - max(sheets, 1))


def trackcut_bound(specs: list[PartSpec], miter_capacity: float) -> int:
    """Crosscuts that no layout can move onto the miter saw.

    A part can only be chopped at home if one of its dimensions fits under the
    saw -- then it can live in a strip that narrow. A part with both dimensions
    over the limit has to be crosscut wide however it is arranged, so one
    track-saw pass per such part is unavoidable.
    """
    return sum(1 for p in specs if min(p.w, p.h) > miter_capacity + 1e-6)


def _chain_settings(chain) -> tuple[int, set[float]]:
    """Settings one run of same-axis cuts cannot avoid.

    A run of n bands makes a fixed number of cuts and gets to choose *which*
    bands they take off. So it needs at least as many distinct settings as it
    takes greedily-largest groups to cover that many cuts, and any setting it
    cannot make its quota without is forced outright.
    """
    quota = chain.cuts
    if quota <= 0:
        return 0, set()
    least = 0
    covered = 0
    for size in sorted(chain.counts.values(), reverse=True):
        if covered >= quota:
            break
        covered += size
        least += 1
    bands = len(chain.sections)
    forced = {offset for offset, count in chain.counts.items()
              if bands - count < quota}
    return least, forced


def _sheet_settings(chains) -> int:
    """Distinct stop settings this sheet's cuts cannot be made without.

    Three arguments, and the strongest of them wins:

    *Capacity.* Every cut on the sheet has to be made at some setting, and one
    setting can only make as many cuts as there are bands wanting it -- capped
    per run by how many cuts that run makes at all. So the settings have to
    cover the sheet's whole cut count, and no k of them can cover more than the
    k largest capacities.

    *Per run.* One run alone may need several settings; see `_chain_settings`.

    *Forced.* A setting some run cannot make its quota without has to appear.
    """
    quota = 0
    capacity: dict[float, int] = {}
    least = 0
    forced: set[float] = set()
    for chain in chains:
        if chain.saw != sched.TRACK or not chain.cuts:
            continue
        quota += chain.cuts
        for offset, count in chain.counts.items():
            capacity[offset] = capacity.get(offset, 0) + min(count, chain.cuts)
        count, must = _chain_settings(chain)
        least = max(least, count)
        forced |= must

    covered = 0
    by_capacity = 0
    for size in sorted(capacity.values(), reverse=True):
        if covered >= quota:
            break
        covered += size
        by_capacity += 1
    return max(by_capacity, least, len(forced))


def stopchange_bound(sheets: list[SheetLayout],
                     miter_capacity: float = 304.8) -> int:
    """Stop moves no cutting order can avoid, for these layouts.

    Each sheet is broken down in one go, so a run of cuts at one setting lies
    inside a single sheet -- except at a sheet boundary, where one run can serve
    the end of one sheet and the start of the next. A sheet therefore needs at
    least as many runs as it has settings it cannot avoid using, and the job
    needs at least the sum of those, less one per boundary.

    This is a bound on *ordering*, given the layouts: meeting it proves the cut
    order is the best one for this layout, not that no better layout exists.
    """
    if not sheets:
        return 0
    job = sched.Job(sheets, miter_capacity)
    total = sum(_sheet_settings(chains) for chains in job.per_sheet)
    return max(0, total - len(sheets))


def sawchange_bound(sheets: list[SheetLayout],
                    miter_capacity: float = 304.8) -> int:
    """Trips between the two saws no cutting order can avoid, for these layouts.

    Which saw makes a cut is fixed by how far it has to reach, not chosen, so a
    sheet holding both kinds needs at least two blocks of work and one trip
    between them. Sheet portions of the order are contiguous, so those blocks
    are contiguous too -- and only at a sheet boundary can two of them merge.
    """
    if not sheets:
        return 0
    total = 0
    for sheet in sheets:
        saws = {sched.MITER if (piece.w if piece.cut.axis == "H" else piece.h)
                <= miter_capacity + 1e-6 else sched.TRACK
                for piece in sheet.pieces.values() if piece.cut is not None}
        total += max(1, len(saws))
    return max(0, total - len(sheets))


def compute(specs: list[PartSpec], usable_w: float, usable_l: float,
            achieved_sheets: int | None = None,
            miter_capacity: float = 304.8) -> dict[str, float]:
    """Bounds for every criterion.

    `cuts` and `grouping` depend on how many sheets are in play -- more sheets
    means fewer cuts are forced -- so they are computed against the achieved
    sheet count where one is known, and the sheet bound otherwise.
    """
    sheets_lb = sheet_bound(specs, usable_w, usable_l)
    basis = achieved_sheets if achieved_sheets else sheets_lb
    return {
        SHEETS: sheets_lb,
        GROUPING: grouping_bound(specs, usable_w, usable_l, basis),
        # Any layout needs at least one saw. A tighter floor would have to
        # prove both saws are unavoidable, which the part sizes alone cannot.
        STAGED: 1 if len(specs) > 1 else 0,
        TRACKCUTS: trackcut_bound(specs, miter_capacity),
        # No useful floor: chop-saw cuts are the cheap kind, so there is nothing
        # to prove about doing few of them.
        MITERCUTS: float("-inf"),
        OFFCUT: float("-inf"),      # no useful bound; larger is better
    }
