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
    # material: fewest sheets. cuts: least sawing. cabinets: every sheet belongs
    # to one cabinet, so parts never need sorting between units.
    mode: Literal["material", "cuts", "cabinets"] = "material"
    background_seconds: float = 0.0     # keep refining after the first answer
    kerf_mm: float = 2.2
    sheet_width_mm: float = 1219.2      # 48"
    sheet_length_mm: float = 2438.4     # 96"
    edge_trim_mm: float = 0.0
    effort: Literal["fast", "normal", "thorough"] = "thorough"
    units: Literal["in", "mm"] = "in"


class LayoutResult(BaseModel):
    sheets: list[Sheet]
    bom: list[BomRow]
    groups: list[ThicknessGroup]
    stats: dict
    warnings: list[str] = Field(default_factory=list)
