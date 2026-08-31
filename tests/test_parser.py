"""Parser and end-to-end assertions against the real Onshape export.

The expected values here come from reading the STEP file's assembly records
directly: 6 top-level parts plus three 5-part drawer subassemblies = 21 solids.
"""
from pathlib import Path

import pytest

from app.bom import build_bom
from app.models import LayoutParams
from app.solver import build_groups, solve
from app.step_parser import assembly_extent_mm, parse_step

from conftest import CABINET

ROOT = Path(__file__).resolve().parent.parent
SYNTHETIC = ROOT / "tests" / "fixtures" / "synthetic.step"

pytestmark = pytest.mark.skipif(CABINET is None, reason="cabinet STEP not present")


@pytest.fixture(scope="module")
def cabinet():
    return parse_step(str(CABINET))[0]


def test_finds_every_solid_as_a_separate_panel(cabinet):
    assert len(cabinet) == 21


def test_three_drawer_subassemblies_each_contribute_five_panels(cabinet):
    drawers = {}
    for p in cabinet:
        if len(p.path) > 1 and p.path[0].startswith("Drawer"):
            drawers.setdefault(p.path[0], []).append(p)
    assert len(drawers) == 3
    assert all(len(v) == 5 for v in drawers.values())


def test_drawer_instances_read_as_drawer_1_2_3(cabinet):
    """Onshape writes "Drawer <1>"; there is no way to rename instances of one
    part individually, so the tool normalises the numbering itself."""
    names = {p.path[0] for p in cabinet if len(p.path) > 1}
    assert names == {"Drawer 1", "Drawer 2", "Drawer 3"}
    assert not any("<" in p.label for p in cabinet)


def test_identically_named_parts_get_distinct_labels(cabinet):
    backs = [p for p in cabinet if p.path[-1] == "Back"]
    assert len(backs) == 3
    assert len({p.label for p in backs}) == 3          # not collapsed into one
    assert sorted(p.label for p in backs) == [
        "Drawer 1 / Back", "Drawer 2 / Back", "Drawer 3 / Back"]


def test_unique_names_stay_short(cabinet):
    top = [p for p in cabinet if p.path[-1] == "Top Stretcher"][0]
    assert top.label == "Top Stretcher"                 # no needless path prefix


def test_units_are_millimetres_not_metres():
    """The file declares SI_UNIT(.METRE.); OCCT must normalise it to mm.

    A unit-scale slip is the classic STEP bug and would silently produce a
    layout in the wrong scale, so assert the cabinet is cabinet-sized.
    """
    dx, dy, dz = assembly_extent_mm(str(CABINET))
    biggest = max(dx, dy, dz) / 25.4
    assert 12.0 < biggest < 120.0, f"overall size {biggest:.1f} in is not plausible"


def test_thicknesses_cluster_into_two_plywood_stocks(cabinet):
    groups = build_groups(cabinet)
    assert len(groups) == 2
    nominals = sorted(g.nominal for g in groups)
    assert nominals == ['1/2"', '3/4"']


def test_every_panel_yields_a_true_outline(cabinet):
    assert all(p.outline and len(p.outline) >= 4 for p in cabinet)


def test_outline_matches_measured_dimensions(cabinet):
    for p in cabinet:
        us = [u for u, _ in p.outline]
        vs = [v for _, v in p.outline]
        assert max(us) - min(us) == pytest.approx(p.width_mm, abs=0.5)
        assert max(vs) - min(vs) == pytest.approx(p.length_mm, abs=0.5)


def test_bom_groups_the_three_identical_drawer_bottoms(cabinet):
    groups = build_groups(cabinet)
    rows = build_bom(cabinet, groups)
    assert any(r.qty == 3 for r in rows)
    assert sum(r.qty for r in rows) == 21


def test_full_solve_is_valid_and_fits_two_sheets(cabinet):
    result = solve(cabinet, LayoutParams(effort="normal"))
    assert not any("validation failed" in w for w in result.warnings)
    placed = {p.panel_id for s in result.sheets for p in s.placements}
    assert len(placed) == 21
    assert result.stats["sheets"] == 2


def test_cut_list_is_ordered_and_measurable(cabinet):
    result = solve(cabinet, LayoutParams(effort="fast"))
    for sheet in result.sheets:
        assert [c.index for c in sheet.cuts] == list(range(1, len(sheet.cuts) + 1))
        for c in sheet.cuts:
            assert 0 < c.offset_mm < max(c.piece_w, c.piece_h) + 1
            assert c.from_edge in ("left", "bottom")
            assert c.note


@pytest.mark.skipif(not SYNTHETIC.exists(), reason="run tools/make_fixture.py first")
def test_synthetic_fixture_rejects_non_plywood():
    panels, _ = parse_step(str(SYNTHETIC))
    by_name = {p.path[-1]: p for p in panels}
    assert not by_name["Blocky Block"].included
    assert not by_name["Giant Panel"].included
    assert by_name["Wide Panel"].included


@pytest.mark.skipif(not SYNTHETIC.exists(), reason="run tools/make_fixture.py first")
def test_repeated_instances_are_counted_separately():
    panels, _ = parse_step(str(SYNTHETIC))
    shelves = [p for p in panels if p.path[-1].startswith("Shelf")]
    assert len(shelves) == 2                            # not deduplicated
    assert len({p.label for p in shelves}) == 2
