"""Generate a synthetic STEP assembly exercising cases the sample cabinet lacks.

The real Onshape export is all well-behaved plywood. This fixture adds the
awkward ones: a blocky part that is not sheet goods, a panel too big for a
sheet, mixed thicknesses, and a subassembly holding two identical parts (which
must be counted as two panels, not one).
"""
from __future__ import annotations

import sys
from pathlib import Path

from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.gp import gp_Trsf, gp_Vec
from OCP.STEPCAFControl import STEPCAFControl_Writer
from OCP.STEPControl import STEPControl_StepModelType
from OCP.TCollection import TCollection_ExtendedString
from OCP.TDataStd import TDataStd_Name
from OCP.TDocStd import TDocStd_Document
from OCP.TopLoc import TopLoc_Location
from OCP.XCAFDoc import XCAFDoc_DocumentTool

# (name, width, length, thickness, why it is here)
PARTS = [
    ("Wide Panel",   600.0, 900.0, 18.0),
    ("Narrow Strip",  60.0, 800.0, 18.0),
    ("Thin Back",    500.0, 700.0,  6.0),
    ("Blocky Block",  50.0,  50.0, 50.0),   # not sheet goods -- must be rejected
    ("Giant Panel", 1500.0, 3000.0, 18.0),  # bigger than a sheet -- must be flagged
]


def build(path: Path) -> None:
    doc = TDocStd_Document(TCollection_ExtendedString("fixture"))
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())

    root = tool.NewShape()
    TDataStd_Name.Set_s(root, TCollection_ExtendedString("Test Assembly"))

    offset = 0.0
    for name, w, l, t in PARTS:
        box = BRepPrimAPI_MakeBox(w, l, t).Shape()
        label = tool.AddShape(box, False)
        TDataStd_Name.Set_s(label, TCollection_ExtendedString(name))
        trsf = gp_Trsf()
        trsf.SetTranslation(gp_Vec(offset, 0.0, 0.0))
        comp = tool.AddComponent(root, label, TopLoc_Location(trsf))
        TDataStd_Name.Set_s(comp, TCollection_ExtendedString(name))
        offset += w + 100.0

    # A subassembly holding two copies of one part. Both must survive as separate
    # panels -- collapsing them would silently under-order material.
    sub = tool.NewShape()
    TDataStd_Name.Set_s(sub, TCollection_ExtendedString("Shelf Unit"))
    shelf = tool.AddShape(BRepPrimAPI_MakeBox(300.0, 900.0, 18.0).Shape(), False)
    TDataStd_Name.Set_s(shelf, TCollection_ExtendedString("Shelf"))
    for i in range(2):
        trsf = gp_Trsf()
        trsf.SetTranslation(gp_Vec(0.0, 0.0, i * 200.0))
        comp = tool.AddComponent(sub, shelf, TopLoc_Location(trsf))
        TDataStd_Name.Set_s(comp, TCollection_ExtendedString("Shelf"))
    sub_trsf = gp_Trsf()
    sub_trsf.SetTranslation(gp_Vec(0.0, 1200.0, 0.0))
    sub_comp = tool.AddComponent(root, sub, TopLoc_Location(sub_trsf))
    TDataStd_Name.Set_s(sub_comp, TCollection_ExtendedString("Shelf Unit"))

    tool.UpdateAssemblies()

    writer = STEPCAFControl_Writer()
    writer.Transfer(doc, STEPControl_StepModelType.STEPControl_AsIs)
    if writer.Write(str(path)) != 1:
        raise RuntimeError(f"failed to write {path}")


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "tests/fixtures/synthetic.step")
    out.parent.mkdir(parents=True, exist_ok=True)
    build(out)
    print(f"wrote {out}")
