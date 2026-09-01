"""Reorder the bands of a same-axis run so equal widths are cut back to back.

Within a run of same-axis cuts every cut spans its piece completely, so the
sections are parallel bands and may be permuted freely: the same parts, the same
sheet, the same number of cuts. What changes is the order you meet them in.

That matters because only *consecutive* equal widths save anything. Two columns
of one width either side of a third cannot be cut one after the other -- the
middle column's rip has to happen in between, at another measurement, so the
stop moves twice.

The real work lives in `app.schedule`, which decides that order as part of the
whole job's cutting sequence rather than sheet by sheet: it can also match a
crosscut buried in one column to a rip on the far side of the sheet, and match
the first rip of a sheet to the last rip of the sheet before it. This module is
the single-sheet door into it, and the guarantee it carries is what the name
promises -- the same parts, the same cuts, never a worse stop count.
"""
from __future__ import annotations

from . import schedule as sched
from .nesting import SheetLayout


def regroup_sheet(sheet: SheetLayout,
                  miter_capacity: float = sched.MITER_DEFAULT) -> bool:
    """Put this sheet's bands in the order the scheduler wants. Returns changed."""
    plan = sched.quick_plan([sheet], miter_capacity)
    job = sched.Job([sheet], miter_capacity)
    changed = False
    for chain in job.per_sheet[0]:
        bands = plan.orders.get((0, chain.head))
        if bands and sched.apply_chain_order(sheet, chain, bands):
            changed = True
    return changed


def regroup(layouts: list[SheetLayout],
            miter_capacity: float = sched.MITER_DEFAULT) -> int:
    """Regroup every sheet; returns how many were changed."""
    return sum(1 for sheet in layouts
               if regroup_sheet(sheet, miter_capacity))
