"""Continuous grain across a cabinet's drawer faces.

Plywood grain runs along the 8' axis of the sheet. For the faces of one cabinet
to read as a single continuous piece of timber once installed, they have to be
cut from one uninterrupted run of that grain: laid out touching, in installation
order, along the sheet's long axis.

That also fixes their orientation. The grain has to run *vertically* on an
installed face, so each face's short dimension lies along the sheet length and
its long dimension across the width -- the opposite of how the packer would
normally turn a wide, shallow part.

The whole run is handed to the packer as one composite part, which is the only
way to make the requirement hard rather than a preference, and expanded back
into the individual faces afterwards.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .models import Panel
from .nesting import PartSpec, SheetLayout

FACE_PATTERN = re.compile(r"\bface\b", re.IGNORECASE)
COMPOSITE_PREFIX = "grainrun:"


@dataclass
class FaceRun:
    """One cabinet's visible faces, to be cut from a single run of grain."""
    cabinet: str
    members: list[Panel]          # in installation order

    @property
    def id(self) -> str:
        return f"{COMPOSITE_PREFIX}{self.cabinet}"

    def across(self) -> float:
        """Extent across the sheet: every face shares this, being the same width."""
        return max(p.length_mm for p in self.members)

    def along(self, kerf: float) -> float:
        """Extent along the grain: the faces stacked, plus a saw kerf between."""
        return (sum(p.width_mm for p in self.members)
                + kerf * max(0, len(self.members) - 1))


def looks_like_face(panel: Panel) -> bool:
    return bool(FACE_PATTERN.search(panel.label or "")) or any(
        FACE_PATTERN.search(part) for part in panel.path)


def auto_select(panels: list[Panel], cabinet_of) -> set[str]:
    """Panel ids that look like the *visible* faces, one set per cabinet.

    A drawer usually carries two parts called "face": the sub-front fixed to the
    box, and the applied face you actually see, which is larger because it laps
    the opening. Only the outer one needs matching, so within each cabinet we
    keep the widest family and leave the rest to pack freely.
    """
    by_cabinet: dict[str, list[Panel]] = {}
    for panel in panels:
        if panel.included and looks_like_face(panel):
            by_cabinet.setdefault(cabinet_of(panel), []).append(panel)

    chosen: set[str] = set()
    for members in by_cabinet.values():
        widest = max(round(p.length_mm, 1) for p in members)
        chosen.update(p.id for p in members
                      if abs(round(p.length_mm, 1) - widest) <= 0.1)
    return chosen


def _sort_key(panel: Panel):
    """Installation order, from the drawer numbers in the part's path."""
    numbers = [int(n) for n in re.findall(r"\d+", " ".join(panel.path))]
    return (numbers, panel.label)


def build_runs(panels: list[Panel], cabinet_of,
               selected: set[str] | None = None) -> list[FaceRun]:
    """Group the chosen faces into one run per cabinet. Runs of one are dropped."""
    ids = selected if selected is not None else auto_select(panels, cabinet_of)
    grouped: dict[str, list[Panel]] = {}
    for panel in panels:
        if panel.included and panel.id in ids:
            grouped.setdefault(cabinet_of(panel), []).append(panel)

    runs = []
    for cabinet, members in sorted(grouped.items()):
        if len(members) < 2:
            continue          # nothing to keep continuous
        runs.append(FaceRun(cabinet, sorted(members, key=_sort_key)))
    return runs


def substitute(specs: list[PartSpec], runs: list[FaceRun],
               kerf: float) -> list[PartSpec]:
    """Swap each run's faces for one rigid composite the packer must place whole."""
    if not runs:
        return specs
    claimed = {p.id for run in runs for p in run.members}
    out = [s for s in specs if s.id not in claimed]
    for run in runs:
        out.append(PartSpec(
            id=run.id, label=f"{run.cabinet} faces",
            w=run.across(), h=run.along(kerf),
            # Never rotate: the grain direction is the whole point.
            grain_locked=True, group_key=run.cabinet,
        ))
    return out


def expand(layout: SheetLayout, runs: list[FaceRun]) -> None:
    """Replace each placed composite with its faces, crosscut in order.

    Rebuilds the placement list in one pass -- splicing in place would shift the
    positions of composites not yet reached. Idempotent: once expanded there are
    no composites left to match.

    The resulting run of crosscuts is pinned: reordering the bands of a run is
    free for the saw, but not here -- the order is what makes the grain continue
    across the installed faces.
    """
    by_id = {run.id: run for run in runs}
    rebuilt: list[tuple[str, int, bool]] = []

    for entry in layout.placed:
        part_id, piece_id, _ = entry
        run = by_id.get(part_id)
        if run is None:
            rebuilt.append(entry)
            continue

        layout.pieces[piece_id].part_id = None
        cursor = piece_id
        for panel in run.members:
            # The order these come off in *is* the requirement, so the
            # sequencer must not permute them like an ordinary run of bands.
            layout.pieces[cursor].pinned = True
            kept, rest = layout._cut(cursor, "H", panel.width_mm)
            layout.pieces[kept].part_id = panel.id
            # Rotated: the face's long side runs across the sheet, so the grain
            # runs up its short side.
            rebuilt.append((panel.id, kept, True))
            if rest is None:
                cursor = None
                break
            cursor = rest

        if cursor is not None:
            tail = layout.pieces[cursor]
            if tail.part_id is None and not tail.children:
                layout.free.append(cursor)

    layout.placed = rebuilt


def expand_all(layouts: list[SheetLayout], runs: list[FaceRun]) -> None:
    for layout in layouts:
        expand(layout, runs)
