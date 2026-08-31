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

from .nesting import PartSpec
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
