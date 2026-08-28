"""Guillotine nesting with kerf, plus a randomised-restart search.

Every cut a track saw makes runs edge-to-edge, so the layout has to be a
guillotine pattern: recursively split a rectangle in two, then split those.
This module builds that split tree explicitly, which is what lets `cuts.py`
emit a physically valid cutting order later.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import NamedTuple

EPS = 1e-6
HEURISTICS = ("BAF", "BSSF", "BLSF")

# Objective modes.
MODE_MATERIAL = "material"
MODE_CUTS = "cuts"
MODE_CABINETS = "cabinets"

# In cuts mode, what one extra sheet is worth in saved cuts. Without a price on
# sheets the search would "win" by scattering parts over many near-empty sheets,
# which needs very few cuts but is obviously useless.
SHEET_COST_IN_CUTS = 20.0


@dataclass
class CutRecord:
    axis: str          # "V" = vertical line at constant x; "H" = horizontal at constant y
    pos: float         # absolute coordinate on the sheet


@dataclass
class Piece:
    """A rectangle of material: the whole sheet, an offcut, or a finished part."""

    id: int
    x: float
    y: float
    w: float
    h: float
    parent: int | None = None
    children: list[int] = field(default_factory=list)
    cut: CutRecord | None = None
    part_id: str | None = None
    label: str = ""

    @property
    def area(self) -> float:
        return self.w * self.h


@dataclass
class PartSpec:
    id: str
    label: str
    w: float           # across the sheet width when unrotated
    h: float           # along the sheet length when unrotated
    grain_locked: bool = False
    group_key: str = ""   # which cabinet this part belongs to


class SheetLayout:
    def __init__(self, width: float, length: float, kerf: float):
        self.width = width
        self.length = length
        self.kerf = kerf
        self.pieces: dict[int, Piece] = {0: Piece(0, 0.0, 0.0, width, length)}
        self.next_id = 1
        self.free: list[int] = [0]
        self.placed: list[tuple[str, int, bool]] = []   # (part_id, piece_id, rotated)

    # -- cutting ---------------------------------------------------------

    def _cut(self, pid: int, axis: str, size: float) -> tuple[int, int | None]:
        """Trim piece `pid` down to `size` along `axis`.

        Returns (kept_piece_id, offcut_piece_id or None). The offcut is None both
        when the piece already fits exactly (no cut needed, no kerf consumed) and
        when the remainder is narrower than the blade (the cut happens but nothing
        usable survives it).
        """
        pc = self.pieces[pid]
        remainder = (pc.w - size) if axis == "V" else (pc.h - size)
        if remainder <= EPS:
            return pid, None

        if axis == "V":
            kept = Piece(self.next_id, pc.x, pc.y, size, pc.h, parent=pid)
            cut_pos = pc.x + size
        else:
            kept = Piece(self.next_id, pc.x, pc.y, pc.w, size, parent=pid)
            cut_pos = pc.y + size
        self.next_id += 1
        self.pieces[kept.id] = kept
        pc.children.append(kept.id)
        pc.cut = CutRecord(axis, cut_pos)

        off_id = None
        off_size = remainder - self.kerf
        if off_size > EPS:
            if axis == "V":
                off = Piece(self.next_id, pc.x + size + self.kerf, pc.y, off_size, pc.h, parent=pid)
            else:
                off = Piece(self.next_id, pc.x, pc.y + size + self.kerf, pc.w, off_size, parent=pid)
            self.next_id += 1
            self.pieces[off.id] = off
            pc.children.append(off.id)
            off_id = off.id
        return kept.id, off_id

    # -- placement -------------------------------------------------------

    def _fit_score(self, pc: Piece, w: float, h: float, heuristic: str) -> float:
        if heuristic == "BAF":
            return pc.area - w * h
        if heuristic == "BSSF":
            return min(pc.w - w, pc.h - h)
        return max(pc.w - w, pc.h - h)

    def try_place(self, part: PartSpec, heuristic: str, vertical_first: bool) -> bool:
        options = [(part.w, part.h, False)]
        if not part.grain_locked and abs(part.w - part.h) > EPS:
            options.append((part.h, part.w, True))

        best = None
        for pid in self.free:
            pc = self.pieces[pid]
            for w, h, rotated in options:
                if w <= pc.w + EPS and h <= pc.h + EPS:
                    s = self._fit_score(pc, w, h, heuristic)
                    if best is None or s < best[0]:
                        best = (s, pid, w, h, rotated)
        if best is None:
            return False

        _, pid, w, h, rotated = best
        order = [("V", w), ("H", h)] if vertical_first else [("H", h), ("V", w)]
        kept = pid
        offcuts = []
        for axis, size in order:
            kept, off = self._cut(kept, axis, size)
            if off is not None:
                offcuts.append(off)

        self.pieces[kept].part_id = part.id
        self.free.remove(pid)
        self.free.extend(offcuts)
        self.placed.append((part.id, kept, rotated))
        return True

    # -- reporting -------------------------------------------------------

    def used_area(self) -> float:
        return sum(self.pieces[pid].area for _, pid, _ in self.placed)

    def largest_offcut(self) -> Piece | None:
        if not self.free:
            return None
        return max((self.pieces[p] for p in self.free), key=lambda p: p.area)

    def cut_count(self) -> int:
        return sum(1 for pc in self.pieces.values() if pc.cut is not None)

    def total_cut_length(self) -> float:
        total = 0.0
        for pc in self.pieces.values():
            if pc.cut is not None:
                total += pc.h if pc.cut.axis == "V" else pc.w
        return total


def separating_pieces(sheet: SheetLayout, cabinets: dict[str, str]) -> set[int]:
    """Pieces whose cut must be made before every piece holds one cabinet.

    Walk the guillotine tree: a node whose subtree still contains parts from more
    than one cabinet has to be cut before the pieces can be sorted. Counting those
    nodes gives the real cost of untangling cabinets on a shared sheet -- one cut
    to halve a sheet between two cabinets scores 1, whereas cabinets interleaved
    part-by-part score once for nearly every cut on the sheet.
    """
    seen: dict[int, set[str]] = {}

    def subtree(pid: int) -> set[str]:
        cached = seen.get(pid)
        if cached is not None:
            return cached
        piece = sheet.pieces[pid]
        names: set[str] = set()
        if piece.part_id is not None:
            name = cabinets.get(piece.part_id)
            if name:
                names.add(name)
        for child in piece.children:
            names |= subtree(child)
        seen[pid] = names
        return names

    subtree(0)
    return {pid for pid, piece in sheet.pieces.items()
            if piece.cut is not None and len(seen.get(pid, ())) > 1}


def separating_cuts(sheet: SheetLayout, cabinets: dict[str, str]) -> int:
    """How many cuts have to happen before cabinets can be sorted apart."""
    return len(separating_pieces(sheet, cabinets))


def pack(parts: list[PartSpec], width: float, length: float, kerf: float,
         heuristic: str, vertical_first: bool) -> list[SheetLayout] | None:
    """First-fit the parts across as many sheets as needed. None if a part cannot fit."""
    sheets: list[SheetLayout] = []
    for part in parts:
        if not any(s.try_place(part, heuristic, vertical_first) for s in sheets):
            fresh = SheetLayout(width, length, kerf)
            if not fresh.try_place(part, heuristic, vertical_first):
                return None
            sheets.append(fresh)
    return sheets


def score(sheets: list[SheetLayout], mode: str = MODE_MATERIAL,
          cabinets: dict[str, str] | None = None) -> tuple:
    """Lexicographic objective, lower is better.

    material: sheet count dominates -- that is what costs money. Among
      equal-sheet layouts prefer one big reusable offcut over the same area
      scattered as unusable slivers, then prefer fewer metres of sawing.
    cuts: saw time dominates, with each extra sheet priced in saved cuts so the
      search cannot cheat by spreading parts thinly.
    cabinets: sheet count still dominates -- an extra sheet costs far more than
      a handful of extra cuts -- then minimise the cuts needed to get each
      cabinet's parts into their own pile.
    """
    biggest = 0.0
    for s in sheets:
        off = s.largest_offcut()
        if off is not None:
            biggest = max(biggest, off.area)

    if mode == MODE_CUTS:
        cuts = sum(s.cut_count() for s in sheets)
        return (cuts + SHEET_COST_IN_CUTS * len(sheets), len(sheets), -biggest)

    if mode == MODE_CABINETS and cabinets:
        separating = sum(separating_cuts(s, cabinets) for s in sheets)
        return (len(sheets), separating, -biggest,
                sum(s.total_cut_length() for s in sheets))

    return (len(sheets), -biggest, sum(s.total_cut_length() for s in sheets))


def _grouped_orders(parts: list[PartSpec]) -> list[list[PartSpec]]:
    """Orderings that keep each cabinet's parts together, largest cabinet first."""
    buckets: dict[str, list[PartSpec]] = {}
    for part in parts:
        buckets.setdefault(part.group_key, []).append(part)
    if len(buckets) < 2:
        return []
    by_area = sorted(buckets.values(), key=lambda ps: -sum(p.w * p.h for p in ps))
    orders = []
    for inner in (lambda ps: sorted(ps, key=lambda p: -(p.w * p.h)),
                  lambda ps: sorted(ps, key=lambda p: (-p.w, -p.h))):
        orders.append([p for block in by_area for p in inner(block)])
    return orders


def _seed_orders(parts: list[PartSpec]) -> list[list[PartSpec]]:
    return [
        sorted(parts, key=lambda p: -(p.w * p.h)),
        sorted(parts, key=lambda p: -max(p.w, p.h)),
        sorted(parts, key=lambda p: -p.h),
        sorted(parts, key=lambda p: -p.w),
        sorted(parts, key=lambda p: -(p.w + p.h)),
        # Identical sizes adjacent, so one rip yields a strip that crosscuts into
        # a row of matching parts instead of a jumble.
        sorted(parts, key=lambda p: (-p.w, -p.h)),
        sorted(parts, key=lambda p: (-p.h, -p.w)),
    ]


class SearchState(NamedTuple):
    """One observation of the search, handed to the caller as it runs."""
    sheets: list[SheetLayout]
    score: tuple[float, float, float]
    attempts: int
    improved: bool          # False for a heartbeat: same layout, still working
    order: list = ()        # the part ordering behind this layout, for warm starts


def _perturb(order: list[PartSpec], rng: random.Random) -> list[PartSpec]:
    """A small change to an ordering -- the neighbourhood for the local search."""
    out = order[:]
    n = len(out)
    if n < 2:
        return out
    roll = rng.random()
    if roll < 0.40:                       # swap a few pairs
        for _ in range(rng.randint(1, 3)):
            i, j = rng.randrange(n), rng.randrange(n)
            out[i], out[j] = out[j], out[i]
    elif roll < 0.75:                     # move one part elsewhere in the order
        out.insert(rng.randrange(n), out.pop(rng.randrange(n)))
    else:                                 # reverse a short run
        i = rng.randrange(n)
        length = rng.randint(2, max(2, n // 8))
        out[i:i + length] = reversed(out[i:i + length])
    return out


def _perturb_grouped(order: list[PartSpec], rng: random.Random) -> list[PartSpec]:
    """Perturb without tearing cabinets apart.

    Element-level swaps interleave cabinets, which is exactly what this mode is
    trying to avoid. So move whole cabinet blocks around, or shuffle within one
    block, leaving each cabinet contiguous in the ordering.
    """
    blocks: list[list[PartSpec]] = []
    for part in order:
        if blocks and blocks[-1][0].group_key == part.group_key:
            blocks[-1].append(part)
        else:
            blocks.append([part])

    roll = rng.random()
    if roll < 0.35 and len(blocks) > 1:                  # swap two blocks
        i, j = rng.randrange(len(blocks)), rng.randrange(len(blocks))
        blocks[i], blocks[j] = blocks[j], blocks[i]
    elif roll < 0.60 and len(blocks) > 1:                # move one block
        blocks.insert(rng.randrange(len(blocks)), blocks.pop(rng.randrange(len(blocks))))
    else:                                                # reorder inside a block
        inner = blocks[rng.randrange(len(blocks))]
        if len(inner) > 1:
            i, j = rng.randrange(len(inner)), rng.randrange(len(inner))
            inner[i], inner[j] = inner[j], inner[i]

    return [part for block in blocks for part in block]


def optimise_iter(parts: list[PartSpec], width: float, length: float, kerf: float,
                  time_budget: float = 5.0, seed: int = 12345,
                  heartbeat: float = 0.2, mode: str = MODE_MATERIAL,
                  warm_start: list[PartSpec] | None = None,
                  cabinets: dict[str, str] | None = None):
    """Search for the best guillotine layout, reporting progress as it goes.

    Deterministic size-ordered seeds first, then **iterated local search**: keep
    perturbing the best ordering found rather than reshuffling from scratch.
    That matters at scale -- with 100+ parts a random shuffle essentially never
    beats a size-sorted order, so a restart-only search finds everything in the
    first few milliseconds and then stalls for the rest of its budget.

    Yields SearchState. `improved` is True when the layout actually got better;
    heartbeats (same layout) are emitted every `heartbeat` seconds so a caller
    can show that the search is still alive.

    The caller is free to stop consuming early -- see solver.solve_streaming,
    which gives up on a group once it is provably at its minimum sheet count.
    """
    rng = random.Random(seed)
    best: list[SheetLayout] | None = None
    best_score: tuple[float, float, float] | None = None
    best_order: list[PartSpec] | None = None
    best_config: tuple[str, bool] = (HEURISTICS[0], True)
    attempts = 0

    def consider(order, heuristic, vertical_first) -> bool:
        nonlocal best, best_score, best_order, best_config, attempts
        attempts += 1
        result = pack(order, width, length, kerf, heuristic, vertical_first)
        if result is None:
            return False
        s = score(result, mode, cabinets)
        if best_score is None or s < best_score:
            best, best_score = result, s
            best_order, best_config = order[:], (heuristic, vertical_first)
            return True
        return False

    seeds = _seed_orders(parts)
    if mode == MODE_CABINETS:
        seeds = _grouped_orders(parts) + seeds
    if warm_start:
        seeds.insert(0, list(warm_start))   # continue from a previous run's best
    for order in seeds:
        for heuristic in HEURISTICS:
            for vertical_first in (True, False):
                if consider(order, heuristic, vertical_first):
                    yield SearchState(best, best_score, attempts, True, best_order)

    if best is None or best_order is None:
        return

    deadline = time.perf_counter() + time_budget
    last_beat = time.perf_counter()
    current = best_order[:]
    stale = 0

    while time.perf_counter() < deadline:
        candidate = (_perturb_grouped(current, rng) if mode == MODE_CABINETS
                     else _perturb(current, rng))
        # Mostly reuse the configuration that produced the best result; explore
        # the others occasionally so the search is not locked into one shape.
        if rng.random() < 0.8:
            heuristic, vertical_first = best_config
        else:
            heuristic, vertical_first = rng.choice(HEURISTICS), rng.random() < 0.5

        if consider(candidate, heuristic, vertical_first):
            current = candidate
            stale = 0
            last_beat = time.perf_counter()
            yield SearchState(best, best_score, attempts, True, best_order)
        else:
            stale += 1
            if stale > 300:               # kick back to the best known ordering
                current = best_order[:]
                stale = 0
            now = time.perf_counter()
            if now - last_beat >= heartbeat:
                last_beat = now
                yield SearchState(best, best_score, attempts, False, best_order)


def optimise(parts: list[PartSpec], width: float, length: float, kerf: float,
             time_budget: float = 5.0, seed: int = 12345,
             mode: str = MODE_MATERIAL,
             cabinets: dict[str, str] | None = None) -> tuple[list[SheetLayout], int]:
    """Best layout found within the budget.

    Returns (sheets, improvements) -- the number of times the search found a
    better layout, not the number of packings it tried.
    """
    best: list[SheetLayout] = []
    improvements = 0
    for state in optimise_iter(parts, width, length, kerf, time_budget, seed,
                               mode=mode, cabinets=cabinets):
        if state.improved:
            best = state.sheets
            improvements += 1
    return best, improvements
