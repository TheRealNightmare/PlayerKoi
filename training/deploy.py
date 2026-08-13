#!/usr/bin/env python3
"""Copy an exported NCNN model directory to the Raspberry Pi over scp.

src/main.py expects the model at ~/MicroChess/models/best_ncnn_model on the Pi.

Usage:
    python training/deploy.py pi@raspberrypi.local
    python training/deploy.py pi@192.168.1.42 --model-dir runs/detect/chessred_yolo26l/weights/best_ncnn_model
    python training/deploy.py pi@raspberrypi.local --dry-run
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_MODEL_DIR = Path("runs/detect/chessred/weights/best_ncnn_model")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("target", help="SSH target, e.g. pi@raspberrypi.local or user@192.168.1.42")
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR, help="exported NCNN model directory")
    parser.add_argument("--dest", default="~/MicroChess/models/", help="destination directory on the Pi")
    parser.add_argument("--dry-run", action="store_true", help="print the scp command without running it")
    return parser.parse_args()


def main():
    args = parse_args()

    if not args.dry_run and not args.model_dir.is_dir():
        raise SystemExit(f"model dir not found: {args.model_dir} (run export_ncnn.py first)")

    if shutil.which("scp") is None:
        raise SystemExit("scp not found on PATH. Install openssh-client.")

    remote = f"{args.target}:{args.dest}"
    cmd = ["scp", "-r", str(args.model_dir), remote]
    print("Running:", " ".join(cmd))

    if args.dry_run:
        print("(dry run -- not executed)")
        return 0

    result = subprocess.run(cmd)
    if result.returncode != 0:
        return result.returncode

    dest = args.dest.rstrip("/") + "/" + args.model_dir.name
    print(f"\nCopied to {args.target}:{dest}")
    print("On the Pi, run it with:")
    print(f"  python3 src/main.py --model {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
