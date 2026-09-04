"""Diagnostics for the live pipeline -- two modes, for the two things that
can silently go wrong.

    python3 src/debug_classifier.py            # what does the classifier see?
    python3 src/debug_classifier.py --watch    # is the motion gate firing?

One-shot mode captures a frame, classifies all 64 squares, and prints the
board as a grid (W = white, B = black, . = empty, ? = below --min-conf)
alongside the weakest predictions -- use it to judge whether the trained
model is any good on the live rig.

--watch mode loops printing roi_diff.BoardMotionGate's raw score and state
each poll. The gate is what decides *when* the classifier runs at all: it
needs the score to exceed its motion threshold while your hand is over the
board ("moving"), then drop back down ("settled"). If moving a piece never
prints "moving", the classifier never runs and the board will look frozen
no matter how good the model is -- that's what this mode is for. Press
Ctrl+C to stop.
"""

import argparse
import time
from pathlib import Path

import cv2
import numpy as np

from board_state import FILES, load_calibration
from capture import Camera, CaptureStream
from roi_diff import BoardMotionGate, to_gray_roi
from square_classifier import ALL_SQUARES, BLACK, EMPTY, WHITE, load_classifier
from square_geometry import board_roi_bbox, square_pixel_bboxes

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CALIBRATION = REPO_ROOT / "config" / "calibration.json"
DEFAULT_CLASSIFIER = REPO_ROOT / "models" / "square_classifier_ncnn_model"

_SYMBOLS = {EMPTY: ".", WHITE: "W", BLACK: "B"}


def square_name(square):
    file_idx, rank_idx = square
    return f"{FILES[file_idx]}{rank_idx + 1}"


def classify_with_confidence(model, frame, square_bboxes, imgsz):
    """Like square_classifier.classify_board, but keeps the confidence so
    the debug output can show *how* sure the model was."""
    crops = []
    kept = []
    for square in ALL_SQUARES:
        x1, y1, x2, y2 = square_bboxes[square]
        if x2 <= x1 or y2 <= y1:
            continue
        crops.append(frame[y1:y2, x1:x2])
        kept.append(square)

    results = {}
    if crops:
        for square, result in zip(kept, model.predict(crops, imgsz=imgsz, verbose=False)):
            class_id = int(result.probs.top1)
            results[square] = (result.names[class_id], float(result.probs.top1conf))
    return results


def print_board(results, min_conf):
    print("\n     a   b   c   d   e   f   g   h")
    for rank_idx in reversed(range(8)):
        cells = []
        for file_idx in range(8):
            name, conf = results.get((file_idx, rank_idx), (None, 0.0))
            symbol = "?" if conf < min_conf else _SYMBOLS.get(name, "?")
            cells.append(f"{symbol}{int(conf * 100):3d}")
        print(f"  {rank_idx + 1}  " + " ".join(cells))
    print("     (letter = class, number = confidence %; ? = below --min-conf)\n")


def one_shot(args):
    calibration_matrix = load_calibration(args.calibration)
    print("Loading classifier...")
    model = load_classifier(str(args.classifier))

    with Camera() as cam:
        frame = cam.read_frame()

    image_size = (frame.shape[1], frame.shape[0])
    square_bboxes = square_pixel_bboxes(calibration_matrix, image_size)
    results = classify_with_confidence(model, frame, square_bboxes, args.imgsz)

    print_board(results, args.min_conf)

    counts = {EMPTY: 0, WHITE: 0, BLACK: 0}
    low = []
    for square, (name, conf) in results.items():
        counts[name] = counts.get(name, 0) + 1
        if conf < args.min_conf:
            low.append((conf, square, name))

    print(f"predicted: empty={counts.get(EMPTY, 0)} white={counts.get(WHITE, 0)} black={counts.get(BLACK, 0)}")
    print(f"{len(low)}/64 below --min-conf={args.min_conf}")
    for conf, square, name in sorted(low)[:10]:
        print(f"    {square_name(square):>3}  {name:>5}  {conf:.2f}")


def watch(args):
    calibration_matrix = load_calibration(args.calibration)
    gate = BoardMotionGate() if args.motion_thresh is None else BoardMotionGate(motion_thresh=args.motion_thresh)

    print("Watching the motion gate. Move a piece and see whether it reports 'moving' then")
    print(f"'settled'. Scores at or below the gate's threshold ({gate._motion_thresh}) count as quiet.")
    print("Ctrl+C to stop.\n")

    with Camera() as cam, CaptureStream(cam) as stream:
        frame = None
        while frame is None:
            frame, _timestamp = stream.get_latest()
        roi_bbox = board_roi_bbox(calibration_matrix, (frame.shape[1], frame.shape[0]))

        prev_gray = None
        peak = 0.0
        try:
            while True:
                frame, _timestamp = stream.get_latest()
                if frame is None:
                    continue
                roi_gray = to_gray_roi(frame, roi_bbox)
                score = 0.0 if prev_gray is None else float(np.mean(cv2.absdiff(roi_gray, prev_gray)))
                prev_gray = roi_gray
                peak = max(peak, score)

                state = gate.update(roi_gray)
                marker = "  <-- SETTLED" if state == "settled" else ""
                print(f"  score={score:6.2f}  peak={peak:6.2f}  state={state}{marker}")
                time.sleep(args.poll_interval)
        except KeyboardInterrupt:
            print(f"\nPeak score seen: {peak:.2f} (gate threshold: {gate._motion_thresh})")
            if peak <= gate._motion_thresh:
                print("Never exceeded the threshold -- the gate would never fire, so the")
                print("classifier would never run. The threshold needs lowering.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--calibration", type=Path, default=DEFAULT_CALIBRATION)
    parser.add_argument("--classifier", type=Path, default=DEFAULT_CLASSIFIER)
    parser.add_argument("--min-conf", type=float, default=0.7)
    parser.add_argument("--imgsz", type=int, default=64)
    parser.add_argument("--poll-interval", type=float, default=0.12)
    parser.add_argument("--motion-thresh", type=float, default=None,
                        help="try a candidate motion threshold in --watch mode")
    parser.add_argument("--watch", action="store_true", help="watch the motion gate instead of classifying")
    args = parser.parse_args()

    if not args.calibration.exists():
        raise SystemExit(f"No calibration found at {args.calibration} -- run calibrate.py first.")

    if args.watch:
        watch(args)
    else:
        if not args.classifier.exists():
            raise SystemExit(f"No classifier model found at {args.classifier}.")
        one_shot(args)


if __name__ == "__main__":
    main()
