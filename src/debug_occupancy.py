"""One-shot occupancy/color debugger -- answers "why is this square
flagged?" by printing every square's occupancy margin, color margin, and
final state for a single live frame, instead of just the generic
"low-confidence or inconsistent" message the web UI shows.

    python3 src/debug_occupancy.py
    python3 src/debug_occupancy.py --min-margin-std 1.5
    python3 src/debug_occupancy.py --save-crops debug_masks

Use this when the web UI is stuck flagging every settle. A margin below
--min-margin-std (default matches occupancy_color.DEFAULT_MIN_MARGIN_STD)
means that square would read UNRESOLVED live. If most/all squares are
borderline in the same direction, that points at a global mismatch
(lighting/exposure drifted since calibration, or the calibration burst was
too short to capture realistic noise) rather than a problem with any one
square.

--save-crops writes one PNG per square showing exactly what the color
stage's background-subtraction mask is picking up -- the live crop, the
calibrated background reference, and a visualization with everything
*except* the pixels counted as "foreground" dimmed out. Useful for seeing
whether the mask traces a clean piece silhouette or bleeds into a shadow.
Since the Pi is usually headless, view the results with:

    python3 -m http.server 8080 --directory debug_masks

then open http://<pi-host>:8080/ from a browser on another machine.
"""

import argparse
import json
from pathlib import Path

import cv2
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


def save_square_debug_image(square, frame, baseline, square_bboxes, pixel_threshold, out_dir):
    """Writes <out_dir>/<square>.png: live crop | background reference |
    mask visualization (live crop dimmed to 15% brightness everywhere
    except pixels that pass pixel_threshold, restored to full brightness)
    -- the same mask classify_square_occupancy's color stage would use,
    made visible."""
    x1, y1, x2, y2 = square_bboxes[square]
    if x2 <= x1 or y2 <= y1:
        return
    body_crop = frame[y1:y2, x1:x2]
    background_crop = baseline["background_crops"][square]
    if body_crop.shape != background_crop.shape:
        return

    diff = oc._pixel_diff(body_crop, background_crop)
    mask = diff > pixel_threshold

    dimmed = (body_crop.astype(np.float64) * 0.15).astype(np.uint8)
    mask_vis = dimmed.copy()
    mask_vis[mask] = body_crop[mask]

    separator = np.full((body_crop.shape[0], 2, 3), (0, 0, 255), dtype=np.uint8)
    composite = np.hstack([body_crop, separator, background_crop, separator, mask_vis])

    out_dir.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_dir / f"{square_name(square)}.png"), composite)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--calibration", type=Path, default=DEFAULT_CALIBRATION)
    parser.add_argument("--min-margin-std", type=float, default=oc.DEFAULT_MIN_MARGIN_STD)
    parser.add_argument("--save-crops", type=Path, default=None,
                        help="directory to write one PNG per square visualizing the color mask")
    args = parser.parse_args()

    with open(args.calibration) as f:
        calibration = json.load(f)
    calibration_matrix = load_calibration(args.calibration)
    baseline = oc.load_baseline(calibration, args.calibration)

    occ = calibration["occupancy_diff_threshold"]
    color = calibration["piece_color_baseline"]
    fg = calibration["foreground_pixel_diff_threshold"]
    print("calibrated baseline:")
    print(f"  occupancy: boundary={occ['boundary']:.2f} scale={occ['scale']:.2f}")
    print(f"  color: white_mean={color['white_luminance_mean']:.2f} white_std={color['white_luminance_std']:.2f}  "
          f"black_mean={color['black_luminance_mean']:.2f} black_std={color['black_luminance_std']:.2f}  "
          f"threshold={color['threshold']:.2f} white_is_brighter={color['white_is_brighter']}")
    print(f"  foreground_pixel_scale={fg['scale']:.2f}\n")

    with Camera() as cam:
        frame = cam.read_frame()

    image_size = (frame.shape[1], frame.shape[0])
    square_bboxes = square_pixel_bboxes(calibration_matrix, image_size)
    footprint_bboxes = square_pixel_bboxes(calibration_matrix, image_size, margin_up_px=0)
    baseline = oc.finalize_baseline(baseline, square_bboxes)

    pixel_threshold = args.min_margin_std * baseline["foreground_pixel_scale"]

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

        if args.save_crops:
            save_square_debug_image(square, frame, baseline, square_bboxes, pixel_threshold, args.save_crops)

    if args.save_crops:
        print(f"\nSaved per-square crop/background/mask images to {args.save_crops}/ "
              f"(view with: python3 -m http.server 8080 --directory {args.save_crops})")

    print(f"\n{len(unresolved)}/64 squares UNRESOLVED at --min-margin-std={args.min_margin_std}")
    if unresolved:
        print("  " + ", ".join(square_name(s) for s in unresolved))
    print(f"\noccupancy margin: min={min(occ_margins):.2f} max={max(occ_margins):.2f} "
          f"mean={sum(occ_margins) / len(occ_margins):.2f}  (need >= {args.min_margin_std} or <= -{args.min_margin_std})")


if __name__ == "__main__":
    main()
