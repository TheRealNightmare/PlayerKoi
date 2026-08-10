#!/usr/bin/env python3
"""Remove a junk/unwanted class from a YOLO dataset and reindex the rest.

Some public datasets ship a stray class -- e.g. the Roboflow "Chess Pieces v24"
set has a colorless `bishop` class (a mislabel, only a handful of instances)
alongside the real `white-bishop`/`black-bishop`. That extra class breaks the
12-class {color}-{piece} convention MicroChess expects. This drops it: label
lines for that class are removed, all higher class ids shift down by one, and
data.yaml's `names:` is rewritten to match.

Usage:
    python training/drop_class.py training/datasets/chess --class bishop

Backs up data.yaml to data.yaml.bak and each split's labels/ to labels.bak/
the first time it runs, so it's reversible. Idempotent: if the class is
already gone it does nothing.
"""

import argparse
import shutil
import sys
from pathlib import Path

import yaml

SPLIT_LABEL_DIRS = ["train/labels", "valid/labels", "test/labels"]


def _load_names(data_yaml):
    with open(data_yaml) as f:
        data = yaml.safe_load(f) or {}
    names = data.get("names")
    if isinstance(names, dict):
        names = [names[k] for k in sorted(names, key=lambda x: int(x))]
    if not isinstance(names, list):
        raise SystemExit(f"Could not read a `names:` list from {data_yaml}")
    return data, names


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("dataset", type=Path, help="dataset root (folder containing data.yaml)")
    parser.add_argument("--class", dest="cls", required=True, help="exact class name to drop")
    args = parser.parse_args()

    root = args.dataset
    data_yaml = root / "data.yaml"
    if not data_yaml.exists():
        raise SystemExit(f"data.yaml not found at {data_yaml}")

    data, names = _load_names(data_yaml)
    if args.cls not in names:
        print(f"Class '{args.cls}' not present -- nothing to do (already clean?).")
        print(f"Current classes ({len(names)}): {names}")
        return 0

    drop_idx = names.index(args.cls)
    # old class id -> new class id (None means drop the line)
    remap = {}
    new_id = 0
    for old_id in range(len(names)):
        if old_id == drop_idx:
            remap[old_id] = None
        else:
            remap[old_id] = new_id
            new_id += 1
    new_names = [n for i, n in enumerate(names) if i != drop_idx]

    print(f"Dropping class {drop_idx} ('{args.cls}'); reindexing {len(names)} -> {len(new_names)} classes.")

    dropped_lines = 0
    remapped_lines = 0
    for rel in SPLIT_LABEL_DIRS:
        label_dir = root / rel
        if not label_dir.is_dir():
            continue
        backup = label_dir.with_name(label_dir.name + ".bak")
        if not backup.exists():
            shutil.copytree(label_dir, backup)
        for txt in label_dir.glob("*.txt"):
            out_lines = []
            for line in txt.read_text().splitlines():
                parts = line.split()
                if not parts:
                    continue
                old = int(parts[0])
                new = remap.get(old)
                if new is None:
                    dropped_lines += 1
                    continue
                if new != old:
                    remapped_lines += 1
                parts[0] = str(new)
                out_lines.append(" ".join(parts))
            txt.write_text("\n".join(out_lines) + ("\n" if out_lines else ""))

    bak = data_yaml.with_suffix(data_yaml.suffix + ".bak")
    if not bak.exists():
        bak.write_text(data_yaml.read_text())
    data["names"] = {i: n for i, n in enumerate(new_names)}
    data["nc"] = len(new_names)
    with open(data_yaml, "w") as f:
        yaml.safe_dump(data, f, sort_keys=False)

    print(f"Removed {dropped_lines} label line(s) for '{args.cls}'; reindexed {remapped_lines} line(s).")
    print(f"Backups: data.yaml.bak and labels.bak/ per split.")
    print(f"New classes: {new_names}")
    print(f"\nVerify with:\n  python training/prepare_dataset.py {root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
