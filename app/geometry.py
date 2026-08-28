"""Geometric analysis of a solid: is it a panel, how thick, what shape.

Everything here works off the oriented bounding box rather than an axis-aligned
one -- parts sit at arbitrary angles inside an assembly, and an AABB of a rotated
panel reports meaningless dimensions.
"""
from __future__ import annotations

import math

from OCP.Bnd import Bnd_OBB
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepGProp import BRepGProp
from OCP.BRepTools import BRepTools, BRepTools_WireExplorer
from OCP.GCPnts import GCPnts_QuasiUniformDeflection
from OCP.GeomAbs import GeomAbs_Plane
from OCP.gp import gp_Dir, gp_Vec, gp_Pnt
from OCP.GProp import GProp_GProps
from OCP.TopAbs import TopAbs_FACE
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS

# A part counts as sheet goods when it is plate-like -- both large dimensions
# clearly exceed the thickness -- and the thickness is a plausible plywood value.
# The width ratio is deliberately loose: a 3"-wide stretcher ripped from 3/4" ply
# is a ratio of only 4, and narrow strips like that are ordinary plywood parts.
MIN_WIDTH_RATIO = 1.9
MIN_LENGTH_RATIO = 3.9
MIN_THICKNESS_MM = 3.0
MAX_THICKNESS_MM = 30.0


class PanelGeometry:
    """Measured dimensions and (optionally) the true outline of one solid."""

    def __init__(self, thickness: float, width: float, length: float,
                 outline: list[tuple[float, float]] | None = None):
        self.thickness = thickness
        self.width = width
        self.length = length
        self.outline = outline


def measure(shape) -> PanelGeometry:
    """Measure a solid via its oriented bounding box, and try for a true outline."""
    obb = Bnd_OBB()
    BRepBndLib.AddOBB_s(shape, obb, True, True, True)

    half = [obb.XHSize(), obb.YHSize(), obb.ZHSize()]
    # OBB direction accessors hand back gp_XYZ; gp_Dir is what the maths wants.
    dirs = [gp_Dir(obb.XDirection()), gp_Dir(obb.YDirection()), gp_Dir(obb.ZDirection())]
    extents = [h * 2.0 for h in half]

    order = sorted(range(3), key=lambda i: extents[i])
    thick_i, mid_i, long_i = order
    thickness = extents[thick_i]
    width = extents[mid_i]
    length = extents[long_i]

    outline = None
    try:
        outline = _extract_outline(shape, obb, dirs[thick_i], dirs[mid_i],
                                   dirs[long_i], width, length)
    except Exception:
        # Outline is a display nicety. Unusual topology must never block a layout.
        outline = None

    return PanelGeometry(thickness, width, length, outline)


def _extract_outline(shape, obb, thick_dir, u_dir, v_dir, width, length):
    """Project the largest face perpendicular to the thickness axis into 2D.

    Returns points in panel-local millimetres, origin at the panel's corner,
    u along `width` and v along `length`.
    """
    best_face = None
    best_area = 0.0
    exp = TopExp_Explorer(shape, TopAbs_FACE)
    while exp.More():
        face = TopoDS.Face_s(exp.Current())
        exp.Next()
        surf = BRepAdaptor_Surface(face)
        if surf.GetType() != GeomAbs_Plane:
            continue
        normal = surf.Plane().Axis().Direction()
        if abs(normal.Dot(thick_dir)) < 0.99:
            continue
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, props)
        if props.Mass() > best_area:
            best_area = props.Mass()
            best_face = face

    if best_face is None:
        return None

    centre = gp_Pnt(obb.Center())   # Center() also returns gp_XYZ
    u_vec = gp_Vec(u_dir)
    v_vec = gp_Vec(v_dir)

    def to_2d(p: gp_Pnt) -> tuple[float, float]:
        d = gp_Vec(centre, p)
        return (d.Dot(u_vec) + width / 2.0, d.Dot(v_vec) + length / 2.0)

    wire = BRepTools.OuterWire_s(best_face)
    segments: list[list[tuple[float, float]]] = []
    wexp = BRepTools_WireExplorer(wire)
    while wexp.More():
        edge = wexp.Current()
        wexp.Next()
        curve = BRepAdaptor_Curve(edge)
        sampler = GCPnts_QuasiUniformDeflection(curve, 0.5)
        if not sampler.IsDone() or sampler.NbPoints() < 2:
            continue
        segments.append([to_2d(sampler.Value(i))
                         for i in range(1, sampler.NbPoints() + 1)])

    if not segments:
        return None

    # Each curve is sampled along its own parameter, which need not run the same
    # way round as the wire -- and the first edge can come out backwards too, which
    # would anchor the whole chain the wrong way. So stitch segments end-to-end by
    # matching endpoints rather than trusting the traversal order.
    if len(segments) > 1:
        others = [pt for seg in segments[1:] for pt in (seg[0], seg[-1])]
        head = min(math.dist(segments[0][0], q) for q in others)
        tail = min(math.dist(segments[0][-1], q) for q in others)
        if head < tail:
            segments[0].reverse()

    pts = list(segments[0])
    remaining = segments[1:]
    while remaining:
        best_i, best_rev, best_d = 0, False, float("inf")
        for i, seg in enumerate(remaining):
            for rev in (False, True):
                d = math.dist(pts[-1], seg[-1] if rev else seg[0])
                if d < best_d:
                    best_i, best_rev, best_d = i, rev, d
        seg = remaining.pop(best_i)
        if best_rev:
            seg = seg[::-1]
        if math.dist(pts[-1], seg[0]) < 1e-6:
            seg = seg[1:]
        pts.extend(seg)

    if len(pts) < 3:
        return None

    # Drop consecutive duplicates left behind by shared edge endpoints.
    cleaned = [pts[0]]
    for p in pts[1:]:
        if math.dist(p, cleaned[-1]) > 1e-6:
            cleaned.append(p)
    if len(cleaned) > 1 and math.dist(cleaned[0], cleaned[-1]) < 1e-6:
        cleaned.pop()
    return cleaned if len(cleaned) >= 3 else None


def classify(geom: PanelGeometry, max_sheet_dim: float) -> tuple[bool, str | None]:
    """Decide whether a solid is a plywood panel. Returns (is_panel, reason_if_not)."""
    if geom.thickness <= 0:
        return False, "degenerate geometry"
    if geom.thickness < MIN_THICKNESS_MM:
        return False, f"too thin ({geom.thickness:.1f} mm)"
    if geom.thickness > MAX_THICKNESS_MM:
        return False, f"too thick for sheet goods ({geom.thickness:.1f} mm)"
    if geom.width / geom.thickness < MIN_WIDTH_RATIO:
        return False, "not sheet-like (blocky proportions)"
    if geom.length / geom.thickness < MIN_LENGTH_RATIO:
        return False, "not sheet-like (blocky proportions)"
    if geom.length > max_sheet_dim:
        return False, f"longer than a sheet ({geom.length:.0f} mm)"
    return True, None


def cluster_thicknesses(values: list[float], tol: float = 0.8) -> list[list[int]]:
    """Single-linkage cluster thickness measurements.

    Returns lists of indices into `values`, ordered by thickness descending so the
    heaviest stock is presented first.
    """
    if not values:
        return []
    order = sorted(range(len(values)), key=lambda i: values[i])
    clusters: list[list[int]] = [[order[0]]]
    for idx in order[1:]:
        if values[idx] - values[clusters[-1][-1]] <= tol:
            clusters[-1].append(idx)
        else:
            clusters.append([idx])
    clusters.sort(key=lambda c: -sum(values[i] for i in c) / len(c))
    return clusters
