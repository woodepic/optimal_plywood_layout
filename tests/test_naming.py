"""Instance-name normalisation and the shortest-unique-suffix labelling."""
import pytest

from app.models import Panel
from app.step_parser import assign_labels, prettify_name


@pytest.mark.parametrize("raw,expected", [
    ("Drawer <1>", "Drawer 1"),
    ("Drawer <12>", "Drawer 12"),
    ("Drawer Subassembly <2>", "Drawer 2"),
    ("Drawer Sub-assembly <3>", "Drawer 3"),
    ("Shelf Assembly <4>", "Shelf 4"),
    ("Top Stretcher", "Top Stretcher"),      # untouched
    ("Part <A>", "Part <A>"),                # only numeric instance markers
    ("  Spaced <5>  ", "Spaced 5"),
])
def test_prettify_name(raw, expected):
    assert prettify_name(raw) == expected


def panel(pid, path):
    return Panel(id=pid, path=path, label="", thickness_mm=18.0,
                 width_mm=100.0, length_mm=200.0)


def test_unique_names_are_left_short():
    panels = [panel("a", ["Top Stretcher"]), panel("b", ["Toe Kick"])]
    assign_labels(panels)
    assert [p.label for p in panels] == ["Top Stretcher", "Toe Kick"]


def test_ambiguous_names_grow_only_as_far_as_needed():
    panels = [
        panel("a", ["Bottom"]),                 # cannot grow -- stays short
        panel("b", ["Drawer 1", "Bottom"]),
        panel("c", ["Drawer 2", "Bottom"]),
    ]
    assign_labels(panels)
    labels = {p.id: p.label for p in panels}
    assert labels["a"] == "Bottom"
    assert labels["b"] == "Drawer 1 / Bottom"
    assert labels["c"] == "Drawer 2 / Bottom"
    assert len(set(labels.values())) == 3


def test_genuinely_identical_paths_are_numbered():
    panels = [panel("a", ["Unit", "Shelf"]), panel("b", ["Unit", "Shelf"])]
    assign_labels(panels)
    assert sorted(p.label for p in panels) == ["Unit / Shelf #1", "Unit / Shelf #2"]
