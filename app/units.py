"""Unit conversion and shop-friendly formatting.

All internal geometry is in millimetres. Display defaults to fractional inches
because that is what you read off a tape measure at the saw.
"""
from __future__ import annotations

from math import gcd

MM_PER_INCH = 25.4

# Nominal plywood names keyed by approximate measured thickness in mm.
# Real sheet goods are undersized: "3/4 inch" ply is typically 18mm / 0.709".
# These are labels of convenience only -- the tool always reports what it measured.
NOMINAL_THICKNESSES = [
    (6.0, '1/4"'),
    (6.35, '1/4"'),
    (9.0, '3/8"'),
    (9.525, '3/8"'),
    (12.0, '1/2"'),
    (12.7, '1/2"'),
    (15.0, '5/8"'),
    (15.875, '5/8"'),
    (18.0, '3/4"'),
    (19.05, '3/4"'),
    (25.4, '1"'),
]


def mm_to_in(mm: float) -> float:
    return mm / MM_PER_INCH


def in_to_mm(inches: float) -> float:
    return inches * MM_PER_INCH


def format_frac_inches(mm: float, denom: int = 32) -> str:
    """Format a millimetre length as fractional inches, e.g. 24-1/16".

    Rounds to the nearest 1/denom and reduces the fraction.
    """
    inches = mm / MM_PER_INCH
    sign = "-" if inches < 0 else ""
    inches = abs(inches)

    total = round(inches * denom)
    whole, num = divmod(total, denom)
    if num == 0:
        return f'{sign}{whole}"'

    d = denom
    g = gcd(num, d)
    num //= g
    d //= g
    if whole == 0:
        return f'{sign}{num}/{d}"'
    return f'{sign}{whole}-{num}/{d}"'


def format_mm(mm: float) -> str:
    return f"{mm:.1f} mm"


def format_length(mm: float, units: str = "in") -> str:
    return format_frac_inches(mm) if units == "in" else format_mm(mm)


def nominal_thickness_label(mm: float, tol: float = 1.0) -> str | None:
    """Best-guess nominal name for a measured thickness, or None if nothing is close."""
    best = None
    best_delta = tol
    for value, label in NOMINAL_THICKNESSES:
        delta = abs(value - mm)
        if delta < best_delta:
            best_delta = delta
            best = label
    return best
