"""Minimal PDF / SVG / DXF writers, in millimetres.

These generators emit flat vector art -- rectangles, circles, lines, a little
text -- and nothing else. That is a small enough target to write by hand, and
doing so keeps `tools/` runnable with a bare `python3` on any machine.

That matters here specifically: the repo's `.venv-train` is broken (its
`pyvenv.cfg` points at a `site-packages` under a path the venv no longer lives
at), so `import reportlab` fails even though pip reports it installed. A board
you are about to spend money cutting should not be blocked on that.

Everything takes millimetres with the origin at the bottom-left, the same
convention as the source Illustrator file and as DXF, so the three outputs
agree without per-format flipping.
"""

from pathlib import Path

MM_TO_PT = 72.0 / 25.4

# Bezier circle constant: the control-point offset, as a fraction of the
# radius, that makes four cubic segments approximate a circle.
KAPPA = 0.5522847498307936


def circle_beziers(cx, cy, r):
    """A circle as four cubic segments: (start, [(c1, c2, end), ...])."""
    k = r * KAPPA
    return (cx + r, cy), [
        ((cx + r, cy + k), (cx + k, cy + r), (cx, cy + r)),
        ((cx - k, cy + r), (cx - r, cy + k), (cx - r, cy)),
        ((cx - r, cy - k), (cx - k, cy - r), (cx, cy - r)),
        ((cx + k, cy - r), (cx + r, cy - k), (cx + r, cy)),
    ]


def rounded_rect_path(x, y, w, h, r):
    """A rounded rectangle as (start, segments), where each segment is either
    ('l', end) or ('c', c1, c2, end). Corners run counter-clockwise from the
    bottom edge."""
    k = r * KAPPA
    x1, y1 = x + w, y + h
    return (x + r, y), [
        ("l", (x1 - r, y)),
        ("c", (x1 - r + k, y), (x1, y + r - k), (x1, y + r)),
        ("l", (x1, y1 - r)),
        ("c", (x1, y1 - r + k), (x1 - r + k, y1), (x1 - r, y1)),
        ("l", (x + r, y1)),
        ("c", (x + r - k, y1), (x, y1 - r + k), (x, y1 - r)),
        ("l", (x, y + r)),
        ("c", (x, y + r - k), (x + r - k, y), (x + r, y)),
    ]


# --------------------------------------------------------------------- PDF


class PDF:
    """One page of vector art. Colours are (r, g, b) floats 0..1."""

    def __init__(self, width_mm, height_mm, title="", creator="MicroChess tools"):
        self.w, self.h = width_mm, height_mm
        self.title, self.creator = title, creator
        self.ops = []

    def _p(self, x, y):
        return f"{x * MM_TO_PT:.4f} {y * MM_TO_PT:.4f}"

    def stroke_colour(self, rgb):
        self.ops.append(f"{rgb[0]:.4f} {rgb[1]:.4f} {rgb[2]:.4f} RG")

    def fill_colour(self, rgb):
        self.ops.append(f"{rgb[0]:.4f} {rgb[1]:.4f} {rgb[2]:.4f} rg")

    def line_width(self, mm):
        self.ops.append(f"{mm * MM_TO_PT:.4f} w")

    def rect(self, x, y, w, h, fill=None, stroke=None, lw=0.1):
        if fill:
            self.fill_colour(fill)
        if stroke:
            self.stroke_colour(stroke)
            self.line_width(lw)
        self.ops.append(
            f"{self._p(x, y)} {w * MM_TO_PT:.4f} {h * MM_TO_PT:.4f} re "
            + ("B" if fill and stroke else "f" if fill else "S")
        )

    def circle(self, cx, cy, r, fill=None, stroke=None, lw=0.1):
        start, segs = circle_beziers(cx, cy, r)
        if fill:
            self.fill_colour(fill)
        if stroke:
            self.stroke_colour(stroke)
            self.line_width(lw)
        self.ops.append(f"{self._p(*start)} m")
        for c1, c2, end in segs:
            self.ops.append(f"{self._p(*c1)} {self._p(*c2)} {self._p(*end)} c")
        self.ops.append("B" if fill and stroke else "f" if fill else "S")

    def rounded_rect(self, x, y, w, h, r, fill=None, stroke=None, lw=0.1):
        start, segs = rounded_rect_path(x, y, w, h, r)
        if fill:
            self.fill_colour(fill)
        if stroke:
            self.stroke_colour(stroke)
            self.line_width(lw)
        self.ops.append(f"{self._p(*start)} m")
        for seg in segs:
            if seg[0] == "l":
                self.ops.append(f"{self._p(*seg[1])} l")
            else:
                self.ops.append(
                    f"{self._p(*seg[1])} {self._p(*seg[2])} {self._p(*seg[3])} c")
        self.ops.append("h " + ("B" if fill and stroke else "f" if fill else "S"))

    def line(self, x0, y0, x1, y1, stroke=(0, 0, 0), lw=0.1, dash=None):
        self.stroke_colour(stroke)
        self.line_width(lw)
        if dash:
            pattern = " ".join(f"{d * MM_TO_PT:.3f}" for d in dash)
            self.ops.append(f"[{pattern}] 0 d")
        self.ops.append(f"{self._p(x0, y0)} m {self._p(x1, y1)} l S")
        if dash:
            self.ops.append("[] 0 d")

    def text(self, x, y, s, size_mm=4.0, rgb=(0, 0, 0)):
        escaped = s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        self.fill_colour(rgb)
        self.ops.append(
            f"BT /F1 {size_mm * MM_TO_PT:.3f} Tf {self._p(x, y)} Td ({escaped}) Tj ET")

    def save(self, path):
        content = "\n".join(self.ops).encode("latin-1")
        wpt, hpt = self.w * MM_TO_PT, self.h * MM_TO_PT

        objects = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {wpt:.4f} {hpt:.4f}] "
            f"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>".encode("latin-1"),
            b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            f"<< /Title ({self.title}) /Producer ({self.creator}) >>".encode("latin-1"),
        ]

        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for i, body in enumerate(objects, start=1):
            offsets.append(len(out))
            out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"

        xref_at = len(out)
        n = len(objects) + 1
        out += f"xref\n0 {n}\n".encode()
        out += b"0000000000 65535 f \n"
        for off in offsets:
            out += f"{off:010d} 00000 n \n".encode()
        out += (f"trailer\n<< /Size {n} /Root 1 0 R /Info {len(objects)} 0 R >>\n"
                f"startxref\n{xref_at}\n%%EOF\n").encode()

        Path(path).write_bytes(out)
        return path


# --------------------------------------------------------------------- SVG


class SVG:
    """Same drawing calls as PDF, for eyeballing in a browser. y is flipped on
    the way out so callers keep using bottom-left origin."""

    def __init__(self, width_mm, height_mm):
        self.w, self.h = width_mm, height_mm
        self.parts = []

    def _y(self, y):
        return self.h - y

    @staticmethod
    def _c(rgb):
        return "none" if rgb is None else "#%02x%02x%02x" % tuple(
            max(0, min(255, round(v * 255))) for v in rgb)

    def rect(self, x, y, w, h, fill=None, stroke=None, lw=0.1):
        self.parts.append(
            f'<rect x="{x:.4f}" y="{self._y(y + h):.4f}" width="{w:.4f}" '
            f'height="{h:.4f}" fill="{self._c(fill)}" stroke="{self._c(stroke)}" '
            f'stroke-width="{lw}"/>')

    def circle(self, cx, cy, r, fill=None, stroke=None, lw=0.1):
        self.parts.append(
            f'<circle cx="{cx:.4f}" cy="{self._y(cy):.4f}" r="{r:.4f}" '
            f'fill="{self._c(fill)}" stroke="{self._c(stroke)}" stroke-width="{lw}"/>')

    def rounded_rect(self, x, y, w, h, r, fill=None, stroke=None, lw=0.1):
        self.parts.append(
            f'<rect x="{x:.4f}" y="{self._y(y + h):.4f}" width="{w:.4f}" '
            f'height="{h:.4f}" rx="{r:.4f}" ry="{r:.4f}" fill="{self._c(fill)}" '
            f'stroke="{self._c(stroke)}" stroke-width="{lw}"/>')

    def line(self, x0, y0, x1, y1, stroke=(0, 0, 0), lw=0.1, dash=None):
        d = f' stroke-dasharray="{",".join(str(v) for v in dash)}"' if dash else ""
        self.parts.append(
            f'<line x1="{x0:.4f}" y1="{self._y(y0):.4f}" x2="{x1:.4f}" '
            f'y2="{self._y(y1):.4f}" stroke="{self._c(stroke)}" stroke-width="{lw}"{d}/>')

    def text(self, x, y, s, size_mm=4.0, rgb=(0, 0, 0)):
        body = s.replace("&", "&amp;").replace("<", "&lt;")
        self.parts.append(
            f'<text x="{x:.4f}" y="{self._y(y):.4f}" font-family="Helvetica" '
            f'font-size="{size_mm:.3f}" fill="{self._c(rgb)}">{body}</text>')

    def save(self, path):
        head = (f'<svg xmlns="http://www.w3.org/2000/svg" '
                f'width="{self.w}mm" height="{self.h}mm" '
                f'viewBox="0 0 {self.w} {self.h}">'
                f'<rect width="100%" height="100%" fill="white"/>')
        Path(path).write_text(head + "".join(self.parts) + "</svg>")
        return path


# --------------------------------------------------------------------- DXF


class DXF:
    """AutoCAD R12 DXF -- the format every laser shop accepts without argument.

    R12 has no LWPOLYLINE, so curves go out as many-segment POLYLINEs. That is
    what the cutter wants anyway: it flattens everything before driving the
    head, and a fixed flattening here means what you see is what gets cut.
    """

    ARC_SEGMENTS = 16

    def __init__(self):
        self.entities = []

    def _poly(self, points, layer, closed=True):
        e = [0, "POLYLINE", 8, layer, 66, 1, 70, 1 if closed else 0]
        for x, y in points:
            e += [0, "VERTEX", 8, layer, 10, f"{x:.4f}", 20, f"{y:.4f}"]
        e += [0, "SEQEND", 8, layer]
        self.entities.append(e)

    def circle(self, cx, cy, r, layer="HOLES"):
        self.entities.append(
            [0, "CIRCLE", 8, layer, 10, f"{cx:.4f}", 20, f"{cy:.4f}", 40, f"{r:.4f}"])

    def line(self, x0, y0, x1, y1, layer="GUIDE"):
        self.entities.append(
            [0, "LINE", 8, layer, 10, f"{x0:.4f}", 20, f"{y0:.4f}",
             11, f"{x1:.4f}", 21, f"{y1:.4f}"])

    def rect(self, x, y, w, h, layer="GUIDE"):
        self._poly([(x, y), (x + w, y), (x + w, y + h), (x, y + h)], layer)

    def rounded_rect(self, x, y, w, h, r, layer="CUT"):
        import math
        pts = []
        # (centre, start angle) for each corner, counter-clockwise from bottom-right
        corners = [
            ((x + w - r, y + r), -90.0),
            ((x + w - r, y + h - r), 0.0),
            ((x + r, y + h - r), 90.0),
            ((x + r, y + r), 180.0),
        ]
        for (ccx, ccy), start in corners:
            for i in range(self.ARC_SEGMENTS + 1):
                a = math.radians(start + 90.0 * i / self.ARC_SEGMENTS)
                pts.append((ccx + r * math.cos(a), ccy + r * math.sin(a)))
        self._poly(pts, layer)

    def save(self, path):
        out = ["0", "SECTION", "2", "ENTITIES"]
        for e in self.entities:
            for v in e:
                out.append(str(v))
        out += ["0", "ENDSEC", "0", "EOF"]
        Path(path).write_text("\n".join(out) + "\n")
        return path
