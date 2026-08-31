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

from . import bounds as bounds_mod
from .bom import build_bom
from .cuts import order_cuts
from .geometry import cluster_thicknesses
from .models import (CriterionReport, LayoutAlternative, LayoutParams,
                     LayoutResult, Panel, Placement, Sheet, StopRun,
                     ThicknessGroup)
from .nesting import PartSpec, SheetLayout, optimise_iter, separating_cuts
from .grain import build_runs, expand_all, substitute
from .regroup import regroup
from .staged import search_staged_iter
from .objective import (GROUPING, LABELS, SHEETS, STAGED, STOPCHANGES, measure,
                        resolve_order, score as rank_score, stop_runs)
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
    face_runs: list = field(default_factory=list)
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
                  trim: float, cabinets: dict[str, str] | None = None,
                  cut_order_preference: str = "stops") -> list[Sheet]:
    """Convert raw packer output into API sheets, shifted by the edge trim."""
    sheets: list[Sheet] = []
    for n, layout in enumerate(layouts, start=1):
        cuts = order_cuts(layout, n, labels, params.units, cabinets,
                          params.miter_capacity_mm, cut_order_preference)
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
        "priorities": list(params.priorities),
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
    order = resolve_order(params.priorities)
    # The cut list can be organised for the saw or for sorting parts, but not
    # both; follow whichever the ranking cares about more.
    cut_pref = ("cabinets"
                if GROUPING in order and STOPCHANGES in order
                and order.index(GROUPING) < order.index(STOPCHANGES)
                else "stops")
    sheet_floors: dict[str, int] = {}
    # Worth spending moves on shared cut measurements only if the user cares.
    align_offsets = STOPCHANGES in order[:3]
    group_aware = GROUPING in order[:2]
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

        # Each cabinet's faces become one rigid block the packer must place
        # whole; they are cut back apart once a layout is chosen.
        runs = []
        if params.continuous_grain:
            runs = build_runs(members, cabinet_of,
                              {m.id for m in members if m.grain_match})
            oversized = [r for r in runs
                         if r.across() > usable_w + 1e-6
                         or r.along(params.kerf_mm) > usable_l + 1e-6]
            if oversized:
                warnings.append(
                    "Continuous grain needs one uncut run per cabinet, and "
                    + ", ".join(r.cabinet for r in oversized)
                    + " will not fit on a sheet that way; those faces were left free.")
                runs = [r for r in runs if r not in oversized]
            specs = substitute(specs, runs, params.kerf_mm)

        tasks.append(Task(
            id=group.id, group=group, cabinet=None,
            specs=specs, labels={m.id: m.label for m in members},
            face_runs=runs,
            area_bound=max(1, ceil(sum(s.w * s.h for s in specs)
                                   / (usable_w * usable_l))),
        ))

    if not tasks:
        yield bail("Nothing left to lay out.")
        return

    # task id -> (layouts, score, order)
    # Only flag separating cuts when cabinet grouping is actually being
    # optimised; otherwise every cut on a mixed sheet lights up and means nothing.
    cabinet_arg = cabinets if group_aware else None

    results: dict[str, tuple[list[SheetLayout], tuple, list[PartSpec]]] = {}
    weights = {t.id: max(len(t.specs), 1) for t in tasks}
    last_frame = 0.0

    # Runner-up layouts per task, best first, one entry per distinct score.
    alternates: dict[str, list[tuple]] = {}
    ALTERNATE_POOL = 3

    caps: dict[str, int | None] = {}

    def cap_for(task: Task) -> int | None:
        """How many sheets this task may use right now.

        The allowance is shared across the whole job, not handed to each stock:
        giving every stock "floor + 1" let two of them take an extra sheet each
        and quietly overrun the cap. So a task gets whatever is left once the
        others' current usage is accounted for -- never less than its own floor,
        which is already known to be achievable.
        """
        # No cap until every floor is known -- pass zero is what discovers them.
        if len(sheet_floors) < len(tasks):
            return None
        # Unset means "the floor": plywood is always a constraint, so the default
        # is the fewest sheets proven achievable rather than no limit at all.
        limit = (params.max_sheets if params.max_sheets is not None
                 else sum(sheet_floors.values()))
        others = sum(
            len(results[t.id][0]) if t.id in results
            else sheet_floors.get(t.id, t.area_bound)
            for t in tasks if t.id != task.id)
        return max(sheet_floors.get(task.id, 1), limit - others)


    def remember(task_id: str, layouts, value) -> None:
        """Keep a few genuinely different good layouts, not near-duplicates.

        A layout over the plywood cap is not a worse option, it is not an option
        at all -- so it never enters the pool and never reaches the candidate
        list. Letting them in was how a 22-sheet layout surfaced under a 17-sheet
        cap.
        """
        cap = caps.get(task_id)
        if cap is not None and len(layouts) > cap:
            return
        pool = alternates.setdefault(task_id, [])
        if any(existing[1] == value for existing in pool):
            return
        pool.append((layouts, value))
        pool.sort(key=lambda entry: entry[1])
        del pool[ALTERNATE_POOL:]

    def compose(live_task: Task | None = None,
                live_layouts: list[SheetLayout] | None = None,
                choice: dict[str, list] | None = None) -> list[Sheet]:
        """Every task's best sheets, with one task's in-flight layout swapped in.

        `choice` overrides which layout each task contributes, which is how a
        runner-up combination is assembled.
        """
        out: list[Sheet] = []
        for task in tasks:
            if live_task is not None and task.id == live_task.id:
                if live_layouts:
                    out.extend(_build_sheets(live_layouts, task.group.id, params,
                                         task.labels, trim, cabinet_arg, cut_pref))
            elif choice is not None and task.id in choice:
                out.extend(_build_sheets(choice[task.id], task.group.id, params,
                                         task.labels, trim, cabinet_arg, cut_pref))
            elif task.id in results:
                out.extend(_build_sheets(results[task.id][0], task.group.id, params,
                                         task.labels, trim, cabinet_arg, cut_pref))
        for i, sheet in enumerate(out, start=1):     # number sheets across all tasks
            sheet.index = i
            for c in sheet.cuts:
                c.sheet_index = i
        return out

    def surrogate_rate(sheets):
        """Density-guided score used only while trying to shed a sheet.

        The search needs a gradient to follow, and consolidating free area into
        one big rectangle is what eventually lets everything fit on one sheet
        fewer. Ranking (say) total cuts second gives no such gradient, so a tight
        pack that only just fits is never found.
        """
        biggest = 0.0
        for sheet in sheets:
            off = sheet.largest_offcut()
            if off is not None:
                biggest = max(biggest, off.area)
        return (len(sheets), -biggest, sum(s.total_cut_length() for s in sheets))

    def ranked_rate(ceiling: int):
        """The user's actual ranking, held to a sheet count already achieved."""
        def rate(sheets):
            return (max(0, len(sheets) - ceiling),) + rank_score(
                sheets, params.priorities, cabinet_arg, params.miter_capacity_mm)
        return rate

    def run_task(task: Task, budget: float, seed: int, pass_no: int, warm=None,
                 rate=None):
        """Search one task, streaming frames. Keeps the result only if better."""
        nonlocal last_frame
        # Both capped rather than pure fractions of the budget: at two minutes a
        # plain fraction means sitting silent for half a minute after the last
        # improvement, which just makes a finished solve feel broken.
        settle = min(max(0.4, budget * 0.15), 5.0)
        hard_stall = min(max(3.0, budget * 0.35), 12.0)

        caps[task.id] = cap_for(task)

        best_layouts = best_score = best_order = None
        last_improve = time.perf_counter()
        dirty = False

        if rate is None:
            # A hard ceiling on plywood: ranking grouping above sheets lets the
            # search spend material, but never without limit.
            rate = ranked_rate(task.area_bound + max(0, params.max_extra_sheets))

        # A rip-first candidate competes on the same score. It wins when stages
        # or rip widths are ranked highly -- a store's panel saw will do long
        # rips for free but nothing else -- and loses on sheet count, which is
        # exactly the trade the ranking is supposed to decide.
        rip_share = 0.6 if order[0] in (STAGED, STOPCHANGES) else 0.15
        rip_layouts = rip_score = None
        rip_attempts = 0
        for layouts, value, improved in search_staged_iter(
                task.specs, usable_w, usable_l, params.kerf_mm, rate,
                time_budget=min(budget * rip_share, 8.0), seed=seed,
                min_offcut=params.min_offcut_mm,
                miter_capacity=params.miter_capacity_mm):
            rip_attempts += 1
            if improved:
                rip_layouts, rip_score = layouts, value
            now = time.perf_counter()
            if now - last_frame < frame_interval:
                continue
            # Claim the slot here: a telemetry-only frame is still a frame, and
            # not marking it let them fire on every heartbeat instead.
            last_frame = now
            search_now = {"group": task.title,
                          "group_index": tasks.index(task) + 1,
                          "group_count": len(tasks), "attempts": rip_attempts,
                          "elapsed": round(now - started, 2),
                          "budget": round(total_budget, 2), "pass": pass_no}

            # Never *draw* a candidate that breaks the plywood cap: it cannot be
            # chosen, and drawing it made the running total jump to a number the
            # user had explicitly ruled out. Still send telemetry, or the UI goes
            # silent for the whole rip search.
            cap_now = caps.get(task.id)
            if rip_layouts is None or (cap_now is not None
                                       and len(rip_layouts) > cap_now):
                yield {"type": "progress", "group_id": task.group.id,
                       "search": search_now}
                continue
            partial = compose(task, rip_layouts)
            stats = _stats(partial, groups, params, trim, cabinets)
            pending = sum(sheet_floors.get(t.id, t.area_bound) for t in tasks
                          if t.id != task.id and t.id not in results)
            if pending:
                stats["sheets"] += pending
                stats["projected"] = True
            yield {"type": "progress", "group_id": task.group.id,
                   "sheets": [sh.model_dump() for sh in partial],
                   "stats": stats,
                   "search": {"group": task.title,
                              "group_index": tasks.index(task) + 1,
                              "group_count": len(tasks),
                              "attempts": rip_attempts,
                              "elapsed": round(now - started, 2),
                              "budget": round(total_budget, 2), "pass": pass_no}}
        budget = max(0.3, budget * (1.0 - rip_share))

        for state in optimise_iter(task.specs, usable_w, usable_l, params.kerf_mm,
                                   time_budget=budget, seed=seed, heartbeat=0.1,
                                   warm_start=warm, cabinets=cabinet_arg,
                                   scorer=rate, group_aware=group_aware,
                                   min_offcut=params.min_offcut_mm,
                                   align_offsets=align_offsets):
            if state.improved:
                best_layouts, best_score, best_order = state.sheets, state.score, state.order
                dirty = True
                last_improve = time.perf_counter()

            now = time.perf_counter()
            quiet = now - last_improve
            settled = False
            if best_layouts is not None and quiet >= settle:
                top = order[0]
                got = measure(best_layouts, cabinet_arg,
                              params.miter_capacity_mm)[top]
                floor = bounds_mod.compute(task.specs, usable_w, usable_l,
                                           len(best_layouts),
                                           params.miter_capacity_mm).get(top)
                settled = floor is not None and got <= floor
            if settled or (best_score is not None and quiet >= hard_stall):
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
                stats = _stats(partial, groups, params, trim, cabinets)
                # Tasks not yet packed contribute nothing to `partial`, so the
                # raw count starts at one stock's worth and leaps when the next
                # finishes -- "best so far: 4 sheets" for a 17-sheet job. Add
                # the pending stocks' floors so the number converges instead.
                pending = sum(sheet_floors.get(t.id, t.area_bound) for t in tasks
                              if t.id != task.id and t.id not in results)
                if pending:
                    stats["sheets"] += pending
                    stats["projected"] = True
                yield {"type": "progress", "group_id": task.group.id,
                       "sheets": [s.model_dump() for s in partial],
                       "stats": stats, "search": search}
            else:
                yield {"type": "progress", "group_id": task.group.id, "search": search}

        # Free and never changes the parts, the sheet or the cut count: it only
        # puts equal-width sections next to each other so their cuts can be made
        # without moving the stop.
        for candidate in (best_layouts, rip_layouts):
            if candidate is not None:
                regroup(candidate)

        # Expansion is idempotent, so both candidates can be made real before
        # either is judged or stashed as a runner-up.
        if task.face_runs:
            for candidate in (best_layouts, rip_layouts):
                if candidate is not None:
                    expand_all(candidate, task.face_runs)

        # Keep the rip-first layout as an alternative even when it loses on the
        # ranked score: it is the one to hand a store's panel saw, which the
        # ranking cannot express.
        cap = caps.get(task.id)
        over = lambda lay: cap is not None and len(lay) > cap

        if rip_layouts is not None and not over(rip_layouts):
            remember(task.id, rip_layouts, rip_score)
            if best_score is None or rip_score < best_score or over(best_layouts or []):
                best_layouts, best_score, best_order = rip_layouts, rip_score, []

        if best_layouts is not None and not over(best_layouts):
            remember(task.id, best_layouts, best_score)

        if best_layouts is not None and not over(best_layouts):
            known = sheet_floors.get(task.id)
            if known is None or len(best_layouts) < known:
                sheet_floors[task.id] = len(best_layouts)
            previous = results.get(task.id)
            if previous is None or best_score < previous[1]:
                results[task.id] = (best_layouts, best_score, list(best_order or ()))
                return True
        return False

    # ---- pass 0: the tightest pack, which sets the plywood floor -----------
    # Every task's floor has to be known before any cap can be shared out, so
    # this runs to completion before the ranked search starts.
    # Finding the floor converges fast, so cap it. Without this, "open a file"
    # inherited 22% of a two-minute budget and took half a minute.
    # Opening a file wants a quick estimate; a real run can afford to look
    # harder, because the floor it finds becomes the cap everything else is
    # held to. Six seconds is not enough on a 200-part job -- it settles a
    # sheet high.
    floor_budget = (min(total_budget * 0.22, 6.0) if params.floor_only
                    else min(total_budget * 0.35, 30.0))
    for task in tasks:
        share = floor_budget * weights[task.id] / max(sum(weights.values()), 1)
        yield from run_task(task, share, FIRST_PASS_SEED, 1, rate=surrogate_rate)
        sheet_floors[task.id] = (len(results[task.id][0]) if task.id in results
                                 else task.area_bound)

    global_floor = sum(sheet_floors.values())
    allowance = (max(0, params.max_sheets - global_floor)
                 if params.max_sheets is not None else 0)

    # ---- pass 1: optimise what was actually ranked, held to that cap -------
    # `floor_only` stops here: enough to say how much plywood the job needs,
    # without spending the rest of the budget before the user has said what
    # matters to them.
    remaining = total_budget * 0.78
    remaining_weight = sum(weights.values())
    for task in ([] if params.floor_only else tasks):
        budget = remaining * weights[task.id] / max(remaining_weight, 1)
        remaining_weight -= weights[task.id]
        began = time.perf_counter()
        warm = results[task.id][2] if task.id in results else None
        yield from run_task(task, budget, FIRST_PASS_SEED + 7, 1, warm=warm,
                            rate=ranked_rate(cap_for(task) or sheet_floors[task.id]))
        remaining = max(0.0, remaining - (time.perf_counter() - began))

    # ---- pass 2: spend what is left on tasks still above their area bound ---
    leftover = total_budget - (time.perf_counter() - started)
    needy = [t for t in tasks
             if t.id in results and results[t.id][1][0] > t.area_bound]
    if not params.floor_only and leftover > 1.0 and needy:
        share = sum(weights[t.id] for t in needy)
        for task in needy:
            yield from run_task(task, leftover * weights[task.id] / share,
                                SECOND_PASS_SEED, 2,
                                rate=ranked_rate(len(results[task.id][0])))

    def build_report() -> list[CriterionReport]:
        """Each criterion's achieved value against its certified lower bound.

        A bound is what no layout can beat, so meeting one is a proof of
        optimality for that criterion. Falling short of one proves nothing on its
        own -- guillotine cutting is NP-hard and the bound may be unreachable --
        so the gap is shown rather than any claim about it.
        """
        totals: dict[str, float] = {}
        floors: dict[str, float] = {}
        for task in tasks:
            if task.id not in results:
                continue
            layouts = results[task.id][0]
            for key, value in measure(layouts, cabinets,
                                      params.miter_capacity_mm).items():
                totals[key] = totals.get(key, 0.0) + value
            for key, value in bounds_mod.compute(task.specs, usable_w, usable_l,
                                                 len(layouts),
                                                 params.miter_capacity_mm).items():
                if value == float("-inf"):
                    continue
                floors[key] = floors.get(key, 0.0) + value

        ranking = {key: i + 1 for i, key in enumerate(order)}
        out = []
        for key in order:
            if key not in totals:
                continue
            floor = floors.get(key)
            out.append(CriterionReport(
                key=key, label=LABELS.get(key, key),
                value=round(totals[key], 2),
                bound=None if floor is None else round(floor, 2),
                optimal=floor is not None and totals[key] <= floor + 1e-9,
                rank=ranking.get(key),
            ))
        return out

    def build_stop_plan() -> list[StopRun]:
        """Every run of track cuts that shares one stop setting, in order.

        This is the list to work from at the saw: set the stop once per row and
        make that many cuts before touching it again.
        """
        layouts = [lay for task in tasks if task.id in results
                   for lay in results[task.id][0]]
        return [StopRun(index=i, width_mm=width, count=count)
                for i, (width, count) in enumerate(
                    stop_runs(layouts, params.miter_capacity_mm), start=1)]

    def build_alternatives() -> list[LayoutAlternative]:
        """The next-best whole solutions that still fit inside the plywood cap.

        Combinations are enumerated across the per-task pools and scored as
        complete layouts, so these really are runners-up to the chosen one --
        not "each task's second-best", which could add up to something nobody
        would pick, and could exceed the cap.
        """
        import itertools

        pools = [[(task, entry) for entry in alternates.get(task.id, [])]
                 for task in tasks if alternates.get(task.id)]
        if not pools:
            return []

        cap = (params.max_sheets if params.max_sheets is not None
               else sum(sheet_floors.values()))
        scored: list[tuple[tuple, dict[str, list]]] = []
        for combo in itertools.islice(itertools.product(*pools), 64):
            choice = {task.id: entry[0] for task, entry in combo}
            total = sum(len(entry[0]) for _, entry in combo)
            if cap is not None and total > cap:
                continue
            values: dict[str, float] = {}
            for task, entry in combo:
                for key, value in measure(entry[0], cabinet_arg,
                                          params.miter_capacity_mm).items():
                    values[key] = values.get(key, 0.0) + value
            key = tuple(values.get(c, 0.0) for c in order)
            scored.append((key, choice, values, total))

        scored.sort(key=lambda row: row[0])
        chosen = {task.id: results[task.id][0] for task in tasks if task.id in results}

        out: list[LayoutAlternative] = []
        seen: set[tuple] = set()
        for _, choice, values, total in scored:
            if choice == chosen:
                continue                       # that is the chosen layout
            # Two candidates are the same option if they trade off identically
            # on the ranked criteria; tiny offcut differences do not count.
            signature = tuple(round(values.get(c, 0.0), 2) for c in order)
            if signature in seen:
                continue
            seen.add(signature)
            sheets_alt = compose(choice=choice)
            values["sheets"] = total
            out.append(LayoutAlternative(
                label=f"Candidate {len(out) + 2}",
                sheets=sheets_alt,
                stats=_stats(sheets_alt, groups, params, trim, cabinets),
                values=values))
            if len(out) >= 2:
                break
        return out

    def finish(refined: bool) -> dict:
        errors: list[str] = []
        for task in tasks:
            if task.id not in results:
                errors.append(f"Could not lay out {task.title}.")
                continue
            # Composites have been cut back into real faces by now, so the
            # layout must be checked against those, not the placeholder.
            expected = {s.id for s in task.specs}
            for run in task.face_runs:
                expected.discard(run.id)
                expected.update(m.id for m in run.members)
            errors.extend(validate_all(results[task.id][0], params.kerf_mm,
                                       expected)[:2])
        final = compose()
        notes = list(warnings)
        if errors:
            notes.append("Layout validation failed: " + "; ".join(errors[:3]))
        if trim > 0:
            notes.append(f"Trim {trim:.1f} mm off all four edges of every sheet "
                         "before the numbered cuts.")
        return {"type": "result", "refined": refined, "payload": LayoutResult(
            stop_plan=build_stop_plan(),
            # The floor is the fewest sheets anything has actually achieved, not
            # whatever the opening pass happened to find. A later pass that packs
            # tighter lowers it, rather than leaving a cap the user cannot reach.
            sheet_floor=min(sum(sheet_floors.values()),
                            len(final) if final else sum(sheet_floors.values())),
            sheet_bound=sum(t.area_bound for t in tasks),
            alternatives=build_alternatives(),
            sheets=final, bom=build_bom(panels, groups), groups=groups,
            stats=_stats(final, groups, params, trim, cabinets),
            report=build_report(), warnings=notes).model_dump()}

    yield finish(refined=False)

    # ---- keep refining while the page stays open ---------------------------
    if params.background_seconds <= 0 or params.floor_only:
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
            ceiling = (len(results[task.id][0]) if task.id in results
                       else task.area_bound + max(0, params.max_extra_sheets))
            got = yield from run_task(task, slice_budget,
                                      SECOND_PASS_SEED + round_no, 3, warm,
                                      rate=ranked_rate(ceiling))
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
