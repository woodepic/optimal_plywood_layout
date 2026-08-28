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
from .units import format_length


def _piece_name(n: int) -> str:
    """A, B, ... Z, AA, AB, ..."""
    name = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        name = chr(ord("A") + r) + name
    return name


def order_cuts(sheet: SheetLayout, sheet_index: int, part_labels: dict[str, str],
               units: str = "in",
               cabinets: dict[str, str] | None = None) -> list[Cut]:
    """Walk the split tree breadth-first and emit the cut list for one sheet."""
    # Cuts that still have more than one cabinet below them: make these and the
    # remaining pieces can be sorted into per-cabinet piles.
    isolating = separating_pieces(sheet, cabinets) if cabinets else set()
    counter = 0
    sheet.pieces[0].label = _piece_name(counter)
    counter += 1

    cuts: list[Cut] = []
    queue: deque[int] = deque([0])
    while queue:
        pid = queue.popleft()
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
            queue.append(child_id)

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
            note=note,
        ))
    return cuts
