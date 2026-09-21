"""Read the panel outlines and M3 holes out of the original Illustrator board file.

`Mirajul [Converted].ai` is not a PDF -- it is the legacy Illustrator 8 format,
which is PostScript with AI5's own path operators. So neither pdfminer nor
anything PDF-shaped will open it, but the paths are plain text after
`%%EndSetup` and there are only four operators to care about:

    x y m          moveto, starts a subpath
    x y l / L      lineto
    x1 y1 x2 y2 x3 y3 c / C   curveto
    s / S / f / F  paints and ends the subpath

The uppercase/lowercase pairs differ only in whether the segment is part of a
closed path, which does not matter for reading coordinates back out.

The file is an AutoCAD export: every circle is four bezier arcs, and every
panel outline is a rounded rectangle of lines and arcs. Both fall out of the
subpath bounding boxes without having to flatten any curves, because an arc's
extreme points are its endpoints for these quarter-turn spans.

Run it to print the table; import `read_panels()` to use the numbers.
"""

import re
import sys
from pathlib import Path

# The original lives outside the repo -- it is the user's working file, not a
# checked-in asset. Override with argv[1].
DEFAULT_AI = Path.home() / "Mirajul [Converted].ai"

PT_PER_MM = 72.0 / 25.4

# The horizontal gap that separates one panel from the next. Segments within a
# panel touch end to end, so any positive gap works -- but the two panels sit
# only ~13.6mm apart in this file, so it has to stay well under that.
PANEL_GAP_MM = 5.0

PATH_OPS = {"m", "l", "c", "v", "y", "L", "C", "V", "Y"}
PAINT_OPS = {"s", "S", "f", "F", "b", "B", "n", "N"}


def _pt(v):
    return v / PT_PER_MM


class Subpath:
    def __init__(self, points):
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        self.points = points
        self.x0, self.x1 = min(xs), max(xs)
        self.y0, self.y1 = min(ys), max(ys)
        self.w = self.x1 - self.x0
        self.h = self.y1 - self.y0
        self.cx = (self.x0 + self.x1) / 2
        self.cy = (self.y0 + self.y1) / 2


def parse_subpaths(text):
    """Every painted subpath in the art section, in mm."""
    try:
        art = text.split("%%EndSetup", 1)[1]
    except IndexError:
        raise ValueError("no %%EndSetup -- is this really an AI8/EPS file?")

    subpaths = []
    current = []
    for line in art.splitlines():
        tokens = line.split()
        if not tokens:
            continue
        op = tokens[-1]
        if op in PATH_OPS:
            try:
                nums = [float(t) for t in tokens[:-1]]
            except ValueError:
                continue
            if op == "m" and current:
                subpaths.append(Subpath(current))
                current = []
            for i in range(0, len(nums) - 1, 2):
                current.append((_pt(nums[i]), _pt(nums[i + 1])))
        elif op in PAINT_OPS and current:
            subpaths.append(Subpath(current))
            current = []
    if current:
        subpaths.append(Subpath(current))
    return subpaths


def classify(subpaths):
    """(holes, outline_segments). A hole is a small square-bounded subpath."""
    holes, segments = [], []
    for s in subpaths:
        square = abs(s.w - s.h) < 0.3
        if square and 1.0 < s.w < 20.0 and len(s.points) >= 7:
            holes.append(s)
        else:
            segments.append(s)
    return holes, segments


def group_by_x(items, gap=PANEL_GAP_MM):
    """Split into clusters along x, one per panel.

    Clustering on segment *centres* does not work: a rounded rectangle is
    exported as separate painted segments, so a panel's four corner arcs and
    its four sides have centres scattered across its whole width and get torn
    into several "panels". What actually separates the two panels is the gap
    between one panel's right edge and the next panel's left edge, so sort by
    left edge and break only when a segment starts clear of everything seen so
    far.
    """
    if not items:
        return []
    items = sorted(items, key=lambda s: s.x0)
    groups, current, reach = [], [items[0]], items[0].x1
    for s in items[1:]:
        if s.x0 - reach > gap:
            groups.append(current)
            current = []
            reach = s.x1
        current.append(s)
        reach = max(reach, s.x1)
    groups.append(current)
    return groups


def read_panels(path=None):
    """[{bbox, size, holes}] per panel, holes relative to the panel's own
    bottom-left corner."""
    path = Path(path or DEFAULT_AI)
    text = path.read_text(errors="ignore")
    holes, segments = classify(parse_subpaths(text))

    panels = []
    for group in group_by_x(segments):
        x0 = min(s.x0 for s in group)
        x1 = max(s.x1 for s in group)
        y0 = min(s.y0 for s in group)
        y1 = max(s.y1 for s in group)
        # The corner radius is the arc segments' bounding box -- a quarter turn
        # spans exactly r in each direction. Take the smallest curved segment.
        radii = [s.w for s in group if len(s.points) >= 4 and 0.5 < s.w < 50 and abs(s.w - s.h) < 0.3]
        mine = [
            (h.cx - x0, h.cy - y0, h.w)
            for h in holes
            if x0 - 1 <= h.cx <= x1 + 1 and y0 - 1 <= h.cy <= y1 + 1
        ]
        panels.append({
            "bbox": (x0, y0, x1, y1),
            "size": (x1 - x0, y1 - y0),
            "radius": min(radii) if radii else None,
            "holes": sorted(mine, key=lambda h: (-h[1], h[0])),
        })
    return panels


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_AI
    panels = read_panels(path)
    print(f"{path}\n{len(panels)} panel(s)\n")
    for i, p in enumerate(panels, 1):
        w, h = p["size"]
        x0, y0, x1, y1 = p["bbox"]
        r = p["radius"]
        radius = f"{r:.2f}mm" if r else "?"
        print(f"panel {i}: {w:.2f} x {h:.2f} mm   "
              f"origin ({x0:.2f}, {y0:.2f})   corner r={radius}")
        print(f"  {len(p['holes'])} holes, relative to this panel's bottom-left:")
        for hx, hy, hd in p["holes"]:
            print(f"    x={hx:8.2f}  y={hy:8.2f}  dia={hd:.2f}"
                  f"   (inset right {w - hx:6.2f}, top {h - hy:6.2f})")
        print()


if __name__ == "__main__":
    main()
