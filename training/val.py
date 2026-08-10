#!/usr/bin/env python3
"""Sanity-check a trained model's per-class precision/recall before exporting.

Weak piece types (queen vs. bishop confusion, under-annotated pawns, etc.) are
much cheaper to catch here than while debugging live on the Pi.

Usage:
    python training/val.py --weights runs/detect/train/weights/best.pt \\
                           --data training/datasets/<name>/data.yaml
"""

import argparse
import sys
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--weights", type=Path, default=Path("runs/detect/train/weights/best.pt"))
    parser.add_argument("--data", type=Path, required=True, help="path to the dataset's data.yaml")
    parser.add_argument("--imgsz", type=int, default=480)
    parser.add_argument("--device", default="0", help="CUDA device index, or 'cpu'")
    return parser.parse_args()


def main():
    args = parse_args()
    if not args.weights.exists():
        raise SystemExit(f"weights not found: {args.weights} (train first)")
    if not args.data.exists():
        raise SystemExit(f"data.yaml not found: {args.data}")

    import torch
    from ultralytics import YOLO

    device = "cpu" if (args.device == "cpu" or not torch.cuda.is_available()) else int(args.device)
    model = YOLO(str(args.weights))
    metrics = model.val(data=str(args.data.resolve()), imgsz=args.imgsz, device=device)

    names = model.names  # {idx: class_name}
    print("\nPer-class results")
    print(f"{'class':<16} {'precision':>10} {'recall':>10} {'mAP50':>10}")
    print("-" * 48)
    # ultralytics exposes per-class arrays keyed by the *index within ap_class_index*.
    ap_class_index = list(metrics.box.ap_class_index)
    for row, cls_idx in enumerate(ap_class_index):
        p = metrics.box.p[row]
        r = metrics.box.r[row]
        ap50 = metrics.box.ap50[row]
        print(f"{names[cls_idx]:<16} {p:>10.3f} {r:>10.3f} {ap50:>10.3f}")

    print("-" * 48)
    print(f"{'overall mAP50':<16} {'':>10} {'':>10} {metrics.box.map50:>10.3f}")
    print(f"{'overall mAP50-95':<16} {'':>10} {'':>10} {metrics.box.map:>10.3f}")
    print("\nIf a piece class sits near 0, gather/label more of it before deploying.")
    print("Otherwise: python training/export_ncnn.py --weights", args.weights)
    return 0


if __name__ == "__main__":
    sys.exit(main())
