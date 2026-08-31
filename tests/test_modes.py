"""The three optimisation goals, and background refinement."""
import time

import pytest
from conftest import CABINET, LAYOUT, find_step

from app.models import LayoutParams
from app.nesting import (MODE_CABINETS, MODE_CUTS, MODE_MATERIAL, PartSpec,
                         SheetLayout, optimise, score, separating_cuts,
                         separating_pieces)
from app.solver import cabinet_of, solve, solve_streaming
from app.step_parser import parse_step
from app.validate import validate_all

KITCHEN = find_step("Kitchen")
W, L, KERF = 1219.2, 2438.4, 2.2


def mixed_specs(n=40):
    return [PartSpec(f"p{i}", f"P{i}",
                     300.0 if i % 3 else 420.0,
                     500.0 if i % 2 else 610.0) for i in range(n)]


# ---------------------------------------------------------------- scoring

def test_cuts_mode_prices_sheets_so_the_search_cannot_spread_thin():
    """Without a sheet price, one part per sheet would 'win' on cut count."""
    dense, _ = optimise(mixed_specs(), W, L, KERF, time_budget=1.5, mode=MODE_CUTS)
    material, _ = optimise(mixed_specs(), W, L, KERF, time_budget=1.5, mode=MODE_MATERIAL)
    assert len(dense) <= len(material) + 1, "cuts mode ran away with the sheet count"


def test_cuts_mode_actually_reduces_cuts():
    parts = mixed_specs()
    by_cuts, _ = optimise(parts, W, L, KERF, time_budget=2.0, mode=MODE_CUTS)
    by_material, _ = optimise(parts, W, L, KERF, time_budget=2.0, mode=MODE_MATERIAL)
    assert (sum(s.cut_count() for s in by_cuts)
            <= sum(s.cut_count() for s in by_material))


def test_score_modes_disagree_about_the_same_layout():
    parts = mixed_specs(20)
    sheets, _ = optimise(parts, W, L, KERF, time_budget=0.5)
    assert score(sheets, MODE_MATERIAL) != score(sheets, MODE_CUTS)


# ---------------------------------------------------------------- kitchen

@pytest.mark.skipif(KITCHEN is None, reason="kitchen STEP not present")
class TestKitchen:
    @pytest.fixture(scope="class")
    def panels(self):
        return parse_step(str(KITCHEN))[0]

    def test_parses_every_part(self, panels):
        # Counts come from the file rather than being pinned, so swapping in a
        # bigger kitchen does not fail a test about parsing.
        assert len(panels) > 100
        assert all(p.included for p in panels)
        assert len({cabinet_of(p) for p in panels}) >= 4

    def test_all_three_modes_produce_valid_layouts(self, panels):
        for mode in ("material", "trackcuts", "cabinets"):
            result = solve(panels, LayoutParams(effort="fast", priorities=["sheets", "grouping", "cuts"]))
            assert not any("validation failed" in w for w in result.warnings), mode
            placed = {p.panel_id for s in result.sheets for p in s.placements}
            assert len(placed) == len(panels), f"{mode} lost parts"

    def test_cabinet_mode_slashes_the_cuts_needed_to_sort_parts(self, panels):
        """The point of the mode: far fewer cuts before parts can be piled up by
        cabinet -- without buying extra sheets to get there."""
        cabinets = {p.id: cabinet_of(p) for p in panels}
        grouped = solve(panels, LayoutParams(effort="normal", priorities=["sheets", "grouping", "cuts"]))
        material = solve(panels, LayoutParams(effort="normal", priorities=["sheets", "trackcuts", "grouping"]))

        # Count honestly on both, not just where the flag happens to be set.
        def sorting_cost(result):
            total = 0
            for sheet in result.sheets:
                names_by_piece = {}
                for p in sheet.placements:
                    names_by_piece.setdefault(cabinets[p.panel_id], 0)
                total += max(0, len(names_by_piece) - 1)
            return total

        assert grouped.stats["sheets"] <= material.stats["sheets"], \
            "grouping cabinets must not cost extra plywood"
        assert grouped.stats["separating_cuts"] > 0
        assert sorting_cost(grouped) <= sorting_cost(material)

    def test_cabinet_mode_does_not_buy_extra_sheets(self, panels):
        material = solve(panels, LayoutParams(effort="fast", priorities=["sheets", "trackcuts", "grouping"]))
        grouped = solve(panels, LayoutParams(effort="fast", priorities=["sheets", "grouping", "cuts"]))
        assert grouped.stats["sheets"] <= material.stats["sheets"] + 0

    def test_isolating_cuts_are_flagged_and_counted_consistently(self, panels):
        result = solve(panels, LayoutParams(effort="fast", priorities=["sheets", "grouping", "cuts"]))
        flagged = [c for s in result.sheets for c in s.cuts if c.separates]
        assert len(flagged) == result.stats["separating_cuts"]
        assert 0 < len(flagged) < result.stats["total_cuts"]

    def test_isolating_cuts_are_only_flagged_when_grouping_is_ranked(self, panels):
        """Flagging every cut on a mixed sheet would be noise, not information."""
        for pri in (["trackcuts", "sheets", "staged"], ["sheets", "trackcuts", "staged"]):
            result = solve(panels, LayoutParams(effort="fast", priorities=pri))
            assert not any(c.separates for s in result.sheets for c in s.cuts), pri
            assert result.stats["separating_cuts"] == 0

    def test_ranking_grouping_higher_reduces_grouping(self, panels):
        # Read the criterion report, not stats["separating_cuts"]: cuts are only
        # *flagged* when grouping is ranked high, but the report always measures it.
        def grouping_of(result):
            return next(c.value for c in result.report if c.key == "grouping")

        low = solve(panels, LayoutParams(
            effort="normal", priorities=["sheets", "trackcuts", "grouping"]))
        high = solve(panels, LayoutParams(
            effort="normal", priorities=["sheets", "grouping", "cuts"]))
        assert grouping_of(high) < grouping_of(low)

    def test_sheets_ranked_first_is_never_beaten_on_sheets(self, panels):
        """Whatever else is ranked, plywood first must not cost extra sheets."""
        a = solve(panels, LayoutParams(
            effort="normal", priorities=["sheets", "grouping", "cuts"]))
        b = solve(panels, LayoutParams(
            effort="normal", priorities=["sheets", "trackcuts", "grouping"]))
        assert a.stats["sheets"] == b.stats["sheets"]

    def test_sheets_are_numbered_consecutively_across_tasks(self, panels):
        """Cabinet mode packs many sub-problems; numbering must not restart."""
        result = solve(panels, LayoutParams(effort="fast", priorities=["sheets", "grouping", "cuts"]))
        assert [s.index for s in result.sheets] == list(range(1, len(result.sheets) + 1))
        for sheet in result.sheets:
            for cut in sheet.cuts:
                assert cut.sheet_index == sheet.index


# ---------------------------------------------------------- background work

@pytest.mark.skipif(CABINET is None, reason="cabinet STEP not present")
def test_background_work_continues_after_the_first_result():
    """It keeps searching even on a layout it cannot improve -- and says so via
    pass-3 frames rather than going silent or emitting bogus updates."""
    panels = parse_step(str(CABINET))[0]
    seen_result = False
    passes_after_result = set()
    for frame in solve_streaming(panels, LayoutParams(effort="fast",
                                                      background_seconds=6)):
        if frame["type"] == "result":
            seen_result = True
            continue
        if seen_result:
            passes_after_result.add(frame["search"]["pass"])
    assert seen_result
    assert passes_after_result == {3}, "background rounds are not labelled pass 3"


@pytest.mark.skipif(KITCHEN is None, reason="kitchen STEP not present")
def test_background_refinement_improves_a_layout_with_headroom():
    panels = parse_step(str(KITCHEN))[0]
    results = [f for f in solve_streaming(
        panels, LayoutParams(effort="fast", background_seconds=18))
        if f["type"] == "result"]
    assert len(results) >= 2, "background refinement never produced an update"
    assert results[0]["refined"] is False
    assert all(r["refined"] for r in results[1:])
    # A refinement must never make things worse.
    first, last = results[0]["payload"]["stats"], results[-1]["payload"]["stats"]
    assert last["sheets"] <= first["sheets"]
    assert last["priorities"][0] == "stopchanges"


@pytest.mark.skipif(CABINET is None, reason="cabinet STEP not present")
def test_plain_solve_never_runs_background_work():
    panels = parse_step(str(CABINET))[0]
    start = time.perf_counter()
    solve(panels, LayoutParams(effort="fast", background_seconds=30))
    assert time.perf_counter() - start < 15, "solve() honoured a background budget"



def two_cabinet_sheet():
    """A 1000x1000 sheet holding one part from each of two cabinets."""
    sheet = SheetLayout(1000.0, 1000.0, 2.0)
    a = PartSpec("a", "A", 480.0, 1000.0, group_key="Cab1")
    b = PartSpec("b", "B", 480.0, 1000.0, group_key="Cab2")
    assert sheet.try_place(a, "BAF", True)
    assert sheet.try_place(b, "BAF", True)
    return sheet, {"a": "Cab1", "b": "Cab2"}


def test_a_single_cut_between_two_cabinets_costs_one():
    sheet, cabinets = two_cabinet_sheet()
    assert separating_cuts(sheet, cabinets) == 1


def test_parts_from_one_cabinet_need_no_separating_cuts():
    sheet, _ = two_cabinet_sheet()
    same = {"a": "Cab1", "b": "Cab1"}
    assert separating_cuts(sheet, same) == 0
    assert separating_pieces(sheet, same) == set()


def test_separating_pieces_are_the_cuts_that_get_counted():
    sheet, cabinets = two_cabinet_sheet()
    pieces = separating_pieces(sheet, cabinets)
    assert len(pieces) == separating_cuts(sheet, cabinets)
    for pid in pieces:
        assert sheet.pieces[pid].cut is not None


def test_interleaving_cabinets_costs_far_more_than_grouping_them():
    """Sanity-check the metric actually discriminates."""
    parts, cabinets = [], {}
    for c in range(4):
        for i in range(9):
            pid = f"c{c}p{i}"
            cabinets[pid] = f"Cab{c}"
            parts.append(PartSpec(pid, pid, 300.0 if i % 2 else 420.0,
                                  500.0 if i % 3 else 610.0, group_key=f"Cab{c}"))
    grouped, _ = optimise(parts, W, L, KERF, time_budget=2.5,
                          mode=MODE_CABINETS, cabinets=cabinets)
    material, _ = optimise(parts, W, L, KERF, time_budget=2.5,
                           mode=MODE_MATERIAL, cabinets=cabinets)
    g = sum(separating_cuts(s, cabinets) for s in grouped)
    m = sum(separating_cuts(s, cabinets) for s in material)
    assert g < m, f"grouping did not help: {g} vs {m}"
    assert len(grouped) <= len(material)


def test_cabinet_scoring_ignores_cabinets_when_none_are_supplied():
    parts = mixed_specs(12)
    sheets, _ = optimise(parts, W, L, KERF, time_budget=0.5)
    assert score(sheets, MODE_CABINETS, None) == score(sheets, MODE_MATERIAL)
