"""The plywood cap is a hard constraint, not a preference.

Two ways it leaked before: runner-up layouts were kept without checking it, and
the allowance was handed to every stock instead of shared, so two stocks could
each take an extra sheet.
"""
import pytest
from conftest import CABINET, find_step

from app.models import LayoutParams
from app.solver import solve
from app.step_parser import parse_step

V3 = find_step("V3") or find_step("Kitchen")
RANKINGS = [
    ["trackcuts", "staged", "stopchanges"],
    ["stopchanges", "trackcuts", "staged"],
    ["staged", "trackcuts"],
]


@pytest.mark.skipif(V3 is None, reason="no large kitchen STEP present")
class TestHardCap:
    @pytest.fixture(scope="class")
    def panels(self):
        return parse_step(str(V3))[0]

    @pytest.fixture(scope="class")
    def floor(self, panels):
        return solve(panels, LayoutParams(effort="fast")).sheet_floor

    def test_the_floor_is_reported_and_achieved(self, panels, floor):
        assert floor > 0
        result = solve(panels, LayoutParams(effort="fast"))
        assert result.stats["sheets"] <= floor

    @pytest.mark.parametrize("extra", [0, 1, 2])
    @pytest.mark.parametrize("priorities", RANKINGS)
    def test_the_chosen_layout_never_exceeds_the_cap(self, panels, floor,
                                                    extra, priorities):
        cap = floor + extra
        result = solve(panels, LayoutParams(effort="fast", max_sheets=cap,
                                           priorities=priorities))
        assert result.stats["sheets"] <= cap

    @pytest.mark.parametrize("extra", [0, 1, 2])
    def test_no_candidate_exceeds_the_cap(self, panels, floor, extra):
        """A layout over the cap is not a worse option, it is not an option."""
        cap = floor + extra
        result = solve(panels, LayoutParams(effort="fast", max_sheets=cap))
        for alt in result.alternatives:
            assert alt.values["sheets"] <= cap, f"{alt.label} used {alt.values['sheets']}"
            assert alt.stats["sheets"] <= cap

    def test_an_unset_cap_means_the_floor_not_unlimited(self, panels, floor):
        """Plywood is always a constraint, so the default ceiling is the floor."""
        result = solve(panels, LayoutParams(effort="fast"))
        assert result.stats["sheets"] <= floor
        for alt in result.alternatives:
            assert alt.values["sheets"] <= floor

    def test_the_allowance_is_shared_not_per_stock(self, panels, floor):
        """Handing every stock 'floor + 1' let the total reach floor + 2."""
        result = solve(panels, LayoutParams(effort="fast", max_sheets=floor + 1))
        assert result.stats["sheets"] <= floor + 1

    def test_candidates_are_distinct_trade_offs(self, panels, floor):
        result = solve(panels, LayoutParams(effort="fast", max_sheets=floor + 2))
        keys = ("trackcuts", "staged", "stopchanges", "mitercuts")
        seen = {tuple(round(v.value, 2) for v in result.report if v.key in keys)}
        for alt in result.alternatives:
            sig = tuple(round(alt.values[k], 2) for k in keys if k in alt.values)
            assert sig not in seen, f"{alt.label} duplicates another candidate"
            seen.add(sig)

    def test_candidates_are_complete_layouts(self, panels, floor):
        result = solve(panels, LayoutParams(effort="fast", max_sheets=floor + 1))
        expected = {p.panel_id for s in result.sheets for p in s.placements}
        for alt in result.alternatives:
            placed = {p.panel_id for s in alt.sheets for p in s.placements}
            assert placed == expected, f"{alt.label} lost a part"


@pytest.mark.skipif(CABINET is None, reason="cabinet STEP not present")
def test_a_cap_below_the_floor_is_not_honoured_silently():
    """Asking for fewer sheets than possible cannot be granted; the layout must
    still be complete rather than dropping parts to fit."""
    panels = parse_step(str(CABINET))[0]
    floor = solve(panels, LayoutParams(effort="fast")).sheet_floor
    result = solve(panels, LayoutParams(effort="fast", max_sheets=max(1, floor - 1)))
    placed = {p.panel_id for s in result.sheets for p in s.placements}
    assert len(placed) == len([p for p in panels if p.included])
