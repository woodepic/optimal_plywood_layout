"""Guillotine invariants and the layout validator."""
import pytest

from app.nesting import PartSpec, SheetLayout, optimise, pack
from app.validate import validate_all, validate_sheet

W, L, KERF = 1219.2, 2438.4, 2.2


def specs(n, w=300.0, h=400.0):
    return [PartSpec(f"p{i}", f"P{i}", w, h) for i in range(n)]


def test_pack_places_every_part_and_validates():
    parts = specs(12)
    sheets = pack(parts, W, L, KERF, "BAF", True)
    assert sheets is not None
    assert validate_all(sheets, KERF, {p.id for p in parts}) == []
    placed = sum(len(s.placed) for s in sheets)
    assert placed == len(parts)


def test_part_larger_than_sheet_is_unpackable():
    assert pack([PartSpec("x", "X", 5000.0, 5000.0)], W, L, KERF, "BAF", True) is None


def test_grain_lock_prevents_rotation():
    part = PartSpec("a", "A", 200.0, 900.0, grain_locked=True)
    s = SheetLayout(W, L, KERF)
    assert s.try_place(part, "BAF", True)
    _, pid, rotated = s.placed[0]
    assert rotated is False
    assert s.pieces[pid].w == 200.0 and s.pieces[pid].h == 900.0


def test_rotation_used_when_it_helps():
    # 900 wide will not fit across a 500-wide sheet unless it turns.
    part = PartSpec("a", "A", 900.0, 200.0)
    s = SheetLayout(500.0, 2000.0, KERF)
    assert s.try_place(part, "BAF", True)
    assert s.placed[0][2] is True


def test_validator_catches_overlap():
    s = SheetLayout(W, L, KERF)
    a = PartSpec("a", "A", 300.0, 300.0)
    b = PartSpec("b", "B", 300.0, 300.0)
    s.try_place(a, "BAF", True)
    s.try_place(b, "BAF", True)
    # Force B on top of A and confirm the check fires.
    _, pid_b, _ = s.placed[1]
    s.pieces[pid_b].x = s.pieces[s.placed[0][1]].x
    s.pieces[pid_b].y = s.pieces[s.placed[0][1]].y
    assert any("overlaps" in e for e in validate_sheet(s, KERF))


def test_validator_catches_missing_part():
    parts = specs(3)
    sheets = pack(parts, W, L, KERF, "BAF", True)
    errors = validate_all(sheets, KERF, {"p0", "p1", "p2", "p3"})
    assert any("never placed" in e for e in errors)


def test_every_cut_is_edge_to_edge():
    parts = specs(20, 280.0, 380.0)
    sheets = pack(parts, W, L, KERF, "BSSF", False)
    for sheet in sheets:
        for piece in sheet.pieces.values():
            if piece.cut is None:
                continue
            for cid in piece.children:
                kid = sheet.pieces[cid]
                if piece.cut.axis == "V":
                    assert kid.h == pytest.approx(piece.h)
                else:
                    assert kid.w == pytest.approx(piece.w)


def test_optimise_beats_or_matches_a_single_pass():
    parts = specs(18, 380.0, 520.0)
    single = pack(parts, W, L, KERF, "BAF", True)
    best, improvements = optimise(parts, W, L, KERF, time_budget=1.0)
    assert improvements >= 1
    assert len(best) <= len(single)
    assert validate_all(best, KERF, {p.id for p in parts}) == []


def test_optimise_is_deterministic_for_a_given_seed():
    parts = specs(14, 330.0, 470.0)
    a, _ = optimise(parts, W, L, KERF, time_budget=0.4, seed=7)
    b, _ = optimise(parts, W, L, KERF, time_budget=0.4, seed=7)
    assert len(a) == len(b)
