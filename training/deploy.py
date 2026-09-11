#!/usr/bin/env python3
"""Copy an exported NCNN model directory to the Raspberry Pi over scp.

src/main.py, src/web_ui.py and src/debug_classifier.py all default to
models/square_classifier_ncnn_model on the Pi, so rename the exported
best_ncnn_model/ directory to that before deploying (or pass --classifier
to each of them).

Delete any existing model directory on the Pi first -- `scp -r` copies
*into* a directory of the same name, producing a nested
square_classifier_ncnn_model/square_classifier_ncnn_model/ that fails to
load.

Usage:
    python training/deploy.py pi@raspberrypi.local
    python training/deploy.py pi@192.168.1.42 --run train-2
    python training/deploy.py pi@raspberrypi.local --dry-run

With no --model-dir or --run it deploys the NCNN export from the most recent
run under runs/classify/ and asks you to confirm (--yes skips the prompt).
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

import run_paths


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("target", help="SSH target, e.g. pi@raspberrypi.local or user@192.168.1.42")
    run_paths.add_arguments(parser, "--model-dir",
                            "exported NCNN model directory (default: from the newest run)")
    parser.add_argument("--dest", default="~/MicroChess/models/", help="destination directory on the Pi")
    parser.add_argument("--dry-run", action="store_true", help="print the scp command without running it")
    return parser.parse_args()


def main():
    args = parse_args()

    if args.dry_run and args.model_dir is not None:
        # Previewing a command for a path that doesn't exist yet is a
        # legitimate use of --dry-run, so don't resolve it.
        model_dir = args.model_dir
    else:
        model_dir = run_paths.resolve(args.model_dir, args.run, kind="ncnn", assume_yes=args.yes)

    if shutil.which("scp") is None:
        raise SystemExit("scp not found on PATH. Install openssh-client.")

    remote = f"{args.target}:{args.dest}"
    cmd = ["scp", "-r", str(model_dir), remote]
    print("Running:", " ".join(cmd))

    if args.dry_run:
        print("(dry run -- not executed)")
        return 0

    result = subprocess.run(cmd)
    if result.returncode != 0:
        return result.returncode

    dest = args.dest.rstrip("/") + "/" + model_dir.name
    print(f"\nCopied to {args.target}:{dest}")
    print("On the Pi, check it before trusting it:")
    print(f"  python3 src/debug_classifier.py --classifier {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
