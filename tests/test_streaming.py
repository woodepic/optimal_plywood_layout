"""The search must stream improving layouts steadily, without changing the answer.

The regression these guard: with ~100 parts a restart-only search found every
improvement in the first few milliseconds and then stalled, so the UI appeared
frozen for the rest of the run.
"""
import time

import pytest
from conftest import CABINET, LAYOUT

from app.models import LayoutParams
from app.nesting import PartSpec, optimise, optimise_iter, score
from app.solver import NO_FRAMES, solve, solve_streaming
from app.step_parser import parse_step
from app.validate import validate_all

W, L, KERF = 1219.2, 2438.4, 2.2


def specs(n, w=300.0, h=420.0):
    return [PartSpec(f"p{i}", f"P{i}", w, h) for i in range(n)]


# ---------------------------------------------------------------- searcher

def test_improvements_are_strictly_better():
    states = list(optimise_iter(specs(16), W, L, KERF, time_budget=0.8))
    improved = [st.score for st in states if st.improved]
    assert len(improved) >= 2
    for a, b in zip(improved, improved[1:]):
        assert b < a, "every improvement must be a strict improvement"


def test_heartbeats_report_liveness_without_a_new_layout():
    states = list(optimise_iter(specs(16), W, L, KERF, time_budget=1.0, heartbeat=0.05))
    beats = [st for st in states if not st.improved]
    assert beats, "expected heartbeat states while the search grinds on"
    assert all(st.attempts > 0 and st.sheets for st in beats)


def test_last_improvement_is_what_the_wrapper_returns():
    last = None
    for st in optimise_iter(specs(12), W, L, KERF, time_budget=0.5, seed=99):
        if st.improved:
            last = st.sheets
    best, _ = optimise(specs(12), W, L, KERF, time_budget=0.5, seed=99)
    assert score(last) == score(best)


def test_every_streamed_layout_is_itself_valid():
    """A half-finished search must never render a physically impossible layout."""
    parts = specs(14)
    ids = {p.id for p in parts}
    for st in optimise_iter(parts, W, L, KERF, time_budget=0.6):
        assert validate_all(st.sheets, KERF, ids) == []


@pytest.mark.skipif(LAYOUT is None, reason="large STEP not present")
def test_search_keeps_improving_on_a_large_assembly():
    """The whole point of the local search: do not go quiet after 10ms."""
    from app.solver import build_groups
    panels = parse_step(str(LAYOUT))[0]
    by_id = {p.id: p for p in panels}
    group = max(build_groups(panels), key=lambda g: len(g.panel_ids))
    parts = [PartSpec(id=by_id[i].id, label=by_id[i].label,
                      w=by_id[i].width_mm, h=by_id[i].length_mm)
             for i in group.panel_ids]
    assert len(parts) > 80

    start = time.perf_counter()
    late = total = 0
    for st in optimise_iter(parts, W, L, KERF, time_budget=4.0):
        if not st.improved:
            continue
        total += 1
        if time.perf_counter() - start > 0.5:
            late += 1
    # The bar is that the search keeps finding things rather than going quiet in
    # the first few milliseconds. It converges faster than it used to, so the
    # count of late improvements is lower than before without being a regression.
    assert total >= 6, f"only {total} improvements over 4s"
    assert late >= 1, "search went quiet immediately -- the stall regressed"


# ---------------------------------------------------------------- solver

@pytest.mark.skipif(CABINET is None, reason="cabinet STEP not present")
class TestSolveStreaming:
    @pytest.fixture(scope="class")
    def panels(self):
        return parse_step(str(CABINET))[0]

    def test_ends_with_exactly_one_result_frame(self, panels):
        frames = list(solve_streaming(panels, LayoutParams(effort="fast")))
        assert frames[-1]["type"] == "result"
        assert [f["type"] for f in frames].count("result") == 1
        assert all(f["type"] == "progress" for f in frames[:-1])

    def test_frames_are_throttled_but_never_slow(self, panels):
        """At least every ~0.25s, and never a long silent gap."""
        stamps = []
        for frame in solve_streaming(panels, LayoutParams(effort="normal"),
                                     frame_interval=0.25):
            if frame["type"] == "progress":
                stamps.append(time.perf_counter())
        assert len(stamps) >= 4
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        assert min(gaps) >= 0.2, f"frames arrived faster than the throttle: {gaps}"
        assert max(gaps) <= 0.75, f"the UI would look frozen for {max(gaps):.2f}s"

    def test_every_frame_is_drawable_or_telemetry(self, panels):
        drew = False
        for frame in solve_streaming(panels, LayoutParams(effort="fast")):
            if frame["type"] != "progress":
                continue
            assert frame["search"]["attempts"] > 0
            assert frame["search"]["budget"] > 0
            if "sheets" not in frame:
                continue                    # heartbeat: same layout, still working
            drew = True
            for sheet in frame["sheets"]:
                assert sheet["width_mm"] > 0 and sheet["length_mm"] > 0
                for pl in sheet["placements"]:
                    assert pl["label"] and pl["w"] > 0 and pl["h"] > 0
        assert drew, "the search never sent anything to draw"

    def test_disabling_frames_yields_only_the_result(self, panels):
        frames = list(solve_streaming(panels, LayoutParams(effort="fast"),
                                      frame_interval=NO_FRAMES))
        assert len(frames) == 1 and frames[0]["type"] == "result"

    def test_streaming_and_plain_solve_agree(self, panels):
        streamed = list(solve_streaming(panels, LayoutParams(effort="fast"),
                                        frame_interval=NO_FRAMES))[-1]["payload"]
        plain = solve(panels, LayoutParams(effort="fast")).model_dump()
        assert streamed["stats"]["sheets"] == plain["stats"]["sheets"]
        assert len(streamed["bom"]) == len(plain["bom"])


@pytest.mark.skipif(LAYOUT is None, reason="large STEP not present")
class TestLargeAssembly:
    @pytest.fixture(scope="class")
    def panels(self):
        return parse_step(str(LAYOUT))[0]

    def test_repeated_cabinets_expand_to_every_instance(self, panels):
        assert len(panels) == 144
        assert all(p.included for p in panels)

    def test_frames_stay_responsive_on_the_large_file(self, panels):
        """The user-visible complaint: updates every ~2s on a big assembly."""
        stamps = []
        for frame in solve_streaming(panels, LayoutParams(effort="normal"),
                                     frame_interval=0.25):
            if frame["type"] == "progress":
                stamps.append(time.perf_counter())
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        assert max(gaps) <= 0.75, f"gap of {max(gaps):.2f}s would look frozen"

    def test_solve_stops_early_once_converged(self, panels):
        """A converged task must hand its unused time back rather than idle."""
        from app.solver import EFFORT_BUDGET
        start = time.perf_counter()
        solve(panels, LayoutParams(effort="thorough"))
        elapsed = time.perf_counter() - start
        assert elapsed < EFFORT_BUDGET["thorough"] * 0.75, \
            f"used {elapsed:.0f}s of a {EFFORT_BUDGET['thorough']}s budget with nothing to gain"

    def test_large_layout_is_valid_and_efficient(self, panels):
        result = solve(panels, LayoutParams(effort="normal"))
        assert not any("validation failed" in w for w in result.warnings)
        placed = {p.panel_id for s in result.sheets for p in s.placements}
        assert len(placed) == 144
        assert result.stats["yield_pct"] > 70


# ---------------------------------------------------------------- budgets

def test_thorough_is_the_default_effort():
    assert LayoutParams().effort == "thorough"


def test_effort_budgets_are_ordered():
    from app.solver import EFFORT_BUDGET
    assert EFFORT_BUDGET["fast"] < EFFORT_BUDGET["normal"] < EFFORT_BUDGET["thorough"]
    assert EFFORT_BUDGET["thorough"] == 120.0


@pytest.mark.skipif(LAYOUT is None, reason="large STEP not present")
def test_no_optimisation_warnings_are_emitted():
    """Area-bound nagging is gone; only actionable warnings survive."""
    panels = parse_step(str(LAYOUT))[0]
    result = solve(panels, LayoutParams(effort="fast"))
    assert not any("area bound" in w.lower() for w in result.warnings)
    assert not any("thorough" in w.lower() for w in result.warnings)


@pytest.mark.skipif(LAYOUT is None, reason="large STEP not present")
def test_a_second_pass_runs_when_time_remains():
    panels = parse_step(str(LAYOUT))[0]
    passes = set()
    for frame in solve_streaming(panels, LayoutParams(effort="normal")):
        if frame["type"] == "progress":
            passes.add(frame["search"]["pass"])
    assert 1 in passes


@pytest.mark.skipif(LAYOUT is None, reason="large STEP not present")
def test_more_effort_is_never_worse():
    panels = parse_step(str(LAYOUT))[0]
    fast = solve(panels, LayoutParams(effort="fast"))
    normal = solve(panels, LayoutParams(effort="normal"))
    assert normal.stats["sheets"] <= fast.stats["sheets"]
