"""Bill of materials: how many pieces of each size, per plywood thickness."""
from __future__ import annotations

from .models import BomRow, Panel, ThicknessGroup

ROUND_MM = 2   # collapse sub-0.01mm modelling noise so identical parts group


def build_bom(panels: list[Panel], groups: list[ThicknessGroup]) -> list[BomRow]:
    by_id = {p.id: p for p in panels}
    rows: list[BomRow] = []

    for group in groups:
        buckets: dict[tuple[float, float], list[Panel]] = {}
        for pid in group.panel_ids:
            panel = by_id[pid]
            key = (round(panel.width_mm, ROUND_MM), round(panel.length_mm, ROUND_MM))
            buckets.setdefault(key, []).append(panel)

        for (width, length), members in sorted(
                buckets.items(), key=lambda kv: -(kv[0][0] * kv[0][1])):
            rows.append(BomRow(
                group_id=group.id,
                thickness_mm=group.thickness_mm,
                nominal=group.nominal,
                width_mm=width,
                length_mm=length,
                qty=len(members),
                labels=[m.label for m in members],
            ))
    return rows
