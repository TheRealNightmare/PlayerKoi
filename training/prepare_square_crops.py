#!/usr/bin/env python3
"""Convert the ChessReD2K subset into per-square crop classification data.

Unlike prepare_chessred.py (which produces a YOLO detection dataset for the
full-frame fallback model), this produces an ImageFolder-style
classification dataset -- one crop per square, labeled "empty" or
"{color}-{piece}" -- for the small per-square classifier that
square_classifier.py runs on just the handful of squares roi_diff flags as
changed each move.

ChessReD ships a 4-corner board calibration for every one of the 2,078
ChessReD2K images (annotations["corners"]), separate from the per-piece
bounding boxes (annotations["pieces"]). This script builds a homography
per image from those corners and reuses the *exact same* geometric rule
the Pi uses at inference (board_state.bbox_bottom_center /
image_point_to_square to bucket each piece into a grid cell,
square_geometry.square_pixel_bboxes to crop all 64 squares) so training
crops and live-inference crops are drawn identically.

Prerequisites (from the 4TU ChessReD page, same as prepare_chessred.py):
    annotations.json   (22 MB)
    chessred2k.zip      (unzipped so an `images/` directory exists)

Usage:
    python training/prepare_square_crops.py \
        --annotations training/datasets/chessred_raw/annotations.json \
        --images-root training/datasets/chessred_raw \
        --out training/datasets/chessred_squares

Then train with:
    python training/train_classifier.py --data training/datasets/chessred_squares
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import cv2
import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from board_state import bbox_bottom_center, image_point_to_square  # noqa: E402
from square_geometry import square_pixel_bboxes  # noqa: E402

SPLIT_DIRS = {"train": "train", "val": "valid", "test": "test"}
EMPTY_CLASS = "empty"


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--annotations", type=Path, required=True, help="path to annotations.json")
    parser.add_argument("--images-root", type=Path, required=True,
                        help="directory containing the extracted `images/` tree")
    parser.add_argument("--out", type=Path, required=True, help="output classification dataset directory")
    parser.add_argument("--crop-size", type=int, default=64,
                        help="crops are resized to crop-size x crop-size on save (must match "
                        "src/square_classifier.py's imgsz)")
    return parser.parse_args()


def build_homography(corners):
    # ChessReD's corner labels (top_left/top_right/bottom_right/bottom_left) are
    # image-frame-relative, not board-orientation-relative -- these are handheld
    # photos taken from any side of the board, so "top_left" doesn't consistently
    # mean the same physical corner (a1/h1/h8/a8) across images. That's fine here:
    # we only need internally-consistent bucketing of pieces into grid cells within
    # a single image so each crop gets the right label, not cross-image algebraic
    # square identity -- unlike calibrate.py's homography, which does need a fixed
    # a1/h1/h8/a8 order because it's used live to name squares.
    src = np.array(
        [corners["top_left"], corners["top_right"], corners["bottom_right"], corners["bottom_left"]],
        dtype=np.float32,
    )
    dst = np.array([[0, 0], [8, 0], [8, 8], [0, 8]], dtype=np.float32)
    return cv2.getPerspectiveTransform(src, dst)


def main():
    args = parse_args()
    if not args.annotations.exists():
        raise SystemExit(f"annotations.json not found: {args.annotations}")

    data = json.loads(args.annotations.read_text())
    images = {img["id"]: img for img in data["images"]}
    categories = {c["id"]: c["name"] for c in data["categories"]}

    corners_by_image = {c["image_id"]: c["corners"] for c in data["annotations"]["corners"]}

    pieces_by_image = {}
    for ann in data["annotations"]["pieces"]:
        if not ann.get("bbox"):
            continue
        pieces_by_image.setdefault(ann["image_id"], []).append(ann)

    splits = data["splits"]["chessred2k"]
    out_root = args.out.resolve()

    total_images = sum(len(splits[split]["image_ids"]) for split in SPLIT_DIRS)
    print(f"Processing {total_images} images (each is decoded full-res and cropped into 64 "
          f"squares -- this can take a couple of minutes with no output in between)...")

    totals = Counter()
    skipped_no_corners = 0
    skipped_missing_image = 0
    processed = 0
    progress_every = 100

    for split, dirname in SPLIT_DIRS.items():
        for image_id in splits[split]["image_ids"]:
            processed += 1
            if processed % progress_every == 0 or processed == total_images:
                print(f"  {processed}/{total_images} images processed...")
            corners = corners_by_image.get(image_id)
            if corners is None:
                skipped_no_corners += 1
                continue

            meta = images[image_id]
            src_path = (args.images_root / meta["path"]).resolve()
            image = cv2.imread(str(src_path))
            if image is None:
                skipped_missing_image += 1
                continue

            matrix = build_homography(corners)

            grid = {}
            for ann in pieces_by_image.get(image_id, []):
                class_name = categories[ann["category_id"]]
                if class_name == EMPTY_CLASS:
                    continue
                x, y, w, h = ann["bbox"]
                point = bbox_bottom_center((x, y, x + w, y + h))
                square = image_point_to_square(point, matrix)
                if square is None:
                    continue
                grid[square] = class_name

            bboxes = square_pixel_bboxes(matrix, (meta["width"], meta["height"]))
            stem = Path(meta["file_name"]).stem

            for square, (x1, y1, x2, y2) in bboxes.items():
                if x2 <= x1 or y2 <= y1:
                    continue
                crop = image[y1:y2, x1:x2]
                label = grid.get(square, EMPTY_CLASS)

                class_dir = out_root / dirname / label
                class_dir.mkdir(parents=True, exist_ok=True)
                crop_resized = cv2.resize(
                    crop, (args.crop_size, args.crop_size), interpolation=cv2.INTER_AREA
                )
                file_idx, rank_idx = square
                out_path = class_dir / f"{stem}_{file_idx}{rank_idx}.jpg"
                cv2.imwrite(str(out_path), crop_resized, [cv2.IMWRITE_JPEG_QUALITY, 92])
                totals[f"{dirname}:{label}"] += 1
                totals[dirname] += 1

    print(f"\nWrote crops to {out_root}")
    for dirname in SPLIT_DIRS.values():
        print(f"  {dirname:<6} {totals[dirname]:>6} crops")
    empty_total = sum(v for k, v in totals.items() if k.endswith(f":{EMPTY_CLASS}"))
    piece_total = sum(totals[d] for d in SPLIT_DIRS.values()) - empty_total
    print(f"  empty crops: {empty_total}, piece crops: {piece_total}")
    if skipped_no_corners:
        print(f"  skipped {skipped_no_corners} images with no corners annotation")
    if skipped_missing_image:
        print(f"  skipped {skipped_missing_image} images that failed to load")
    print(f"\nTrain: python training/train_classifier.py --data {out_root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
