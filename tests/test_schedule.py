"""The cutting order: what the stop is set to, and when.

Everything here rests on one claim -- that sequencing is *free*. Permuting the
bands of a same-axis run, choosing which sheet to break down first, and putting
off the chop cuts all change the order and nothing else: the same parts, the
same sheets, the same cuts. So every test that reorders also checks that the
layout survived it, because a "saving" that quietly moved a part or lost a cut
is not a saving at all.
"""
import random

import pytest
from conftest import find_step

from app import bounds, schedule as sched
from app.models import LayoutParams
from app.nesting import HEURISTICS, PartSpec, pack, pack_one
from app.objective import cut_saws, saw_changes, stop_changes, stop_runs
from app.solver import build_groups, cabinet_of, solve
from app.staged import pack_staged
from app.step_parser import parse_step
from app.validate import validate_all

V3 = find_step("V3") or find_step("Kitchen")
W, L, KERF, MITER = 1219.2, 2438.4, 2.2, 304.8


def spec(name, w, h, **kw):
    return PartSpec(name, name.upper(), w, h, **kw)


def strips(widths, height=2400.0):
    """Full-length columns of the given widths, in that order across the sheet."""
    return [spec(f"p{i}", w, height, grain_locked=True)
            for i, w in enumerate(widths)]


# ---------------------------------------------------- the stop is one number

def test_a_rip_and_a_crosscut_at_one_measurement_are_one_setting():
    """Turning the work under the saw is free; touching the stop is not.

    A 200 mm rip and a 200 mm crosscut want the parallel guide in the same
    place, so cutting them back to back costs nothing.
    """
    # A 200 mm rip down a full-length column, and a 200 mm crosscut across a
    # 600 mm band. Both spans are well over the chop saw's reach, so both are
    # track cuts -- and both want the guide 200 mm from the edge.
    layout = pack_one([spec("a", 200.0, 2400.0, grain_locked=True),
                       spec("b", 600.0, 200.0, grain_locked=True)],
                      W, L, KERF, "BAF", True)
    assert layout is not None
    at_200 = {p.cut.axis for p in layout.pieces.values()
              if p.cut is not None
              and round(p.cut.pos - (p.x if p.cut.axis == "V" else p.y), 1) == 200.0}
    assert at_200 == {"V", "H"}, f"wanted a rip and a crosscut at 200: {at_200}"

    runs = [r for r in stop_runs([layout], MITER) if r[0] == 200.0]
    assert len(runs) == 1, f"200 mm was set up more than once: {runs}"
    assert runs[0][1] == 2, "the rip and the crosscut should share one setting"


def test_bands_are_reordered_so_equal_widths_are_cut_together():
    """200, 400, 200, 400 across the sheet: four settings if you cut left to
    right, two if you take the bands in a sensible order. They are the same
    layout -- the packer's order is arbitrary."""
    parts = strips([200.0, 400.0, 200.0, 400.0])
    sheets = pack(parts, W, L, KERF, "BAF", True)
    runs = stop_runs(sheets, MITER)
    settings = [offset for offset, _ in runs]
    assert len(settings) == len(set(settings)), \
        f"a setting was returned to: {settings}"


def test_a_setting_carries_from_one_sheet_into_the_next():
    """Two sheets that both hold 300 mm columns should be cut with the stop set
    once, not once each."""
    parts = ([spec(f"a{i}", 300.0, 2400.0, grain_locked=True) for i in range(4)]
             + [spec(f"b{i}", 300.0, 2400.0, grain_locked=True) for i in range(4)])
    sheets = pack(parts, W, L, KERF, "BAF", True)
    assert len(sheets) >= 2
    runs = stop_runs(sheets, MITER)
    assert [offset for offset, _ in runs].count(300.0) == 1, \
        f"the 300 mm stop was set more than once: {runs}"


def test_the_sheet_order_is_chosen_not_inherited():
    """Sheets 1 and 3 share a width; sheet 2 does not. Cut sheet 3 second and
    the shared setting is made once instead of twice."""
    # Full-length columns, so the only cuts are rips and the case is about
    # nothing but which sheet follows which.
    sheets = [pack_one(strips(sizes, L), W, L, KERF, "BAF", True) for sizes in (
        [600.0, 617.0],        # 617 here...
        [400.0, 817.0],        # nothing in common
        [617.0, 600.0],        # ...and 617 here
    )]
    assert all(s is not None for s in sheets)
    in_place = stop_changes(sheets, MITER)
    plan = sched.plan_job(sheets, MITER)
    assert plan.stop_changes < in_place, "the sheet order bought nothing"
    assert plan.order != [0, 1, 2]
    # The shared setting is now one run covering two sheets.
    shared = [(w, n) for w, n in plan.stop_runs if w == 617.0]
    assert len(shared) == 1 and shared[0][1] == 2


# ------------------------------------------------------- sequencing is free

def _fingerprint(sheets):
    """Everything a reorder must not change."""
    return sorted(
        (part_id, round(sheet.pieces[pid].w, 3), round(sheet.pieces[pid].h, 3))
        for sheet in sheets for part_id, pid, _ in sheet.placed)


@pytest.mark.parametrize("heuristic", HEURISTICS)
def test_applying_a_plan_moves_no_part_and_adds_no_cut(heuristic):
    rng = random.Random(4)
    parts = [spec(f"p{i}", rng.choice([150.0, 300.0, 150.0, 470.0]),
                  rng.choice([400.0, 700.0, 400.0])) for i in range(24)]
    sheets = pack(parts, W, L, KERF, heuristic, True)
    before_cuts = sorted(s.cut_count() for s in sheets)
    before = _fingerprint(sheets)

    job = sched.Job(sheets, MITER)
    plan = sched.plan_job(sheets, MITER, job=job)
    ordered = sched.apply_plan(sheets, plan, job)

    assert len(ordered) == len(sheets)
    assert sorted(s.cut_count() for s in ordered) == before_cuts
    assert _fingerprint(ordered) == before
    assert validate_all(ordered, KERF, {p.id for p in parts}) == []


def test_the_plan_is_the_order_the_cut_list_gets():
    """Reporting a number the cut list cannot reproduce would be a lie."""
    parts = strips([200.0, 400.0, 200.0, 400.0, 200.0])
    sheets = pack(parts, W, L, KERF, "BAF", True)
    job = sched.Job(sheets, MITER)
    plan = sched.plan_job(sheets, MITER, job=job)
    ordered = sched.apply_plan(sheets, plan, job)

    # Walk the plan's own steps against the rewritten trees.
    seen: list[float] = []
    for step in plan.steps:
        if step.saw != sched.TRACK:
            continue
        piece = sheets[step.sheet].pieces[step.pid]
        offset = ((piece.cut.pos - piece.x) if piece.cut.axis == "V"
                  else (piece.cut.pos - piece.y))
        assert round(offset, 1) == step.offset
        seen.append(step.offset)
    blocks = sum(1 for i, w in enumerate(seen) if i == 0 or w != seen[i - 1])
    assert blocks == len(plan.stop_runs)
    assert plan.stop_changes == max(0, blocks - 1)
    assert ordered  # the reordered list is what gets drawn


def test_a_plan_is_reproducible():
    parts = strips([200.0, 400.0, 200.0, 300.0, 400.0])
    a = pack(parts, W, L, KERF, "BAF", True)
    b = pack(parts, W, L, KERF, "BAF", True)
    assert (sched.plan_job(a, MITER).stop_changes
            == sched.plan_job(b, MITER).stop_changes)


def test_rewriting_a_run_twice_settles():
    parts = strips([200.0, 400.0, 200.0, 400.0])
    sheets = pack(parts, W, L, KERF, "BAF", True)
    sched.apply_plan(sheets, sched.plan_job(sheets, MITER), miter_capacity=MITER)
    once = _fingerprint(sheets), stop_changes(sheets, MITER)
    sched.apply_plan(sheets, sched.plan_job(sheets, MITER), miter_capacity=MITER)
    assert (_fingerprint(sheets), stop_changes(sheets, MITER)) == once


# ------------------------------------------------------------ the two saws

def test_chop_cuts_do_not_break_a_run():
    """The miter saw has nothing to do with the parallel guide, so a trip to it
    leaves the stop where it was."""
    parts = ([spec(f"w{i}", 500.0, 700.0) for i in range(4)]
             + [spec(f"n{i}", 150.0, 200.0) for i in range(6)])
    sheets = pack(parts, W, L, KERF, "BAF", True)
    plan = sched.quick_plan(sheets, MITER)
    saws = [step.saw for step in plan.steps]
    assert sched.MITER in saws and sched.TRACK in saws
    # Dropping the chop cuts must not change the run structure at all.
    track_only = [step.offset for step in plan.steps if step.saw == sched.TRACK]
    blocks = sum(1 for i, w in enumerate(track_only)
                 if i == 0 or w != track_only[i - 1])
    assert blocks == len(plan.stop_runs)


def test_saw_changes_are_counted_on_the_sequence_not_the_tree():
    """Two per sheet is the shape to expect: break it down, then chop."""
    parts = [spec(f"n{i}", 150.0, 400.0) for i in range(12)]
    sheets = pack(parts, W, L, KERF, "BAF", True)
    changes = saw_changes(sheets, MITER)
    assert 0 < changes <= 2 * len(sheets)
    both = sum(1 for s in sheets if len(set(cut_saws(s, MITER).values())) > 1)
    assert changes >= max(0, both - 1)


# ----------------------------------------------------------- certified floors

def test_no_cutting_order_beats_the_stop_change_floor():
    """The bound is the whole basis for claiming a cut order is optimal, so it
    has to hold for every layout, not the ones it was written against."""
    rng = random.Random(11)
    for trial in range(12):
        parts = [spec(f"p{i}",
                      rng.choice([120.0, 200.0, 305.0, 420.0, 600.0]),
                      rng.choice([250.0, 400.0, 610.0, 900.0]))
                 for i in range(rng.randint(6, 26))]
        heuristic = rng.choice(HEURISTICS)
        sheets = pack(parts, W, L, KERF, heuristic, rng.random() < 0.5)
        floor = bounds.stopchange_bound(sheets, MITER)
        got = sched.plan_job(sheets, MITER).stop_changes
        assert got >= floor, f"trial {trial}: {got} beat its floor {floor}"
        assert saw_changes(sheets, MITER) >= bounds.sawchange_bound(sheets, MITER)


def test_the_floor_is_worth_something():
    """A floor of nearly zero would be true and useless."""
    parts = strips([200.0, 400.0, 200.0, 300.0])
    sheets = pack(parts, W, L, KERF, "BAF", True)
    assert bounds.stopchange_bound(sheets, MITER) >= 2


# --------------------------------------------------- pinned runs stay pinned

def test_a_pinned_run_is_never_permuted():
    """Grain-matched faces have to come off in installation order. That order is
    the requirement, so it is not the sequencer's to spend."""
    layout = pack_one([spec("a", 900.0, 600.0)], W, L, KERF, "BAF", True)
    assert layout is not None
    piece = layout.pieces[layout.placed[0][1]]
    cursor = piece.id
    order = []
    for size in (150.0, 250.0, 100.0):
        layout.pieces[cursor].pinned = True
        kept, rest = layout._cut(cursor, "H", size)
        order.append(kept)
        assert rest is not None
        cursor = rest

    chains = sched.build_chains(layout, MITER)
    pinned = [c for c in chains if c.pinned]
    assert pinned, "the run should have been marked pinned"
    plan = sched.quick_plan([layout], MITER)
    for chain in pinned:
        chosen = plan.orders.get((0, chain.head))
        if chosen:
            assert chosen == chain.sections[:len(chosen)], \
                "a pinned run was reordered"


# --------------------------------------------------------- against real parts

@pytest.mark.skipif(V3 is None, reason="no large kitchen STEP present")
class TestRealJob:
    @pytest.fixture(scope="class")
    def stock(self):
        panels = parse_step(str(V3))[0]
        by_id = {p.id: p for p in panels}
        group = max(build_groups(panels), key=lambda g: len(g.panel_ids))
        return [PartSpec(id=by_id[i].id, label=by_id[i].label,
                         w=by_id[i].width_mm, h=by_id[i].length_mm,
                         group_key=cabinet_of(by_id[i]))
                for i in group.panel_ids
                if by_id[i].width_mm <= W and by_id[i].length_mm <= L]

    def test_planning_never_costs_and_usually_saves(self, stock):
        sheets = pack(sorted(stock, key=lambda p: -(p.w * p.h)),
                      W, L, KERF, "BAF", True)
        in_place = stop_changes(sheets, MITER)
        planned = sched.plan_job(sheets, MITER, effort=2).stop_changes
        assert planned <= in_place

    def test_a_rip_first_layout_schedules_into_long_runs(self, stock):
        """Rip-first is the easy case: every rip is reachable from the start, so
        a width should never need setting up twice on one sheet."""
        sheets = pack_staged(stock, W, L, KERF, miter_capacity=MITER)
        assert sheets
        job = sched.Job(sheets, MITER)
        for index in range(len(sheets)):
            got = sched._schedule_sheet(job, index, None)
            settings = [s.offset for s in got.steps if s.saw == sched.TRACK]
            blocks = [w for i, w in enumerate(settings)
                      if i == 0 or w != settings[i - 1]]
            assert len(blocks) == len(set(blocks)), \
                f"sheet {index} returned to a setting: {blocks}"

    def test_the_whole_job_is_sequenced_across_stocks(self, stock):
        """The stop does not care what thickness it is cutting."""
        panels = parse_step(str(V3))[0]
        result = solve(panels, LayoutParams(effort="fast"))
        order = [s.group_id for s in result.sheets]
        assert len(set(order)) > 1, "this job should have two stocks"
        # Sheets in cutting order, and the plan is free to interleave them.
        assert [s.index for s in result.sheets] == list(
            range(1, len(result.sheets) + 1))

    def test_the_reported_figure_is_the_one_in_the_cut_list(self, stock):
        panels = parse_step(str(V3))[0]
        result = solve(panels, LayoutParams(effort="fast"))
        reported = {c.key: c.value for c in result.report}
        # Rebuild the count straight off the cut list, in the order it is given.
        settings: list[float] = []
        for sheet in result.sheets:
            for cut in sheet.cuts:
                if cut.saw == "track":
                    settings.append(round(cut.offset_mm, 1))
        blocks = sum(1 for i, w in enumerate(settings)
                     if i == 0 or w != settings[i - 1])
        assert reported["stopchanges"] == max(0, blocks - 1)
        assert len(result.stop_plan) == blocks

        swaps: list[str] = []
        for sheet in result.sheets:
            for cut in sheet.cuts:
                if not swaps or swaps[-1] != cut.saw:
                    swaps.append(cut.saw)
        assert reported["staged"] == max(0, len(swaps) - 1)

    def test_the_stop_plan_says_which_sheets_a_run_covers(self, stock):
        panels = parse_step(str(V3))[0]
        result = solve(panels, LayoutParams(effort="fast"))
        assert all(row.sheets for row in result.stop_plan)
        for row in result.stop_plan:
            # A run is contiguous, so the sheets it touches must be too.
            assert row.sheets == list(range(row.sheets[0], row.sheets[-1] + 1))
        assert sum(row.count for row in result.stop_plan) == sum(
            1 for s in result.sheets for c in s.cuts if c.saw == "track")
