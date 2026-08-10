#!/usr/bin/env python3
"""Convert the ChessReD2K subset into a YOLO-format detection dataset.

ChessReD (https://data.4tu.nl/datasets/99b5c721-280b-450b-b058-b2900b69a90f) is
10,800 real smartphone photos of chess games. Only the *ChessReD2K* subset
(2,078 images) carries bounding boxes -- the rest have board-state labels only,
which YOLO detection can't use. This script pulls out that subset and writes a
standard YOLO tree.

Its 12 category names already match the {color}-{piece} convention that
src/board_state.py expects, so no remapping is needed. The 13th category
("empty") is dropped -- it marks vacant squares, not an object to detect.

Prerequisites (both from the 4TU page):
    annotations.json   (22 MB)
    chessred2k.zip     (4.7 GB), unzipped so an `images/` directory exists

Usage:
    python training/prepare_chessred.py \
        --annotations training/datasets/chessred/annotations.json \
        --images-root training/datasets/chessred \
        --out training/datasets/chessred_yolo

Then train with the data.yaml it prints.
"""

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

import yaml

# ChessReD's split names -> the directory names we use on disk.
SPLIT_DIRS = {"train": "train", "val": "valid", "test": "test"}
EMPTY_CATEGORY = "empty"


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--annotations", type=Path, required=True, help="path to annotations.json")
    parser.add_argument("--images-root", type=Path, required=True,
                        help="directory containing the extracted `images/` tree")
    parser.add_argument("--out", type=Path, required=True, help="output YOLO dataset directory")
    parser.add_argument("--copy", action="store_true",
                        help="copy image files instead of symlinking (uses ~4.7GB more disk)")
    parser.add_argument(
        "--max-size",
        type=int,
        default=None,
        help="downscale so the longest side is at most this many pixels (e.g. 1024). "
        "ChessReD's originals are 3072x3072, which is ~40x the pixels needed to train at "
        "imgsz 480 -- decoding them dominates epoch time and host RAM. YOLO labels are "
        "normalized, so no label changes are needed.",
    )
    return parser.parse_args()


def build_classes(categories):
    """Return (names list indexed by new class id, {category_id: new class id})."""
    pieces = [c for c in categories if c["name"] != EMPTY_CATEGORY]
    pieces.sort(key=lambda c: c["id"])
    names = [c["name"] for c in pieces]
    remap = {c["id"]: i for i, c in enumerate(pieces)}
    return names, remap


def to_yolo_bbox(bbox, img_w, img_h):
    """COCO [x, y, w, h] (top-left origin, pixels) -> YOLO [cx, cy, w, h] normalized."""
    x, y, w, h = bbox
    cx = (x + w / 2.0) / img_w
    cy = (y + h / 2.0) / img_h
    nw = w / img_w
    nh = h / img_h
    # Boxes can run a hair outside the frame; clamp so ultralytics doesn't reject them.
    cx, cy, nw, nh = (min(max(v, 0.0), 1.0) for v in (cx, cy, nw, nh))
    return cx, cy, nw, nh


def main():
    args = parse_args()
    if not args.annotations.exists():
        raise SystemExit(f"annotations.json not found: {args.annotations}")

    data = json.loads(args.annotations.read_text())
    names, remap = build_classes(data["categories"])
    images = {img["id"]: img for img in data["images"]}

    # Group bbox annotations by image (only the 2K subset has them).
    by_image = {}
    for ann in data["annotations"]["pieces"]:
        if not ann.get("bbox") or ann["category_id"] not in remap:
            continue
        by_image.setdefault(ann["image_id"], []).append(ann)

    splits = data["splits"]["chessred2k"]
    out_root = args.out.resolve()

    totals = Counter()
    missing_images = []
    for split, dirname in SPLIT_DIRS.items():
        img_dir = out_root / dirname / "images"
        lbl_dir = out_root / dirname / "labels"
        img_dir.mkdir(parents=True, exist_ok=True)
        lbl_dir.mkdir(parents=True, exist_ok=True)

        for image_id in splits[split]["image_ids"]:
            meta = images[image_id]
            src = (args.images_root / meta["path"]).resolve()
            if not src.exists():
                missing_images.append(str(src))
                continue

            anns = by_image.get(image_id, [])
            lines = []
            for ann in anns:
                cls = remap[ann["category_id"]]
                cx, cy, w, h = to_yolo_bbox(ann["bbox"], meta["width"], meta["height"])
                if w <= 0 or h <= 0:
                    continue
                lines.append(f"{cls} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}")

            stem = Path(meta["file_name"]).stem
            (lbl_dir / f"{stem}.txt").write_text("\n".join(lines) + ("\n" if lines else ""))

            dst = img_dir / meta["file_name"]
            if dst.exists() or dst.is_symlink():
                dst.unlink()
            if args.max_size and max(meta["width"], meta["height"]) > args.max_size:
                import cv2

                im = cv2.imread(str(src))
                if im is None:
                    missing_images.append(str(src))
                    continue
                scale = args.max_size / max(im.shape[0], im.shape[1])
                im = cv2.resize(
                    im,
                    (round(im.shape[1] * scale), round(im.shape[0] * scale)),
                    interpolation=cv2.INTER_AREA,
                )
                cv2.imwrite(str(dst), im, [cv2.IMWRITE_JPEG_QUALITY, 92])
                totals["resized"] += 1
            elif args.copy:
                import shutil

                shutil.copy2(src, dst)
            else:
                os.symlink(src, dst)

            totals[dirname] += 1
            totals["boxes"] += len(lines)

    if missing_images:
        print(f"WARNING: {len(missing_images)} image file(s) not found under {args.images_root}.")
        print("         Did you unzip chessred2k.zip there? Example missing path:")
        print(f"         {missing_images[0]}")
        if not totals["train"]:
            raise SystemExit("No images were linked -- aborting.")

    data_yaml = out_root / "data.yaml"
    with open(data_yaml, "w") as f:
        yaml.safe_dump(
            {
                "path": str(out_root),
                "train": "train/images",
                "val": "valid/images",
                "test": "test/images",
                "names": {i: n for i, n in enumerate(names)},
                "nc": len(names),
            },
            f,
            sort_keys=False,
        )

    print(f"\nWrote {data_yaml}")
    for dirname in SPLIT_DIRS.values():
        print(f"  {dirname:<6} {totals[dirname]:>5} images")
    print(f"  boxes  {totals['boxes']:>5}")
    print(f"  classes ({len(names)}): {names}")
    print(f"\nVerify:  python training/prepare_dataset.py {data_yaml}")
    print(f"Train:   python training/train.py --data {data_yaml}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
