"""FastAPI app: upload a STEP assembly, get sheet layouts back.

State lives in memory keyed by job id. This is a single-user local shop tool --
no database, and restarting the server clears everything. That is deliberate.
"""
from __future__ import annotations

import json
import tempfile
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .models import LayoutParams, LayoutResult, Panel
from .solver import build_groups, cabinet_of, solve, solve_streaming
from .step_parser import StepParseError, assign_labels, parse_step

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"


def find_sample() -> Path | None:
    """Any STEP file sitting in the project folder, offered as a one-click sample.

    Nothing is loaded automatically -- the user picks a file first.
    """
    found: list[Path] = []
    for pattern in ("*.step", "*.stp", "*.STEP", "*.STP"):
        found.extend(ROOT.glob(pattern))
    if not found:
        return None
    # Most recently added wins -- that is almost always the one just dropped in.
    return max(found, key=lambda f: f.stat().st_mtime)

app = FastAPI(title="Plywood Cut-Layout Optimizer")

JOBS: dict[str, list[Panel]] = {}


class UploadResponse(BaseModel):
    job_id: str
    source: str
    panels: list[Panel]
    warnings: list[str]


class PanelOverride(BaseModel):
    included: bool | None = None
    grain_match: bool | None = None
    grain_locked: bool | None = None
    label: str | None = None


class LayoutRequest(BaseModel):
    job_id: str
    params: LayoutParams = LayoutParams()
    overrides: dict[str, PanelOverride] = {}


def _load(path: Path, source: str) -> UploadResponse:
    try:
        panels, warnings = parse_step(str(path))
    except StepParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Pre-tick the faces that look visible, so the parts table opens on a
    # sensible selection the user can correct rather than an empty one.
    from .grain import auto_select
    from .solver import cabinet_of
    for panel in panels:
        panel.grain_match = panel.id in auto_select(panels, cabinet_of)

    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = panels
    return UploadResponse(job_id=job_id, source=source, panels=panels,
                          warnings=warnings)


class SampleInfo(BaseModel):
    available: bool
    name: str | None = None


@app.get("/api/sample")
def sample_info() -> SampleInfo:
    """Whether a sample file exists. The UI offers it as a button; it never auto-loads."""
    found = find_sample()
    return SampleInfo(available=found is not None, name=found.name if found else None)


@app.post("/api/sample")
def load_sample() -> UploadResponse:
    """Load the sample assembly -- only when the user explicitly asks for it."""
    found = find_sample()
    if found is None:
        raise HTTPException(status_code=404, detail="no STEP file in the project folder")
    return _load(found, found.name)


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)) -> UploadResponse:
    suffix = Path(file.filename or "upload.step").suffix or ".step"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)
    try:
        return _load(tmp_path, file.filename or "uploaded file")
    finally:
        tmp_path.unlink(missing_ok=True)


def _panels_for(req: LayoutRequest) -> list[Panel]:
    stored = JOBS.get(req.job_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="unknown job id -- reload the file")

    # Work on copies. Mutating the stored panels would let one request's overrides
    # leak into the next, so a layout would depend on request history rather than
    # on what was actually asked for.
    panels = [p.model_copy(deep=True) for p in stored]

    relabel = False
    for panel in panels:
        override = req.overrides.get(panel.id)
        if override is None:
            continue
        if override.included is not None:
            panel.included = override.included
        if override.grain_match is not None:
            panel.grain_match = override.grain_match
        if override.grain_locked is not None:
            panel.grain_locked = override.grain_locked
        if override.label is not None:
            stripped = override.label.strip()
            if stripped:
                panel.label = stripped
            else:
                relabel = True
    if relabel:
        assign_labels(panels)
    return panels


@app.post("/api/layout")
def layout(req: LayoutRequest) -> LayoutResult:
    return solve(_panels_for(req), req.params)


@app.post("/api/layout/stream")
def layout_stream(req: LayoutRequest):
    """Newline-delimited JSON: progress frames as the search improves, then the result."""
    panels = _panels_for(req)

    def frames():
        try:
            for frame in solve_streaming(panels, req.params):
                yield json.dumps(frame) + "\n"
        except Exception as exc:                      # never leave the client hanging
            yield json.dumps({"type": "error", "detail": str(exc)}) + "\n"

    return StreamingResponse(frames(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-store",
                                      "X-Accel-Buffering": "no"})


@app.post("/api/prove")
def prove(req: LayoutRequest):
    """Try to prove each sheet's arrangement uses the fewest possible cuts.

    Small sheets finish and yield a real proof; large ones time out and are
    reported as unproven rather than being presented as one.
    """
    from .exact import prove_layout
    from .nesting import PartSpec

    panels = _panels_for(req)
    result = solve(panels, req.params)
    by_id = {p.id: p for p in panels}
    cabinets = {p.id: cabinet_of(p) for p in panels}

    sheet_parts, current = [], []
    for sheet in result.sheets:
        specs = []
        for placement in sheet.placements:
            panel = by_id[placement.panel_id]
            specs.append(PartSpec(id=panel.id, label=panel.label,
                                  w=panel.width_mm, h=panel.length_mm,
                                  grain_locked=panel.grain_locked,
                                  group_key=cabinets[panel.id]))
        sheet_parts.append(specs)
        current.append(len(sheet.cuts))

    proof = prove_layout(sheet_parts, req.params.sheet_width_mm - 2 * req.params.edge_trim_mm,
                         req.params.sheet_length_mm - 2 * req.params.edge_trim_mm,
                         req.params.kerf_mm, current, budget=45.0)
    proof["total_cuts"] = sum(current)
    return proof


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
