"""Exact search: prove a layout optimal, or prove how close it is.

Naive enumeration is hopeless at real sizes -- 138 parts have 2**138 orientation
combinations alone, which no amount of time touches. Branch and bound gives the
same guarantee and can actually finish: it walks the whole space but abandons any
branch a bound proves cannot beat the best result so far. Nothing that could have
been better is skipped, so a completed search is a proof.

`prove_sheet` is complete for the placement model the packer uses: every part sits
at the lower-left corner of a free rectangle. At each step the lowest-leftmost
free rectangle is either filled by some part or abandoned as scrap, so every
reachable layout is reachable here too, and symmetric re-orderings collapse.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from .nesting import EPS, PartSpec

Rect = tuple[float, float, float, float]        # x, y, w, h


@dataclass
class ExactResult:
    cuts: int | None            # best cut count found, None if nothing fits
    proven: bool                # True when the search completed
    nodes: int                  # branches explored
    seconds: float

    @property
    def status(self) -> str:
        if self.cuts is None:
            return "no arrangement found"
        return "proven optimal" if self.proven else "best found (search incomplete)"


def _place(rect: Rect, pw: float, ph: float, kerf: float,
           vertical_first: bool) -> tuple[int, list[Rect]]:
    """Cuts consumed and offcuts produced by seating a part in a rectangle."""
    cuts = 0
    offcuts: list[Rect] = []
    x, y, w, h = rect
    order = (("V", pw), ("H", ph)) if vertical_first else (("H", ph), ("V", pw))
    for axis, size in order:
        remainder = (w - size) if axis == "V" else (h - size)
        if remainder <= EPS:
            continue
        cuts += 1
        spare = remainder - kerf
        if axis == "V":
            if spare > EPS:
                offcuts.append((x + size + kerf, y, spare, h))
            w = size
        else:
            if spare > EPS:
                offcuts.append((x, y + size + kerf, w, spare))
            h = size
    return cuts, offcuts


def prove_sheet(parts: list[PartSpec], width: float, length: float, kerf: float,
                incumbent: int | None = None,
                time_limit: float = 10.0) -> ExactResult:
    """Fewest cuts that can seat every one of `parts` on one sheet.

    Returns `proven=True` only if the search ran to completion; a timeout yields
    the best found so far and makes no claim about optimality.
    """
    started = time.perf_counter()
    deadline = started + time_limit
    nodes = 0
    complete = True

    # Seed from the heuristic packer so a timeout still returns a real answer
    # rather than nothing, and so pruning has something to prune against.
    best = incumbent
    if best is None:
        from .nesting import HEURISTICS, pack_one
        for heuristic in HEURISTICS:
            for vertical_first in (True, False):
                seeded = pack_one(sorted(parts, key=lambda p: -(p.w * p.h)),
                                  width, length, kerf, heuristic, vertical_first)
                if seeded is not None:
                    got = seeded.cut_count()
                    if best is None or got < best:
                        best = got
    ceiling = best

    # Identical sizes are interchangeable; branching on distinct shapes only is
    # what keeps this finishing at all.
    def shapes(remaining: tuple[int, ...], specs):
        seen = set()
        for i, count in enumerate(remaining):
            if not count:
                continue
            key = (specs[i].w, specs[i].h, specs[i].grain_locked)
            if key in seen:
                continue
            seen.add(key)
            yield i

    distinct: list[PartSpec] = []
    counts: list[int] = []
    index: dict[tuple, int] = {}
    for part in parts:
        key = (round(part.w, 4), round(part.h, 4), part.grain_locked)
        if key not in index:
            index[key] = len(distinct)
            distinct.append(part)
            counts.append(0)
        counts[index[key]] += 1

    def search(free: tuple[Rect, ...], remaining: tuple[int, ...], cuts: int):
        nonlocal best, nodes, complete
        if time.perf_counter() > deadline:
            complete = False
            return
        nodes += 1
        if best is not None and cuts >= best:
            return                              # cannot beat the incumbent
        # Admissible bound: each free rectangle can absorb at most one more part
        # without a cut, so every part beyond that forces at least one.
        outstanding = sum(remaining)
        if best is not None and cuts + max(0, outstanding - len(free)) >= best:
            return
        if not any(remaining):
            best = cuts
            return
        if not free:
            return

        # Canonical choice: always deal with the lowest-leftmost rectangle.
        target = min(range(len(free)), key=lambda i: (free[i][1], free[i][0]))
        rect = free[target]
        rest = free[:target] + free[target + 1:]

        for i in shapes(remaining, distinct):
            spec = distinct[i]
            options = [(spec.w, spec.h)]
            if not spec.grain_locked and abs(spec.w - spec.h) > EPS:
                options.append((spec.h, spec.w))
            for pw, ph in options:
                if pw > rect[2] + EPS or ph > rect[3] + EPS:
                    continue
                for vertical_first in (True, False):
                    used, offcuts = _place(rect, pw, ph, kerf, vertical_first)
                    nxt = remaining[:i] + (remaining[i] - 1,) + remaining[i + 1:]
                    search(rest + tuple(offcuts), nxt, cuts + used)

        search(rest, remaining, cuts)           # abandon this rectangle as scrap

    search(((0.0, 0.0, width, length),), tuple(counts), 0)
    # If the search completed, `best` is optimal. If it timed out, `best` may be
    # only the seeded heuristic value, which proves nothing.
    return ExactResult(cuts=best, proven=complete and best is not None,
                       nodes=nodes, seconds=round(time.perf_counter() - started, 3))


def prove_layout(sheet_parts: list[list[PartSpec]], width: float, length: float,
                 kerf: float, current_cuts: list[int],
                 budget: float = 30.0) -> dict:
    """Try to prove each sheet's arrangement optimal, easiest sheets first.

    Small sheets finish quickly and give real proofs; large ones are reported
    honestly as unproven rather than being dressed up as one.
    """
    order = sorted(range(len(sheet_parts)), key=lambda i: len(sheet_parts[i]))
    per_sheet: list[dict] = [{} for _ in sheet_parts]
    remaining = budget
    proven = improved = 0

    for i in order:
        if remaining <= 0.05:
            per_sheet[i] = {"sheet": i + 1, "parts": len(sheet_parts[i]),
                            "status": "not attempted", "proven": False}
            continue
        slice_budget = min(remaining, max(1.0, budget / max(len(sheet_parts), 1)))
        started = time.perf_counter()
        result = prove_sheet(sheet_parts[i], width, length, kerf,
                             incumbent=current_cuts[i] + 1, time_limit=slice_budget)
        remaining -= time.perf_counter() - started
        better = result.cuts is not None and result.cuts < current_cuts[i]
        proven += 1 if result.proven else 0
        improved += 1 if better else 0
        per_sheet[i] = {
            "sheet": i + 1, "parts": len(sheet_parts[i]),
            "current": current_cuts[i], "best": result.cuts,
            "proven": result.proven, "nodes": result.nodes,
            "seconds": result.seconds,
            "status": ("proven optimal" if result.proven and not better
                       else "a better arrangement exists" if better
                       else "unproven -- search did not finish"),
        }

    return {"sheets": per_sheet, "proven_sheets": proven,
            "improvable_sheets": improved, "total_sheets": len(sheet_parts)}


def brute_force_sheet(parts: list[PartSpec], width: float, length: float,
                      kerf: float) -> int | None:
    """Literal enumeration, for tiny inputs only.

    This exists to check the prover: on instances small enough to enumerate every
    ordering, orientation and split order, both must agree. If branch and bound
    ever prunes away a better layout, this is what catches it.
    """
    from itertools import permutations, product

    from .nesting import HEURISTICS, pack_one

    best = None
    for order in permutations(parts):
        for flips in product((False, True), repeat=len(parts)):
            attempt = []
            for part, flip in zip(order, flips):
                attempt.append(PartSpec(part.id, part.label,
                                        part.h if flip else part.w,
                                        part.w if flip else part.h,
                                        grain_locked=True,
                                        group_key=part.group_key))
            for heuristic in HEURISTICS:
                for vertical_first in (True, False):
                    layout = pack_one(attempt, width, length, kerf,
                                      heuristic, vertical_first)
                    if layout is None:
                        continue
                    cuts = layout.cut_count()
                    if best is None or cuts < best:
                        best = cuts
    return best
