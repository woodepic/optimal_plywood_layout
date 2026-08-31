"""Data models shared across the parser, optimizer and API."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Axis = Literal["V", "H"]


class Panel(BaseModel):
    """One physical piece of plywood to cut -- a single occurrence in the assembly."""

    id: str
    path: list[str]                     # ["Drawer Subassembly <2>", "Back"]
    label: str                          # shortest unique suffix of path, or user override
    thickness_mm: float
    width_mm: float                     # the smaller of the two large dimensions
    length_mm: float                    # the larger
    outline: list[tuple[float, float]] | None = None   # true profile, in panel-local mm
    sheet_like: bool = True
    reject_reason: str | None = None
    included: bool = True
    grain_locked: bool = False
    # One of a cabinet's visible faces: cut from a single run of grain so the
    # installed drawer fronts read as one continuous board.
    grain_match: bool = False

    @property
    def full_path(self) -> str:
        return " / ".join(self.path)


class ThicknessGroup(BaseModel):
    id: str
    thickness_mm: float
    nominal: str | None = None
    panel_ids: list[str] = Field(default_factory=list)


class Placement(BaseModel):
    panel_id: str
    label: str
    x: float
    y: float
    w: float                            # as placed on the sheet
    h: float
    rotated: bool


class Cut(BaseModel):
    index: int                          # 1-based, order to perform
    sheet_index: int
    axis: Axis                          # V = cut runs along Y (a rip down the length)
    piece_label: str                    # which piece this cut divides, e.g. "A", "B1"
    # absolute coordinates on the sheet, for drawing
    x1: float
    y1: float
    x2: float
    y2: float
    # what to measure at the saw
    offset_mm: float                    # distance from the piece's reference edge
    from_edge: str                      # "left" | "bottom"
    piece_w: float
    piece_h: float
    produces: list[str] = Field(default_factory=list)
    separates: bool = False   # this cut is one of those that isolates a cabinet
    # Which constant-width run this cut belongs to, 1-based; 0 for miter cuts.
    stop_group: int = 0
    # Which saw makes this cut.
    saw: Literal["miter", "track"] = "track"
    note: str = ""      # server-rendered fallback; the UI rebuilds this per unit


class Sheet(BaseModel):
    index: int                          # 1-based within its thickness group
    group_id: str
    width_mm: float
    length_mm: float
    placements: list[Placement] = Field(default_factory=list)
    cuts: list[Cut] = Field(default_factory=list)
    used_area_mm2: float = 0.0
    largest_offcut: tuple[float, float] | None = None


class BomRow(BaseModel):
    group_id: str
    thickness_mm: float
    nominal: str | None
    width_mm: float
    length_mm: float
    qty: int
    labels: list[str]


class LayoutParams(BaseModel):
    # Ranked lowest-first, compared lexicographically. Any criterion left out is
    # appended as a tiebreaker. See app/objective.py.
    priorities: list[str] = Field(default_factory=lambda: [
        "stopchanges", "trackcuts", "staged", "mitercuts",
        "offcut", "grouping"])
    # Hard cap on plywood regardless of ranking, so ranking grouping first can
    # never quietly run the sheet count away.
    # Hard ceiling on plywood. None means 'use the proven floor'.
    max_sheets: int | None = None
    # Stop after establishing the plywood floor, skipping the ranked search.
    floor_only: bool = False
    max_extra_sheets: int = 4
    background_seconds: float = 0.0     # keep refining after the first answer
    kerf_mm: float = 2.2
    # Longest cut the miter saw can make. Crosscuts on strips at or under this
    # are the easy ones; anything wider means setting up the track saw again.
    miter_capacity_mm: float = 304.8    # 12"
    # Hard constraint: each cabinet's grain-matched faces are cut touching, in
    # order, with the grain running up the face rather than across it.
    continuous_grain: bool = False
    # Offcuts narrower than this are treated as waste. 0 keeps every sliver.
    min_offcut_mm: float = 0.0
    sheet_width_mm: float = 1219.2      # 48"
    sheet_length_mm: float = 2438.4     # 96"
    edge_trim_mm: float = 0.0
    effort: Literal["fast", "normal", "thorough"] = "thorough"
    units: Literal["in", "mm"] = "in"


class StopRun(BaseModel):
    """Consecutive track cuts sharing one stop setting."""
    index: int              # 1-based, in cutting order
    width_mm: float
    count: int


class LayoutAlternative(BaseModel):
    """A runner-up layout: same parts, a different set of trade-offs."""
    label: str
    sheets: list[Sheet] = Field(default_factory=list)
    stats: dict = Field(default_factory=dict)
    values: dict = Field(default_factory=dict)   # criterion key -> value


class CriterionReport(BaseModel):
    key: str
    label: str
    value: float
    bound: float | None = None
    optimal: bool = False
    rank: int | None = None      # 1-based position in the user's ranking


class LayoutResult(BaseModel):
    sheets: list[Sheet]
    bom: list[BomRow]
    groups: list[ThicknessGroup]
    stats: dict
    report: list[CriterionReport] = Field(default_factory=list)
    # Stage-one rips only: what a store's panel saw can do for you.
    stop_plan: list[StopRun] = Field(default_factory=list)
    # Fewest sheets any layout can use, found before anything else is ranked.
    sheet_floor: int = 0
    # Area-derived lower bound: no layout can use fewer, though nothing
    # guarantees it is reachable. Shown so the cap is not stuck at whatever the
    # search happened to achieve.
    sheet_bound: int = 0
    # Runner-up layouts worth a look, best first.
    alternatives: list[LayoutAlternative] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
