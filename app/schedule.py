"""Global cut sequencing: what to cut, in what order, across the whole job.

Four facts about a track saw with parallel stops drive this module.

*The stop is one number.* It is the width of the strip the saw takes off the
reference edge, so two cuts share a setting whenever their offsets match -- no
matter which axis they run along, which piece they divide, or which sheet that
piece came from. Turning the work ninety degrees under the saw costs nothing;
touching the stop is what costs. So a full-length rip at 7-3/8" and a crosscut
at 7-3/8" buried in some strip are the *same* setting, and putting them next to
each other is free.

*Runs carry across sheets.* Finishing one sheet at 7-3/8" and opening the next
at 7-3/8" costs nothing, because the saw has not been touched. The order of the
sheets is therefore a real decision, not bookkeeping -- and so is which column
of the next sheet gets ripped first. It carries across *stocks* too: the stop
does not care whether it is cutting half-inch or three-quarter.

*The miter saw never touches the stop.* A chop cut can sit anywhere in the
sequence without breaking a run, so chop cuts are placed for saw changes alone.

*Same-axis runs are permutable.* Every cut in a run of same-axis cuts spans its
piece completely, so the sections are parallel bands: the same parts, the same
cuts, in whatever order you like -- including which band is left as the
remainder and so never cut at all. The tree the packer happens to emit is only
one of those orders.

That last point is why measuring stop changes on the raw tree overstates them:
the band order is arbitrary, so it is not a property of the layout at all. This
module therefore scores a layout by the best schedule it can find *for* it, and
rewrites the tree to match once a layout is chosen. Sequencing is free -- it
never moves a part, buys a sheet or adds a cut -- so the search finally sees the
same number the shop will.

The model of the shop: one sheet is broken down at a time, and everything that
came off it is finished before the next sheet is opened. Within that, the order
is free.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from .nesting import SheetLayout

ROUND = 1                 # mm; offsets this close are one stop setting
MITER_DEFAULT = 304.8     # 12"
TRACK = "track"
MITER = "miter"


def q(value: float) -> float:
    """An offset as the saw sees it: one number, to a tenth of a millimetre.

    Nudged before rounding, because band positions are reached by accumulating
    extents and kerfs: two ways of computing the same offset agree to about
    1e-13, which is invisible until the value sits exactly on a rounding
    boundary and the two land on opposite sides. Two cuts either share a setting
    or they do not, and which way a tie goes matters far less than everything
    agreeing on it.
    """
    return round(value + 1e-9, ROUND)


# --------------------------------------------------------------------------
# the chain model


@dataclass
class Chain:
    """A maximal run of same-axis cuts on one piece.

    `n` bands and either `n - 1` cuts -- the last band is the remainder, left
    uncut -- or `n`, when a blade-width sliver was left over and every band had
    to be cut off.

    `nodes[k]` is the piece that makes the k-th cut of the run, so it is also
    where the k-th band of the chosen order ends up. Every cut in a run spans
    the same distance, which is why one saw does the whole run.
    """

    head: int                    # piece the run starts from
    axis: str
    nodes: list[int]             # spine, in cutting order; len == cut count
    sections: list[int]          # bands the run divides the piece into
    extents: dict[int, float]    # band piece id -> its extent along `axis`
    saw: str
    cut_all: bool                # True when every band is cut off
    stops: dict[int, float]      # band -> the setting that cuts it
    counts: dict[float, int]     # setting -> how many bands want it
    # Bands normally permute freely, but not always: a cabinet's grain-matched
    # faces have to come off in installation order or the grain does not run
    # continuously across them. A pinned run can only ever cut its next band.
    pinned: bool = False

    @property
    def cuts(self) -> int:
        return len(self.nodes)


def _is_head(sheet: SheetLayout, pid: int) -> bool:
    """Does the run of same-axis cuts start here?

    A run continues only through the *remainder* child: the kept child is a
    finished band, so any cut on it opens a new run even when it happens to run
    the same way.
    """
    piece = sheet.pieces[pid]
    if piece.cut is None:
        return False
    if piece.parent is None:
        return True
    parent = sheet.pieces[piece.parent]
    if parent.cut is None or parent.cut.axis != piece.cut.axis:
        return True
    return not (len(parent.children) > 1 and parent.children[1] == pid)


def build_chains(sheet: SheetLayout,
                 miter_capacity: float = MITER_DEFAULT) -> list[Chain]:
    """Decompose one sheet's cut tree into permutable runs.

    Cached on the sheet: the layout search schedules the same sheets over and
    over, and this walk was the single biggest cost of scoring a candidate.
    """
    cached = getattr(sheet, "chain_cache", None)
    if cached is not None and cached[0] == miter_capacity:
        return cached[1]

    chains: list[Chain] = []
    for pid in sorted(sheet.pieces):
        if not _is_head(sheet, pid):
            continue
        axis = sheet.pieces[pid].cut.axis
        nodes: list[int] = []
        sections: list[int] = []
        node = pid
        while True:
            piece = sheet.pieces[node]
            if piece.cut is None or piece.cut.axis != axis or not piece.children:
                sections.append(node)           # the remainder, left uncut
                break
            nodes.append(node)
            sections.append(piece.children[0])
            if len(piece.children) < 2:
                break                           # blade ate the remainder
            node = piece.children[1]

        head = sheet.pieces[pid]
        span = head.h if axis == "V" else head.w
        extents = {s: (sheet.pieces[s].w if axis == "V" else sheet.pieces[s].h)
                   for s in sections}
        stops = {s: q(e) for s, e in extents.items()}
        counts: dict[float, int] = {}
        for stop in stops.values():
            counts[stop] = counts.get(stop, 0) + 1
        chains.append(Chain(
            head=pid, axis=axis, nodes=nodes, sections=sections,
            extents=extents,
            saw=MITER if span <= miter_capacity + 1e-6 else TRACK,
            cut_all=len(nodes) == len(sections),
            stops=stops, counts=counts,
            pinned=any(sheet.pieces[nd].pinned for nd in nodes),
        ))
    sheet.chain_cache = (miter_capacity, chains)
    return chains


class Job:
    """Every cut in the whole job, grouped into permutable runs.

    Runs are held per sheet and addressed by their position in that sheet's
    list, so nothing has to be renumbered when the same sheet turns up in
    another job -- which is what makes the per-sheet caches worth having.
    """

    def __init__(self, sheets: list[SheetLayout],
                 miter_capacity: float = MITER_DEFAULT):
        self.sheets = sheets
        self.miter_capacity = miter_capacity
        self.per_sheet: list[list[Chain]] = [
            build_chains(sheet, miter_capacity) for sheet in sheets]
        # A run that does not start at the sheet root starts at a band of some
        # other run, and cannot begin until that band is cut free.
        self.heads: list[dict[int, int]] = [
            {chain.head: i for i, chain in enumerate(chains)}
            for chains in self.per_sheet]
        self.offsets: list[dict[float, int]] = []
        for chains in self.per_sheet:
            tally: dict[float, int] = {}
            for chain in chains:
                if chain.saw != TRACK:
                    continue
                # Which bands end up cut depends on the order chosen, so the
                # honest tally is over every band that *could* be cut.
                for offset, count in chain.counts.items():
                    tally[offset] = tally.get(offset, 0) + count
            self.offsets.append(tally)

    def sheet_offsets(self, index: int) -> dict[float, int]:
        return self.offsets[index]


# --------------------------------------------------------------------------
# scheduling one sheet


@dataclass
class Step:
    """One cut, in the order it is made."""
    sheet: int          # index into the job's sheet list
    pid: int            # piece the cut divides, once the tree is rewritten
    offset: float
    saw: str


@dataclass
class SheetSchedule:
    runs: int                       # stop settings started inside this sheet
    exit: float | None              # stop left set when the sheet is done
    steps: list[Step]
    orders: dict[int, list[int]]    # run head piece -> band order chosen
    saw_blocks: int
    first_saw: str | None = None
    last_saw: str | None = None

    @property
    def summary(self) -> tuple:
        return (self.runs, self.exit, self.saw_blocks,
                self.first_saw, self.last_saw)


def _track_below(job: Job, index: int, section: int,
                 memo: dict[int, bool]) -> bool:
    """Is there any track-saw work inside this band?

    Holding a band back for the end of a sheet only works if nothing that has to
    follow it needs the stop moved again.
    """
    cached = memo.get(section)
    if cached is not None:
        return cached
    memo[section] = False               # break cycles defensively
    local = job.heads[index].get(section)
    answer = False
    if local is not None:
        chain = job.per_sheet[index][local]
        answer = chain.saw == TRACK or any(
            _track_below(job, index, s, memo) for s in chain.sections)
    memo[section] = answer
    return answer


# Where to move the stop to, when it has to move. Every policy starts from the
# same rule -- do not go somewhere you will have to come back to -- and then
# breaks the tie differently. None dominates the others across real layouts, so
# the final plan tries them all per sheet and keeps whichever came out shortest;
# scoring uses the first, which is the best of them on average.
#
#   ready[o]      cuts at this setting that can be made right now
#   waiting[o]    cuts it certainly still owes but cannot reach yet
#   unlocks(o)    of the ready ones, how many free a band with more work inside
POLICIES = 4


def _rank_key(policy: int, ready, waiting, unlocks):
    if policy == 1:
        return lambda o: (1 if waiting.get(o) else 0, -unlocks(o), -ready[o], -o)
    if policy == 2:
        return lambda o: (-ready[o], -o)
    if policy == 3:
        return lambda o: (1 if waiting.get(o) else 0, -ready[o], o)
    return lambda o: (1 if waiting.get(o) else 0, -ready[o], -unlocks(o), -o)


def _schedule_sheet(job: Job, index: int, entry: float | None,
                    finish_on: float | None = None,
                    free_miter: bool = True,
                    policy: int = 0) -> SheetSchedule | None:
    """Order one sheet's cuts, keeping the stop still as long as it can.

    Greedy, but over a far larger set of moves than "which cut next": at any
    moment an open run can cut off *any* of the bands it still holds, so a run
    holding a band of the current width can always serve it. That is what lets a
    crosscut buried in one column follow a rip on the far side of the sheet at
    the same measurement, and what lets the first rip of a sheet match the last
    rip of the sheet before it.

    `entry` is the setting the saw is already at, so cuts at that width cost
    nothing to start with. `finish_on` asks the sheet to *end* at a width -- the
    cuts at it are held back -- which is how the next sheet is handed something
    it can continue. Returns None when that cannot be arranged.

    `free_miter` lets chop cuts be made mid-run to open up more work at the
    current setting; worth it when stop changes are ranked above saw changes.

    Bookkeeping is incremental rather than rescanned, because this runs
    thousands of times a second inside the layout search: `ready` counts the
    cuts each setting can make right now, `waiting` the ones it certainly still
    owes but cannot reach yet.
    """
    chains = job.per_sheet[index]
    heads = job.heads[index]
    n = len(chains)
    if not n:
        return SheetSchedule(0, entry, [], {}, 0)

    remaining: list[list[int]] = [list(c.sections) for c in chains]
    tally: list[dict[float, int]] = [dict(c.counts) for c in chains]
    left: list[int] = [c.cuts for c in chains]
    held: list[set[int]] = [set() for _ in chains]

    # Bands held back so the sheet ends on `finish_on`. Only bands with no track
    # work inside them can be held: anything that has to follow a held cut would
    # move the stop again and the sheet would not end where promised. When some
    # cuts at that width can be held and others cannot, the width gets visited
    # twice -- which may still be free, since the greedy often visits it twice
    # anyway. The caller compares and keeps whichever came out better.
    holding = 0
    if finish_on is not None:
        memo: dict[int, bool] = {}
        for cid, chain in enumerate(chains):
            if (chain.saw != TRACK or chain.pinned
                    or finish_on not in chain.counts):
                continue
            for section in chain.sections:
                if chain.stops[section] != finish_on:
                    continue
                if _track_below(job, index, section, memo):
                    continue
                held[cid].add(section)
                holding += 1
        if not holding:
            return None
        for cid in range(n):
            # A run whose every band is held could never be cut at all.
            if held[cid] and len(held[cid]) >= len(remaining[cid]):
                return None

    ready: dict[float, int] = {}      # cuts this setting can make right now
    waiting: dict[float, int] = {}    # cuts it still owes, out of reach so far
    counted: list[set[int]] = [set() for _ in chains]
    owed: list[dict[float, int]] = [{} for _ in chains]
    for cid, chain in enumerate(chains):
        if chain.saw != TRACK or not chain.cuts:
            continue
        # A run of n bands making k cuts leaves n - k uncut, so it certainly
        # still owes any setting it holds more of than that.
        spare = len(chain.sections) - chain.cuts
        for offset, count in chain.counts.items():
            forced = count - spare
            if forced > 0:
                owed[cid][offset] = forced
                waiting[offset] = waiting.get(offset, 0) + forced

    active: set[int] = set()
    opened: set[int] = set()
    orders: list[list[int]] = [[] for _ in chains]
    steps: list[Step] = []
    pending_miter: list[Step] = []

    def _offerable(cid: int) -> list[int]:
        """Bands this run could cut off next.

        Any of them, normally -- the bands are parallel and the order is free.
        A pinned run only ever offers the next one.
        """
        spare = [b for b in remaining[cid] if b not in held[cid]]
        if chains[cid].pinned:
            return spare[:1]
        return spare

    def bump(table: dict[float, int], offset: float, delta: int) -> None:
        total = table.get(offset, 0) + delta
        if total > 0:
            table[offset] = total
        else:
            table.pop(offset, None)

    def wake(section: int) -> None:
        cid = heads.get(section)
        if cid is None or cid in opened:
            return
        opened.add(cid)
        active.add(cid)
        if chains[cid].saw != TRACK:
            return
        for offset, forced in owed[cid].items():
            bump(waiting, offset, -forced)
        if left[cid] > 0:
            chain = chains[cid]
            mine = counted[cid]
            for band in _offerable(cid):
                mine.add(band)
                bump(ready, chain.stops[band], 1)

    wake(0)

    def take(cid: int, section: int) -> Step:
        chain = chains[cid]
        offset = chain.stops[section]
        remaining[cid].remove(section)
        tally[cid][offset] -= 1
        left[cid] -= 1
        orders[cid].append(section)
        if section in counted[cid]:
            counted[cid].discard(section)
            bump(ready, offset, -1)
        if left[cid] == 0:
            # The last cut of a run frees both the band it takes off and
            # whatever is left behind it -- and puts the rest out of reach.
            for band in counted[cid]:
                bump(ready, chain.stops[band], -1)
            counted[cid] = set()
            active.discard(cid)
            for rest in list(remaining[cid]):
                wake(rest)
        elif chain.pinned:
            for band in _offerable(cid):
                if band not in counted[cid]:
                    counted[cid].add(band)
                    bump(ready, chain.stops[band], 1)
        wake(section)
        return Step(index, chain.nodes[len(orders[cid]) - 1], offset, chain.saw)

    def _unlocks(offset: float) -> int:
        """Cuts at this setting that would free a band with more work inside it.

        Only asked when the stop is about to move, which is rare by design.
        """
        total = 0
        for cid in active:
            if chains[cid].saw != TRACK or left[cid] == 0:
                continue
            stops = chains[cid].stops
            total += sum(1 for band in counted[cid]
                         if stops[band] == offset and band in heads)
        return total

    def open_chains(saw: str) -> list[int]:
        return [cid for cid in active
                if chains[cid].saw == saw and left[cid] > 0
                and len(remaining[cid]) > len(held[cid])]

    def serve(stop: float) -> int:
        """Make every cut this setting reaches, cascading as bands come free."""
        made = 0
        while ready.get(stop):
            hit = section = None
            for cid in active:
                if (chains[cid].saw != TRACK or left[cid] == 0
                        or not tally[cid].get(stop)):
                    continue
                stops = chains[cid].stops
                found = next((s for s in counted[cid] if stops[s] == stop), None)
                if found is not None:
                    hit, section = cid, found
                    break
            if hit is None:
                return made
            steps.append(take(hit, section))
            made += 1
        return made

    def miter_reach() -> set[float]:
        """Settings that chop cuts alone would expose.

        Every cut in a chop run can be made right now, so all of its bands come
        free; if one of those bands opens a track run, that run's widths are
        within reach without touching the stop.
        """
        found: set[float] = set()
        seen: set[int] = set()
        stack = open_chains(MITER)
        while stack:
            cid = stack.pop()
            if cid in seen:
                continue
            seen.add(cid)
            for section in remaining[cid]:
                child = heads.get(section)
                if child is None or child in opened or child in seen:
                    continue
                if chains[child].saw == MITER:
                    stack.append(child)
                else:
                    found |= {chains[child].stops[s]
                              for s in remaining[child]
                              if s not in held[child]}
        return found

    def drain_miter() -> int:
        """Clear the chop cuts that are available now."""
        made = 0
        while True:
            batch = open_chains(MITER)
            if not batch:
                return made
            for cid in batch:
                while left[cid] > 0 and len(remaining[cid]) > len(held[cid]):
                    section = next(s for s in remaining[cid]
                                   if s not in held[cid])
                    pending_miter.append(take(cid, section))
                    made += 1

    rank = _rank_key(policy, ready, waiting, _unlocks)
    runs = 0
    stop = entry
    if stop is not None and serve(stop) == 0:
        stop = None

    while True:
        if stop is not None:
            serve(stop)

        if not ready:
            if open_chains(MITER):
                drain_miter()
                continue
            if holding:
                # Everything else is done, so release the held bands: they
                # become the sheet's last run, at the width the next sheet
                # wants.
                if stop != finish_on:
                    runs += 1
                    stop = finish_on
                for cid in range(n):
                    if not held[cid]:
                        continue
                    freed = held[cid]
                    held[cid] = set()
                    if cid in opened and left[cid] > 0:
                        for band in freed:
                            counted[cid].add(band)
                            bump(ready, finish_on, 1)
                holding = 0
                continue
            break

        if free_miter and stop is not None and stop in miter_reach():
            drain_miter()
            continue

        # The stop has to move; `POLICIES` decides where to. The rule they share
        # is not to go somewhere you will have to come back to: a setting with
        # cuts still out of reach will need setting up twice, and coming back is
        # the whole cost here.
        stop = min(ready, key=rank)
        runs += 1
        serve(stop)

    drain_miter()
    steps.extend(pending_miter)

    if any(left):
        return None                   # a held band was never reached

    blocks = 0
    last: str | None = None
    for step in steps:
        if step.saw != last:
            blocks += 1
            last = step.saw
    return SheetSchedule(
        runs=runs, exit=stop, steps=steps,
        orders={chains[cid].head: orders[cid]
                for cid in range(n) if orders[cid]},
        saw_blocks=blocks,
        first_saw=steps[0].saw if steps else None,
        last_saw=steps[-1].saw if steps else None)


def _summary(job: Job, index: int, entry: float | None) -> tuple:
    """(runs, exit, saw blocks, first saw, last saw), cached per sheet.

    The scoring path asks for this over and over -- the same sheets, a handful of
    entry settings -- so it is worth remembering. A sheet's schedule depends on
    nothing but its own runs and the setting the saw arrives at.
    """
    sheet = job.sheets[index]
    cache = getattr(sheet, "sched_cache", None)
    if cache is None or cache[0] != job.miter_capacity:
        cache = (job.miter_capacity, {})
        sheet.sched_cache = cache
    got = cache[1].get(entry)
    if got is None:
        plan = _schedule_sheet(job, index, entry)
        assert plan is not None            # only `finish_on` can be refused
        got = plan.summary
        cache[1][entry] = got
    return got


# --------------------------------------------------------------------------
# scheduling the whole job


@dataclass
class Plan:
    """A complete cutting order for the job."""
    order: list[int] = field(default_factory=list)       # sheet indices
    steps: list[Step] = field(default_factory=list)
    stop_runs: list[tuple[float, int]] = field(default_factory=list)
    # (sheet index, run head piece) -> band order chosen
    orders: dict[tuple[int, int], list[int]] = field(default_factory=dict)

    @property
    def stop_changes(self) -> int:
        return max(0, len(self.stop_runs) - 1)

    @property
    def distinct_stops(self) -> int:
        return len({offset for offset, _ in self.stop_runs})

    @property
    def saw_changes(self) -> int:
        changes = 0
        last: str | None = None
        for step in self.steps:
            if last is not None and step.saw != last:
                changes += 1
            last = step.saw
        return changes


def _runs_from(steps: list[Step]) -> list[tuple[float, int]]:
    runs: list[tuple[float, int]] = []
    for step in steps:
        if step.saw != TRACK:
            continue
        if runs and runs[-1][0] == step.offset:
            runs[-1] = (step.offset, runs[-1][1] + 1)
        else:
            runs.append((step.offset, 1))
    return runs


def score_job(sheets: list[SheetLayout],
              miter_capacity: float = MITER_DEFAULT) -> tuple[int, int]:
    """(stop changes, saw changes) for these sheets in the order given.

    The scoring path: it schedules every sheet but builds no cut list and does
    not shop around for a sheet order. `plan_job` does that once a layout has
    been chosen, and reordering can only improve on this -- so the search is
    never flattered by a number the final answer cannot meet.
    """
    job = Job(sheets, miter_capacity)
    blocks = 0
    saw_blocks = 0
    joins = 0
    stop: float | None = None
    previous: str | None = None
    for index in range(len(job.per_sheet)):
        runs, stop, sheet_blocks, first, last = _summary(job, index, stop)
        blocks += runs
        if first is None:
            continue                       # a sheet with nothing to cut
        saw_blocks += sheet_blocks
        if previous is not None and previous == first:
            joins += 1
        previous = last
    return (max(0, blocks - 1), max(0, saw_blocks - joins - 1))


def _entry_table(job: Job, index: int, free_miter: bool) -> dict:
    """What this sheet costs for every setting it could be handed.

    {entry -> (runs started, setting left behind)}, with `None` as the fallback
    for a setting the sheet cannot pick up. This is what makes the sheet order
    searchable: a sheet's behaviour depends only on where the stop already is.
    """
    table: dict[float | None, tuple[int, float | None]] = {}
    for entry in [None] + list(job.sheet_offsets(index)):
        if free_miter:
            runs, exit_stop = _summary(job, index, entry)[:2]
        else:
            got = _schedule_sheet(job, index, entry, None, False)
            assert got is not None
            runs, exit_stop = got.runs, got.exit
        table[entry] = (runs, exit_stop)
    return table


def _useful_entries(table: dict) -> set[float]:
    """Settings worth handing this sheet: ones that save it a stop change."""
    baseline = table[None][0]
    return {entry for entry, (runs, _) in table.items()
            if entry is not None and runs < baseline}


def _sequence_cost(order: list[int], tables: list[dict]) -> int:
    """Stop settings started across the whole job, for this sheet order."""
    total = 0
    stop: float | None = None
    for index in order:
        table = tables[index]
        runs, stop = table.get(stop) or table[None]
        total += runs
    return total


def _order_sheets(tables: list[dict], seed: int, effort: int = 1) -> list[int]:
    """Choose which sheet to break down first, and what follows what.

    A sheet's cost depends only on the setting the saw is already at, so an
    order can be scored from the table in linear time. That makes a real search
    affordable: nearest-neighbour from every possible first sheet, then
    relocate-and-reverse passes until nothing improves.
    """
    n = len(tables)
    if n < 2:
        return list(range(n))

    def follow(first: int) -> list[int]:
        order = [first]
        unused = set(range(n)) - {first}
        stop = tables[first][None][1]
        while unused:
            pick = min(unused, key=lambda i: (
                (tables[i].get(stop) or tables[i][None])[0], i))
            order.append(pick)
            stop = (tables[pick].get(stop) or tables[pick][None])[1]
            unused.discard(pick)
        return order

    best = min((follow(i) for i in range(n)),
               key=lambda o: _sequence_cost(o, tables))
    best_cost = _sequence_cost(best, tables)

    rng = random.Random(seed)
    rounds = max(1, effort)
    for attempt in range(rounds):
        improved = True
        while improved:
            improved = False
            for i in range(n):
                for j in range(n):
                    if i == j:
                        continue
                    trial = best[:]
                    trial.insert(j, trial.pop(i))
                    cost = _sequence_cost(trial, tables)
                    if cost < best_cost:
                        best, best_cost = trial, cost
                        improved = True
            for i in range(n - 1):
                for j in range(i + 2, n + 1):
                    trial = best[:i] + best[i:j][::-1] + best[j:]
                    cost = _sequence_cost(trial, tables)
                    if cost < best_cost:
                        best, best_cost = trial, cost
                        improved = True
        if attempt + 1 < rounds:
            shaken = best[:]
            for _ in range(3):
                shaken.insert(rng.randrange(n), shaken.pop(rng.randrange(n)))
            cost = _sequence_cost(shaken, tables)
            if cost <= best_cost:
                best, best_cost = shaken, cost
    return best


def plan_job(sheets: list[SheetLayout], miter_capacity: float = MITER_DEFAULT,
             reorder_sheets: bool = True, free_miter: bool = True,
             seed: int = 12345, effort: int = 1,
             job: Job | None = None, polish: bool | None = None) -> Plan:
    """The best cutting order this module can find for a set of layouts.

    `reorder_sheets` searches for the order to break the sheets down in;
    `polish` additionally tries to leave the stop where the next sheet wants it.
    """
    job = job if job is not None else Job(sheets, miter_capacity)
    n = len(job.per_sheet)
    if n == 0:
        return Plan()
    if polish is None:
        polish = reorder_sheets

    tables = ([_entry_table(job, i, free_miter) for i in range(n)]
              if (reorder_sheets or polish) and n > 1 else None)
    order = (_order_sheets(tables, seed, effort)
             if reorder_sheets and tables is not None else list(range(n)))

    steps: list[Step] = []
    orders: dict[tuple[int, int], list[int]] = {}
    stop: float | None = None
    for position, index in enumerate(order):
        # Try to leave the stop where the *next* sheet can pick it up. Only
        # settings that would actually save it a change are worth aiming for,
        # and only if aiming for one costs nothing here.
        wants: set[float] = set()
        if tables is not None and position + 1 < len(order):
            wants = _useful_entries(tables[order[position + 1]])
        targets = sorted(wants & set(job.sheet_offsets(index)),
                         key=lambda w: -job.sheet_offsets(index)[w])[:3]

        best: SheetSchedule | None = None
        best_key: tuple | None = None
        for policy in range(POLICIES if polish else 1):
            for finish_on in [None] + targets:
                got = _schedule_sheet(job, index, stop, finish_on, free_miter,
                                      policy)
                if got is None:
                    continue
                key = (got.runs, 0 if got.exit in wants else 1, got.saw_blocks)
                if best_key is None or key < best_key:
                    best, best_key = got, key
        assert best is not None
        steps.extend(best.steps)
        for head, bands in best.orders.items():
            orders[(index, head)] = bands
        stop = best.exit

    return Plan(order=order, steps=steps, stop_runs=_runs_from(steps),
                orders=orders)


def quick_plan(sheets: list[SheetLayout],
               miter_capacity: float = MITER_DEFAULT) -> Plan:
    """Schedule in the order given -- no sheet reordering, no polish."""
    return plan_job(sheets, miter_capacity, reorder_sheets=False, polish=False)


def quick_runs(sheets: list[SheetLayout],
               miter_capacity: float = MITER_DEFAULT) -> list[tuple[float, int]]:
    """Stop runs for the sheets in the order given, without reordering them."""
    return quick_plan(sheets, miter_capacity).stop_runs


def plan_sheet(sheet: SheetLayout, miter_capacity: float = MITER_DEFAULT,
               entry: float | None = None) -> Plan:
    """One sheet's cutting order, given the setting the saw is already at."""
    job = Job([sheet], miter_capacity)
    got = _schedule_sheet(job, 0, entry)
    assert got is not None                 # only `finish_on` can be refused
    return Plan(order=[0], steps=got.steps, stop_runs=_runs_from(got.steps),
                orders={(0, head): bands for head, bands in got.orders.items()})


# --------------------------------------------------------------------------
# making a plan real


def _shift(sheet: SheetLayout, pid: int, axis: str, delta: float) -> None:
    """Slide a whole subtree along one axis, cut positions included."""
    if not delta:
        return
    stack = [pid]
    while stack:
        piece = sheet.pieces[stack.pop()]
        if axis == "V":
            piece.x += delta
        else:
            piece.y += delta
        if piece.cut is not None and piece.cut.axis == axis:
            piece.cut.pos += delta
        stack.extend(piece.children)


def apply_chain_order(sheet: SheetLayout, chain: Chain,
                      order: list[int]) -> bool:
    """Rewrite one run so its bands sit in `order`. In place; returns changed.

    Free by construction: the bands are the same rectangles, the kerfs between
    them are the same count, and their extents still add up to the piece. Only
    the order you meet them in changes.
    """
    axis = chain.axis
    head = sheet.pieces[chain.head]
    start = head.x if axis == "V" else head.y
    span = head.w if axis == "V" else head.h
    sizes = chain.extents

    cut = [s for s in order if s in sizes]
    if len(cut) != len(chain.nodes) or len(set(cut)) != len(cut):
        return False                        # not a complete plan for this run
    chosen = set(cut)
    tail = [s for s in chain.sections if s not in chosen]
    if cut + tail == chain.sections:
        return False

    sheet.chain_cache = None
    sheet.sched_cache = None

    cursor = start
    for node_id, section in zip(chain.nodes, cut):
        piece = sheet.pieces[section]
        _shift(sheet, section, axis,
               cursor - (piece.x if axis == "V" else piece.y))
        node = sheet.pieces[node_id]
        # A spine piece covers everything not yet cut off, so both where it
        # starts and how much it holds move.
        if axis == "V":
            node.x = cursor
            node.w = start + span - cursor
        else:
            node.y = cursor
            node.h = start + span - cursor
        node.cut.pos = cursor + sizes[section]
        node.children[0] = section
        piece.parent = node_id
        cursor += sizes[section] + sheet.kerf

    if tail:
        rest = sheet.pieces[tail[0]]
        _shift(sheet, tail[0], axis,
               cursor - (rest.x if axis == "V" else rest.y))
        last = sheet.pieces[chain.nodes[-1]]
        if len(last.children) > 1:
            last.children[1] = tail[0]
            rest.parent = chain.nodes[-1]

    chain.sections = cut + tail
    return True


def apply_plan(sheets: list[SheetLayout], plan: Plan,
               job: Job | None = None,
               miter_capacity: float = MITER_DEFAULT) -> list[SheetLayout]:
    """Rewrite the trees to match the plan, and return the sheets in cut order.

    Nothing moves between sheets and no cut is added or removed -- only the order
    the bands of a run are met in, which is the whole point: the saving is free.
    """
    job = job if job is not None else Job(sheets, miter_capacity)
    for index, chains in enumerate(job.per_sheet):
        for chain in chains:
            bands = plan.orders.get((index, chain.head))
            if bands:
                apply_chain_order(sheets[index], chain, bands)
    return [sheets[i] for i in plan.order]


def sequence_by_sheet(plan: Plan) -> dict[int, list[int]]:
    """Cut order per sheet, as piece ids -- what the cut list is built from."""
    out: dict[int, list[int]] = {}
    for step in plan.steps:
        out.setdefault(step.sheet, []).append(step.pid)
    return out


def settings_by_sheet(plan: Plan) -> dict[int, dict[int, float]]:
    """The setting each cut is made at, per sheet: {sheet -> {piece -> stop}}.

    The cut list takes its measurements from here rather than recomputing them
    from the geometry, so the number in the instructions, the number in the stop
    plan and the number in the report are the same number.
    """
    out: dict[int, dict[int, float]] = {}
    for step in plan.steps:
        out.setdefault(step.sheet, {})[step.pid] = step.offset
    return out


def entry_stops(plan: Plan) -> dict[int, float | None]:
    """The setting the saw is already at when each sheet is opened."""
    out: dict[int, float | None] = {}
    stop: float | None = None
    seen: set[int] = set()
    for step in plan.steps:
        if step.sheet not in seen:
            seen.add(step.sheet)
            out[step.sheet] = stop
        if step.saw == TRACK:
            stop = step.offset
    for index in plan.order:
        out.setdefault(index, None)
    return out
