"""Rip-first layouts: every cut in stage one runs the full length of the sheet.

Home Depot's panel saw will do long rips for free but will not crosscut small
pieces, so the useful layout is one you can hand over as "three strips of 4
inches, then four of 15" -- all of their work in one direction, all of yours on
the miter saw afterwards.

That reduces the 2D packing problem to two 1D bin packings, which is both easier
and exactly the structure we want:

  1. parts of equal width are laid end to end into full-length strips
  2. strips are laid side by side across the sheet

Every strip width equals a real part dimension, so no width is ever wasted --
only the tail of a strip and the last strip on a sheet. Stage depth is 2 by
construction, and the number of distinct rip widths is what the caller minimises.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from .nesting import EPS, PartSpec, SheetLayout

ROUND = 1          # mm; dimensions this close are the same width to a saw


def _key(value: float) -> float:
    return round(value, ROUND)


@dataclass
class Strip:
    """A full-length rip of one width, holding parts end to end."""
    width: float
    parts: list[tuple[PartSpec, float, bool]] = field(default_factory=list)
    used: float = 0.0          # length consumed, including crosscut kerfs

    def fits(self, length: float, capacity: float, kerf: float) -> bool:
        extra = length + (kerf if self.parts else 0.0)
        return self.used + extra <= capacity + EPS

    def add(self, part: PartSpec, length: float, rotated: bool, kerf: float) -> None:
        if self.parts:
            self.used += kerf
        self.parts.append((part, length, rotated))
        self.used += length


def candidate_widths(part: PartSpec) -> list[float]:
    """Which of a part's dimensions may become the strip width."""
    if part.grain_locked or abs(part.w - part.h) <= EPS:
        return [part.w]
    return [part.w, part.h]


def default_choices(parts: list[PartSpec],
                    miter_capacity: float | None = None) -> dict[str, float]:
    """Pick each part's strip width, favouring widths many parts share.

    Sharing is the whole point: a width used by eight parts becomes one
    instruction at the counter, while eight one-off widths become eight.
    """
    popularity: Counter[float] = Counter()
    for part in parts:
        for width in candidate_widths(part):
            popularity[_key(width)] += 1

    choices: dict[str, float] = {}
    for part in parts:
        options = candidate_widths(part)
        # A strip the miter saw can cross is worth more than a popular width:
        # it turns every crosscut in that strip into an easy chop at home.
        def rank(w: float) -> tuple:
            choppable = miter_capacity is not None and w <= miter_capacity + EPS
            return (0 if choppable else 1, -popularity[_key(w)], w)

        choices[part.id] = min(options, key=rank)
    return choices


def _other_dimension(part: PartSpec, width: float) -> tuple[float, bool]:
    """Length along the strip, and whether the part had to turn to get there."""
    if abs(part.w - width) <= EPS:
        return part.h, False
    return part.w, True


def plan(parts: list[PartSpec], sheet_w: float, sheet_l: float, kerf: float,
         choices: dict[str, float] | None = None,
         miter_capacity: float | None = None) -> list[list[Strip]] | None:
    """Group parts into strips, then strips into sheets. None if something cannot fit."""
    choices = choices or default_choices(parts, miter_capacity)

    by_width: dict[float, list[tuple[PartSpec, float, bool]]] = {}
    for part in parts:
        width = choices.get(part.id, candidate_widths(part)[0])
        length, rotated = _other_dimension(part, width)
        if width > sheet_w + EPS or length > sheet_l + EPS:
            # The chosen orientation does not fit; try turning the part.
            alternatives = [w for w in candidate_widths(part) if abs(w - width) > EPS]
            if not alternatives:
                return None
            width = alternatives[0]
            length, rotated = _other_dimension(part, width)
            if width > sheet_w + EPS or length > sheet_l + EPS:
                return None
        by_width.setdefault(_key(width), []).append((part, length, rotated))

    # 1D pack: parts end to end into full-length strips of their own width.
    strips: list[Strip] = []
    for width, members in by_width.items():
        members.sort(key=lambda m: -m[1])
        open_strips: list[Strip] = []
        for part, length, rotated in members:
            for strip in open_strips:
                if strip.fits(length, sheet_l, kerf):
                    strip.add(part, length, rotated, kerf)
                    break
            else:
                fresh = Strip(width=width)
                fresh.add(part, length, rotated, kerf)
                open_strips.append(fresh)
        strips.extend(open_strips)

    # 1D pack again: strips side by side across the sheet. Widest first, which
    # also lands equal widths next to each other for free.
    strips.sort(key=lambda s: (-s.width, -s.used))
    sheets: list[list[Strip]] = []
    for strip in strips:
        for sheet in sheets:
            span = sum(s.width for s in sheet) + kerf * len(sheet) + strip.width
            if span <= sheet_w + EPS:
                sheet.append(strip)
                break
        else:
            if strip.width > sheet_w + EPS:
                return None
            sheets.append([strip])

    # Same widths consecutive on every sheet, so the instruction stays short.
    for sheet in sheets:
        sheet.sort(key=lambda s: -s.width)
    return sheets


def build(sheets: list[list[Strip]], sheet_w: float, sheet_l: float,
          kerf: float, min_offcut: float = 0.0,
          miter_capacity: float | None = None) -> list[SheetLayout]:
    """Turn a strip plan into real guillotine layouts.

    Cuts are driven directly rather than going through the free-rectangle
    packer, because the whole point is to dictate the tree shape: one run of
    rips at the root, one run of crosscuts inside each strip.
    """
    layouts: list[SheetLayout] = []
    for plan_sheet in sheets:
        layout = SheetLayout(sheet_w, sheet_l, kerf, min_offcut,
                             miter_capacity if miter_capacity else 304.8)
        remaining = 0                      # piece still to be ripped
        layout.free.clear()

        for strip in plan_sheet:
            # Always rip to the strip's own width. Skipping this for the final
            # strip would leave its parts as wide as whatever was left of the
            # sheet -- they would come off the saw oversize.
            strip_piece, remaining = layout._cut(remaining, "V", strip.width)

            cursor = strip_piece
            for part, length, rotated in strip.parts:
                kept, rest = layout._cut(cursor, "H", length)
                layout.pieces[kept].part_id = part.id
                layout.placed.append((part.id, kept, rotated))
                if rest is None:
                    cursor = None
                    break
                cursor = rest

            # Whatever is left at the end of the strip is a usable offcut.
            if cursor is not None:
                tail = layout.pieces[cursor]
                if tail.part_id is None and not tail.children:
                    layout.free.append(cursor)

            if remaining is None:          # nothing usable left to rip from
                break

        if remaining is not None and remaining not in layout.free:
            piece = layout.pieces.get(remaining)
            if piece is not None and piece.part_id is None and not piece.children:
                layout.free.append(remaining)
        layouts.append(layout)
    return layouts


def pack_staged(parts: list[PartSpec], sheet_w: float, sheet_l: float, kerf: float,
                choices: dict[str, float] | None = None,
                min_offcut: float = 0.0,
                miter_capacity: float | None = None) -> list[SheetLayout] | None:
    """Full rip-first layout, or None if the parts cannot be laid out this way."""
    plan_sheets = plan(parts, sheet_w, sheet_l, kerf, choices, miter_capacity)
    if plan_sheets is None:
        return None
    return build(plan_sheets, sheet_w, sheet_l, kerf, min_offcut, miter_capacity)


def search_staged_iter(parts: list[PartSpec], sheet_w: float, sheet_l: float,
                       kerf: float, scorer, time_budget: float = 2.0,
                       seed: int = 12345, min_offcut: float = 0.0,
                       heartbeat: float = 0.1, miter_capacity: float | None = None):
    """Hunt for the best rip-first layout, reporting progress as it goes.

    The decision variable here is not a part ordering -- it is which of a part's
    two dimensions becomes the strip width. Flipping one part can merge two
    near-identical widths into one instruction, so that is what gets perturbed.

    Yields (layouts, score, improved) so the caller can keep the UI alive; a
    long search that emitted nothing would freeze the animation.
    """
    import random
    import time as _time

    choices = default_choices(parts, miter_capacity)
    flexible = [p for p in parts if len(candidate_widths(p)) > 1]

    def evaluate(candidate):
        layouts = pack_staged(parts, sheet_w, sheet_l, kerf, candidate, min_offcut,
                              miter_capacity)
        if layouts is None:
            return None, None
        return layouts, scorer(layouts)

    best_layouts, best_score = evaluate(choices)
    if best_layouts is None:
        return
    yield best_layouts, best_score, True
    if not flexible:
        return

    rng = random.Random(seed)
    current, current_score = dict(choices), best_score
    now = _time.perf_counter()
    deadline = now + time_budget
    last_beat = now

    while _time.perf_counter() < deadline:
        candidate = dict(current)
        for _ in range(rng.randint(1, 3)):
            part = rng.choice(flexible)
            options = candidate_widths(part)
            candidate[part.id] = (options[1]
                                  if abs(candidate[part.id] - options[0]) <= EPS
                                  else options[0])
        layouts, value = evaluate(candidate)
        if layouts is None:
            continue
        if value < current_score:
            current, current_score = candidate, value
            if value < best_score:
                best_layouts, best_score = layouts, value
                last_beat = _time.perf_counter()
                yield best_layouts, best_score, True
                continue
        now = _time.perf_counter()
        if now - last_beat >= heartbeat:
            last_beat = now
            yield best_layouts, best_score, False


def search_staged(parts: list[PartSpec], sheet_w: float, sheet_l: float, kerf: float,
                  scorer, time_budget: float = 2.0, seed: int = 12345,
                  min_offcut: float = 0.0):
    """Best rip-first layout found within the budget."""
    best = (None, None)
    for layouts, value, improved in search_staged_iter(
            parts, sheet_w, sheet_l, kerf, scorer, time_budget, seed, min_offcut):
        if improved:
            best = (layouts, value)
    return best
