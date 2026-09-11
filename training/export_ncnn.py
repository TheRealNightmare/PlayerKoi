#!/usr/bin/env python3
"""Export trained weights to NCNN -- the fastest CPU inference backend on the Pi.

Usage:
    python training/export_ncnn.py --imgsz 64            # newest run
    python training/export_ncnn.py --imgsz 64 --run train-2

With no --weights or --run it uses the most recent run under runs/classify/
and asks you to confirm (--yes skips the prompt). See training/run_paths.py.

Produces a `best_ncnn_model/` directory (a .param + .bin pair) next to the
weights. Copy that whole directory to the Pi with training/deploy.py.
"""

import argparse
import sys
from pathlib import Path

import run_paths


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    run_paths.add_arguments(parser, "--weights", "path to best.pt (default: the newest run)")
    parser.add_argument("--imgsz", type=int, default=64, help="must match src/square_classifier.py's imgsz")
    return parser.parse_args()


def main():
    args = parse_args()
    weights = run_paths.resolve(args.weights, args.run, kind="pt", assume_yes=args.yes)

    from ultralytics import YOLO

    model = YOLO(str(weights))
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
