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
from . import schedule as sched
from .bom import build_bom
from .cuts import order_cuts
from .geometry import cluster_thicknesses
from .models import (CriterionReport, LayoutAlternative, LayoutParams,
                     LayoutResult, Panel, Placement, Sheet, StopRun,
                     ThicknessGroup)
from .nesting import (DENSITY_HEURISTICS, PartSpec, SheetLayout, optimise_iter)
from .grain import build_runs, expand_all, substitute
from .staged import search_staged_iter
from .objective import (GROUPING, LABELS, STAGED, STOPCHANGES, measure,
                        resolve_order, score as rank_score, sheet_surrogates)
from .units import nominal_thickness_label
from .validate import validate_all

EFFORT_BUDGET = {"fast": 2.0, "normal": 15.0, "thorough": 120.0}
FRAME_INTERVAL = 0.25       # seconds between animation frames
NO_FRAMES = float("inf")

FIRST_PASS_SEED = 12345
SECOND_PASS_SEED = 98765
# Below this a stage cannot do anything but re-measure what it started with, so
# it is skipped and the time left to the stages that can still move.
STAGE_FLOOR = 0.25
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


def _sheet_model(layout: SheetLayout, index: int, group_id: str,
                 params: LayoutParams, labels: dict[str, str], trim: float,
                 cuts) -> Sheet:
    """One API sheet from one packed layout and its already-ordered cuts."""
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
    return Sheet(
        index=index, group_id=group_id,
        width_mm=params.sheet_width_mm, length_mm=params.sheet_length_mm,
        placements=placements, cuts=cuts, used_area_mm2=layout.used_area(),
        largest_offcut=(round(offcut.w, 1), round(offcut.h, 1)) if offcut else None,
    )


def schedule_job(entries: list[tuple], params: LayoutParams, trim: float,
                 cabinets: dict[str, str] | None = None,
                 cut_order_preference: str = "stops",
                 final: bool = False,
                 seed: int = FIRST_PASS_SEED) -> tuple[list[Sheet], "sched.Plan | None"]:
    """Order every cut in the whole job, then emit the sheets in that order.

    `entries` is [(group_id, labels, layout), ...] across every stock, because
    the stop setting does not care what thickness it is cutting: ending one
    sheet at 7-3/8" and starting the next at 7-3/8" is free whether or not the
    two are the same material. Scheduling the stocks separately would throw
    those joins away.

    The trees are rewritten in place to match -- free, since permuting the bands
    of a same-axis run moves no part and adds no cut. `final` additionally
    searches for the order to break the sheets down in, which is worth a few
    milliseconds once but not on every animation frame.
    """
    # Copies, because sequencing rewrites the tree and the search is still
    # holding these layouts alongside scores taken before the rewrite.
    layouts = [layout.copy() for _, _, layout in entries]
    if not layouts:
        return [], None

    job = sched.Job(layouts, params.miter_capacity_mm)
    plan = sched.plan_job(layouts, params.miter_capacity_mm,
                          reorder_sheets=final, job=job, seed=seed,
                          effort=2 if final else 1)
    sched.apply_plan(layouts, plan, job)
    sequence = sched.sequence_by_sheet(plan)
    settings = sched.settings_by_sheet(plan)
    entry = sched.entry_stops(plan)

    out: list[Sheet] = []
    group = 0
    for position, index in enumerate(plan.order, start=1):
        group_id, labels, _ = entries[index]
        layout = layouts[index]
        cuts = order_cuts(layout, position, labels, params.units, cabinets,
                          params.miter_capacity_mm, cut_order_preference,
                          start_width=entry.get(index),
                          sequence=sequence.get(index),
                          settings=settings.get(index), group_start=group)
        group = max([c.stop_group for c in cuts] or [group])
        out.append(_sheet_model(layout, position, group_id, params, labels,
                                trim, cuts))
    return out, plan


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
        # Keyed on the plain ranked score, not on the stage's own tuple: those
        # mean different things from one stage to the next, so a pool sorted by
        # them would be sorted by nothing in particular.
        key = rank_score(layouts, params.priorities, cabinet_arg,
                         params.miter_capacity_mm)
        pool = alternates.setdefault(task_id, [])
        if any(existing[1] == key for existing in pool):
            return
        pool.append((layouts, key))
        pool.sort(key=lambda entry: entry[1])
        del pool[ALTERNATE_POOL:]

    def gather(live_task: Task | None = None,
               live_layouts: list[SheetLayout] | None = None,
               choice: dict[str, list] | None = None) -> list[tuple]:
        """Which layout each task contributes right now, as scheduler entries."""
        out: list[tuple] = []
        for task in tasks:
            if live_task is not None and task.id == live_task.id:
                picked = live_layouts
            elif choice is not None and task.id in choice:
                picked = choice[task.id]
            elif task.id in results:
                picked = results[task.id][0]
            else:
                continue
            for layout in picked or ():
                out.append((task.group.id, task.labels, layout))
        return out

    def compose(live_task: Task | None = None,
                live_layouts: list[SheetLayout] | None = None) -> list[Sheet]:
        """Every task's best sheets, with one task's in-flight layout swapped in.

        For the animation, so it sequences in place: searching for a sheet order
        as well would be a few milliseconds spent four times a second to shave a
        number off a layout that is about to be replaced.
        """
        return compose_plan(live_task, live_layouts)[0]

    def compose_plan(live_task: Task | None = None,
                     live_layouts: list[SheetLayout] | None = None,
                     choice: dict[str, list] | None = None,
                     final: bool = False):
        """Sheets and the plan behind them. `choice` assembles a runner-up
        combination; `final` also searches for the order to break the sheets
        down in -- across stocks, since the stop does not care what thickness it
        is cutting."""
        return schedule_job(gather(live_task, live_layouts, choice), params,
                            trim, cabinet_arg, cut_pref, final=final)

    # Both gradients toward one sheet fewer; see app/objective.py for why there
    # are two and why the choice matters more than the time spent.
    SURROGATES = sheet_surrogates(params.sheet_width_mm * params.sheet_length_mm,
                                  params.miter_capacity_mm)

    def neighbours(task: Task) -> tuple[list[SheetLayout], list[SheetLayout]]:
        """The other stocks' sheets, in job order, either side of this one.

        Two criteria are properties of the whole job rather than of one stock:
        the stop carries from the last cut of one sheet to the first of the next
        whatever the two are made of, and so does the walk between saws. So a
        candidate is judged in company. The others' sheets do not change while
        this task is being searched, and the scheduler caches its working per
        sheet, so they cost almost nothing to carry along.
        """
        before: list[SheetLayout] = []
        after: list[SheetLayout] = []
        passed = False
        for other in tasks:
            if other.id == task.id:
                passed = True
                continue
            got = results.get(other.id)
            if got is None:
                continue
            (after if passed else before).extend(got[0])
        return before, after

    def whole_job(task: Task, sheets: list[SheetLayout]) -> list[SheetLayout]:
        before, after = neighbours(task)
        return before + list(sheets) + after

    def staged_rate(task: Task, locks: dict[str, float], depth: int):
        """Rank `depth` as the objective, everything above it as a constraint.

        This is what makes the ranking a sequence of problems rather than a list
        of tiebreaks. Criteria ranked above `depth` are *held* at the value
        already achieved: exceeding one is a violation, and a violation on a
        higher rank outweighs any gain below it, so the constraint is hard --
        but it is expressed as a distance rather than a rejection, which leaves
        the search a gradient back to feasibility instead of a cliff. Criteria
        ranked below are plain tiebreaks, which is all they can be until their
        own stage comes round.

        Beating a lock is not rewarded, only permitted: at that point the higher
        rank is being improved for free, and the lock is re-tightened between
        stages so the gain is not given back.
        """
        ceiling = cap_for(task) or sheet_floors.get(task.id, task.area_bound)
        before, after = neighbours(task)
        held = order[:depth]
        tail = order[depth:]

        def rate(sheets):
            values = measure(before + list(sheets) + after, cabinet_arg,
                             params.miter_capacity_mm)
            return ((max(0, len(sheets) - ceiling),)
                    + tuple(max(0.0, values[c] - locks[c]) for c in held)
                    + tuple(values[c] for c in tail))
        return rate

    def job_values() -> dict[str, float]:
        """Every criterion's value for the job as it currently stands."""
        layouts = [lay for task in tasks if task.id in results
                   for lay in results[task.id][0]]
        if not layouts:
            return {}
        return measure(layouts, cabinet_arg, params.miter_capacity_mm)

    def run_task(task: Task, budget: float, seed: int, pass_no: int, warm=None,
                 rate=None, stage: str | None = None, patient: bool = False):
        """Search one task, streaming frames. Keeps the result only if better.

        `patient` spends the whole budget instead of handing it back when the
        search goes quiet. The floor pass sets it: plywood is priority one, its
        budget is already capped, and a stall is not evidence that no tighter
        pack exists -- it is what a hill climb looks like just before it finds
        one. Starving it cost a sheet on a sixteen-part job.
        """
        nonlocal last_frame
        # Both capped rather than pure fractions of the budget: at two minutes a
        # plain fraction means sitting silent for half a minute after the last
        # improvement, which just makes a finished solve feel broken.
        settle = min(max(0.4, budget * 0.15), 5.0)
        # How long "it has stopped finding anything" takes to establish scales
        # with the size of the search as well as the budget: two dozen parts are
        # exhausted in a moment, two hundred are not. Without the part-count term
        # a twenty-one-part cabinet sat out a fixed stall in every stage of every
        # sweep and spent two minutes finishing work it had done in the first
        # second.
        hard_stall = (float("inf") if patient
                      else min(max(0.5, budget * 0.35), 12.0,
                               max(0.5, 0.05 * len(task.specs))))

        caps[task.id] = cap_for(task)

        best_layouts = best_score = best_order = None
        last_improve = time.perf_counter()
        dirty = False

        if rate is None:
            # A hard ceiling on plywood: ranking grouping above sheets lets the
            # search spend material, but never without limit.
            rate = staged_rate(task, {}, 0)

        # A rip-first candidate competes on the same score. It wins when stages
        # or rip widths are ranked highly -- a store's panel saw will do long
        # rips for free but nothing else -- and loses on sheet count, which is
        # exactly the trade the ranking is supposed to decide.
        # A rip-first layout wins on saw work and loses on sheet count, so it
        # has no business spending the floor pass's budget -- that pass is
        # looking for the tightest pack there is.
        rip_share = (0.05 if stage is None and patient
                     else (0.6 if order[0] in (STAGED, STOPCHANGES) else 0.15))
        rip_layouts = rip_score = None
        rip_attempts = 0
        # The rip search decides one thing per part -- which of its two
        # dimensions becomes the strip width -- so it converges in a fraction of
        # what it is given and then flips rotations that change nothing. Hand the
        # rest back like the main loop does: with a stage per ranked criterion
        # and two sweeps over them, this is asked for two dozen times in a solve,
        # and sitting out its full share each time was most of a two-minute
        # budget spent on a job that had finished in the first second.
        rip_stall = min(max(0.4, budget * 0.15), 3.0)
        rip_quiet = time.perf_counter()
        for layouts, value, improved in search_staged_iter(
                task.specs, usable_w, usable_l, params.kerf_mm, rate,
                time_budget=min(budget * rip_share, 8.0), seed=seed,
                min_offcut=params.min_offcut_mm,
                miter_capacity=params.miter_capacity_mm):
            rip_attempts += 1
            if improved:
                rip_layouts, rip_score = layouts, value
                rip_quiet = time.perf_counter()
            now = time.perf_counter()
            if now - rip_quiet >= rip_stall:
                break
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

        # Only let the packer spread parts for the chop saw's benefit when the
        # ceiling is above the area floor -- that is, when there is a sheet to
        # spare. At the floor it cannot help and only dilutes the search.
        ceiling_now = (caps.get(task.id)
                       or sheet_floors.get(task.id, task.area_bound))
        for state in optimise_iter(task.specs, usable_w, usable_l, params.kerf_mm,
                                   time_budget=budget, seed=seed, heartbeat=0.1,
                                   warm_start=warm, cabinets=cabinet_arg,
                                   scorer=rate, group_aware=group_aware,
                                   min_offcut=params.min_offcut_mm,
                                   # The floor pass is looking for the tightest
                                   # pack there is; gathering parts that measure
                                   # alike is a fine instinct for the saw and a
                                   # poor one for density, and it was costing a
                                   # sheet on small jobs.
                                   align_offsets=align_offsets and not patient,
                                   miter_capacity=params.miter_capacity_mm,
                                   # The floor pass wants density and nothing
                                   # else, so it gets the three classic fits.
                                   # The other two each trade area for something
                                   # it has no use for yet, and offering them
                                   # here cost both the sheet and the structure.
                                   heuristics=(DENSITY_HEURISTICS if patient
                                               else None),
                                   allow_best_fit=ceiling_now > task.area_bound):
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

        # Nothing to regroup here: `schedule` treats the order of the bands a
        # same-axis run divides a piece into as part of the cutting order, so
        # the scorer already valued this layout at its best arrangement and the
        # tree is rewritten to match when the layout is composed.

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
            # Re-score what we already had under *this* stage's rate. Scores from
            # different stages measure different things -- rank 2 under rank 1
            # held is not comparable with rank 1 itself -- so comparing the
            # stored numbers would let a later stage discard a better layout, or
            # keep a worse one, depending on which tuple happened to sort first.
            if previous is None or best_score < rate(previous[0]):
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
    # Plywood is priority one and the only figure a later pass cannot recover,
    # so the floor pass gets an absolute minimum as well as a share -- reaching
    # the area bound on the sample kitchen's biggest stock takes about a second,
    # and a share of a two-second budget is not a second. It is a ceiling, not a
    # duration: the loop below stops the moment a stock is at its area bound,
    # because nothing can beat that, and hands the rest back to the ranked
    # stages.
    floor_budget = (min(total_budget * 0.22, 6.0) if params.floor_only
                    else min(max(total_budget * 0.35, 3.0), 30.0))
    for task in tasks:
        share = floor_budget * weights[task.id] / max(sum(weights.values()), 1)
        began = time.perf_counter()
        for n, rate in enumerate(SURROGATES):
            if task.id in results and len(results[task.id][0]) <= task.area_bound:
                break     # at the area bound: no layout can beat this, so stop
            left = share - (time.perf_counter() - began)
            if left <= 0:
                break
            # The first gradient gets the larger slice because it is the one
            # that usually gets there; the second only runs if it did not.
            slice_now = left * 0.65 if n < len(SURROGATES) - 1 else left
            yield from run_task(task, slice_now, FIRST_PASS_SEED + n * 13, 1,
                                rate=rate, patient=True)
        # `run_task` only stores a layout that beats what it already had, and
        # sheet count leads both surrogates, so this is the better of the two.
        sheet_floors[task.id] = (len(results[task.id][0]) if task.id in results
                                 else task.area_bound)

    global_floor = sum(sheet_floors.values())
    allowance = (max(0, params.max_sheets - global_floor)
                 if params.max_sheets is not None else 0)

    # ---- pass 1: the ranking as a sequence of problems ---------------------
    # One stage per ranked criterion. Stage 1 minimises rank 1; whatever it
    # achieves becomes a hard constraint, and stage 2 minimises rank 2 under it;
    # and so on down the ranking.
    #
    # A single search with a lexicographic comparator is *not* the same thing,
    # even though the two agree at the true optimum. Under a time budget the
    # comparator hands almost every decision to rank 1: two layouts rarely tie
    # on it, so the ranks below are consulted for a handful of comparisons out
    # of thousands and are optimised only by accident. Locking rank 1 is what
    # makes rank 2 an objective at all -- every candidate now ties above it.
    #
    # The price is that rank 1 is frozen at whatever a heuristic search found,
    # so stage 1 gets the largest share of the budget, and the locks are
    # re-tightened after every stage in case a later one improves an earlier
    # criterion for free.
    locks: dict[str, float] = {}
    solved: set[str] = set()

    def relock(depth: int) -> None:
        values = job_values()
        for key in order[:depth]:
            if key in values:
                locks[key] = min(locks.get(key, values[key]), values[key])

    def stage_bounds() -> dict[str, float]:
        """Certified floors for the job as it stands, to skip settled stages."""
        floors: dict[str, float] = {}
        for task in tasks:
            if task.id not in results:
                continue
            for key, value in bounds_mod.compute(
                    task.specs, usable_w, usable_l, len(results[task.id][0]),
                    params.miter_capacity_mm).items():
                if value == float("-inf") or key in (STOPCHANGES, STAGED):
                    continue
                floors[key] = floors.get(key, 0.0) + value
        layouts = [lay for task in tasks if task.id in results
                   for lay in results[task.id][0]]
        if layouts:
            floors[STOPCHANGES] = bounds_mod.stopchange_bound(
                layouts, params.miter_capacity_mm)
            floors[STAGED] = bounds_mod.sawchange_bound(
                layouts, params.miter_capacity_mm)
        return floors

    def sweep(share: float, seed: int, pass_no: int):
        """One pass down the ranking, locking each criterion as it is solved.

        Returns whether anything actually improved, so a caller refining in the
        background can stop when the sweeps stop paying.
        """
        gained = False
        spent = 0.0
        for depth, criterion in enumerate(order):
            left = share - spent
            if left <= 0:
                break
            # Rank 1 gets the largest slice both because it dominates and
            # because its value is the one that gets frozen; the rest taper.
            slice_budget = left * (0.42 if depth == 0
                                   else 1.0 / max(1, len(order) - depth))
            relock(depth)
            floors = stage_bounds()
            values = job_values()
            settled = (criterion in floors and criterion in values
                       and values[criterion] <= floors[criterion] + 1e-9)
            if settled:
                # Already at a floor no layout can beat: nothing to search for.
                solved.add(criterion)
                relock(depth + 1)
                continue
            if slice_budget < STAGE_FLOOR and depth > 0:
                break                     # too little time left to mean anything
            began = time.perf_counter()
            for task in tasks:
                if time.perf_counter() - began >= slice_budget:
                    break
                warm = results[task.id][2] if task.id in results else None
                got = yield from run_task(
                    task, slice_budget * weights[task.id]
                    / max(sum(weights.values()), 1),
                    seed + depth * 101, pass_no, warm=warm,
                    rate=staged_rate(task, locks, depth), stage=criterion)
                gained = gained or bool(got)
            solved.add(criterion)
            spent += time.perf_counter() - began
            relock(depth + 1)
        return gained

    # `floor_only` stops here: enough to say how much plywood the job needs,
    # without spending the rest of the budget before the user has said what
    # matters to them.
    if not params.floor_only:
        yield from sweep(total_budget * 0.78, FIRST_PASS_SEED + 7, 1)

        # ---- pass 2: another sweep with whatever is left ------------------
        # A second sweep is not a repeat: the locks are tighter now, so stage 1
        # starts from a better layout and the stages below it start from a
        # feasible one rather than from the floor pass.
        leftover = total_budget - (time.perf_counter() - started)
        if leftover > 1.0:
            yield from sweep(leftover, SECOND_PASS_SEED, 2)

    def build_report(plan) -> list[CriterionReport]:
        """Each criterion's achieved value against its certified lower bound.

        A bound is what no layout can beat, so meeting one is a proof of
        optimality for that criterion. Falling short of one proves nothing on its
        own -- guillotine cutting is NP-hard and the bound may be unreachable --
        so the gap is shown rather than any claim about it.

        Sequence criteria are measured across the *whole job*, not summed per
        stock: the saw's stop carries from the last cut of one sheet to the first
        of the next whatever the two are made of, so adding up per-stock figures
        would both miscount the joins and hide them from the search.
        """
        layouts = [lay for task in tasks if task.id in results
                   for lay in results[task.id][0]]
        # Measured once over the whole job, not summed per stock. Most criteria
        # are additive and it makes no difference, but two are not: the largest
        # offcut is a maximum, so adding the stocks' maxima invents a piece
        # nobody has, and the sequence criteria run across stock boundaries.
        totals: dict[str, float] = dict(
            measure(layouts, cabinets, params.miter_capacity_mm)) if layouts else {}
        floors: dict[str, float] = {}
        for task in tasks:
            if task.id not in results:
                continue
            # Bounds *are* summed: a floor per stock is a valid floor for the
            # total, because the stocks are disjoint sets of cuts.
            for key, value in bounds_mod.compute(task.specs, usable_w, usable_l,
                                                 len(results[task.id][0]),
                                                 params.miter_capacity_mm).items():
                if value == float("-inf") or key in (STOPCHANGES, STAGED):
                    continue
                floors[key] = floors.get(key, 0.0) + value

        if plan is not None:
            totals[STOPCHANGES] = plan.stop_changes
            totals[STAGED] = plan.saw_changes
        else:
            totals[STOPCHANGES] = totals[STAGED] = 0.0
        # Both of these are properties of the cutting order across the whole
        # job, so their floors are too.
        floors[STOPCHANGES] = bounds_mod.stopchange_bound(
            layouts, params.miter_capacity_mm)
        floors[STAGED] = bounds_mod.sawchange_bound(
            layouts, params.miter_capacity_mm)

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
                solved=key in solved,
                held=None if key not in locks else round(locks[key], 2),
            ))
        return out

    def build_stop_plan(plan) -> list[StopRun]:
        """Every run of track cuts that shares one stop setting, in order.

        This is the list to work from at the saw: set the stop once per row and
        make that many cuts before touching it again. Straight off the chosen
        plan, so it is the same order the cut list gives -- including the runs
        that carry from one sheet into the next.
        """
        if plan is None:
            return []
        position = {index: n for n, index in enumerate(plan.order, start=1)}
        rows: list[StopRun] = []
        for step in plan.steps:
            if step.saw != "track":
                continue
            here = position.get(step.sheet, 0)
            if rows and rows[-1].width_mm == step.offset:
                rows[-1].count += 1
                if here not in rows[-1].sheets:
                    rows[-1].sheets.append(here)
            else:
                rows.append(StopRun(index=len(rows) + 1, width_mm=step.offset,
                                    count=1, sheets=[here]))
        return rows

    def build_alternatives() -> list[LayoutAlternative]:
        """The next-best whole solutions that still fit inside the plywood cap.

        Combinations are enumerated across the per-task pools and scored as
        complete layouts, so these really are runners-up to the chosen one --
        not "each task's second-best", which could add up to something nobody
        would pick, and could exceed the cap.

        Ranking the combinations uses per-stock figures, which is cheap and good
        enough to sort by. The two that survive are then measured the way the
        chosen layout is -- sequenced across the whole job -- so the numbers on a
        candidate mean the same thing as the numbers it is being compared with.
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
            sheets_alt, alt_plan = compose_plan(choice=choice, final=True)
            values["sheets"] = total
            if alt_plan is not None:
                values[STOPCHANGES] = alt_plan.stop_changes
                values[STAGED] = alt_plan.saw_changes
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
        # The one pass that also searches for a sheet order: it can only lower
        # the stop-change count, and it is what makes a run carry from the last
        # cut of one sheet into the first of the next.
        final, plan = compose_plan(final=True)
        notes = list(warnings)
        if errors:
            notes.append("Layout validation failed: " + "; ".join(errors[:3]))
        if trim > 0:
            notes.append(f"Trim {trim:.1f} mm off all four edges of every sheet "
                         "before the numbered cuts.")
        report = build_report(plan)
        stats = _stats(final, groups, params, trim, cabinets)
        stats["stages"] = [
            {"key": c.key, "label": c.label, "value": c.value}
            for c in report if c.solved]
        unsolved = [c.label for c in report if not c.solved]
        if unsolved:
            notes.append(
                "Ran out of time before solving for "
                + ", ".join(unsolved).lower()
                + " in its own right; "
                + ("it was" if len(unsolved) == 1 else "they were")
                + " only a tiebreak. Raise Effort to give "
                + ("it" if len(unsolved) == 1 else "them") + " a pass.")
        return {"type": "result", "refined": refined, "payload": LayoutResult(
            stop_plan=build_stop_plan(plan),
            # The floor is the fewest sheets anything has actually achieved, not
            # whatever the opening pass happened to find. A later pass that packs
            # tighter lowers it, rather than leaving a cap the user cannot reach.
            sheet_floor=min(sum(sheet_floors.values()),
                            len(final) if final else sum(sheet_floors.values())),
            sheet_bound=sum(t.area_bound for t in tasks),
            alternatives=build_alternatives(),
            sheets=final, bom=build_bom(panels, groups), groups=groups,
            stats=stats, report=report, warnings=notes).model_dump()}

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
        # Another sweep down the ranking, from tighter locks than the last one.
        # `yield from` re-yields the frames and evaluates to the sweep's return
        # value, which says whether anything actually improved.
        slice_budget = min(30.0, max(2.0, params.background_seconds / 6),
                           max(0.5, deadline - time.perf_counter()))
        improved = yield from sweep(slice_budget,
                                    SECOND_PASS_SEED + round_no * 7, 3)
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
