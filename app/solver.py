"""Orchestration: panels in, sheet layouts out.

Parts are grouped by measured thickness first -- you cannot cut a 1/2" panel
from a 3/4" sheet -- and each group is nested independently onto its own stock.

The unit of work is a *task*: a set of parts that share a stock of sheets. Modes
differ in how tasks are formed and scored:

  material  one task per thickness; minimise sheets.
  cuts      one task per thickness; minimise sawing, sheets priced in.
  cabinets  one task per (thickness, cabinet); every sheet then belongs to
            exactly one cabinet, so nothing needs sorting between units.

`solve_streaming` yields the layout as the search improves it, and can keep
refining in the background after the first answer is on screen.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from math import ceil
from typing import Iterator

from .bom import build_bom
from .cuts import order_cuts
from .geometry import cluster_thicknesses
from .models import (LayoutParams, LayoutResult, Panel, Placement, Sheet,
                     ThicknessGroup)
from .nesting import (MODE_CABINETS, MODE_CUTS, MODE_MATERIAL, PartSpec,
                      SheetLayout, optimise_iter, separating_cuts)
from .units import nominal_thickness_label
from .validate import validate_all

EFFORT_BUDGET = {"fast": 2.0, "normal": 15.0, "thorough": 120.0}
FRAME_INTERVAL = 0.25       # seconds between animation frames
NO_FRAMES = float("inf")

FIRST_PASS_SEED = 12345
SECOND_PASS_SEED = 98765
BACKGROUND_QUIET = 120.0    # give up refining after this long with no gain


@dataclass
class Task:
    """One packing problem: parts that will share a stock of sheets."""
    id: str
    group: ThicknessGroup
    cabinet: str | None
    specs: list[PartSpec]
    labels: dict[str, str]
    area_bound: int = 1

    @property
    def title(self) -> str:
        stock = self.group.nominal or f"{self.group.thickness_mm:.1f} mm"
        return f"{stock} · {self.cabinet}" if self.cabinet else stock


def build_groups(panels: list[Panel]) -> list[ThicknessGroup]:
    included = [p for p in panels if p.included]
    if not included:
        return []
    clusters = cluster_thicknesses([p.thickness_mm for p in included])
    groups = []
    for n, idxs in enumerate(clusters):
        members = [included[i] for i in idxs]
        thickness = sum(m.thickness_mm for m in members) / len(members)
        groups.append(ThicknessGroup(
            id=f"g{n}",
            thickness_mm=round(thickness, 3),
            nominal=nominal_thickness_label(thickness),
            panel_ids=[m.id for m in members],
        ))
    return groups


def cabinet_of(panel: Panel) -> str:
    """Top-level assembly a part belongs to, or its own name if it sits at root."""
    return panel.path[0] if panel.path else "—"


def _build_sheets(layouts: list[SheetLayout], group_id: str,
                  params: LayoutParams, labels: dict[str, str],
                  trim: float, cabinets: dict[str, str] | None = None) -> list[Sheet]:
    """Convert raw packer output into API sheets, shifted by the edge trim."""
    sheets: list[Sheet] = []
    for n, layout in enumerate(layouts, start=1):
        cuts = order_cuts(layout, n, labels, params.units, cabinets)
        placements = []
        for part_id, piece_id, rotated in layout.placed:
            pc = layout.pieces[piece_id]
            placements.append(Placement(
                panel_id=part_id, label=labels.get(part_id, part_id),
                x=pc.x + trim, y=pc.y + trim, w=pc.w, h=pc.h, rotated=rotated,
            ))
        for c in cuts:
            c.x1 += trim; c.x2 += trim; c.y1 += trim; c.y2 += trim

        offcut = layout.largest_offcut()
        sheets.append(Sheet(
            index=n, group_id=group_id,
            width_mm=params.sheet_width_mm, length_mm=params.sheet_length_mm,
            placements=placements, cuts=cuts, used_area_mm2=layout.used_area(),
            largest_offcut=(round(offcut.w, 1), round(offcut.h, 1)) if offcut else None,
        ))
    return sheets


def _stats(sheets: list[Sheet], groups: list[ThicknessGroup],
           params: LayoutParams, trim: float,
           cabinets: dict[str, str] | None = None) -> dict:
    sheet_area = params.sheet_width_mm * params.sheet_length_mm
    total_area = sheet_area * len(sheets)
    used = sum(s.used_area_mm2 for s in sheets)

    single = 0
    if cabinets:
        for sheet in sheets:
            names = {cabinets.get(p.panel_id, "") for p in sheet.placements}
            if len(names) <= 1:
                single += 1

    return {
        "sheets": len(sheets),
        "sheets_by_group": {g.id: sum(1 for s in sheets if s.group_id == g.id)
                            for g in groups},
        "yield_pct": round(100.0 * used / total_area, 1) if total_area else 0.0,
        "total_cuts": sum(len(s.cuts) for s in sheets),
        "edge_trim_mm": trim,
        "single_cabinet_sheets": single,
        "separating_cuts": sum(1 for s in sheets for c in s.cuts if c.separates),
        "mode": params.mode,
    }


def solve_streaming(panels: list[Panel], params: LayoutParams,
                    frame_interval: float = FRAME_INTERVAL) -> Iterator[dict]:
    """Yield {"type": "progress"|"result", ...} frames as the search improves."""
    warnings: list[str] = []
    groups = build_groups(panels)

    def bail(message: str, keep: bool = True) -> dict:
        return {"type": "result", "payload": LayoutResult(
            sheets=[], bom=[], groups=groups if keep else [],
            stats={"sheets": 0}, warnings=[message]).model_dump()}

    if not groups:
        yield bail("No parts selected.", keep=False)
        return

    trim = params.edge_trim_mm
    usable_w = params.sheet_width_mm - 2 * trim
    usable_l = params.sheet_length_mm - 2 * trim
    if usable_w <= 0 or usable_l <= 0:
        yield bail("Edge trim is larger than the sheet.")
        return

    by_id = {p.id: p for p in panels}
    cabinets = {p.id: cabinet_of(p) for p in panels}
    score_mode = {"cuts": MODE_CUTS, "cabinets": MODE_CABINETS}.get(
        params.mode, MODE_MATERIAL)
    total_budget = EFFORT_BUDGET.get(params.effort, 15.0)
    started = time.perf_counter()

    # ---- form the tasks ----------------------------------------------------
    tasks: list[Task] = []
    for group in groups:
        members = [by_id[pid] for pid in group.panel_ids]
        oversized = [m for m in members
                     if m.width_mm > usable_w + 1e-6 or m.length_mm > usable_l + 1e-6]
        if oversized:
            warnings.append(
                f'{group.nominal or ""} {group.thickness_mm:.1f}mm: '
                + ", ".join(m.label for m in oversized)
                + " will not fit on a sheet at this size/trim; excluded."
            )
            drop = {id(m) for m in oversized}
            members = [m for m in members if id(m) not in drop]
        if not members:
            continue

        specs = [PartSpec(id=m.id, label=m.label, w=m.width_mm, h=m.length_mm,
                          grain_locked=m.grain_locked,
                          group_key=cabinets[m.id]) for m in members]
        tasks.append(Task(
            id=group.id, group=group, cabinet=None,
            specs=specs, labels={m.id: m.label for m in members},
            area_bound=max(1, ceil(sum(s.w * s.h for s in specs)
                                   / (usable_w * usable_l))),
        ))

    if not tasks:
        yield bail("Nothing left to lay out.")
        return

    # task id -> (layouts, score, order)
    # Only flag separating cuts when that is what the user asked to minimise;
    # otherwise every cut on a mixed sheet would light up and mean nothing.
    cabinet_arg = cabinets if params.mode == "cabinets" else None

    results: dict[str, tuple[list[SheetLayout], tuple, list[PartSpec]]] = {}
    weights = {t.id: max(len(t.specs), 1) for t in tasks}
    last_frame = 0.0

    def compose(live_task: Task | None = None,
                live_layouts: list[SheetLayout] | None = None) -> list[Sheet]:
        """Every task's best sheets, with one task's in-flight layout swapped in."""
        out: list[Sheet] = []
        for task in tasks:
            if live_task is not None and task.id == live_task.id:
                if live_layouts:
                    out.extend(_build_sheets(live_layouts, task.group.id, params,
                                         task.labels, trim, cabinet_arg))
            elif task.id in results:
                out.extend(_build_sheets(results[task.id][0], task.group.id, params,
                                         task.labels, trim, cabinet_arg))
        for i, sheet in enumerate(out, start=1):     # number sheets across all tasks
            sheet.index = i
            for c in sheet.cuts:
                c.sheet_index = i
        return out

    def run_task(task: Task, budget: float, seed: int, pass_no: int, warm=None):
        """Search one task, streaming frames. Keeps the result only if better."""
        nonlocal last_frame
        # Both capped rather than pure fractions of the budget: at two minutes a
        # plain fraction means sitting silent for half a minute after the last
        # improvement, which just makes a finished solve feel broken.
        settle = min(max(0.4, budget * 0.15), 5.0)
        hard_stall = min(max(3.0, budget * 0.35), 12.0)

        best_layouts = best_score = best_order = None
        last_improve = time.perf_counter()
        dirty = False

        for state in optimise_iter(task.specs, usable_w, usable_l, params.kerf_mm,
                                   time_budget=budget, seed=seed, heartbeat=0.1,
                                   mode=score_mode, warm_start=warm,
                                   cabinets=cabinet_arg):
            if state.improved:
                best_layouts, best_score, best_order = state.sheets, state.score, state.order
                dirty = True
                last_improve = time.perf_counter()

            now = time.perf_counter()
            quiet = now - last_improve
            if best_score is not None and (
                    (best_score[0] <= task.area_bound and quiet >= settle)
                    or quiet >= hard_stall):
                break

            if now - last_frame < frame_interval:
                continue
            last_frame = now

            search = {
                "group": task.title,
                "group_index": tasks.index(task) + 1,
                "group_count": len(tasks),
                "attempts": state.attempts,
                "elapsed": round(now - started, 2),
                "budget": round(total_budget, 2),
                "pass": pass_no,
            }
            if dirty and best_layouts is not None:
                dirty = False
                partial = compose(task, best_layouts)
                yield {"type": "progress", "group_id": task.group.id,
                       "sheets": [s.model_dump() for s in partial],
                       "stats": _stats(partial, groups, params, trim, cabinets),
                       "search": search}
            else:
                yield {"type": "progress", "group_id": task.group.id, "search": search}

        if best_layouts is not None:
            previous = results.get(task.id)
            if previous is None or best_score < previous[1]:
                results[task.id] = (best_layouts, best_score, list(best_order or ()))
                return True
        return False

    # ---- pass 1: share the budget by part count ----------------------------
    remaining = total_budget
    remaining_weight = sum(weights.values())
    for task in tasks:
        budget = remaining * weights[task.id] / max(remaining_weight, 1)
        remaining_weight -= weights[task.id]
        began = time.perf_counter()
        yield from run_task(task, budget, FIRST_PASS_SEED, 1)
        remaining = max(0.0, remaining - (time.perf_counter() - began))

    # ---- pass 2: spend what is left on tasks still above their area bound ---
    leftover = total_budget - (time.perf_counter() - started)
    needy = [t for t in tasks
             if t.id in results and results[t.id][1][0] > t.area_bound]
    if leftover > 1.0 and needy:
        share = sum(weights[t.id] for t in needy)
        for task in needy:
            yield from run_task(task, leftover * weights[task.id] / share,
                                SECOND_PASS_SEED, 2)

    def finish(refined: bool) -> dict:
        errors: list[str] = []
        for task in tasks:
            if task.id not in results:
                errors.append(f"Could not lay out {task.title}.")
                continue
            errors.extend(validate_all(results[task.id][0], params.kerf_mm,
                                       {s.id for s in task.specs})[:2])
        final = compose()
        notes = list(warnings)
        if errors:
            notes.append("Layout validation failed: " + "; ".join(errors[:3]))
        if trim > 0:
            notes.append(f"Trim {trim:.1f} mm off all four edges of every sheet "
                         "before the numbered cuts.")
        return {"type": "result", "refined": refined, "payload": LayoutResult(
            sheets=final, bom=build_bom(panels, groups), groups=groups,
            stats=_stats(final, groups, params, trim, cabinets),
            warnings=notes).model_dump()}

    yield finish(refined=False)

    # ---- keep refining while the page stays open ---------------------------
    if params.background_seconds <= 0:
        return

    deadline = time.perf_counter() + params.background_seconds
    last_gain = time.perf_counter()
    round_no = 0
    while time.perf_counter() < deadline:
        if time.perf_counter() - last_gain > BACKGROUND_QUIET:
            return
        round_no += 1
        improved = False
        for task in tasks:
            if time.perf_counter() >= deadline:
                break
            warm = results[task.id][2] if task.id in results else None
            slice_budget = min(6.0, max(1.0, params.background_seconds / 20))
            # `yield from` re-yields the frames and evaluates to run_task's
            # return value, which says whether this task actually improved.
            got = yield from run_task(task, slice_budget,
                                      SECOND_PASS_SEED + round_no, 3, warm)
            improved = improved or got
        if improved:
            last_gain = time.perf_counter()
            yield finish(refined=True)


def solve(panels: list[Panel], params: LayoutParams) -> LayoutResult:
    """Final layout only, with no intermediate animation frames."""
    quiet = params.model_copy(update={"background_seconds": 0.0})
    frame = None
    for frame in solve_streaming(panels, quiet, frame_interval=NO_FRAMES):
        pass
    assert frame is not None and frame["type"] == "result"
    return LayoutResult(**frame["payload"])
