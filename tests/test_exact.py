"""The exact prover, and the bounds it prunes with.

A branch-and-bound bug produces a *false proof of optimality*, which is worse
than no proof. So the prover is checked against literal enumeration on every
instance small enough to enumerate.
"""
import pytest
from conftest import KITCHEN_OK  # noqa: F401  (ensures conftest path setup)

from app import bounds
from app.exact import brute_force_sheet, prove_sheet
from app.nesting import PartSpec, pack_one
from app.objective import (SHEETS, TRACKCUTS, measure, resolve_order, score,
                           stage_depth, verify_cut_identity)

W, L, K = 1219.2, 2438.4, 2.2


def spec(pid, w, h, cab="C1"):
    return PartSpec(pid, pid, w, h, group_key=cab)


TINY = [
    [spec("a", 600.0, 1200.0), spec("b", 600.0, 1200.0)],
    [spec("a", 600.0, 1200.0), spec("b", 600.0, 1200.0), spec("c", 600.0, 1200.0)],
    [spec("a", 400.0, 900.0), spec("b", 700.0, 500.0), spec("c", 300.0, 300.0)],
    [spec("a", 500.0, 700.0), spec("b", 500.0, 700.0),
     spec("c", 200.0, 400.0), spec("d", 900.0, 300.0)],
]


@pytest.mark.parametrize("parts", TINY, ids=lambda p: f"{len(p)}parts")
def test_prover_agrees_with_literal_enumeration(parts):
    """The only place brute force is affordable -- and the only way to know the
    pruning never discards a better layout."""
    proven = prove_sheet(parts, W, L, K, time_limit=60)
    exhaustive = brute_force_sheet(parts, W, L, K)
    assert proven.proven, "search did not complete on a tiny instance"
    assert proven.cuts == exhaustive


def test_a_weaker_bound_finds_the_same_optimum():
    """If loosening the pruning changes the answer, the bound was not valid."""
    parts = TINY[2]
    tight = prove_sheet(parts, W, L, K, time_limit=60)
    loose = prove_sheet(parts, W, L, K, incumbent=999, time_limit=60)
    assert tight.cuts == loose.cuts
    assert loose.nodes >= tight.nodes


def test_prover_never_beats_the_certified_floor():
    for parts in TINY:
        result = prove_sheet(parts, W, L, K, time_limit=60)
        assert result.cuts >= bounds.cut_bound(len(parts), 1)


def test_timeout_reports_a_result_but_claims_no_proof():
    parts = [spec(f"p{i}", 200.0 + 17 * i, 300.0 + 13 * i) for i in range(11)]
    result = prove_sheet(parts, W, L, K, time_limit=0.4)
    assert result.cuts is not None, "a timeout must still return the seeded answer"
    assert not result.proven
    assert "incomplete" in result.status


def test_prover_is_never_worse_than_the_heuristic():
    parts = [spec(f"p{i}", 300.0, 420.0) for i in range(6)]
    heuristic = pack_one(parts, W, L, K, "BAF", True).cut_count()
    assert prove_sheet(parts, W, L, K, time_limit=30).cuts <= heuristic


# ---------------------------------------------------------------- objective

def test_cut_identity_holds():
    parts = [spec(f"p{i}", 300.0, 420.0) for i in range(9)]
    sheets = [pack_one(parts, W, L, K, "BAF", True)]
    assert verify_cut_identity(sheets)


def test_stage_depth_counts_direction_changes():
    single = pack_one([spec("a", 600.0, 1200.0)], W, L, K, "BAF", True)
    assert stage_depth(single) >= 1
    many = pack_one([spec(f"p{i}", 300.0, 420.0) for i in range(8)],
                    W, L, K, "BAF", True)
    assert stage_depth(many) >= stage_depth(single)


def test_unranked_criteria_become_tiebreakers():
    order = resolve_order(["trackcuts"])
    assert order[0] == TRACKCUTS
    assert set(order) == {"grouping", "staged", "stopchanges", "trackcuts",
            "mitercuts", "offcut"}
    assert len(order) == len(set(order))


def test_ranking_changes_the_comparison():
    parts = [spec(f"p{i}", 300.0, 420.0) for i in range(8)]
    sheets = [pack_one(parts, W, L, K, "BAF", True)]
    # Pick two criteria whose values actually differ, otherwise swapping their
    # positions leaves the tuple identical and proves nothing.
    assert score(sheets, ["mitercuts"]) != score(sheets, ["trackcuts"])


def test_measure_reports_every_criterion():
    """measure() also carries `sheets` for the cap, which is not rankable."""
    from app.objective import CRITERIA
    layout = pack_one([spec("a", 300.0, 400.0), spec("b", 300.0, 400.0)],
                      W, L, K, "BAF", True)
    values = measure([layout])
    assert CRITERIA <= set(values)
    assert "sheets" in values and "sheets" not in CRITERIA



def test_sheet_bound_is_the_area_bound():
    parts = [spec(f"p{i}", 1219.2, 2438.4) for i in range(3)]
    assert bounds.sheet_bound(parts, W, L) == 3


def test_cut_bound_follows_the_identity():
    assert bounds.cut_bound(138, 15) == 123
    assert bounds.cut_bound(5, 10) == 0          # never negative


def test_grouping_bound_counts_forced_incidences():
    # One cabinet needing three sheets on its own forces two grouping cuts.
    big = [spec(f"p{i}", 1219.2, 2438.4, "C1") for i in range(3)]
    assert bounds.grouping_bound(big, W, L, sheets=1) >= 2


def test_no_layout_ever_beats_its_bounds():
    from app.nesting import pack
    parts = [spec(f"p{i}", 300.0 + 11 * i, 420.0 + 7 * i, f"C{i % 3}") for i in range(10)]
    sheets = pack(parts, W, L, K, "BAF", True)      # may span several sheets
    floors = bounds.compute(parts, W, L, len(sheets))
    values = measure(sheets, {p.id: p.group_key for p in parts})
    for key in ("trackcuts", "grouping"):
        assert values[key] >= floors[key], f"{key} beat its own lower bound"


# ---------------------------------------------------------------- kerf policy

def test_offcuts_thinner_than_the_blade_are_kept_by_default():
    """A sliver narrower than the saw kerf is still usable material."""
    layout = pack_one([spec("a", W - K - 1.0, 500.0)], W, L, K, "BAF", True)
    widths = [p.w for p in layout.pieces.values()
              if p.part_id is None and not p.children]
    assert any(0 < w < K for w in widths), "a sub-blade offcut was discarded"


def test_min_offcut_discards_slivers_when_asked():
    from app.nesting import SheetLayout
    layout = SheetLayout(W, L, K, min_offcut=50.0)
    layout.try_place(spec("a", W - K - 1.0, 500.0), "BAF", True)
    widths = [p.w for p in layout.pieces.values()
              if p.part_id is None and not p.children]
    assert not any(0 < w < 50.0 for w in widths)
