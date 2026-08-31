"""Turn a guillotine tree into an ordered, measurable cut list.

Breadth-first from the root guarantees a parent cut always precedes its
children, which is exactly the physical requirement -- you cannot cut a piece
into halves before you have separated it from the sheet. BFS also puts the
full-sheet rips first, which is how you would break a sheet down anyway.
"""
from __future__ import annotations

from collections import deque

from .models import Cut
from .nesting import SheetLayout, separating_pieces
from .objective import cut_offset, cut_saws, schedule_cuts
from .units import format_length


def _piece_name(n: int) -> str:
    """A, B, ... Z, AA, AB, ..."""
    name = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        name = chr(ord("A") + r) + name
    return name


def _cabinet_first_order(sheet: SheetLayout, cabinets: dict[str, str]) -> list[int]:
    """Cut order that finishes one cabinet before starting the next.

    For each cabinet in turn, emit every not-yet-made cut above its parts
    (the cuts that isolate it), then every cut inside its own regions. Because
    each cabinet's ancestors are emitted shallowest-first and shared ancestors
    are already done, parents always precede children -- so this is a valid
    cutting sequence, just organised around the shop workflow rather than
    sweeping the sheet level by level.
    """
    parent: dict[int, int] = {}
    depth: dict[int, int] = {0: 0}
    stack = [0]
    while stack:
        pid = stack.pop()
        for child in sheet.pieces[pid].children:
            parent[child] = pid
            depth[child] = depth[pid] + 1
            stack.append(child)

    by_cabinet: dict[str, list[int]] = {}
    for pid, piece in sheet.pieces.items():
        if piece.part_id is not None:
            by_cabinet.setdefault(cabinets.get(piece.part_id, ""), []).append(pid)

    emitted: set[int] = set()
    sequence: list[int] = []
    for cabinet in sorted(by_cabinet, key=lambda c: -len(by_cabinet[c])):
        needed: set[int] = set()
        for leaf in by_cabinet[cabinet]:
            node = leaf
            while node in parent:
                node = parent[node]
                needed.add(node)
            needed.add(leaf)
        for pid in sorted(needed, key=lambda x: depth[x]):
            if pid not in emitted and sheet.pieces[pid].cut is not None:
                emitted.add(pid)
                sequence.append(pid)

    # Anything left over lives under offcut-only branches; append shallowest first.
    for pid in sorted(sheet.pieces, key=lambda x: depth.get(x, 0)):
        if sheet.pieces[pid].cut is not None and pid not in emitted:
            emitted.add(pid)
            sequence.append(pid)
    return sequence


def order_cuts(sheet: SheetLayout, sheet_index: int, part_labels: dict[str, str],
               units: str = "in",
               cabinets: dict[str, str] | None = None,
               miter_capacity: float = 304.8,
               prefer: str = "stops",
               start_width: float | None = None) -> list[Cut]:
    """Emit one sheet's cuts in a valid, workable order.

    Two orderings are useful and they pull against each other, so the caller
    picks by what is ranked higher:

    "stops"    keep the track saw's stop still as long as possible, so several
               cuts of one width happen together and nothing is re-measured.
    "cabinets" make the cuts that isolate a cabinet first, so parts can be
               sorted into piles early.

    Either way a piece is always cut before its children, so the sequence is
    physically valid.
    """
    isolating = separating_pieces(sheet, cabinets) if cabinets else set()
    saws = cut_saws(sheet, miter_capacity)

    if prefer == "cabinets" and cabinets:
        sequence = _cabinet_first_order(sheet, cabinets)
    else:
        sequence = schedule_cuts(sheet, miter_capacity, start_width)

    counter = 0
    sheet.pieces[0].label = _piece_name(counter)
    counter += 1

    cuts: list[Cut] = []
    group = 0
    width_now = start_width

    for pid in sequence:
        piece = sheet.pieces[pid]
        if piece.cut is None:
            continue

        for child_id in piece.children:
            child = sheet.pieces[child_id]
            if child.part_id is not None:
                child.label = part_labels.get(child.part_id, child.part_id)
            elif child.children or child.cut is not None:
                child.label = _piece_name(counter)
                counter += 1
            else:
                child.label = "offcut"

        axis = piece.cut.axis
        if axis == "V":
            offset = piece.cut.pos - piece.x
            x1 = x2 = piece.cut.pos
            y1, y2 = piece.y, piece.y + piece.h
            from_edge = "left"
            verb = "Rip"
        else:
            offset = piece.cut.pos - piece.y
            y1 = y2 = piece.cut.pos
            x1, x2 = piece.x, piece.x + piece.w
            from_edge = "bottom"
            verb = "Crosscut"

        # Track cuts at one stop setting form a run; a miter cut does not
        # disturb the stop, so it stays in whatever run surrounds it.
        saw = saws.get(pid, "track")
        if saw == "track":
            rounded = round(offset, 1)
            if width_now is None or rounded != width_now:
                group += 1
                width_now = rounded

        produced_list = [sheet.pieces[c].label for c in piece.children]
        produced = " + ".join(produced_list)
        note = (f"{verb} piece {piece.label} "
                f"({format_length(piece.w, units)} x {format_length(piece.h, units)}) "
                f"at {format_length(offset, units)} from the {from_edge} edge "
                f"-> {produced}")

        cuts.append(Cut(
            index=len(cuts) + 1,
            sheet_index=sheet_index,
            axis=axis,
            piece_label=piece.label,
            x1=x1, y1=y1, x2=x2, y2=y2,
            offset_mm=offset,
            from_edge=from_edge,
            piece_w=piece.w,
            piece_h=piece.h,
            produces=produced_list,
            separates=pid in isolating,
            saw=saw,
            stop_group=group if saw == "track" else 0,
            note=note,
        ))
    return cuts
