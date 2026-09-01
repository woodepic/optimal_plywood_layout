"""Guillotine nesting with kerf, plus a randomised-restart search.

Every cut a track saw makes runs edge-to-edge, so the layout has to be a
guillotine pattern: recursively split a rectangle in two, then split those.
This module builds that split tree explicitly, which is what lets `cuts.py`
emit a physically valid cutting order later.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field, replace
from math import ceil
from typing import NamedTuple

EPS = 1e-6
# How to choose which free rectangle a part goes in, and which way round. The
# first three are the classic area/short-side/long-side fits. The other two are
# ours, and each is aimed at one of the ranked criteria:
#
#   "STOP"  prefer cuts that land on a measurement this sheet has already been
#           set to, or need no cut at all. Every cut offset is a part dimension,
#           so this is the difference between a sheet needing two stop settings
#           and needing six.
#   "CHOP"  prefer cuts the miter saw can reach. Which saw makes a cut is
#           decided by how far it has to reach, and reach is decided by how the
#           part is turned -- so this is the difference between chopping a part
#           at the bench and wrestling the track saw across it.
HEURISTICS = ("BAF", "BSSF", "BLSF", "STOP", "CHOP")
# The three classic fits, for the pass that is only trying to shed a sheet.
# "STOP" and "CHOP" each trade area for something the floor pass has no use for
# yet, and offering them there measurably costs both density and structure: on
# the sample kitchen's tight stock the same search that lands on 12 sheets and
# 138 track cuts with these three lands on 196 with all five. They earn their
# place in the ranked stages, which is where the thing they optimise is the
# objective.
DENSITY_HEURISTICS = ("BAF", "BSSF", "BLSF")
STOP_ROUND = 1        # mm; offsets this close are one setting, as in app.schedule
DEFAULT_MITER = 304.8 # 12"; a common sliding miter saw

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
    # Set when this cut's position in its run is not free to change -- the
    # continuous-grain faces have to come off in installation order. See
    # app.grain and Chain.pinned in app.schedule.
    pinned: bool = False

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
    def __init__(self, width: float, length: float, kerf: float,
                 min_offcut: float = 0.0,
                 miter_capacity: float = DEFAULT_MITER):
        self.width = width
        self.length = length
        self.kerf = kerf
        # How far the chop saw reaches. The packer needs it because *placement*
        # decides which saw a cut lands on: turn a 9" x 30" part the other way
        # and its crosscut goes from a bench chop to a track-saw setup.
        self.miter_capacity = miter_capacity
        # Anything narrower than this is treated as waste rather than a usable
        # offcut. Default 0: keep every sliver, however thin. A remainder smaller
        # than the kerf still cannot survive -- the blade consumes that material.
        self.min_offcut = min_offcut
        self.pieces: dict[int, Piece] = {0: Piece(0, 0.0, 0.0, width, length)}
        self.next_id = 1
        self.free: list[int] = [0]
        self.placed: list[tuple[str, int, bool]] = []   # (part_id, piece_id, rotated)
        # Set by app.schedule, which decomposes the cut tree into permutable
        # runs and schedules them. Both are pure functions of the tree, and the
        # search reschedules the same sheets thousands of times, so they are
        # remembered here and dropped the moment the tree changes.
        self.chain_cache = None
        self.sched_cache = None
        # Every stop setting this sheet's cuts have used, and how often. Drives
        # the "STOP" placement heuristic.
        self.stops: dict[float, int] = {}

    def copy(self) -> "SheetLayout":
        """A detached copy, so a caller can rewrite the tree without side effects.

        Sequencing rewrites the order the bands of a run are cut in. That is
        free and desirable, but the search holds these layouts and compares them
        against scores taken earlier, so composing an answer works on a copy.
        """
        clone = SheetLayout(self.width, self.length, self.kerf, self.min_offcut,
                            self.miter_capacity)
        clone.pieces = {pid: replace(pc, children=list(pc.children),
                                     cut=(None if pc.cut is None
                                          else replace(pc.cut)))
                        for pid, pc in self.pieces.items()}
        clone.next_id = self.next_id
        clone.free = list(self.free)
        clone.placed = list(self.placed)
        clone.stops = dict(self.stops)
        return clone

    # -- cutting ---------------------------------------------------------

    def _cut(self, pid: int, axis: str, size: float) -> tuple[int, int | None]:
        """Trim piece `pid` down to `size` along `axis`.

        Returns (kept_piece_id, offcut_piece_id or None). The offcut is None both
        when the piece already fits exactly (no cut needed, no kerf consumed) and
        when the remainder is narrower than the blade (the cut happens but nothing
        usable survives it -- that material becomes sawdust, which is physics
        rather than policy). Offcuts thinner than the blade are kept; set
        `min_offcut` to discard slivers below some width instead.
        """
        pc = self.pieces[pid]
        remainder = (pc.w - size) if axis == "V" else (pc.h - size)
        if remainder <= EPS:
            return pid, None
        self.chain_cache = self.sched_cache = None
        setting = round(size, STOP_ROUND)
        self.stops[setting] = self.stops.get(setting, 0) + 1

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
        if off_size > max(EPS, self.min_offcut):
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

    def _fit_score(self, pc: Piece, w: float, h: float, heuristic: str,
                   vertical_first: bool = True):
        if heuristic == "BAF":
            return pc.area - w * h
        if heuristic == "BSSF":
            return min(pc.w - w, pc.h - h)
        if heuristic == "BLSF":
            return max(pc.w - w, pc.h - h)
        if heuristic == "STOP":
            # Reuse a setting the saw is already at, and prize a dimension that
            # needs no cut at all, before worrying about wasted area.
            credit = 0
            for extent, size in ((pc.w, w), (pc.h, h)):
                if extent - size <= EPS:
                    credit += 2                 # exact fit: no cut, no setting
                elif round(size, STOP_ROUND) in self.stops:
                    credit += 1
            return (-credit, pc.area - w * h)

        # CHOP: seat the part so its cuts land on the miter saw.
        #
        # Scored by what it actually costs rank 2 -- the number of *track* cuts
        # this placement adds, which is between zero and two and is entirely
        # decided here. Seating a part takes a cut to width and a cut to length;
        # either is skipped if the rectangle already fits that way, and either
        # goes to the chop saw if it does not have to reach further than the saw
        # does. How far it has to reach depends on which cut comes first and how
        # the part is turned, and nothing else in the packer has any reason to
        # care -- so on the sample kitchen 22 parts that could have been chopped
        # were being wrestled with the track saw instead.
        cap = self.miter_capacity + EPS
        if vertical_first:
            cuts = ((pc.w - w > EPS, pc.h),      # rip to width, spans the height
                    (pc.h - h > EPS, w))         # then crosscut, spans the width
        else:
            cuts = ((pc.h - h > EPS, pc.w),      # crosscut first, spans the width
                    (pc.w - w > EPS, h))         # then rip, spans the length
        cost = sum(1 for needed, span in cuts if needed and span > cap)
        return (cost, pc.area - w * h)

    def offer(self, part: PartSpec, heuristic: str, vertical_first: bool):
        """The best seat this sheet has for the part, or None. Changes nothing.

        Split out from `try_place` so a caller can ask every sheet what it would
        do before committing to one -- see `pack`'s `best_fit`.
        """
        options = [(part.w, part.h, False)]
        if not part.grain_locked and abs(part.w - part.h) > EPS:
            options.append((part.h, part.w, True))

        best = None
        for pid in self.free:
            pc = self.pieces[pid]
            for w, h, rotated in options:
                if w <= pc.w + EPS and h <= pc.h + EPS:
                    s = self._fit_score(pc, w, h, heuristic, vertical_first)
                    if best is None or s < best[0]:
                        best = (s, pid, w, h, rotated)
        return best

    def try_place(self, part: PartSpec, heuristic: str, vertical_first: bool) -> bool:
        best = self.offer(part, heuristic, vertical_first)
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
         heuristic: str, vertical_first: bool, min_offcut: float = 0.0,
         miter_capacity: float = DEFAULT_MITER,
         best_fit: bool = False) -> list[SheetLayout] | None:
    """Fit the parts across as many sheets as needed. None if a part cannot fit.

    Two ways to choose the sheet, and they are good at opposite things, so both
    are in the search's config space rather than one being picked as the winner:

    *First fit* takes the first sheet that will have the part. That fills sheets
    before opening the next, which is what drives the count down -- and the sheet
    count is priority one.

    *Best fit* asks every open sheet what it would do and takes the best seat
    going. That spreads the parts, which costs a sheet on a tight job, but on a
    job with a sheet to spare it seats more parts where the chop saw can reach
    them. The ranked score decides: the sheet ceiling is the first term, so a
    best-fit layout that needs an extra sheet is rejected before its cut counts
    are even looked at.
    """
    sheets: list[SheetLayout] = []
    for part in parts:
        seated = False
        if best_fit:
            pick = None
            for index, sheet in enumerate(sheets):
                got = sheet.offer(part, heuristic, vertical_first)
                if got is not None and (pick is None or got[0] < pick[0]):
                    pick = (got[0], index)
            seated = (pick is not None
                      and sheets[pick[1]].try_place(part, heuristic, vertical_first))
        else:
            seated = any(s.try_place(part, heuristic, vertical_first)
                         for s in sheets)
        if not seated:
            fresh = SheetLayout(width, length, kerf, min_offcut, miter_capacity)
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


def _families(parts: list[PartSpec]) -> dict[float, list[PartSpec]]:
    """Group parts by the dimension they share with the most other parts.

    Every cut offset is some part's dimension, so a sheet holding six different
    dimensions needs six stop settings. Parts that share one belong together:
    put them on the same sheet and those six settings become two.
    """
    popularity: dict[float, int] = {}
    for part in parts:
        for dim in {round(part.w, 1), round(part.h, 1)}:
            popularity[dim] = popularity.get(dim, 0) + 1
    families: dict[float, list[PartSpec]] = {}
    for part in parts:
        dims = {round(part.w, 1), round(part.h, 1)}
        home = max(dims, key=lambda d: (popularity[d], d))
        families.setdefault(home, []).append(part)
    return families


def _family_orders(parts: list[PartSpec]) -> list[list[PartSpec]]:
    """Orderings that keep each shared-dimension family together.

    First-fit then finishes one family before starting the next, so a sheet
    fills up with parts that measure the same rather than a jumble -- which is
    the difference between one long run at the saw and several short ones.
    """
    families = _families(parts)
    if len(families) < 2:
        return []
    orders = []
    for rank in (lambda item: -len(item[1]),         # biggest family first
                 lambda item: -item[0]):             # widest dimension first
        blocks = sorted(families.items(), key=rank)
        for inner in (lambda ps: sorted(ps, key=lambda p: -(p.w * p.h)),
                      lambda ps: sorted(ps, key=lambda p: (-max(p.w, p.h),
                                                           -min(p.w, p.h)))):
            orders.append([p for _, block in blocks for p in inner(block)])
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


def pack_one(parts: list[PartSpec], width: float, length: float, kerf: float,
             heuristic: str, vertical_first: bool,
             min_offcut: float = 0.0,
             miter_capacity: float = DEFAULT_MITER) -> SheetLayout | None:
    """Pack these parts onto exactly one sheet, or None if they do not all fit."""
    layout = SheetLayout(width, length, kerf, min_offcut, miter_capacity)
    for part in parts:
        if not layout.try_place(part, heuristic, vertical_first):
            return None
    return layout


def _buckets_from(sheets: list[SheetLayout],
                  by_id: dict[str, PartSpec]) -> list[list[PartSpec]]:
    """Recover per-sheet part lists so parts can be moved between sheets."""
    return [[by_id[pid] for pid, _, _ in sheet.placed] for sheet in sheets]


def dissolve(buckets: list[list[PartSpec]], victim: int, width: float,
             length: float, kerf: float, heuristic: str, vertical_first: bool,
             min_offcut: float = 0.0,
             miter_capacity: float = DEFAULT_MITER) -> list[list[PartSpec]] | None:
    """Empty one sheet onto the others. None if its parts will not all fit.

    This is the move nothing else can make. Reordering cannot carry a part to a
    different sheet, and the assignment moves shuffle parts between existing
    sheets without ever removing one, so a layout that has settled on N sheets
    stays on N. Dissolving is how it gets to N - 1.

    It packs for real rather than guessing: each orphan is offered to every
    surviving sheet's live free rectangles in turn. So a bucket list that comes
    back is one that is already known to fit -- and because each orphan is
    appended in the order it was placed, repacking those buckets reproduces
    exactly the layout that was just proved feasible.
    """
    if len(buckets) < 2 or not 0 <= victim < len(buckets):
        return None
    survivors = [b[:] for i, b in enumerate(buckets) if i != victim]
    layouts = []
    for bucket in survivors:
        layout = pack_one(bucket, width, length, kerf, heuristic,
                          vertical_first, min_offcut, miter_capacity)
        if layout is None:
            return None
        layouts.append(layout)

    for part in sorted(buckets[victim], key=lambda p: -(p.w * p.h)):
        for index, layout in enumerate(layouts):
            if layout.try_place(part, heuristic, vertical_first):
                survivors[index].append(part)
                break
        else:
            return None                 # this sheet cannot be dissolved
    return survivors


def _neighbour(buckets: list[list[PartSpec]], rng: random.Random,
               allow_new_sheet: bool) -> list[list[PartSpec]] | None:
    """One move in assignment space.

    Reordering alone can never carry a part to a different sheet, which is why
    a toe kick could end up marooned from the rest of its cabinet. These moves
    relocate real parts.
    """
    n = len(buckets)
    if n == 0:
        return None
    out = [b[:] for b in buckets]
    roll = rng.random()

    if roll < 0.30:                                   # move one part elsewhere
        i = rng.randrange(n)
        if not out[i]:
            return None
        targets = [k for k in range(n) if k != i]
        if allow_new_sheet and rng.random() < 0.15:
            out.append([])
            targets.append(len(out) - 1)
        if not targets:
            return None
        j = rng.choice(targets)
        part = out[i].pop(rng.randrange(len(out[i])))
        out[j].insert(rng.randrange(len(out[j]) + 1), part)

    elif roll < 0.50:                                 # swap parts across sheets
        if n < 2:
            return None
        i, j = rng.sample(range(n), 2)
        if not out[i] or not out[j]:
            return None
        a, b = rng.randrange(len(out[i])), rng.randrange(len(out[j]))
        out[i][a], out[j][b] = out[j][b], out[i][a]

    elif roll < 0.68:                                 # move a whole cabinet
        if n < 2:
            return None
        i, j = rng.sample(range(n), 2)
        if not out[i]:
            return None
        cabinet = rng.choice(out[i]).group_key
        moving = [p for p in out[i] if p.group_key == cabinet]
        if not moving or len(moving) == len(out[i]) and n == 2:
            return None
        out[i] = [p for p in out[i] if p.group_key != cabinet]
        out[j].extend(moving)

    elif roll < 0.86:                                 # reorder inside one sheet
        i = rng.randrange(n)
        if len(out[i]) < 2:
            return None
        out[i] = _perturb(out[i], rng)

    else:                                  # gather one dimension onto one sheet
        if n < 2:
            return None
        out = _gather(out, rng) or out

    return [b for b in out if b]


def _gather(buckets: list[list[PartSpec]],
            rng: random.Random) -> list[list[PartSpec]] | None:
    """Pull the parts that share one dimension onto the sheet holding most of them.

    Feasibility is left to the repack, as with the other assignment moves: what
    matters is that the move exists at all, because nothing else pushes a layout
    towards sheets whose parts all measure the same.
    """
    where: dict[float, set[int]] = {}
    for index, bucket in enumerate(buckets):
        for part in bucket:
            for dim in {round(part.w, 1), round(part.h, 1)}:
                where.setdefault(dim, set()).add(index)
    spread = [dim for dim, sheets in where.items() if len(sheets) > 1]
    if not spread:
        return None

    target = rng.choice(spread)
    matches = lambda p: target in (round(p.w, 1), round(p.h, 1))
    homes = where[target]
    home = max(homes, key=lambda i: sum(1 for p in buckets[i] if matches(p)))

    out = [b[:] for b in buckets]
    moved: list[PartSpec] = []
    for index in homes:
        if index == home:
            continue
        staying = [p for p in out[index] if not matches(p)]
        going = [p for p in out[index] if matches(p)]
        if going and staying:
            out[index] = staying
            moved.extend(going)
    if not moved:
        return None
    # Trade the same number of parts back, so the home sheet is not simply
    # asked to hold everything.
    rng.shuffle(moved)
    keep = moved[:max(1, len(moved) // 2)]
    for part in moved[len(keep):]:
        out[rng.randrange(len(out))].append(part)
    out[home].extend(keep)
    return out


def optimise_assign(buckets, width, length, kerf, heuristic, vertical_first,
                    min_offcut: float = 0.0,
                    miter_capacity: float = DEFAULT_MITER):
    """Realise an assignment, or None if any sheet's parts do not fit.

    The search itself goes through `optimise_iter`'s own depot, which keeps one
    packed sheet per distinct set of parts -- an assignment move changes one or
    two sheets, so repacking all of them was most of the cost of trying a move.
    This is the plain version, for a caller that just wants the layouts.
    """
    sheets = []
    for bucket in buckets:
        layout = pack_one(bucket, width, length, kerf, heuristic, vertical_first,
                          min_offcut, miter_capacity)
        if layout is None:
            return None
        sheets.append(layout)
    return sheets


def _perturb_family(order: list[PartSpec], rng: random.Random,
                    family_of: dict[str, float]) -> list[PartSpec]:
    """Perturb without breaking up the shared-dimension families.

    Element-level swaps scatter parts that measure the same across sheets, which
    is exactly what costs stop settings. So move whole families around, or
    reorder inside one, leaving each family contiguous. Same idea as keeping a
    cabinet together, applied to the thing the saw cares about.
    """
    blocks: list[list[PartSpec]] = []
    for part in order:
        key = family_of.get(part.id)
        if blocks and family_of.get(blocks[-1][0].id) == key:
            blocks[-1].append(part)
        else:
            blocks.append([part])

    roll = rng.random()
    if roll < 0.35 and len(blocks) > 1:                  # swap two families
        i, j = rng.randrange(len(blocks)), rng.randrange(len(blocks))
        blocks[i], blocks[j] = blocks[j], blocks[i]
    elif roll < 0.65 and len(blocks) > 1:                # move one family
        blocks.insert(rng.randrange(len(blocks)),
                      blocks.pop(rng.randrange(len(blocks))))
    else:                                                # reorder inside one
        inner = blocks[rng.randrange(len(blocks))]
        if len(inner) > 1:
            i, j = rng.randrange(len(inner)), rng.randrange(len(inner))
            inner[i], inner[j] = inner[j], inner[i]

    return [part for block in blocks for part in block]


def _perturb_align(order: list[PartSpec], rng: random.Random) -> list[PartSpec]:
    """Pull parts that share a dimension together in the ordering.

    Cut offsets come from part dimensions, so a run of parts that are all the
    same width produces a run of cuts at the same measurement -- which is one
    stop setting on the track saw instead of several. Generic swaps scatter
    those parts; this move gathers them.
    """
    out = order[:]
    if len(out) < 3:
        return out

    tally: dict[float, int] = {}
    for part in out:
        for dim in (round(part.w, 1), round(part.h, 1)):
            tally[dim] = tally.get(dim, 0) + 1
    shared = [d for d, n in tally.items() if n > 1]
    if not shared:
        return _perturb(out, rng)

    target = rng.choice(shared)
    matching = [p for p in out
                if round(p.w, 1) == target or round(p.h, 1) == target]
    rest = [p for p in out
            if not (round(p.w, 1) == target or round(p.h, 1) == target)]
    if not matching or not rest:
        return _perturb(out, rng)

    # Drop the whole family in as one block, somewhere in the order.
    at = rng.randrange(len(rest) + 1)
    return rest[:at] + matching + rest[at:]


def optimise_iter(parts: list[PartSpec], width: float, length: float, kerf: float,
                  time_budget: float = 5.0, seed: int = 12345,
                  heartbeat: float = 0.2, mode: str = MODE_MATERIAL,
                  warm_start: list[PartSpec] | None = None,
                  cabinets: dict[str, str] | None = None,
                  scorer=None, group_aware: bool | None = None,
                  min_offcut: float = 0.0, align_offsets: bool = False,
                  heuristics: tuple[str, ...] | None = None,
                  ordering_only: bool = False,
                  miter_capacity: float = DEFAULT_MITER,
                  allow_best_fit: bool = False):
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
    rate = scorer if scorer is not None else (lambda sh: score(sh, mode, cabinets))
    by_id = {p.id: p for p in parts}
    family_of = {part.id: dim
                 for dim, block in _families(parts).items() for part in block}
    best: list[SheetLayout] | None = None
    best_score: tuple | None = None
    best_order: list[PartSpec] | None = None
    choices = heuristics or HEURISTICS
    best_config: tuple[str, bool, bool] = (choices[0], True, False)
    attempts = 0

    def consider(order, heuristic, vertical_first, best_fit=False) -> bool:
        nonlocal best, best_score, best_order, best_config, attempts
        attempts += 1
        result = pack(order, width, length, kerf, heuristic, vertical_first,
                      min_offcut, miter_capacity, best_fit)
        if result is None:
            return False
        s = rate(result)
        if best_score is None or s < best_score:
            best, best_score = result, s
            best_order = order[:]
            best_config = (heuristic, vertical_first, best_fit)
            return True
        return False

    # Cabinet-grouped seeds are cheap, so include them whenever cabinets are
    # known; whether to *perturb* group-wise follows how the user ranked things.
    grouped = group_aware if group_aware is not None else (mode == MODE_CABINETS)
    seeds = _family_orders(parts) + _seed_orders(parts)
    if cabinets or mode == MODE_CABINETS:
        seeds = _grouped_orders(parts) + seeds
    if warm_start:
        seeds.insert(0, list(warm_start))   # continue from a previous run's best
    # Best fit is only worth a look when there is a sheet to spare: it seats
    # more parts where the chop saw can reach them, at the price of spreading
    # them over one more sheet. With the ceiling already at the floor it cannot
    # win, and offering it doubles the configurations the search has to sift.
    fits = (False, True) if allow_best_fit else (False,)
    for order in seeds:
        for heuristic in choices:
            for vertical_first in (True, False):
                for best_fit in fits:
                    if consider(order, heuristic, vertical_first, best_fit):
                        yield SearchState(best, best_score, attempts, True,
                                          best_order)

    if best is None or best_order is None:
        return

    def consider_sheets(sheets, order_hint, config):
        """Score a layout produced in assignment space."""
        nonlocal best, best_score, best_order, best_config, attempts
        attempts += 1
        if sheets is None:
            return False
        s = rate(sheets)
        if best_score is None or s < best_score:
            best, best_score = sheets, s
            best_order, best_config = list(order_hint), config
            return True
        return False

    # One packed sheet per distinct set of parts. Assignment moves change one or
    # two sheets and leave the rest alone, so repacking every sheet each time was
    # most of the cost of trying a move -- and the scheduler's own per-sheet
    # working is cached on the layout object, so a reused sheet is free to score
    # as well. Nothing mutates a layout once packed, so sharing is safe.
    depot: dict[tuple, SheetLayout] = {}

    def realise(assignment, heuristic, vertical_first, best_fit=False):
        sheets = []
        for bucket in assignment:
            key = (heuristic, vertical_first, tuple(p.id for p in bucket))
            layout = depot.get(key)
            if layout is None:
                layout = pack_one(bucket, width, length, kerf, heuristic,
                                  vertical_first, min_offcut, miter_capacity)
                if layout is None:
                    return None
                if len(depot) > 8000:
                    depot.clear()
                depot[key] = layout
            sheets.append(layout)
        return sheets

    deadline = time.perf_counter() + time_budget
    last_beat = time.perf_counter()
    current = best_order[:]
    buckets = _buckets_from(best, by_id)
    stale = 0
    # Assignment moves shuffle parts between existing sheets but rarely remove
    # one; only a global repack reliably collapses the sheet count. So while we
    # are still above the area floor, lean on ordering moves.
    sheet_floor = max(1, ceil(sum(p.w * p.h for p in parts) / (width * length) - 1e-9))

    while time.perf_counter() < deadline:
        if rng.random() < 0.8:
            heuristic, vertical_first, best_fit = best_config
        else:
            heuristic = rng.choice(choices)
            vertical_first = rng.random() < 0.5
            best_fit = allow_best_fit and rng.random() < 0.3

        # Alternate between reshuffling the global ordering (which can collapse
        # sheet count) and moving individual parts between sheets (which is the
        # only way to bring a stray part back beside the rest of its cabinet).
        # Reordering the whole list and repacking is a different kind of move
        # from shuffling parts between the sheets that already exist, and they
        # are not interchangeable. The ordering moves are what shape a layout --
        # they decide the columns, and so the cut structure. Offered the other
        # kinds as well, a search told to find a well-shaped layout spends most
        # of its budget nudging individual parts and stops a sheet short of the
        # one the ordering moves would have found. `ordering_only` is for the
        # pass whose whole job is the shape.
        assign_share = (0.0 if ordering_only
                        else 0.55 if len(best) <= sheet_floor else 0.3)
        # Try to shed a sheet outright now and then, and often while the layout
        # is still above the area floor. Nothing else can remove a sheet: the
        # other moves shuffle parts between the sheets that already exist.
        dissolve_share = (0.0 if ordering_only
                          else 0.35 if len(best) > sheet_floor else 0.08)
        if len(best) > 1 and rng.random() < dissolve_share:
            victim = (min(range(len(buckets)), key=lambda k: len(buckets[k]))
                      if rng.random() < 0.5 else rng.randrange(len(buckets)))
            candidate = dissolve(buckets, victim, width, length, kerf,
                                 heuristic, vertical_first, min_offcut,
                                 miter_capacity)
            if candidate is None:
                stale += 1
                continue
            sheets = realise(candidate, heuristic, vertical_first)
            improved = consider_sheets(sheets, [p for b in candidate for p in b],
                                       (heuristic, vertical_first, False))
            if improved:
                buckets = candidate
        elif rng.random() < assign_share:
            candidate = _neighbour(buckets, rng, allow_new_sheet=True)
            if candidate is None:
                stale += 1
                continue
            sheets = realise(candidate, heuristic, vertical_first)
            improved = consider_sheets(sheets, [p for b in candidate for p in b],
                                       (heuristic, vertical_first, False))
            if improved:
                buckets = candidate
        else:
            if grouped:
                candidate = _perturb_grouped(current, rng)
            elif align_offsets and rng.random() < 0.7:
                # Bias towards orderings that reuse cut measurements: mostly by
                # shuffling whole shared-dimension families, occasionally by
                # gathering one family that has drifted apart.
                candidate = (_perturb_family(current, rng, family_of)
                             if rng.random() < 0.7
                             else _perturb_align(current, rng))
            else:
                candidate = _perturb(current, rng)
            improved = consider(candidate, heuristic, vertical_first, best_fit)
            if improved:
                current = candidate
                buckets = _buckets_from(best, by_id)

        if improved:
            stale = 0
            last_beat = time.perf_counter()
            yield SearchState(best, best_score, attempts, True, best_order)
        else:
            stale += 1
            if stale > 400:          # kick back to the best known solution
                current = best_order[:]
                buckets = _buckets_from(best, by_id)
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
