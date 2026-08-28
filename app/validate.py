"""Invariant checks on a solved layout.

These run after every solve, not just in tests. A layout that violates any of
them would send someone to the saw with instructions that cannot be followed.
"""
from __future__ import annotations

from .nesting import SheetLayout

TOL = 1e-4


def validate_sheet(sheet: SheetLayout, kerf: float) -> list[str]:
    errors: list[str] = []

    placed_rects = []
    for part_id, piece_id, _ in sheet.placed:
        p = sheet.pieces[piece_id]
        placed_rects.append((part_id, p))
        if p.x < -TOL or p.y < -TOL or p.x + p.w > sheet.width + TOL \
                or p.y + p.h > sheet.length + TOL:
            errors.append(f"{part_id} lies outside the sheet")

    for i in range(len(placed_rects)):
        for j in range(i + 1, len(placed_rects)):
            a_id, a = placed_rects[i]
            b_id, b = placed_rects[j]
            if (a.x < b.x + b.w - TOL and b.x < a.x + a.w - TOL
                    and a.y < b.y + b.h - TOL and b.y < a.y + a.h - TOL):
                errors.append(f"{a_id} overlaps {b_id}")

    seen = [pid for pid, _, _ in sheet.placed]
    if len(seen) != len(set(seen)):
        errors.append("a part was placed more than once")

    # Guillotine invariant: every cut spans its whole piece, and the children
    # plus one kerf reconstruct the parent exactly.
    for piece in sheet.pieces.values():
        if piece.cut is None:
            continue
        kids = [sheet.pieces[c] for c in piece.children]
        if piece.cut.axis == "V":
            if not (piece.x - TOL < piece.cut.pos < piece.x + piece.w + TOL):
                errors.append(f"cut on piece {piece.id} falls outside it")
            for k in kids:
                if abs(k.h - piece.h) > TOL or abs(k.y - piece.y) > TOL:
                    errors.append(f"cut on piece {piece.id} is not edge-to-edge")
            spanned = sum(k.w for k in kids) + (kerf if len(kids) == 2 else 0)
            if spanned > piece.w + TOL:
                errors.append(f"children of piece {piece.id} exceed it after kerf")
        else:
            if not (piece.y - TOL < piece.cut.pos < piece.y + piece.h + TOL):
                errors.append(f"cut on piece {piece.id} falls outside it")
            for k in kids:
                if abs(k.w - piece.w) > TOL or abs(k.x - piece.x) > TOL:
                    errors.append(f"cut on piece {piece.id} is not edge-to-edge")
            spanned = sum(k.h for k in kids) + (kerf if len(kids) == 2 else 0)
            if spanned > piece.h + TOL:
                errors.append(f"children of piece {piece.id} exceed it after kerf")

    return errors


def validate_all(sheets: list[SheetLayout], kerf: float,
                 expected_part_ids: set[str]) -> list[str]:
    errors: list[str] = []
    for i, s in enumerate(sheets, start=1):
        errors.extend(f"sheet {i}: {e}" for e in validate_sheet(s, kerf))

    placed = {pid for s in sheets for pid, _, _ in s.placed}
    missing = expected_part_ids - placed
    extra = placed - expected_part_ids
    if missing:
        errors.append(f"{len(missing)} part(s) never placed: {sorted(missing)[:5]}")
    if extra:
        errors.append(f"unexpected part(s) placed: {sorted(extra)[:5]}")
    return errors
