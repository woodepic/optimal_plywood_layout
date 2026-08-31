"""Reorder equal-width sections so they sit next to each other.

Within a run of same-axis cuts every cut spans its piece completely, so the
sections are parallel bands and may be permuted freely: the same parts, the same
sheet, the same number of cuts. What changes is the order you meet them in.

That matters because only *adjacent* equal widths save anything. Two columns of
one width either side of a third cannot be cut one after the other -- the middle
column's rip has to happen in between, at another measurement, so the stop moves
twice. Sorting the run by width turns those two stop changes into none.
"""
from __future__ import annotations

from .nesting import SheetLayout

ROUND = 1


def _extent(sheet: SheetLayout, pid: int, axis: str) -> float:
    piece = sheet.pieces[pid]
    return piece.w if axis == "V" else piece.h


def _origin(sheet: SheetLayout, pid: int, axis: str) -> float:
    piece = sheet.pieces[pid]
    return piece.x if axis == "V" else piece.y


def _shift(sheet: SheetLayout, pid: int, axis: str, delta: float) -> None:
    """Slide a whole subtree along one axis, cut positions included."""
    if not delta:
        return
    stack = [pid]
    while stack:
        piece = sheet.pieces[stack.pop()]
        if axis == "V":
            piece.x += delta
        else:
            piece.y += delta
        if piece.cut is not None:
            if piece.cut.axis == axis:
                piece.cut.pos += delta
            stack.extend(piece.children)
        else:
            stack.extend(piece.children)


def _chain(sheet: SheetLayout, root: int) -> tuple[list[int], list[int]]:
    """Walk a run of same-axis cuts.

    Returns (chain nodes, sections). Each chain node cuts one section off the
    front; whatever is left at the end is a section too.
    """
    axis = sheet.pieces[root].cut.axis
    nodes: list[int] = []
    sections: list[int] = []
    node = root
    while True:
        piece = sheet.pieces[node]
        if piece.cut is None or piece.cut.axis != axis or not piece.children:
            sections.append(node)
            break
        nodes.append(node)
        kept = piece.children[0]
        sections.append(kept)
        if len(piece.children) < 2:
            break                       # remainder was consumed by the blade
        node = piece.children[1]
    return nodes, sections


def regroup_sheet(sheet: SheetLayout) -> bool:
    """Group equal-width sections together in every same-axis run. In place."""
    changed = False

    for root in sorted(sheet.pieces):
        piece = sheet.pieces.get(root)
        if piece is None or piece.cut is None:
            continue
        parent = piece.parent
        if (parent is not None and sheet.pieces[parent].cut is not None
                and sheet.pieces[parent].cut.axis == piece.cut.axis):
            continue                    # not the head of its run

        axis = piece.cut.axis
        nodes, sections = _chain(sheet, root)
        if len(sections) < 3:
            continue                    # nothing to reorder

        head, tail = sections[:-1], sections[-1]
        sizes = {s: _extent(sheet, s, axis) for s in sections}
        before = [round(sizes[s], ROUND) for s in head]
        # Widest first, so equal widths become one block. Ties keep their
        # original relative order, which keeps the change minimal.
        order = sorted(head, key=lambda s: -round(sizes[s], ROUND))
        after = [round(sizes[s], ROUND) for s in order]
        if before == after:
            continue

        start = _origin(sheet, root, axis)
        span = _extent(sheet, root, axis)
        cursor = start
        for node, section in zip(nodes, order):
            _shift(sheet, section, axis, cursor - _origin(sheet, section, axis))
            # A chain node covers everything not yet cut off, so both its origin
            # and its extent move -- setting only the origin left it claiming
            # more material than remained, which the kerf check caught.
            block = sheet.pieces[node]
            if axis == "V":
                block.x = cursor
                block.w = start + span - cursor
            else:
                block.y = cursor
                block.h = start + span - cursor
            block.cut.pos = cursor + sizes[section]
            block.children[0] = section
            sheet.pieces[section].parent = node
            cursor += sizes[section] + sheet.kerf

        _shift(sheet, tail, axis, cursor - _origin(sheet, tail, axis))
        if nodes:
            last = sheet.pieces[nodes[-1]]
            if len(last.children) > 1:
                last.children[1] = tail
                sheet.pieces[tail].parent = nodes[-1]
        changed = True

    return changed


def regroup(layouts: list[SheetLayout]) -> int:
    """Regroup every sheet; returns how many were changed."""
    return sum(1 for sheet in layouts if regroup_sheet(sheet))
