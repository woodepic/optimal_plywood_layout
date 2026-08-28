"""Read a STEP AP242 assembly into a flat list of panels.

Uses the XDE/OCAF layer (STEPCAFControl_Reader) rather than the plain
STEPControl_Reader -- only the CAF reader preserves part names and the
assembly tree, and both are needed to tell three drawers' "Back" panels apart.
"""
from __future__ import annotations

import re

from OCP.Bnd import Bnd_Box
from OCP.BRepBndLib import BRepBndLib
from OCP.IFSelect import IFSelect_ReturnStatus
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.TCollection import TCollection_ExtendedString
from OCP.TDataStd import TDataStd_Name
from OCP.TDF import TDF_Label, TDF_LabelSequence
from OCP.TDocStd import TDocStd_Document
from OCP.XCAFDoc import XCAFDoc_DocumentTool, XCAFDoc_ShapeTool

from .geometry import classify, measure
from .models import Panel


class StepParseError(RuntimeError):
    pass


# Onshape numbers repeated instances "Drawer <1>", and there is no way to rename
# them individually when they are instances of one part. Read them as "Drawer 1".
_INSTANCE_SUFFIX = re.compile(r"\s*(?:Sub[- ]?assembly|Assembly|Instance)?\s*<(\d+)>\s*$",
                              re.IGNORECASE)


def prettify_name(name: str) -> str:
    """Turn "Drawer <1>" or "Drawer Subassembly <1>" into "Drawer 1"."""
    match = _INSTANCE_SUFFIX.search(name)
    if not match:
        return name.strip()
    return f"{name[:match.start()].strip()} {match.group(1)}".strip()


def _name_of(label: TDF_Label) -> str | None:
    attr = TDataStd_Name()
    if label.FindAttribute(TDataStd_Name.GetID_s(), attr):
        return attr.Get().ToExtString()
    return None


def parse_step(path: str, max_sheet_dim: float = 2438.4) -> tuple[list[Panel], list[str]]:
    """Return (panels, warnings). One Panel per leaf occurrence in the assembly."""
    doc = TDocStd_Document(TCollection_ExtendedString("doc"))
    reader = STEPCAFControl_Reader()
    reader.SetNameMode(True)
    reader.SetColorMode(True)

    status = reader.ReadFile(path)
    if status != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise StepParseError(f"could not read STEP file (status {status})")
    if not reader.Transfer(doc):
        raise StepParseError("STEP file read but contained no transferable shapes")

    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    warnings: list[str] = []
    leaves: list[tuple[list[str], object]] = []

    def walk(label: TDF_Label, path_names: list[str]) -> None:
        name = _name_of(label)
        target = label
        if XCAFDoc_ShapeTool.IsReference_s(label):
            referred = TDF_Label()
            XCAFDoc_ShapeTool.GetReferredShape_s(label, referred)
            # Prefer the occurrence (NAUO) name -- that is what distinguishes
            # "Drawer Subassembly <1>" from "<2>". Fall back to the product name.
            name = name or _name_of(referred)
            target = referred
        if name:
            name = prettify_name(name)

        new_path = path_names + ([name] if name else [])
        if XCAFDoc_ShapeTool.IsAssembly_s(target):
            comps = TDF_LabelSequence()
            XCAFDoc_ShapeTool.GetComponents_s(target, comps)
            for i in range(1, comps.Length() + 1):
                walk(comps.Value(i), new_path)
        else:
            leaves.append((new_path, XCAFDoc_ShapeTool.GetShape_s(label)))

    free = TDF_LabelSequence()
    tool.GetFreeShapes(free)
    if free.Length() == 0:
        raise StepParseError("no shapes found in STEP file")
    for i in range(1, free.Length() + 1):
        walk(free.Value(i), [])

    if not leaves:
        raise StepParseError("assembly contained no solid parts")

    # Drop the root assembly name from every path -- it is the same for all parts
    # and only makes the labels longer.
    if all(len(p) > 1 for p, _ in leaves):
        roots = {p[0] for p, _ in leaves}
        if len(roots) == 1:
            leaves = [(p[1:], s) for p, s in leaves]

    panels: list[Panel] = []
    for i, (path_names, shape) in enumerate(leaves):
        geom = measure(shape)
        is_panel, reason = classify(geom, max_sheet_dim)
        panels.append(Panel(
            id=f"p{i}",
            path=path_names or [f"Part {i + 1}"],
            label=" / ".join(path_names) or f"Part {i + 1}",
            thickness_mm=round(geom.thickness, 4),
            width_mm=round(geom.width, 4),
            length_mm=round(geom.length, 4),
            outline=geom.outline,
            sheet_like=is_panel,
            reject_reason=reason,
            included=is_panel,
        ))

    assign_labels(panels)

    rejected = [p for p in panels if not p.sheet_like]
    if rejected:
        warnings.append(
            f"{len(rejected)} part(s) auto-excluded as non-plywood: "
            + ", ".join(f"{p.full_path} ({p.reject_reason})" for p in rejected[:5])
            + ("..." if len(rejected) > 5 else "")
        )
    missing_outline = sum(1 for p in panels if p.included and p.outline is None)
    if missing_outline:
        warnings.append(
            f"{missing_outline} part(s) drawn as plain rectangles "
            "(true outline could not be extracted)"
        )
    return panels, warnings


def assign_labels(panels: list[Panel]) -> None:
    """Label each panel with the shortest suffix of its path that is unique.

    "Top Stretcher" stays short; the three drawers' "Back" panels grow to
    "Drawer Subassembly <2> / Back" only because they have to.
    """
    depths = {p.id: 1 for p in panels}

    def label_for(panel: Panel, depth: int) -> str:
        return " / ".join(panel.path[-depth:])

    for _ in range(16):
        by_label: dict[str, list[Panel]] = {}
        for p in panels:
            by_label.setdefault(label_for(p, depths[p.id]), []).append(p)

        collisions = [group for group in by_label.values() if len(group) > 1]
        if not collisions:
            break

        progressed = False
        for group in collisions:
            for p in group:
                if depths[p.id] < len(p.path):
                    depths[p.id] += 1
                    progressed = True
        if not progressed:
            # Genuinely identical paths (the same part in the same subassembly
            # placed twice). Number them so they stay distinguishable.
            for group in collisions:
                for n, p in enumerate(group, start=1):
                    p.label = f"{label_for(p, depths[p.id])} #{n}"
            for p in panels:
                if not p.label:
                    p.label = label_for(p, depths[p.id])
            return

    for p in panels:
        p.label = label_for(p, depths[p.id])


def assembly_extent_mm(path: str) -> tuple[float, float, float]:
    """Overall bounding box of the whole file -- used as a unit-scale sanity check."""
    doc = TDocStd_Document(TCollection_ExtendedString("doc"))
    reader = STEPCAFControl_Reader()
    reader.ReadFile(path)
    reader.Transfer(doc)
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    free = TDF_LabelSequence()
    tool.GetFreeShapes(free)
    box = Bnd_Box()
    for i in range(1, free.Length() + 1):
        BRepBndLib.Add_s(XCAFDoc_ShapeTool.GetShape_s(free.Value(i)), box, True)
    xm, ym, zm, xM, yM, zM = box.Get()
    return (xM - xm, yM - ym, zM - zm)
