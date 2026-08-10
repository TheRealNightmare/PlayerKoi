"""One-shot detection debugger -- answers "is it seeing anything?".

main.py only prints when the board *changes*, so an empty board and a model
detecting nothing look identical (both are all-None). This grabs a single
frame, dumps every raw detection with its confidence and the square it maps
to, and writes an annotated JPEG so you can see boxes and the calibrated grid
together.

    python3 src/debug_detect.py                  # capture from the camera
    python3 src/debug_detect.py --conf 0.05      # show even marginal detections
    python3 src/debug_detect.py --image shot.jpg # re-analyze a saved frame

Use it to pick a --conf value for main.py, and to confirm calibration is
mapping pieces onto the right squares.
"""

import argparse
from pathlib import Path

import cv2
import numpy as np

from board_state import (
    BOARD_SIZE,
    BoardStateTracker,
    bbox_bottom_center,
    format_matrix,
    image_point_to_square,
    load_calibration,
    square_name,
)
from detect import detect, load_model

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CALIBRATION = REPO_ROOT / "config" / "calibration.json"
DEFAULT_MODEL = REPO_ROOT / "models" / "best_ncnn_model"
DEFAULT_OUT = REPO_ROOT / "debug_detect.jpg"

SWEEP = [0.05, 0.10, 0.15, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70]


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--conf", type=float, default=0.05, help="detection threshold for this dump (default: low, to show everything)")
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--calibration", type=Path, default=DEFAULT_CALIBRATION)
    parser.add_argument("--image", type=Path, default=None, help="analyze a saved image instead of the camera")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="annotated output image")
    return parser.parse_args()


def get_frame(image_path):
    if image_path:
        frame = cv2.imread(str(image_path))
        if frame is None:
            raise SystemExit(f"Could not read image: {image_path}")
        print(f"Loaded {frame.shape[1]}x{frame.shape[0]} frame from {image_path}")
        return frame

    from capture import Camera

    with Camera() as cam:
        frame = cam.read_frame()
    print(f"Captured {frame.shape[1]}x{frame.shape[0]} frame from the camera")
    return np.ascontiguousarray(frame)


def draw_grid(canvas, matrix):
    """Project the calibrated board grid back onto the image, to verify calibration."""
    inv = np.linalg.inv(matrix)
    for i in range(BOARD_SIZE + 1):
        for a, b in (((i, 0), (i, BOARD_SIZE)), ((0, i), (BOARD_SIZE, i))):
            pts = np.array([[list(a), list(b)]], dtype=np.float64)
            (x1, y1), (x2, y2) = cv2.perspectiveTransform(pts, inv)[0]
            cv2.line(canvas, (int(x1), int(y1)), (int(x2), int(y2)), (0, 255, 0), 1)


def main():
    args = parse_args()

    if not args.model.exists():
        raise SystemExit(f"No model at {args.model} -- see training/NOTES.md.")

    matrix = None
    if args.calibration.exists():
        matrix = load_calibration(args.calibration)
    else:
        print(f"WARNING: no calibration at {args.calibration} -- squares won't be resolved.\n")

    frame = get_frame(args.image)
    model = load_model(str(args.model))
    detections = detect(model, frame, conf=args.conf)

    print(f"\n{len(detections)} detection(s) at conf >= {args.conf}\n")
    if detections:
        print(f"{'#':>3}  {'class':<14} {'conf':>6}  {'base point':>15}  square")
        print("-" * 60)
        for i, det in enumerate(sorted(detections, key=lambda d: -d["confidence"])):
            px, py = bbox_bottom_center(det["bbox"])
            square = "-"
            if matrix is not None:
                sq = image_point_to_square((px, py), matrix)
                square = square_name(*sq) if sq else "OFF-BOARD"
            print(f"{i:>3}  {det['class']:<14} {det['confidence']:>6.3f}  {px:>7.0f},{py:>7.0f}  {square}")

    # How many survive each threshold -- use this to choose main.py's --conf.
    print("\nDetections by threshold (a full starting board = 32):")
    for t in SWEEP:
        n = sum(1 for d in detections if d["confidence"] >= t)
        bar = "#" * min(n, 50)
        print(f"  conf >= {t:<5} {n:>3}  {bar}")

    if matrix is not None:
        # Feed the same frame enough times to satisfy the tracker's smoothing.
        tracker = BoardStateTracker(matrix)
        for _ in range(3):
            board, _ = tracker.update(detections)
        occupied = sum(1 for row in board for cell in row if cell)
        print(f"\nResulting board ({occupied} squares occupied):\n")
        print(format_matrix(board))

    canvas = frame.copy()
    if matrix is not None:
        draw_grid(canvas, matrix)
    for det in detections:
        x1, y1, x2, y2 = (int(v) for v in det["bbox"])
        cv2.rectangle(canvas, (x1, y1), (x2, y2), (255, 0, 0), 2)
        px, py = bbox_bottom_center(det["bbox"])
        cv2.circle(canvas, (int(px), int(py)), 4, (0, 0, 255), -1)
        cv2.putText(
            canvas,
            f"{det['class']} {det['confidence']:.2f}",
            (x1, max(y1 - 5, 12)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.45,
            (255, 0, 0),
            1,
        )
    cv2.imwrite(str(args.out), canvas)
    print(f"\nAnnotated image -> {args.out}")
    print("  blue box = detection, red dot = base point used for the square, green = calibrated grid")

    if not detections:
        print("\nNothing detected at all. Try --conf 0.01; if still nothing, the model")
        print("likely doesn't generalize to your board/lighting -- fine-tune it on your")
        print("own captured images (see training/NOTES.md).")


if __name__ == "__main__":
    main()
