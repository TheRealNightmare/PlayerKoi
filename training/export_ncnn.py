#!/usr/bin/env python3
"""Export trained weights to NCNN -- the fastest CPU inference backend on the Pi.

Usage:
    python training/export_ncnn.py --weights runs/detect/train/weights/best.pt

Produces a `best_ncnn_model/` directory (a .param + .bin pair) next to the
weights. Copy that whole directory to the Pi with training/deploy.py.
"""

import argparse
import sys
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--weights", type=Path, default=Path("runs/detect/train/weights/best.pt"))
    parser.add_argument("--imgsz", type=int, default=480, help="must match src/detect.py's imgsz")
    return parser.parse_args()


def main():
    args = parse_args()
    if not args.weights.exists():
        raise SystemExit(f"weights not found: {args.weights} (train first)")

    from ultralytics import YOLO

    model = YOLO(str(args.weights))
    out = model.export(format="ncnn", imgsz=args.imgsz)

    out_path = Path(out)
    model_dir = out_path if out_path.is_dir() else out_path.parent
    print(f"\nExported NCNN model: {model_dir.resolve()}")
    contents = sorted(p.name for p in model_dir.glob("*")) if model_dir.is_dir() else []
    print(f"Contents: {contents}")
    print("\nDeploy to the Pi with:")
    print(f"  python training/deploy.py <user>@<pi-host> --model-dir {model_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
