import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def find_step(substring: str) -> Path | None:
    """Locate a sample by name, so tests pin to a known file rather than
    whichever STEP file happens to sort or date first in the project folder."""
    for path in sorted(ROOT.glob("*.step")):
        if substring.lower() in path.name.lower():
            return path
    return None


CABINET = find_step("Final Assembly")     # one cabinet, 21 panels
LAYOUT = find_step("Layout")              # six cabinets, 144 panels
