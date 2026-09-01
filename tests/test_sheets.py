"""Plywood is priority one, so the floor gets its own tests.

The regression these are written against was subtle and expensive: the search
still *reached* a 12-sheet packing on the sample kitchen's half-inch stock, but
only from one lucky random seed, and that seed happened to be the default. Change
anything that consumes the random stream -- a new seed order, an extra placement
heuristic, one more kind of move -- and the luck evaporates, taking a sheet of
plywood with it. Nothing failed, because nothing was checking.

So these tests do not ask "did it find the floor this time". They ask whether it
finds the floor *from every seed*, which is the only version of the question that
stays answered when the code around it changes.
"""
import pytest
from conftest import CABINET, find_step

from app.models import LayoutParams
from app.nesting import PartSpec, optimise_iter
from app.objective import consolidate_first, emptiest_first, sheet_surrogates
from app.solver import build_groups, cabinet_of, solve
from app.step_parser import parse_step

V3 = find_step("V3") or find_step("Kitchen")
W, L, KERF = 1219.2, 2438.4, 2.2
SHEET = W * L
SEEDS = (12345, 98765, 1, 7, 42)
# Long enough that the gradient has converged rather than been caught mid-climb:
# at two seconds this stock reaches its floor on seven seeds in ten, at four on
# ten in ten. The claim being tested is that the search is *aimed* at the floor,
# not that it sprints there.
FLOOR_BUDGET = 4.0


def stock_specs(path, nominal=None):
    """One thickness group's parts, largest group unless one is named."""
    panels = parse_step(str(path))[0]
    by_id = {p.id: p for p in panels}
    groups = build_groups(panels)
    group = (next(g for g in groups if g.nominal == nominal) if nominal
             else max(groups, key=lambda g: len(g.panel_ids)))
    return [PartSpec(id=by_id[i].id, label=by_id[i].label,
                     w=by_id[i].width_mm, h=by_id[i].length_mm,
                     grain_locked=by_id[i].grain_locked,
                     group_key=cabinet_of(by_id[i]))
            for i in group.panel_ids
            if by_id[i].width_mm <= W and by_id[i].length_mm <= L]


def area_bound(specs):
    from math import ceil
    return max(1, ceil(sum(s.w * s.h for s in specs) / SHEET - 1e-9))


def best_count(specs, rate, seed, budget):
    best = None
    for state in optimise_iter(specs, W, L, KERF, time_budget=budget, seed=seed,
                               scorer=rate, align_offsets=False):
        if state.improved:
            best = len(state.sheets)
    return best


# ------------------------------------------------------------- the gradient

def test_a_tiebreaker_is_not_optional():
    """Both surrogates lead on sheet count and differ only below it.

    Which is the point: the tiebreaker is the only thing the search can actually
    follow, since almost every candidate ties on the count itself.
    """
    parts = [PartSpec(f"p{i}", f"P{i}", 400.0, 600.0) for i in range(20)]
    from app.nesting import pack
    sheets = pack(parts, W, L, KERF, "BAF", True)
    for rate in sheet_surrogates(SHEET):
        assert rate(sheets)[0] == len(sheets)


def test_the_emptiest_sheet_term_is_off_for_a_single_sheet():
    """Nothing to shed and nowhere to shed it to; a half-empty sheet is not
    progress, so the term must not argue for one."""
    from app.nesting import pack
    one = pack([PartSpec("a", "A", 400.0, 600.0)], W, L, KERF, "BAF", True)
    assert emptiest_first(SHEET)(one)[1] == 0.0


@pytest.mark.skipif(V3 is None, reason="no large kitchen STEP present")
def test_the_floor_is_found_from_every_seed():
    """The guard the original regression walked straight past.

    This stock needs 92% of twelve sheets, so twelve is only just reachable and
    the search has to be aimed at it rather than stumble on it. One seed finding
    it is luck; every seed finding it is a working gradient.
    """
    specs = stock_specs(V3, '1/2"')
    floor = area_bound(specs)
    got = [best_count(specs, emptiest_first(SHEET), seed, FLOOR_BUDGET)
           for seed in SEEDS]
    assert all(n <= floor for n in got), \
        f"floor is {floor}, seeds gave {dict(zip(SEEDS, got))}"


@pytest.mark.skipif(V3 is None, reason="no large kitchen STEP present")
def test_running_both_gradients_is_never_worse_than_either():
    """Neither dominates, which is why the floor pass runs both."""
    specs = stock_specs(V3, '1/2"')
    seed = SEEDS[0]
    empty = best_count(specs, emptiest_first(SHEET), seed, FLOOR_BUDGET)
    solid = best_count(specs, consolidate_first(SHEET), seed, FLOOR_BUDGET)
    assert min(empty, solid) <= max(empty, solid)
    assert min(empty, solid) <= area_bound(specs)


# ------------------------------------------------------- the whole solve

@pytest.mark.skipif(V3 is None, reason="no large kitchen STEP present")
class TestTheSampleKitchen:
    @pytest.fixture(scope="class")
    def panels(self):
        return parse_step(str(V3))[0]

    def test_it_reaches_the_area_bound(self, panels):
        """No layout can use fewer sheets than the parts' own area needs, so
        meeting that bound is a proof, not a good result."""
        result = solve(panels, LayoutParams(effort="fast"))
        assert result.stats["sheets"] == result.sheet_bound, (
            f"used {result.stats['sheets']} sheets against a bound of "
            f"{result.sheet_bound}")
        assert result.sheet_floor == result.sheet_bound

    @pytest.mark.parametrize("effort", ["fast", "normal"])
    def test_more_time_never_costs_a_sheet(self, panels, effort):
        result = solve(panels, LayoutParams(effort=effort))
        assert result.stats["sheets"] <= result.sheet_bound

    def test_every_part_is_there_and_nothing_overlaps(self, panels):
        """Checked from the API output rather than the solver's own invariants,
        so a tighter pack cannot buy its sheet back by cheating."""
        result = solve(panels, LayoutParams(effort="fast"))
        by_id = {p.id: p for p in panels}
        placed = {}
        for sheet in result.sheets:
            rects = [(p.panel_id, p.x, p.y, p.w, p.h) for p in sheet.placements]
            for pid, x, y, w, h in rects:
                placed[pid] = placed.get(pid, 0) + 1
                assert x >= -1e-4 and y >= -1e-4
                assert x + w <= sheet.width_mm + 1e-4
                assert y + h <= sheet.length_mm + 1e-4
                panel = by_id[pid]
                assert (sorted((round(w, 1), round(h, 1)))
                        == sorted((round(panel.width_mm, 1),
                                   round(panel.length_mm, 1))))
            for i in range(len(rects)):
                for j in range(i + 1, len(rects)):
                    a, b = rects[i], rects[j]
                    apart = (a[1] >= b[1] + b[3] - 1e-4
                             or b[1] >= a[1] + a[3] - 1e-4
                             or a[2] >= b[2] + b[4] - 1e-4
                             or b[2] >= a[2] + a[4] - 1e-4)
                    assert apart, f"{a[0]} overlaps {b[0]}"
        want = {p.id for p in panels if p.included}
        assert set(placed) == want
        assert max(placed.values()) == 1


@pytest.mark.skipif(CABINET is None, reason="cabinet STEP not present")
def test_a_small_job_still_packs_tight():
    """The emptiest-sheet gradient is aimed at big jobs; it must not cost
    anything on a job that fits on a couple of sheets."""
    panels = parse_step(str(CABINET))[0]
    result = solve(panels, LayoutParams(effort="fast"))
    assert result.stats["sheets"] == result.sheet_bound
