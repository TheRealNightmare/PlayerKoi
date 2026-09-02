"""Training-data collection for src/square_classifier.py's empty/white/black
per-square classifier.

Unlike training on a public dataset, this collects real photos from your
own camera/board/lighting -- and since the classifier only needs to tell
occupancy+color apart (not piece type), a legal chess position isn't
needed. Each round randomly assigns some squares "white", some "black";
the rest stay empty. Place *any* piece of the right color on the assigned
squares (type doesn't matter), press Enter, and it captures a burst and
saves labeled crops.

    python3 src/collect_square_crops.py --out training/datasets/squares --rounds 12

Requires config/calibration.json (run calibrate.py first). More rounds =
more data = a more robust classifier; spread rounds across different
times of day / lighting if you can, so the model doesn't overfit to one
lighting condition. Copy the resulting training/datasets/squares/
directory to your training PC and run training/train_classifier.py.
"""

import argparse
import random
import time
from pathlib import Path

import cv2

from board_state import FILES, load_calibration
from capture import Camera
from square_classifier import ALL_SQUARES, BLACK, EMPTY, WHITE
from square_geometry import square_pixel_bboxes

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CALIBRATION = REPO_ROOT / "config" / "calibration.json"
DEFAULT_OUT = REPO_ROOT / "training" / "datasets" / "squares"

BURST_FRAMES = 6
BURST_INTERVAL_S = 0.15
# Splitting whole rounds into train/val (not individual frames) so
# near-duplicate burst frames from the same round never leak across the
# split.
VAL_ROUND_FRACTION = 0.15


def square_name(square):
    file_idx, rank_idx = square
    return f"{FILES[file_idx]}{rank_idx + 1}"


def plan_round(rng, min_each=4, max_each=16):
    """Randomly assigns a subset of the 64 squares to white and another
    subset to black; everything else is implicitly empty for this round."""
    squares = list(ALL_SQUARES)
    rng.shuffle(squares)
    n_white = min(rng.randint(min_each, max_each), len(squares) // 2)
    n_black = min(rng.randint(min_each, max_each), len(squares) // 2)
    white_squares = squares[:n_white]
    black_squares = squares[n_white:n_white + n_black]
    return white_squares, black_squares


def capture_burst(cam, num_frames=BURST_FRAMES, interval_s=BURST_INTERVAL_S):
    frames = []
    for i in range(num_frames):
        if i:
            time.sleep(interval_s)
        frames.append(cam.read_frame())
    return frames


def save_crops(frames, squares, label, square_bboxes, split_dir, round_idx):
    out_dir = split_dir / label
    out_dir.mkdir(parents=True, exist_ok=True)
    saved = 0
    for square in squares:
        x1, y1, x2, y2 = square_bboxes[square]
        if x2 <= x1 or y2 <= y1:
            continue
        for frame_idx, frame in enumerate(frames):
            crop = frame[y1:y2, x1:x2]
            path = out_dir / f"r{round_idx:03d}_{square_name(square)}_{frame_idx}.jpg"
            cv2.imwrite(str(path), crop)
            saved += 1
    return saved


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--calibration", type=Path, default=DEFAULT_CALIBRATION)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--rounds", type=int, default=12)
    parser.add_argument("--seed", type=int, default=None, help="for reproducible round layouts")
    args = parser.parse_args()

    if not args.calibration.exists():
        raise SystemExit(f"No calibration found at {args.calibration} -- run calibrate.py first.")

    calibration_matrix = load_calibration(args.calibration)
    rng = random.Random(args.seed)
    counts = {EMPTY: 0, WHITE: 0, BLACK: 0}

    with Camera() as cam:
        frame = cam.read_frame()
        image_size = (frame.shape[1], frame.shape[0])
        square_bboxes = square_pixel_bboxes(calibration_matrix, image_size)

        for round_idx in range(args.rounds):
            white_squares, black_squares = plan_round(rng)
            assigned = set(white_squares) | set(black_squares)
            empty_squares = [s for s in ALL_SQUARES if s not in assigned]
            split = "val" if rng.random() < VAL_ROUND_FRACTION else "train"

            print(f"\nRound {round_idx + 1}/{args.rounds} ({split}):")
            print(f"  WHITE (any piece) on: {', '.join(square_name(s) for s in sorted(white_squares))}")
            print(f"  BLACK (any piece) on: {', '.join(square_name(s) for s in sorted(black_squares))}")
            print("  Leave every other square empty.")
            input("  Press Enter when the board matches...")

            frames = capture_burst(cam)
            split_dir = args.out / split
            counts[WHITE] += save_crops(frames, white_squares, WHITE, square_bboxes, split_dir, round_idx)
            counts[BLACK] += save_crops(frames, black_squares, BLACK, square_bboxes, split_dir, round_idx)
            counts[EMPTY] += save_crops(frames, empty_squares, EMPTY, square_bboxes, split_dir, round_idx)

    print(f"\nSaved to {args.out}/")
    print(f"  empty: {counts[EMPTY]}  white: {counts[WHITE]}  black: {counts[BLACK]}")
    print("\nNext: copy this directory to your training PC and run")
    print(f"  python training/train_classifier.py --data {args.out}")


if __name__ == "__main__":
    main()
