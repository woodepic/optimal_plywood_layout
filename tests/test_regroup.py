"""Reordering equal-width sections must be free: same parts, same sheet, same cuts."""
import pytest
from conftest import find_step

from app.models import LayoutParams
from app.nesting import PartSpec, optimise, pack
from app.objective import cut_offset, cut_saws, stop_changes, stop_runs
from app.regroup import regroup, regroup_sheet
from app.solver import build_groups, cabinet_of, solve
from app.step_parser import parse_step
from app.validate import validate_all

V3 = find_step("V3") or find_step("Kitchen")
W, L, KERF = 1219.2, 2438.4, 2.2


def scattered():
    """Widths deliberately interleaved: 200, 400, 200, 400 across the sheet."""
    parts = []
    for i, width in enumerate([200.0, 400.0, 200.0, 400.0]):
        parts.append(PartSpec(f"p{i}", f"P{i}", width, 2400.0, grain_locked=True))
    return parts


def test_a_run_is_reordered_so_equal_widths_touch():
    parts = scattered()
    sheets = pack(parts, W, L, KERF, "BAF", True)
    sheet = sheets[0]
    widths = lambda: [round(sheet.pieces[pid].w, 1) for _, pid, _ in sheet.placed]
    assert regroup_sheet(sheet) or widths() == sorted(widths(), reverse=True)
    seen, runs = None, 0
    for w in sorted({round(sheet.pieces[pid].x, 1): round(sheet.pieces[pid].w, 1)
                     for _, pid, _ in sheet.placed}.items()):
        if w[1] != seen:
            runs += 1
            seen = w[1]
    assert runs == len({round(sheet.pieces[pid].w, 1) for _, pid, _ in sheet.placed})


def test_regrouping_keeps_the_layout_valid():
    parts = scattered()
    sheets = pack(parts, W, L, KERF, "BAF", True)
    regroup(sheets)
    assert validate_all(sheets, KERF, {p.id for p in parts}) == []


def test_regrouping_changes_nothing_it_should_not():
    parts = [PartSpec(f"p{i}", f"P{i}", 300.0 if i % 2 else 500.0, 600.0)
             for i in range(14)]
    sheets = pack(parts, W, L, KERF, "BAF", True)
    before_sheets = len(sheets)
    before_cuts = [s.cut_count() for s in sheets]
    before_parts = sorted(pid for s in sheets for pid, _, _ in s.placed)
    before_sizes = sorted((pid, round(s.pieces[q].w, 3), round(s.pieces[q].h, 3))
                          for s in sheets for pid, q, _ in s.placed)

    regroup(sheets)

    assert len(sheets) == before_sheets, "regrouping bought or lost a sheet"
    assert [s.cut_count() for s in sheets] == before_cuts, "cut count changed"
    assert sorted(pid for s in sheets for pid, _, _ in s.placed) == before_parts
    assert sorted((pid, round(s.pieces[q].w, 3), round(s.pieces[q].h, 3))
                  for s in sheets for pid, q, _ in s.placed) == before_sizes, \
        "a part changed size"


def test_regrouping_never_increases_stop_changes():
    parts = [PartSpec(f"p{i}", f"P{i}", (150.0, 300.0, 150.0, 450.0)[i % 4], 700.0)
             for i in range(20)]
    sheets = pack(parts, W, L, KERF, "BAF", True)
    before = stop_changes(sheets, 304.8)
    regroup(sheets)
    assert stop_changes(sheets, 304.8) <= before


def test_regrouping_is_idempotent():
    parts = scattered()
    sheets = pack(parts, W, L, KERF, "BAF", True)
    regroup(sheets)
    snapshot = [(pid, round(s.pieces[q].x, 3), round(s.pieces[q].y, 3))
                for s in sheets for pid, q, _ in s.placed]
    regroup(sheets)
    assert [(pid, round(s.pieces[q].x, 3), round(s.pieces[q].y, 3))
            for s in sheets for pid, q, _ in s.placed] == snapshot


def test_every_cut_still_lands_inside_its_piece():
    parts = [PartSpec(f"p{i}", f"P{i}", (200.0, 350.0, 200.0)[i % 3], 800.0)
             for i in range(15)]
    sheets = pack(parts, W, L, KERF, "BAF", True)
    regroup(sheets)
    for sheet in sheets:
        for piece in sheet.pieces.values():
            if piece.cut is None:
                continue
            low = piece.x if piece.cut.axis == "V" else piece.y
            high = low + (piece.w if piece.cut.axis == "V" else piece.h)
            assert low - 1e-6 <= piece.cut.pos <= high + 1e-6


@pytest.mark.skipif(V3 is None, reason="no large kitchen STEP present")
def test_regrouping_a_real_job_is_free_and_valid():
    panels = parse_step(str(V3))[0]
    by_id = {p.id: p for p in panels}
    cabinets = {p.id: cabinet_of(p) for p in panels}
    for group in build_groups(panels):
        specs = [PartSpec(id=i, label=by_id[i].label, w=by_id[i].width_mm,
                          h=by_id[i].length_mm, group_key=cabinets[i])
                 for i in group.panel_ids]
        layouts, _ = optimise(specs, W, L, KERF, time_budget=1.0)
        sheets_before = len(layouts)
        cuts_before = sum(s.cut_count() for s in layouts)
        stops_before = stop_changes(layouts, 304.8)

        regroup(layouts)

        assert len(layouts) == sheets_before
        assert sum(s.cut_count() for s in layouts) == cuts_before
        assert stop_changes(layouts, 304.8) <= stops_before
        assert validate_all(layouts, KERF, {s.id for s in specs}) == []


@pytest.mark.skipif(V3 is None, reason="no large kitchen STEP present")
def test_the_solver_reports_a_floor_it_can_actually_reach():
    panels = parse_step(str(V3))[0]
    result = solve(panels, LayoutParams(effort="fast"))
    assert result.sheet_floor <= result.stats["sheets"]
    assert result.sheet_bound <= result.sheet_floor
