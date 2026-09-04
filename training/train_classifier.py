#!/usr/bin/env python3
"""Train the empty/white/black per-square classifier on this PC's GPU.

Thin wrapper over `yolo classify train`, mirroring training/train.py's
structure. A 3-class occupancy+color problem is dramatically easier than
the retired 12-piece-type detector, so this needs far less data/training
time -- defaults follow that (yolov8n-cls, 50 epochs, imgsz=64, matching
src/square_classifier.py's inference size).

Usage:
    python training/train_classifier.py --data training/datasets/squares

    # fine-tune from an existing model (faster, keeps what it learned --
    # the right choice when adding data, e.g. a second board)
    python training/train_classifier.py --data training/datasets/squares \
        --model runs/classify/train/weights/best.pt

`--data` is the dataset *directory* (not a data.yaml) -- ultralytics'
classify task expects train/<class>/*.jpg and val/<class>/*.jpg
subfolders, exactly what src/collect_square_crops.py produces.

Augmentation defaults are tuned for this task (see the flags below), but
augmentation is not a substitute for real crops: a new board or new
lighting needs a real collection run.

Training output lands in runs/classify/train/weights/best.pt (ultralytics
default). Pass --name to label separate runs.
"""

import argparse
import sys
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", type=Path, help="path to the dataset directory (not needed with --resume)")
    parser.add_argument("--model", default="yolov8n-cls.pt", help="base weights (default: yolov8n-cls.pt)")
    parser.add_argument("--epochs", type=int, default=50)
    parser.add_argument("--imgsz", type=int, default=64, help="keep in sync with src/square_classifier.py's imgsz")
    parser.add_argument("--batch", type=int, default=-1, help="batch size; -1 lets ultralytics auto-size to VRAM")
    parser.add_argument("--device", default="0", help="CUDA device index, or 'cpu'")
    parser.add_argument("--name", default="train", help="run name under runs/classify/")
    parser.add_argument("--allow-cpu", action="store_true", help="don't abort if CUDA is unavailable")
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="dataloader workers. Lower this (2 or 0) on WSL2 -- each worker pins host "
        "memory, and WSL's small RAM ceiling can surface as 'CUDA error: unknown error'",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="continue an interrupted run from runs/classify/<name>/weights/last.pt",
    )
    # Augmentation, tuned for this task rather than left at library
    # defaults. Brightness jitter matters most here -- lighting variation
    # has been this project's main source of misreads. Rotation/scale/
    # translate absorb slight camera or board shifts so minor calibration
    # drift doesn't wreck accuracy. Both flips are safe: a piece is the
    # same colour whichever way it's flipped, and flipud (off by default
    # in ultralytics) pushes the model to decide on colour and presence
    # rather than on where in the crop the piece happens to sit -- which
    # matters because pieces of different heights fill the crop
    # differently.
    parser.add_argument("--hsv-v", type=float, default=0.4, help="brightness jitter")
    parser.add_argument("--hsv-s", type=float, default=0.7, help="saturation jitter")
    parser.add_argument("--hsv-h", type=float, default=0.015, help="hue jitter")
    parser.add_argument("--degrees", type=float, default=10.0, help="random rotation, degrees")
    parser.add_argument("--scale", type=float, default=0.2, help="random scale gain")
    parser.add_argument("--translate", type=float, default=0.1, help="random translation fraction")
    parser.add_argument("--fliplr", type=float, default=0.5, help="horizontal flip probability")
    parser.add_argument("--flipud", type=float, default=0.5, help="vertical flip probability")
    return parser.parse_args()


def main():
    args = parse_args()

    checkpoint = Path("runs/classify") / args.name / "weights" / "last.pt"
    if args.resume:
        if not checkpoint.exists():
            raise SystemExit(
                f"Nothing to resume: {checkpoint} not found.\n"
                "A run only becomes resumable once it has saved its first epoch."
            )
    elif args.data is None:
        raise SystemExit("--data is required (or pass --resume to continue a previous run)")
    elif not args.data.is_dir():
        raise SystemExit(f"dataset directory not found: {args.data} (run collect_square_crops.py first)")

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
            hsv_h=args.hsv_h,
            hsv_s=args.hsv_s,
            hsv_v=args.hsv_v,
            degrees=args.degrees,
            scale=args.scale,
            translate=args.translate,
            fliplr=args.fliplr,
            flipud=args.flipud,
        )

    best = Path("runs/classify") / args.name / "weights" / "best.pt"
    print(f"\nDone. Best weights: {best.resolve() if best.exists() else best}")
    print(f"Next: python training/export_ncnn.py --weights {best} --imgsz {args.imgsz}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
