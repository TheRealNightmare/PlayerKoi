"""Training-data collection for src/square_classifier.py's empty/white/black
per-square classifier.

Unlike training on a public dataset, this collects real photos from your
own camera/board/lighting -- and since the classifier only needs to tell
occupancy+color apart (not piece type), a legal chess position isn't
needed. Each round randomly assigns some squares "white", some "black";
the rest stay empty. Place *any* piece of the right color on the assigned
squares (type doesn't matter), press Enter, and it captures a burst and
saves labeled crops.

    python3 src/calibrate.py --env 1                    # once per environment
    python3 src/collect_square_crops.py --env 1 --rounds 12 --notes "desk lamp"
    python3 src/collect_square_crops.py --env 2 --rounds 12 --notes "daylight"

The --env tag is the point of this script: the classifier only generalises
across lighting, camera height, board and background if it has seen several
of each. Every crop's filename is prefixed with its environment, so
training/eval_by_env.py can later tell you *which* environment the model is
weakest in. Runs are additive -- collecting env 2 never touches env 1's
images.

Requires config/calibration-env<tag>.json (run calibrate.py --env first).
Copy the resulting training/datasets/squares/ directory to your training PC
and run training/train_classifier.py.
"""

import argparse
import datetime as dt
import json
import random
import time
from pathlib import Path

import cv2

from board_state import FILES, calibration_paths, load_calibration
from square_classifier import ALL_SQUARES, BLACK, EMPTY, WHITE
from square_geometry import square_pixel_bboxes

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CALIBRATION = REPO_ROOT / "config" / "calibration.json"
DEFAULT_OUT = REPO_ROOT / "training" / "datasets" / "squares"
MANIFEST_NAME = "manifest.jsonl"

SPLITS = ("train", "val", "test")

BURST_FRAMES = 6
BURST_INTERVAL_S = 0.15
# Splitting whole rounds into train/val/test (not individual frames) so
# near-duplicate burst frames from the same round never leak across the
# split.
VAL_ROUND_FRACTION = 0.15
TEST_ROUND_FRACTION = 0.15


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


def assign_splits(rng, rounds):
    """Assigns each round index to train/val/test.

    Drawn up front from a shuffled list rather than per-round coin flips: a
    12-round session flipping independently can easily produce zero val or
    zero test rounds, which silently yields an unusable split.
    """
    if rounds <= 0:
        return []
    order = list(range(rounds))
    rng.shuffle(order)

    n_val = round(rounds * VAL_ROUND_FRACTION)
    n_test = round(rounds * TEST_ROUND_FRACTION)
    # Every split gets at least one round once there are enough to go around,
    # and train always keeps the remainder.
    if rounds >= len(SPLITS):
        n_val = max(1, n_val)
        n_test = max(1, n_test)
        n_val = min(n_val, rounds - 2)
        n_test = min(n_test, rounds - 1 - n_val)

    splits = {}
    for i, round_idx in enumerate(order):
        if i < n_val:
            splits[round_idx] = "val"
        elif i < n_val + n_test:
            splits[round_idx] = "test"
        else:
            splits[round_idx] = "train"
    return [splits[i] for i in range(rounds)]


def capture_burst(cam, num_frames=BURST_FRAMES, interval_s=BURST_INTERVAL_S):
    frames = []
    for i in range(num_frames):
        if i:
            time.sleep(interval_s)
        frames.append(cam.read_frame())
    return frames


def dataset_counts(out_dir):
    """Per-class file counts already on disk, so a run makes it obvious
    it's adding to an existing dataset rather than replacing it."""
    counts = {}
    for split in SPLITS:
        for label in (EMPTY, WHITE, BLACK):
            directory = out_dir / split / label
            counts[f"{split}/{label}"] = len(list(directory.glob("*.jpg"))) if directory.is_dir() else 0
    return counts


def save_crops(frames, squares, label, square_bboxes, split_dir, session, round_idx):
    out_dir = split_dir / label
    out_dir.mkdir(parents=True, exist_ok=True)
    saved = 0
    for square in squares:
        x1, y1, x2, y2 = square_bboxes[square]
        if x2 <= x1 or y2 <= y1:
            continue
        for frame_idx, frame in enumerate(frames):
            crop = frame[y1:y2, x1:x2]
            # The session prefix is what makes repeat collection additive:
            # round_idx restarts at 0 every run, so without it a second
            # session silently overwrites the first one's files.
            path = out_dir / f"{session}_r{round_idx:03d}_{square_name(square)}_{frame_idx}.jpg"
            cv2.imwrite(str(path), crop)
            saved += 1
    return saved


def append_manifest(out_dir, record):
    """One JSON line per collection run, so 5-6 environments stay auditable:
    what was shot where, when, with which calibration, and how much of it."""
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / MANIFEST_NAME, "a") as f:
        f.write(json.dumps(record) + "\n")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", default=None,
                        help="environment tag (e.g. 1, 2, kitchen). Selects "
                             "config/calibration-env<tag>.json and prefixes every saved crop, so "
                             "training/eval_by_env.py can score each environment separately")
    parser.add_argument("--calibration", type=Path, default=None,
                        help="override the calibration file (default: the one for --env)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--rounds", type=int, default=12)
    parser.add_argument("--seed", type=int, default=None, help="for reproducible round layouts")
    parser.add_argument("--session", default=None,
                        help="label for this collection run (default: a timestamp). Keeps repeat "
                             "runs from overwriting each other -- e.g. --session greenboard")
    parser.add_argument("--notes", default="",
                        help="free-text description recorded in the manifest, e.g. "
                             "'desk lamp, camera 40cm, wooden set'")
    return parser.parse_args()


def main():
    args = parse_args()

    calibration_path = args.calibration
    if calibration_path is None:
        calibration_path = calibration_paths(args.env)[0] if args.env is not None else DEFAULT_CALIBRATION
    if not calibration_path.exists():
        hint = f"--env {args.env}" if args.env is not None else ""
        raise SystemExit(
            f"No calibration found at {calibration_path} -- run "
            f"python3 src/calibrate.py {hint}".rstrip() + " first."
        )
    if args.rounds < 1:
        raise SystemExit("--rounds must be at least 1.")
    if args.env is None:
        print("Warning: no --env tag. Per-environment evaluation won't be able to "
              "attribute these crops.\n")

    session = args.session or dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    if args.env is not None:
        session = f"env{args.env}-{session}"
    existing = dataset_counts(args.out)
    if any(existing.values()):
        print(f"Adding to the existing dataset at {args.out}:")
        for key, value in existing.items():
            print(f"  {key}: {value}")
    print(f"Session id: {session}\n")

    # Imported here, not at module scope, so the pure crop-saving logic
    # stays importable (and testable) on machines without picamera2.
    from capture import Camera

    calibration_matrix = load_calibration(calibration_path)
    rng = random.Random(args.seed)
    counts = {EMPTY: 0, WHITE: 0, BLACK: 0}
    round_splits = assign_splits(rng, args.rounds)

    with Camera() as cam:
        frame = cam.read_frame()
        image_size = (frame.shape[1], frame.shape[0])
        square_bboxes = square_pixel_bboxes(calibration_matrix, image_size)

        for round_idx in range(args.rounds):
            white_squares, black_squares = plan_round(rng)
            assigned = set(white_squares) | set(black_squares)
            empty_squares = [s for s in ALL_SQUARES if s not in assigned]
            split = round_splits[round_idx]

            print(f"\nRound {round_idx + 1}/{args.rounds} ({split}):")
            print(f"  WHITE (any piece) on: {', '.join(square_name(s) for s in sorted(white_squares))}")
            print(f"  BLACK (any piece) on: {', '.join(square_name(s) for s in sorted(black_squares))}")
            print("  Leave every other square empty.")
            input("  Press Enter when the board matches...")

            frames = capture_burst(cam)
            split_dir = args.out / split
            counts[WHITE] += save_crops(frames, white_squares, WHITE, square_bboxes, split_dir, session, round_idx)
            counts[BLACK] += save_crops(frames, black_squares, BLACK, square_bboxes, split_dir, session, round_idx)
            counts[EMPTY] += save_crops(frames, empty_squares, EMPTY, square_bboxes, split_dir, session, round_idx)

    added = {key: dataset_counts(args.out)[key] - existing[key] for key in existing}
    append_manifest(args.out, {
        "env": args.env,
        "session": session,
        "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
        "calibration": str(calibration_path),
        "rounds": args.rounds,
        "round_splits": round_splits,
        "notes": args.notes,
        "counts": added,
    })

    print(f"\nSaved to {args.out}/")
    print(f"  empty: {counts[EMPTY]}  white: {counts[WHITE]}  black: {counts[BLACK]}")
    for key, value in added.items():
        print(f"  {key}: +{value}")
    print(f"Recorded this run in {args.out / MANIFEST_NAME}")
    print("\nNext: copy this directory to your training PC and run")
    print(f"  python training/train_classifier.py --data {args.out}")


if __name__ == "__main__":
    main()
