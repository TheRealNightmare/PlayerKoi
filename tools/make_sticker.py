"""Generate the printed chessboard sticker that goes on the panel.

The sticker IS the playing surface and the calibration target: the small dot at
each square's centre is the position the gantry's coordinates address, and it
is what you click when running src/calibrate.py. So it has to print at exactly
1:1 -- a sticker printed at 97% puts every dot progressively further from where
the machine believes it is, and the error is worst at the far corners where you
are least likely to notice it until a piece gets dragged off the board.

This replaces the ReportLab script that produced the earlier
chessboard_{200,220,230}mm PDFs, which was never checked in. It takes no
dependencies so it runs anywhere, and it is parameterised, so the old 230mm
board can still be regenerated:

    python3 tools/make_sticker.py                 # 400mm board, 50mm squares
    python3 tools/make_sticker.py --square 28.75  # the original 230mm board
    python3 tools/make_sticker.py --no-graveyard  # playing area only

PRINTING: 570mm is wider than A3. This needs a wide-format or plotter print at
100% scale -- "actual size", never "fit to page". Check it with a ruler across
the full 400mm grid before sticking it down; see --check to print a test strip.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from vector import PDF  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent.parent / "design"

# Matched to ChessBot_Sticker_green_grey-3.pdf, the sticker currently on the
# rig -- the classifier was trained on crops of these colours, so changing them
# means retraining. See training/NOTES.md.
GREEN = (0.353, 0.588, 0.353)
GREY = (0.804, 0.804, 0.804)
DOT = (0.1, 0.1, 0.1)
EDGE = (0.45, 0.45, 0.45)
LABEL = (0.4, 0.4, 0.4)

DOT_RADIUS_MM = 0.45   # relative to a 28.75mm square in the original
DOT_RADIUS_FRACTION = DOT_RADIUS_MM / 28.75


def build(square_mm, page_mm, graveyard_mm, with_graveyard=True):
    board_mm = square_mm * 8
    pdf = PDF(page_mm, page_mm,
              title=f"ChessBot sticker {board_mm:.0f}x{board_mm:.0f}mm "
                    f"sq{square_mm:g}mm")

    ox = oy = (page_mm - board_mm) / 2
    dot_r = square_mm * DOT_RADIUS_FRACTION

    # a1 is the dark square at the bottom-left as White sees it, so (file +
    # rank) even is green. This matches the sticker on the rig; getting it
    # backwards would flip what the classifier expects every square to look
    # like.
    for rank in range(8):
        for file_ in range(8):
            x = ox + file_ * square_mm
            y = oy + rank * square_mm
            fill = GREEN if (file_ + rank) % 2 == 0 else GREY
            pdf.rect(x, y, square_mm, square_mm, fill=fill)
            pdf.circle(x + square_mm / 2, y + square_mm / 2, dot_r, fill=DOT)

    if with_graveyard and graveyard_mm > 0:
        # A strip on all four sides, 8 slots each, 32 shared between both
        # colours. Corners left blank: a corner slot sits on no board centre
        # line, and 32 already outnumbers the 30 pieces that can be captured.
        #
        # Outlined rather than filled, and that matters more with a full ring
        # than it did with two strips: filled, this would read as a 10x10
        # grid and hand the vision board-crop two plausible edges to lock
        # onto instead of one. Unfilled, the 8x8 is the only block of colour
        # on the sheet.
        for i in range(8):
            along = i * square_mm
            for x, y, w, h in (
                (ox + along, oy - graveyard_mm, square_mm, graveyard_mm),   # past rank 1
                (ox + along, oy + board_mm, square_mm, graveyard_mm),       # past rank 8
                (ox - graveyard_mm, oy + along, graveyard_mm, square_mm),   # past a-file
                (ox + board_mm, oy + along, graveyard_mm, square_mm),       # past h-file
            ):
                pdf.rect(x, y, w, h, stroke=EDGE, lw=0.3)
                pdf.circle(x + w / 2, y + h / 2, dot_r, fill=DOT)

    # A 100mm ruler, so a mis-scaled print is caught with a tape measure
    # instead of with a piece halfway off the board.
    ry = oy - graveyard_mm - 12 if with_graveyard else oy - 12
    if ry > 6:
        pdf.line(ox, ry, ox + 100, ry, stroke=LABEL, lw=0.3)
        for i in range(11):
            x = ox + i * 10
            pdf.line(x, ry, x, ry + (3 if i % 5 else 5), stroke=LABEL, lw=0.3)
        pdf.text(ox + 104, ry - 1.2,
                 "100 mm - measure this. If it is not 100mm, the print is scaled.",
                 size_mm=4, rgb=LABEL)

    pdf.text(ox, page_mm - 10,
             f"ChessBot sticker   board {board_mm:.0f}x{board_mm:.0f}mm   "
             f"square {square_mm:g}mm   print at 100%, actual size",
             size_mm=4.5, rgb=LABEL)
    return pdf, board_mm


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--square", type=float, default=50.0,
                    help="square size in mm (default 50; the old board was 28.75)")
    ap.add_argument("--page", type=float, default=None,
                    help="page size in mm (default: panel size for this square)")
    ap.add_argument("--graveyard", type=float, default=None,
                    help="graveyard strip depth in mm (default: one square)")
    ap.add_argument("--no-graveyard", action="store_true",
                    help="playing area only")
    ap.add_argument("-o", "--out", default=None)
    args = ap.parse_args()

    board_mm = args.square * 8
    graveyard_mm = args.square if args.graveyard is None else args.graveyard
    # Default page: the playing area plus a graveyard strip on each side plus
    # the same 70mm frame allowance the panel has. The ring is symmetric, so
    # this is the same number in both axes.
    page_mm = args.page or (board_mm + 2 * graveyard_mm + 70.0)

    pdf, board_mm = build(args.square, page_mm, graveyard_mm,
                          with_graveyard=not args.no_graveyard)

    OUT_DIR.mkdir(exist_ok=True)
    out = Path(args.out) if args.out else (
        OUT_DIR / f"chessboard_{board_mm:.0f}mm_sq{args.square:g}mm.pdf")
    pdf.save(out)

    print(f"board    {board_mm:.0f} x {board_mm:.0f} mm, {args.square:g}mm squares")
    if not args.no_graveyard:
        print(f"graveyard {graveyard_mm:g}mm strip on all 4 sides, "
              f"8 slots each = 32 shared (corners left blank)")
    print(f"page     {page_mm:.0f} x {page_mm:.0f} mm "
          f"({page_mm * 72 / 25.4:.2f} pts)")
    print(f"wrote    {out}")
    if page_mm > 297:
        print("\nNOTE: wider than A3 -- wide-format print at 100% scale, no fit-to-page.")


if __name__ == "__main__":
    main()
