"""One-shot occupancy/color debugger -- answers "why is this square
flagged?" by printing every square's occupancy margin, color margin, and
final state for a single live frame, instead of just the generic
"low-confidence or inconsistent" message the web UI shows.

    python3 src/debug_occupancy.py
    python3 src/debug_occupancy.py --min-margin-std 1.5

Use this when the web UI is stuck flagging every settle. A margin below
--min-margin-std (default matches occupancy_color.DEFAULT_MIN_MARGIN_STD)
means that square would read UNRESOLVED live. If most/all squares are
borderline in the same direction, that points at a global mismatch
(lighting/exposure drifted since calibration, or the calibration burst was
too short to capture realistic noise) rather than a problem with any one
square.
"""

import argparse
import json
from pathlib import Path

import numpy as np

import occupancy_color as oc
from board_state import FILES, load_calibration
from capture import Camera
from square_geometry import square_pixel_bboxes

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CALIBRATION = REPO_ROOT / "config" / "calibration.json"


def square_name(square):
    file_idx, rank_idx = square
    return f"{FILES[file_idx]}{rank_idx + 1}"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--calibration", type=Path, default=DEFAULT_CALIBRATION)
    parser.add_argument("--min-margin-std", type=float, default=oc.DEFAULT_MIN_MARGIN_STD)
    args = parser.parse_args()

    with open(args.calibration) as f:
        calibration = json.load(f)
    calibration_matrix = load_calibration(args.calibration)
    baseline = oc.load_baseline(calibration, args.calibration)

    with Camera() as cam:
        frame = cam.read_frame()

    image_size = (frame.shape[1], frame.shape[0])
    square_bboxes = square_pixel_bboxes(calibration_matrix, image_size)
    footprint_bboxes = square_pixel_bboxes(calibration_matrix, image_size, margin_up_px=0)
    baseline = oc.finalize_baseline(baseline, square_bboxes)

    unresolved = []
    occ_margins = []
    print(f"{'square':>7} {'state':>10} {'occ_margin':>11} {'margin':>8}")
    for square in oc.ALL_SQUARES:
        mean_bgr = oc._mean_bgr(frame, footprint_bboxes[square])
        empty_mean, _empty_std = baseline["empty"][square]
        diff = float(np.linalg.norm(mean_bgr - empty_mean))
        occ_margin = (diff - baseline["occupancy_boundary"]) / max(baseline["occupancy_scale"], 1e-6)
        occ_margins.append(occ_margin)

        state, margin = oc.classify_square_occupancy(
            frame, square, footprint_bboxes, square_bboxes, baseline, min_margin_std=args.min_margin_std
        )
        if state is oc.UNRESOLVED:
            unresolved.append(square)
        label = "UNRESOLVED" if state is oc.UNRESOLVED else state
        print(f"{square_name(square):>7} {label:>10} {occ_margin:>11.2f} {margin:>8.2f}")

    print(f"\n{len(unresolved)}/64 squares UNRESOLVED at --min-margin-std={args.min_margin_std}")
    if unresolved:
        print("  " + ", ".join(square_name(s) for s in unresolved))
    print(f"\noccupancy margin: min={min(occ_margins):.2f} max={max(occ_margins):.2f} "
          f"mean={sum(occ_margins) / len(occ_margins):.2f}  (need >= {args.min_margin_std} or <= -{args.min_margin_std})")


if __name__ == "__main__":
    main()
