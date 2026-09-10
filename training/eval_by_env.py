#!/usr/bin/env python3
"""Score the classifier separately for each collection environment.

A single overall accuracy hides the thing you actually need to know with
5-6 environments: *which* one the model is weak in. src/collect_square_crops.py
prefixes every crop with its environment tag (env2-20260911-142030_r003_e4_2.jpg),
so this walks the split, groups by that tag, and prints per-environment
accuracy plus a confusion breakdown. A low row means "go collect more rounds
in that environment" -- augmentation won't fix a lighting or board the model
has never seen.

Usage:
    python training/eval_by_env.py --data training/datasets/squares
    python training/eval_by_env.py --data training/datasets/squares --split val

Crops with no env prefix (collected before tagging existed) are grouped
under "untagged".
"""

import argparse
import sys
from collections import defaultdict
from pathlib import Path

CLASSES = ("empty", "white", "black")
UNTAGGED = "untagged"


def env_of(path):
    """Environment tag from a crop filename, or UNTAGGED.

    Filenames are `{session}_r{round}_{square}_{frame}.jpg`, and a tagged
    session id is `env<tag>-<timestamp>`.
    """
    name = path.name
    if not name.startswith("env"):
        return UNTAGGED
    tag = name[len("env"):].split("-", 1)[0]
    return tag or UNTAGGED


def collect_images(split_dir):
    """(path, true_label) for every crop in the split, grouped nowhere yet."""
    items = []
    for label in CLASSES:
        directory = split_dir / label
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.jpg")):
            items.append((path, label))
    return items


def format_report(stats):
    """stats: {env: {(true, pred): count}} -> a printable table."""
    lines = []
    header = f"{'env':<12}{'images':>8}{'accuracy':>10}   worst confusions"
    lines.append(header)
    lines.append("-" * len(header))

    for env in sorted(stats, key=lambda e: (e == UNTAGGED, e)):
        pairs = stats[env]
        total = sum(pairs.values())
        correct = sum(count for (true, pred), count in pairs.items() if true == pred)
        accuracy = correct / total if total else 0.0

        wrong = sorted(
            ((count, true, pred) for (true, pred), count in pairs.items() if true != pred),
            reverse=True,
        )[:3]
        detail = ", ".join(f"{true}->{pred} x{count}" for count, true, pred in wrong) or "-"
        lines.append(f"{env:<12}{total:>8}{accuracy:>9.1%}   {detail}")

    grand_total = sum(sum(p.values()) for p in stats.values())
    grand_correct = sum(
        count for pairs in stats.values() for (true, pred), count in pairs.items() if true == pred
    )
    lines.append("-" * len(header))
    overall = grand_correct / grand_total if grand_total else 0.0
    lines.append(f"{'ALL':<12}{grand_total:>8}{overall:>9.1%}")
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", type=Path, required=True, help="dataset directory")
    parser.add_argument("--weights", type=Path, default=Path("runs/classify/train/weights/best.pt"))
    parser.add_argument("--split", default="test", choices=("test", "val", "train"))
    parser.add_argument("--imgsz", type=int, default=64, help="keep in sync with training")
    parser.add_argument("--device", default="0", help="CUDA device index, or 'cpu'")
    parser.add_argument("--batch", type=int, default=256, help="images per predict() call")
    return parser.parse_args()


def main():
    args = parse_args()

    split_dir = args.data / args.split
    if not split_dir.is_dir():
        raise SystemExit(f"No {args.split}/ split at {split_dir}")
    if not args.weights.exists():
        raise SystemExit(f"weights not found: {args.weights} (train first)")

    items = collect_images(split_dir)
    if not items:
        raise SystemExit(f"No .jpg crops found under {split_dir}")

    import os

    os.environ.setdefault("ULTRALYTICS_SKIP_REQUIREMENTS_CHECKS", "1")
    from ultralytics import YOLO

    model = YOLO(str(args.weights), task="classify")
    names = model.names

    stats = defaultdict(lambda: defaultdict(int))
    for start in range(0, len(items), args.batch):
        chunk = items[start:start + args.batch]
        results = model.predict(
            [str(path) for path, _ in chunk],
            imgsz=args.imgsz,
            device=args.device,
            verbose=False,
        )
        for (path, true_label), result in zip(chunk, results):
            pred = names[int(result.probs.top1)]
            stats[env_of(path)][(true_label, pred)] += 1
        print(f"  scored {min(start + args.batch, len(items))}/{len(items)}", end="\r")

    print(" " * 40, end="\r")
    print(f"\n{args.split}/ split, {args.weights}\n")
    print(format_report(stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
