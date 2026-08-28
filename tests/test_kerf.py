"""Kerf arithmetic. An off-by-one-kerf error compounds across a sheet and
produces a layout that does not fit in reality, so these are exact."""
from app.nesting import SheetLayout


def test_offcut_loses_exactly_one_kerf():
    s = SheetLayout(1000.0, 500.0, 3.0)
    kept, off = s._cut(0, "V", 400.0)
    assert s.pieces[kept].w == 400.0
    assert s.pieces[off].w == 1000.0 - 400.0 - 3.0     # 597
    assert s.pieces[off].x == 403.0                     # offcut starts past the blade


def test_exact_fit_consumes_no_kerf():
    s = SheetLayout(400.0, 500.0, 3.0)
    kept, off = s._cut(0, "V", 400.0)
    assert kept == 0 and off is None
    assert s.pieces[0].cut is None                      # no cut was made at all


def test_remainder_thinner_than_blade_yields_no_offcut():
    s = SheetLayout(402.0, 500.0, 3.0)
    kept, off = s._cut(0, "V", 400.0)
    assert off is None                                  # 2mm remainder is sawdust
    assert s.pieces[0].cut is not None                  # but the cut still happened
    assert s.pieces[kept].w == 400.0


def test_horizontal_cut_mirrors_vertical():
    s = SheetLayout(500.0, 1000.0, 3.0)
    kept, off = s._cut(0, "H", 400.0)
    assert s.pieces[kept].h == 400.0
    assert s.pieces[off].h == 597.0
    assert s.pieces[off].y == 403.0


def test_neighbouring_parts_are_separated_by_exactly_one_kerf():
    """Whichever axis the packer happens to stack them on, the gap is the blade."""
    s = SheetLayout(1000.0, 1000.0, 3.0)
    from app.nesting import PartSpec
    a = PartSpec("a", "A", 400.0, 400.0, grain_locked=True)
    b = PartSpec("b", "B", 400.0, 400.0, grain_locked=True)
    assert s.try_place(a, "BAF", True)
    assert s.try_place(b, "BAF", True)

    pa, pb = (s.pieces[pid] for _, pid, _ in s.placed)
    x_gap = max(pa.x, pb.x) - min(pa.x + pa.w, pb.x + pb.w)
    y_gap = max(pa.y, pb.y) - min(pa.y + pa.h, pb.y + pb.h)
    # Exactly one axis separates them, and by exactly the kerf.
    assert sorted([x_gap, y_gap])[1] == 3.0
    assert not (x_gap > 0 and y_gap > 0)
