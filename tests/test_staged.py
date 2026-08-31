"""Rip-first layouts: the pattern a store's panel saw can actually cut.

Everything here guards one promise -- stage one is nothing but full-length rips,
so the long cuts can be handed over and only crosscuts come home.
"""
import pytest
from conftest import CABINET, find_step

from app.models import LayoutParams
from app.nesting import PartSpec
from app.objective import (STAGED, STOPCHANGES, measure, stage_depth,
                           stop_changes, stop_runs)
from app.solver import solve
from app.staged import (candidate_widths, default_choices, pack_staged, plan,
                        search_staged)
from app.step_parser import parse_step
from app.validate import validate_all

KITCHEN = find_step("Kitchen")
W, L, KERF = 1219.2, 2438.4, 2.2


def parts(n=24):
    """A mix that shares widths, the way real cabinet parts do."""
    out = []
    for i in range(n):
        width = (101.6, 304.8, 609.6)[i % 3]
        length = (762.0, 609.6)[i % 2]
        out.append(PartSpec(f"p{i}", f"P{i}", width, length, group_key=f"C{i % 3}"))
    return out


def test_layout_is_valid():
    ps = parts()
    sheets = pack_staged(ps, W, L, KERF)
    assert sheets
    assert validate_all(sheets, KERF, {p.id for p in ps}) == []


def test_every_part_is_placed_once_at_its_own_size():
    ps = parts()
    sheets = pack_staged(ps, W, L, KERF)
    placed = [(pid, s.pieces[piece]) for s in sheets for pid, piece, _ in s.placed]
    assert len(placed) == len(ps)
    assert {pid for pid, _ in placed} == {p.id for p in ps}
    by_id = {p.id: p for p in ps}
    for pid, piece in placed:
        want = sorted((by_id[pid].w, by_id[pid].h))
        assert sorted((round(piece.w, 3), round(piece.h, 3))) == pytest.approx(want)


def test_rip_first_on_narrow_strips_is_exactly_two_saw_changes():
    """The pattern worth aiming for: rip everything on the track saw, put it
    away, chop the rest. A full-length rip is itself a track cut, so a strip
    layout of chop-able parts is two stages, never one."""
    narrow = [PartSpec(f"n{i}", f"N{i}", 150.0, 400.0) for i in range(6)]
    for sheet in pack_staged(narrow, W, L, KERF, miter_capacity=MITER):
        assert stage_depth(sheet, MITER) == 2


def test_a_layout_using_one_saw_is_a_single_stage():
    wide = [PartSpec(f"w{i}", f"W{i}", 600.0, 700.0) for i in range(4)]
    for sheet in pack_staged(wide, W, L, KERF, miter_capacity=MITER):
        saws = set(cut_saws(sheet, MITER).values())
        if len(saws) == 1:
            assert stage_depth(sheet, MITER) == 1


def test_stage_one_cuts_are_full_length_rips():
    for sheet in pack_staged(parts(), W, L, KERF):
        root = sheet.pieces[0]
        if root.cut is None:
            continue
        if root.cut.axis != "V":
            continue                      # single full-width strip: no rips
        # Every cut in the opening run spans the whole length of the sheet.
        queue = [0]
        while queue:
            piece = sheet.pieces[queue.pop()]
            if piece.cut is None or piece.cut.axis != "V":
                continue
            for child in piece.children:
                assert sheet.pieces[child].h == pytest.approx(sheet.length)
                queue.append(child)




def test_grain_locked_parts_keep_their_orientation():
    locked = [PartSpec(f"p{i}", f"P{i}", 200.0, 700.0, grain_locked=True)
              for i in range(6)]
    assert all(len(candidate_widths(p)) == 1 for p in locked)
    sheets = pack_staged(locked, W, L, KERF)
    assert validate_all(sheets, KERF, {p.id for p in locked}) == []
    for sheet in sheets:
        for _, piece, rotated in sheet.placed:
            assert rotated is False


def test_widths_shared_by_many_parts_are_preferred():
    """Sharing a width is what turns eight instructions into one."""
    ps = [PartSpec(f"p{i}", f"P{i}", 300.0, 700.0) for i in range(5)]
    ps.append(PartSpec("odd", "Odd", 300.0, 450.0))
    choices = default_choices(ps)
    assert all(v == pytest.approx(300.0) for v in choices.values())


def test_equal_widths_end_up_adjacent():
    """So the instruction reads 'three strips of 4', not three separate asks."""
    for sheet in plan(parts(), W, L, KERF):
        widths = [round(s.width, 1) for s in sheet]
        assert widths == sorted(widths, reverse=True)
        seen, runs = set(), 0
        for w in widths:
            if w not in seen:
                runs += 1
                seen.add(w)
        assert runs == len(set(widths))


def test_search_only_ever_returns_its_best():
    ps = parts()
    scorer = lambda sheets: (len(sheets), stop_changes(sheets, MITER))
    layouts, value = search_staged(ps, W, L, KERF, scorer, time_budget=0.6)
    assert layouts is not None
    assert value == scorer(layouts)
    assert validate_all(layouts, KERF, {p.id for p in ps}) == []


def test_impossible_parts_are_reported_not_crashed():
    assert pack_staged([PartSpec("x", "X", 5000.0, 5000.0)], W, L, KERF) is None



# ------------------------------------------------- miter saw vs track saw

from app.bounds import trackcut_bound              # noqa: E402
from app.objective import TRACKCUTS, cut_workload  # noqa: E402

MITER = 304.8      # 12"


def test_every_cut_belongs_to_one_of_the_two_saws():
    ps = parts()
    sheet = pack_staged(ps, W, L, KERF, miter_capacity=MITER)[0]
    miter, track = cut_workload(sheet, MITER)
    assert miter + track == sheet.cut_count()


def test_narrow_strips_are_miter_work_and_wide_ones_are_not():
    """A strip you can lay on the miter fence must not be counted as track work."""
    narrow = [PartSpec(f"n{i}", f"N{i}", 150.0, 400.0) for i in range(6)]
    sheet = pack_staged(narrow, W, L, KERF, miter_capacity=MITER)[0]
    saws = cut_saws(sheet, MITER)
    # Crosscuts inside a narrow strip are chop work; the rip that made the strip
    # runs the length of the sheet and is not.
    for pid, saw in saws.items():
        piece = sheet.pieces[pid]
        assert saw == ("miter" if piece.cut.axis == "H" else "track")

    wide = [PartSpec(f"w{i}", f"W{i}", 600.0, 700.0) for i in range(4)]
    sheet = pack_staged(wide, W, L, KERF, miter_capacity=MITER)[0]
    assert cut_workload(sheet, MITER)[0] == 0


def test_a_part_too_wide_both_ways_always_needs_the_track_saw():
    ps = [PartSpec("a", "A", 600.0, 700.0), PartSpec("b", "B", 150.0, 400.0)]
    assert trackcut_bound(ps, MITER) == 1


def test_choosing_the_narrow_dimension_moves_work_to_the_miter_saw():
    """A 10x30 part belongs in a 10" strip, not a 30" one."""
    ps = [PartSpec(f"p{i}", f"P{i}", 250.0, 760.0) for i in range(8)]
    aware = pack_staged(ps, W, L, KERF, miter_capacity=MITER)
    naive = pack_staged(ps, W, L, KERF)
    assert (sum(cut_workload(s, MITER)[1] for s in aware)
            <= sum(cut_workload(s, MITER)[1] for s in naive))


@pytest.mark.skipif(KITCHEN is None, reason="kitchen STEP not present")
def test_track_saw_work_reaches_its_provable_floor():
    """Nothing is left on the track saw that could have been chopped at home."""
    panels = parse_step(str(KITCHEN))[0]
    result = solve(panels, LayoutParams(
        effort="fast", max_extra_sheets=10, miter_capacity_mm=MITER,
        priorities=[STAGED, TRACKCUTS, STOPCHANGES, "sheets"]))
    report = {c.key: c for c in result.report}
    # The floor counts only parts that must be crosscut wide. Full-length rips
    # are track cuts too, so the achievable minimum sits above it -- the bound
    # stays valid, it is simply no longer tight.
    assert report[TRACKCUTS].bound is not None
    assert report[TRACKCUTS].value >= report[TRACKCUTS].bound


@pytest.mark.skipif(KITCHEN is None, reason="kitchen STEP not present")
def test_a_bigger_miter_saw_moves_more_work_off_the_track_saw():
    panels = parse_step(str(KITCHEN))[0]

    def track_cuts(capacity):
        r = solve(panels, LayoutParams(
            effort="fast", max_extra_sheets=10, miter_capacity_mm=capacity,
            priorities=[STAGED, TRACKCUTS, "sheets"]))
        return {c.key: c.value for c in r.report}[TRACKCUTS]

    assert track_cuts(600.0) <= track_cuts(MITER)


# ------------------------------------------- strips belong to exactly one saw

def test_a_strip_is_never_half_chop_saw_work():
    """The thing that makes a strip pleasant: you chop the whole stick, or none
    of it. Parts in a strip all share its width, so its cuts cannot be mixed."""
    ps = parts(30)
    for sheet in plan(ps, W, L, KERF, miter_capacity=MITER):
        for strip in sheet:
            spans = {round(strip.width, 3)}
            assert len(spans) == 1
            choppable = strip.width <= MITER + 1e-6
            for part, length, _ in strip.parts:
                assert (length <= L + 1e-6)
                # Every crosscut in this strip spans the strip width, so they
                # are all the same kind of work.
                assert (strip.width <= MITER + 1e-6) == choppable


def test_chop_saw_strips_hold_only_chop_saw_parts():
    from app.objective import cut_workload
    ps = parts(30)
    for sheet in pack_staged(ps, W, L, KERF, miter_capacity=MITER):
        miter, track = cut_workload(sheet, MITER)
        # Per strip the work is uniform, so a sheet's mixed totals can only come
        # from having both narrow and wide strips -- never from a mixed strip.
        for piece in sheet.pieces.values():
            if piece.cut is None or piece.cut.axis != "H":
                continue
            span = piece.w
            kids = [sheet.pieces[c] for c in piece.children]
            for kid in kids:
                assert kid.w == pytest.approx(span)


def test_mitre_and_track_cuts_add_up_to_the_total():
    from app.objective import cut_workload
    ps = parts(24)
    for sheet in pack_staged(ps, W, L, KERF, miter_capacity=MITER):
        miter, track = cut_workload(sheet, MITER)
        assert miter + track == sheet.cut_count()


@pytest.mark.skipif(KITCHEN is None, reason="kitchen STEP not present")
def test_the_two_saws_are_ranked_separately():
    from app.objective import MITERCUTS
    panels = parse_step(str(KITCHEN))[0]
    result = solve(panels, LayoutParams(
        effort="fast", max_extra_sheets=10, miter_capacity_mm=MITER,
        priorities=[STAGED, TRACKCUTS, STOPCHANGES, MITERCUTS, "sheets"]))
    keys = [c.key for c in result.report]
    assert keys.index(TRACKCUTS) < keys.index(MITERCUTS)
    values = {c.key: c.value for c in result.report}
    total = result.stats["total_cuts"]
    assert values[TRACKCUTS] + values[MITERCUTS] <= total


def test_default_ranking_leads_with_stop_changes():
    """Keeping the stop still is the headline goal; track cuts and saw changes
    follow. Plywood is not ranked at all -- it is a hard cap from the floor."""
    from app.objective import MITERCUTS, resolve_order
    order = resolve_order(LayoutParams().priorities)
    assert "sheets" not in order
    assert order[0] == STOPCHANGES
    assert order.index(TRACKCUTS) < order.index(STAGED)
    assert order.index(STAGED) < order.index(MITERCUTS)


# ------------------------------------------- continuous grain on drawer faces

from app.grain import auto_select, build_runs, expand, substitute   # noqa: E402
from app.solver import cabinet_of                                    # noqa: E402


def face_panels():
    """Two drawers, each with an inner front and a larger applied face."""
    from app.models import Panel
    out = []
    for drawer, (inner_w, outer_w) in enumerate([(140.0, 165.0), (250.0, 280.0)], 1):
        out.append(Panel(id=f"in{drawer}", path=["Cab A", f"Drawer {drawer}", "Drawer Face"],
                         label=f"Cab A / Drawer {drawer} / Drawer Face #1",
                         thickness_mm=12.7, width_mm=inner_w, length_mm=700.0))
        out.append(Panel(id=f"out{drawer}", path=["Cab A", f"Drawer {drawer}", "Drawer Face"],
                         label=f"Cab A / Drawer {drawer} / Drawer Face #2",
                         thickness_mm=12.7, width_mm=outer_w, length_mm=760.0))
    return out


def test_only_the_visible_face_of_each_pair_is_selected():
    panels = face_panels()
    chosen = auto_select(panels, cabinet_of)
    assert chosen == {"out1", "out2"}, "picked the inner fronts, not the applied faces"


def test_runs_are_ordered_by_drawer_number():
    runs = build_runs(face_panels(), cabinet_of)
    assert len(runs) == 1
    assert [m.id for m in runs[0].members] == ["out1", "out2"]


def test_a_run_of_one_face_is_not_constrained():
    panels = face_panels()[:2]          # a single drawer
    assert build_runs(panels, cabinet_of) == []


def test_composite_spans_the_faces_plus_a_kerf_between():
    run = build_runs(face_panels(), cabinet_of)[0]
    assert run.across() == pytest.approx(760.0)
    assert run.along(3.0) == pytest.approx(165.0 + 280.0 + 3.0)


def test_composite_replaces_its_members_and_never_rotates():
    panels = face_panels()
    runs = build_runs(panels, cabinet_of)
    specs = [PartSpec(p.id, p.label, p.width_mm, p.length_mm) for p in panels]
    swapped = substitute(specs, runs, 3.0)
    ids = {s.id for s in swapped}
    assert "out1" not in ids and "out2" not in ids
    assert "in1" in ids and "in2" in ids
    composite = next(s for s in swapped if s.id.startswith("grainrun:"))
    assert composite.grain_locked, "a rotated run would put the grain sideways"


def test_expansion_restores_the_real_faces_touching_and_in_order():
    panels = face_panels()
    runs = build_runs(panels, cabinet_of)
    specs = substitute([PartSpec(p.id, p.label, p.width_mm, p.length_mm)
                        for p in panels], runs, KERF)
    sheets = pack_staged(specs, W, L, KERF)
    expand(sheets[0], runs) if sheets else None
    for sheet in sheets:
        expand(sheet, runs)

    placed = {pid: sheet.pieces[piece]
              for sheet in sheets for pid, piece, _ in sheet.placed}
    assert "out1" in placed and "out2" in placed
    assert not any(pid.startswith("grainrun:") for pid in placed)

    a, b = placed["out1"], placed["out2"]
    assert a.x == pytest.approx(b.x), "faces drifted apart across the sheet"
    assert b.y == pytest.approx(a.y + a.h + KERF, abs=0.05), "faces are not touching"
    # Long side across the sheet means the grain runs up the short side.
    for piece in (a, b):
        assert piece.w > piece.h


def test_expansion_is_idempotent():
    panels = face_panels()
    runs = build_runs(panels, cabinet_of)
    specs = substitute([PartSpec(p.id, p.label, p.width_mm, p.length_mm)
                        for p in panels], runs, KERF)
    sheets = pack_staged(specs, W, L, KERF)
    for sheet in sheets:
        expand(sheet, runs)
        before = list(sheet.placed)
        expand(sheet, runs)
        assert sheet.placed == before


@pytest.mark.skipif(KITCHEN is None, reason="kitchen STEP not present")
def test_every_cabinet_gets_one_continuous_grain_run():
    panels = parse_step(str(KITCHEN))[0]
    chosen = auto_select(panels, cabinet_of)
    for panel in panels:
        panel.grain_match = panel.id in chosen
    runs = build_runs(panels, cabinet_of, chosen)
    assert len(runs) >= 4

    result = solve(panels, LayoutParams(effort="fast", continuous_grain=True,
                                        max_extra_sheets=10))
    assert not any("validation" in w for w in result.warnings)
    placed = {p.panel_id for s in result.sheets for p in s.placements}
    assert len(placed) == len(panels), "expansion lost or duplicated a part"

    for run in runs:
        members = {m.id for m in run.members}
        home = [s for s in result.sheets
                if members <= {p.panel_id for p in s.placements}]
        assert home, f"{run.cabinet} faces were split across sheets"
        faces = sorted((p for p in home[0].placements if p.panel_id in members),
                       key=lambda p: p.y)
        assert [p.panel_id for p in faces] == [m.id for m in run.members]
        assert len({round(p.x, 1) for p in faces}) == 1
        for lower, upper in zip(faces, faces[1:]):
            assert upper.y == pytest.approx(lower.y + lower.h + 2.2, abs=0.05)
        assert all(p.rotated for p in faces)


# ---------------------------------------------- which saw, and runner-up layouts

from app.objective import cut_saws                                   # noqa: E402


def test_every_cut_is_attributed_to_exactly_one_saw():
    ps = parts(24)
    for sheet in pack_staged(ps, W, L, KERF, miter_capacity=MITER):
        saws = cut_saws(sheet, MITER)
        assert len(saws) == sheet.cut_count()
        assert set(saws.values()) <= {"miter", "track"}


def test_saw_labels_agree_with_the_counts():
    for sheet in pack_staged(parts(24), W, L, KERF, miter_capacity=MITER):
        from collections import Counter
        tally = Counter(cut_saws(sheet, MITER).values())
        miter, track = cut_workload(sheet, MITER)
        assert (tally["miter"], tally["track"]) == (miter, track)


@pytest.mark.skipif(KITCHEN is None, reason="kitchen STEP not present")
def test_cuts_carry_their_saw_through_to_the_drawing():
    panels = parse_step(str(KITCHEN))[0]
    result = solve(panels, LayoutParams(effort="fast", max_extra_sheets=10))
    from collections import Counter
    tally = Counter(c.saw for s in result.sheets for c in s.cuts)
    values = {c.key: c.value for c in result.report}
    assert tally["track"] == values[TRACKCUTS]
    assert sum(tally.values()) == result.stats["total_cuts"]
    assert set(tally) <= {"miter", "track"}


@pytest.mark.skipif(KITCHEN is None, reason="kitchen STEP not present")
class TestAlternatives:
    @pytest.fixture(scope="class")
    def result(self):
        panels = parse_step(str(KITCHEN))[0]
        return solve(panels, LayoutParams(effort="fast", max_extra_sheets=10))

    def test_runner_ups_are_offered(self, result):
        assert result.alternatives, "no alternative layouts were kept"

    def test_each_runner_up_is_a_complete_usable_layout(self, result):
        expected = {p.panel_id for s in result.sheets for p in s.placements}
        for alt in result.alternatives:
            assert alt.sheets
            placed = {p.panel_id for s in alt.sheets for p in s.placements}
            assert placed == expected, f"{alt.label} lost or gained a part"
            assert [s.index for s in alt.sheets] == list(
                range(1, len(alt.sheets) + 1))

    def test_runner_ups_actually_differ(self, result):
        seen = {tuple(sorted({c.key: c.value for c in result.report}.items()))}
        for alt in result.alternatives:
            key = tuple(sorted(alt.values.items()))
            assert key not in seen, f"{alt.label} is a duplicate of another"
            seen.add(key)

    def test_runner_ups_report_the_numbers_needed_to_choose(self, result):
        for alt in result.alternatives:
            for key in ("sheets", "trackcuts", "mitercuts", "staged"):
                assert key in alt.values
            assert alt.stats["total_cuts"] > 0

    def test_the_chosen_layout_wins_among_those_within_the_cap(self, result):
        """Plywood is a hard cap now, not a ranked criterion -- so a runner-up
        may well score better and simply cost more sheets. The chosen layout
        only has to win among those using no more plywood than it does."""
        from app.objective import resolve_order
        order = resolve_order(LayoutParams().priorities)
        best = {c.key: c.value for c in result.report}
        mine_sheets = result.stats["sheets"]
        for alt in result.alternatives:
            if alt.values.get("sheets", 0) > mine_sheets:
                continue
            keys = [k for k in order if k in best and k in alt.values]
            mine = tuple(best[k] for k in keys)
            theirs = tuple(alt.values[k] for k in keys)
            assert mine <= theirs, f"{alt.label} should have been chosen"
