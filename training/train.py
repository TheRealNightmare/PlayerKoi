#!/usr/bin/env python3
"""Train a YOLOv8n chess-piece detector on this PC's GPU.

Thin wrapper over ultralytics. Defaults follow training/NOTES.md
(yolov8n, 100 epochs, imgsz 480) and are tuned to keep the exported model in
the right size class for Raspberry Pi 5 CPU inference. Everything is
overridable via flags.

Usage:
    python training/train.py --data training/datasets/<name>/data.yaml

Training output lands in runs/detect/train/weights/best.pt (ultralytics
default). Pass --name to label separate runs.
"""

import argparse
import sys
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", type=Path, help="path to the dataset's data.yaml (not needed with --resume)")
    parser.add_argument("--model", default="yolov8n.pt", help="base weights (default: yolov8n.pt)")
    parser.add_argument("--epochs", type=int, default=100)
    parser.add_argument("--imgsz", type=int, default=480, help="keep in sync with src/detect.py's imgsz")
    parser.add_argument("--batch", type=int, default=-1, help="batch size; -1 lets ultralytics auto-size to VRAM")
    parser.add_argument("--device", default="0", help="CUDA device index, or 'cpu'")
    parser.add_argument("--name", default="train", help="run name under runs/detect/")
    parser.add_argument("--allow-cpu", action="store_true", help="don't abort if CUDA is unavailable")
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="dataloader workers. Lower this (2 or 0) on WSL2 -- each worker pins host "
        "memory, and WSL's small RAM ceiling can surface as 'CUDA error: unknown error'",
    )
    parser.add_argument(
        "--no-amp",
        action="store_true",
        help="disable mixed precision, in case AMP is the source of instability",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="continue an interrupted run from runs/detect/<name>/weights/last.pt",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    checkpoint = Path("runs/detect") / args.name / "weights" / "last.pt"
    if args.resume:
        if not checkpoint.exists():
            raise SystemExit(
                f"Nothing to resume: {checkpoint} not found.\n"
                "A run only becomes resumable once it has saved its first epoch."
            )
    elif args.data is None:
        raise SystemExit("--data is required (or pass --resume to continue a previous run)")
    elif not args.data.exists():
        raise SystemExit(f"data.yaml not found: {args.data} (run prepare_dataset.py first)")

    import torch

    cuda_ok = torch.cuda.is_available()
    if args.device != "cpu" and not cuda_ok and not args.allow_cpu:
        raise SystemExit(
            "CUDA is not available -- training would silently run on CPU (very slow).\n"
            "Fix the GPU setup (see training/setup.sh) or pass --allow-cpu to override."
        )
    if cuda_ok and args.device != "cpu":
        print(f"Training on GPU: {torch.cuda.get_device_name(int(args.device))}")
    else:
        print("Training on CPU.")

    from ultralytics import YOLO

    if args.resume:
        # Ultralytics restores data/epochs/imgsz/optimizer state from the
        # checkpoint itself, so the other flags are deliberately not passed.
        print(f"Resuming from {checkpoint}")
        model = YOLO(str(checkpoint))
        model.train(resume=True)
    else:
        model = YOLO(args.model)
        model.train(
            data=str(args.data.resolve()),
            epochs=args.epochs,
            imgsz=args.imgsz,
            batch=args.batch,
            device="cpu" if (args.device == "cpu" or not cuda_ok) else int(args.device),
            name=args.name,
            workers=args.workers,
            amp=not args.no_amp,
        )

    best = Path("runs/detect") / args.name / "weights" / "best.pt"
    print(f"\nDone. Best weights: {best.resolve() if best.exists() else best}")
    data_hint = args.data if args.data else "<path/to/data.yaml>"
    print(f"Next: python training/val.py --weights {best} --data {data_hint}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
