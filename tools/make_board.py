"""Generate the scaled-up ChessBot panel: outline, M3 holes, sticker guide.

The original board (`Mirajul [Converted].ai`, read by tools/read_board_ai.py)
is two 300 x 295mm panels with Ø3.30mm M3 clearance holes on a 7.5mm inset
grid, for a 230 x 230mm playing area and 230mm of gantry travel. This scales
that to a 400 x 400mm board with a 500 x 500mm travel envelope.

WHICH DISTANCES SCALE, AND WHICH DO NOT

This is the whole design decision, so it is worth being explicit. Scaling the
drawing uniformly would be wrong, because only some of those numbers are about
the board:

  scales      the 172.5mm span between the mid-height hole pairs. That is
              exactly 6 x 28.75mm -- six squares -- so it is board geometry
              and becomes 6 x 50 = 300mm.

  does NOT    the 7.5mm edge inset, the 27.5mm spacing within a hole pair, the
              3.30mm hole diameter, and the 7.5mm corner radius. Those are all
              fixed hardware -- M3 screws, bearing blocks, end supports -- and
              they are the same parts on the bigger machine. Scaling them would
              put the holes where no bracket reaches.

  follows     the panel size, which is travel plus a fixed frame allowance
              (70mm in x, 65mm in y on the original). New travel is square, so
              the panel is made square at the larger of the two allowances.

Outputs into design/:
    ChessBot_V2_board.dxf   for the laser shop
    ChessBot_V2_board.ai    PDF-based; modern Illustrator opens it natively
    ChessBot_V2_board.svg   quick visual check in a browser
    ChessBot_V2_board.step  3D solid, for fitting into the CAD assembly

Run: python3 tools/make_board.py
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import step_writer  # noqa: E402
from vector import DXF, PDF, SVG  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent.parent / "design"

# ------------------------------------------------------------ board geometry

SQUARE_MM = 50.0
BOARD_MM = SQUARE_MM * 8                     # 400 -- the 8x8 playing area
TRAVEL_MM = 500.0                            # total gantry envelope, both axes
GRAVEYARD_MM = (TRAVEL_MM - BOARD_MM) / 2    # 50 -- the strip on each side

# Frame allowance beyond travel, measured off the original: 300 - 230 = 70 in
# x, 295 - 230 = 65 in y. Travel is square now, so use the larger for both and
# keep the panel square -- the extra 5mm in y costs nothing and squares up the
# cut.
FRAME_ALLOWANCE_MM = 70.0
PANEL_MM = TRAVEL_MM + FRAME_ALLOWANCE_MM    # 570

# ------------------------------------------------------------ fixed hardware

CORNER_R_MM = 7.5
HOLE_DIA_MM = 3.30
EDGE_INSET_MM = 7.5      # every hole sits this far in from the panel edge
PAIR_PITCH_MM = 27.5     # spacing within a hole pair
MID_SPAN_SQUARES = 6     # the mid-height pairs were 6 squares apart

PANEL_GAP_MM = 13.6      # gap between the two panels, as in the original

# Panel thickness, for the 3D model only -- the 2D outputs do not care. 5mm
# cast acrylic is the recommendation; 6mm MDF is the other option, and the
# V1 PCB's 1.6mm is far too floppy to span 570mm. Override with --thickness.
DEFAULT_THICKNESS_MM = 5.0

# ------------------------------------------------------------------- colours

CUT = (0.0, 0.0, 0.0)
GUIDE = (0.65, 0.65, 0.65)
LABEL = (0.35, 0.35, 0.35)


def hole_positions(panel_mm=PANEL_MM):
    """(panel_1_holes, panel_2_holes), each a list of (x, y) in panel space.

    Panel 1 carries the 6 frame holes; panel 2 adds the two mid-height pairs,
    exactly as the original pair of panels does.
    """
    near = EDGE_INSET_MM
    far = panel_mm - EDGE_INSET_MM
    xs = [near, far - PAIR_PITCH_MM, far]
    ys = [near, far]

    panel1 = [(x, y) for y in ys for x in xs]

    mid_span = MID_SPAN_SQUARES * SQUARE_MM          # 300
    centre = panel_mm / 2
    mid_ys = [centre - mid_span / 2, centre + mid_span / 2]
    panel2 = panel1 + [(x, y) for y in mid_ys for x in xs[1:]]

    return panel1, sorted(panel2, key=lambda p: (-p[1], p[0]))


def board_origin(panel_mm=PANEL_MM):
    """Bottom-left of the 8x8 playing area within a panel -- it is centred."""
    o = (panel_mm - BOARD_MM) / 2
    return o, o


def draw(canvas, ox, holes, panel_mm=PANEL_MM, guides=True):
    """One panel onto any of the three canvases, offset by ox."""
    is_dxf = isinstance(canvas, DXF)

    if is_dxf:
        canvas.rounded_rect(ox, 0, panel_mm, panel_mm, CORNER_R_MM, layer="CUT")
    else:
        canvas.rounded_rect(ox, 0, panel_mm, panel_mm, CORNER_R_MM,
                            stroke=CUT, lw=0.2)

    for hx, hy in holes:
        if is_dxf:
            canvas.circle(ox + hx, hy, HOLE_DIA_MM / 2, layer="HOLES")
        else:
            canvas.circle(ox + hx, hy, HOLE_DIA_MM / 2, stroke=CUT, lw=0.2)

    if not guides:
        return

    # Non-cut registration guide: where the sticker's grid and the four
    # graveyard strips land. On the DXF this is its own layer so the shop can
    # switch it off; on paper it is grey.
    bx, by = board_origin(panel_mm)
    bx += ox

    for i in range(9):
        p = i * SQUARE_MM
        if is_dxf:
            canvas.line(bx + p, by, bx + p, by + BOARD_MM, layer="GUIDE")
            canvas.line(bx, by + p, bx + BOARD_MM, by + p, layer="GUIDE")
        else:
            canvas.line(bx + p, by, bx + p, by + BOARD_MM, stroke=GUIDE, lw=0.15)
            canvas.line(bx, by + p, bx + BOARD_MM, by + p, stroke=GUIDE, lw=0.15)

    # The graveyard: a strip on all four sides, 8 slots of one square each, 32
    # in total shared between both colours. The four corner cells are left
    # blank -- a corner slot would sit on no board centre line, so its dot
    # would miss the grid, and 32 already outnumbers the 30 pieces that can
    # ever be captured.
    for strip_x, strip_y, horizontal in (
        (bx, by - GRAVEYARD_MM, True),      # past rank 1
        (bx, by + BOARD_MM, True),          # past rank 8
        (bx - GRAVEYARD_MM, by, False),     # past the a-file
        (bx + BOARD_MM, by, False),         # past the h-file
    ):
        w, h = (BOARD_MM, GRAVEYARD_MM) if horizontal else (GRAVEYARD_MM, BOARD_MM)
        if is_dxf:
            canvas.rect(strip_x, strip_y, w, h, layer="GUIDE")
        else:
            canvas.rect(strip_x, strip_y, w, h, stroke=GUIDE, lw=0.15)
        # The 7 dividers between the 8 slots, along whichever axis is long.
        for i in range(1, 8):
            p = i * SQUARE_MM
            if horizontal:
                x0, y0, x1, y1 = strip_x + p, strip_y, strip_x + p, strip_y + h
            else:
                x0, y0, x1, y1 = strip_x, strip_y + p, strip_x + w, strip_y + p
            if is_dxf:
                canvas.line(x0, y0, x1, y1, layer="GUIDE")
            else:
                canvas.line(x0, y0, x1, y1, stroke=GUIDE, lw=0.15)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--thickness", type=float, default=DEFAULT_THICKNESS_MM,
                    help="panel thickness in mm for the STEP model "
                         f"(default {DEFAULT_THICKNESS_MM:g}; 6 for MDF)")
    args = ap.parse_args()

    OUT_DIR.mkdir(exist_ok=True)
    panel1, panel2 = hole_positions()
    total_w = PANEL_MM * 2 + PANEL_GAP_MM
    ox2 = PANEL_MM + PANEL_GAP_MM

    pdf = PDF(total_w, PANEL_MM, title=f"ChessBot V2 board {PANEL_MM:.0f}x{PANEL_MM:.0f}mm")
    svg = SVG(total_w, PANEL_MM)
    dxf = DXF()

    for canvas in (pdf, svg, dxf):
        draw(canvas, 0, panel1)
        draw(canvas, ox2, panel2)

    for canvas in (pdf, svg):
        canvas.text(EDGE_INSET_MM + 6, PANEL_MM - 8,
                    f"ChessBot V2  panel {PANEL_MM:.0f}x{PANEL_MM:.0f}mm  "
                    f"board {BOARD_MM:.0f}mm  square {SQUARE_MM:.0f}mm  "
                    f"travel {TRAVEL_MM:.0f}mm  M3 x{len(panel1)}",
                    size_mm=5, rgb=LABEL)
        canvas.text(ox2 + EDGE_INSET_MM + 6, PANEL_MM - 8,
                    f"panel 2  M3 x{len(panel2)}", size_mm=5, rgb=LABEL)

    pdf.save(OUT_DIR / "ChessBot_V2_board.ai")
    svg.save(OUT_DIR / "ChessBot_V2_board.svg")
    dxf.save(OUT_DIR / "ChessBot_V2_board.dxf")

    # The 3D model. Only the cut geometry goes in -- outline and holes. The
    # grid and graveyard guides are sticker registration marks, not features
    # of the part, and putting them in a solid model would be lying about it.
    plates = [
        step_writer.Plate(
            step_writer.rounded_rect(ox, 0, PANEL_MM, PANEL_MM, CORNER_R_MM),
            [(ox + hx, hy, HOLE_DIA_MM) for hx, hy in holes],
            args.thickness,
            name=label,
        )
        for ox, holes, label in ((0, panel1, "panel_1"), (ox2, panel2, "panel_2"))
    ]
    step_writer.write(OUT_DIR / "ChessBot_V2_board.step", plates,
                      name="ChessBot V2 board")

    print(f"panel      {PANEL_MM:.0f} x {PANEL_MM:.0f} mm, corner r {CORNER_R_MM}mm")
    print(f"board      {BOARD_MM:.0f} x {BOARD_MM:.0f} mm ({SQUARE_MM:.0f}mm squares)")
    print(f"travel     {TRAVEL_MM:.0f} x {TRAVEL_MM:.0f} mm "
          f"({GRAVEYARD_MM:.0f}mm graveyard strip per side)")
    print(f"holes      {len(panel1)} on panel 1, {len(panel2)} on panel 2, "
          f"Ø{HOLE_DIA_MM}mm")
    print(f"sheet      {total_w:.1f} x {PANEL_MM:.0f} mm for both panels")
    print(f"thickness  {args.thickness:g} mm (STEP model only)\n")
    for name in ("ChessBot_V2_board.ai", "ChessBot_V2_board.svg",
                 "ChessBot_V2_board.dxf", "ChessBot_V2_board.step"):
        print(f"  wrote design/{name}")


if __name__ == "__main__":
    main()
