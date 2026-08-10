#!/usr/bin/env python3
"""Validate (and optionally fix) a downloaded YOLO dataset's class names.

Point this at the folder you unzipped a Roboflow "YOLOv8" export into. It
finds the dataset's data.yaml, reads its class names, and checks them against
the 12 classes MicroChess expects (matching src/board_state.py):

    white-king white-queen white-rook white-bishop white-knight white-pawn
    black-king black-queen black-rook black-bishop black-knight black-pawn

Usage:
    python training/prepare_dataset.py training/datasets/<name>
    python training/prepare_dataset.py training/datasets/<name> --fix

Without --fix it only reports. With --fix it rewrites data.yaml's `names:` to
the canonical spelling when every class maps unambiguously (backing up the
original to data.yaml.bak). Classes it cannot map are reported so you can hand-
edit data.yaml or add them to NAME_MAP in src/detect.py instead.
"""

import argparse
import sys
from pathlib import Path

import yaml

COLORS = ("white", "black")
PIECES = ("king", "queen", "rook", "bishop", "knight", "pawn")
CANONICAL = [f"{c}-{p}" for c in COLORS for p in PIECES]
CANONICAL_SET = set(CANONICAL)

# FEN single-letter -> piece (case decides color: upper=white, lower=black).
_FEN_PIECE = {"k": "king", "q": "queen", "r": "rook", "b": "bishop", "n": "knight", "p": "pawn"}

# Multi-token abbreviations (used only when a color token is also present, so
# the bishop/black "b" collision can't happen).
_COLOR_TOKENS = {"white": "white", "w": "white", "black": "black", "b": "black"}
_PIECE_TOKENS = {
    "king": "king", "k": "king",
    "queen": "queen", "q": "queen",
    "rook": "rook", "r": "rook",
    "bishop": "bishop",  # deliberately no "b": it collides with black
    "knight": "knight", "n": "knight",
    "pawn": "pawn", "p": "pawn",
}


def _normalize(name):
    """Same normalization src/detect.py applies at inference time."""
    return str(name).strip().lower().replace(" ", "-").replace("_", "-")


def canonicalize(name):
    """Map a raw dataset class name to one of the 12 canonical labels, or None."""
    norm = _normalize(name)
    if norm in CANONICAL_SET:
        return norm

    # Single-letter FEN notation, e.g. "P" -> white-pawn, "n" -> black-knight.
    raw = str(name).strip()
    if len(raw) == 1 and raw.lower() in _FEN_PIECE:
        color = "white" if raw.isupper() else "black"
        return f"{color}-{_FEN_PIECE[raw.lower()]}"

    # Token-based: find a color token and a piece token in any order.
    tokens = [t for t in norm.split("-") if t]
    color = next((_COLOR_TOKENS[t] for t in tokens if t in _COLOR_TOKENS), None)
    piece = next((_PIECE_TOKENS[t] for t in tokens if t in _PIECE_TOKENS), None)
    if color and piece:
        return f"{color}-{piece}"
    return None


def _find_data_yaml(dataset_path):
    p = Path(dataset_path)
    if p.is_file() and p.name.endswith((".yaml", ".yml")):
        return p
    if p.is_dir():
        direct = p / "data.yaml"
        if direct.exists():
            return direct
        candidates = sorted(p.glob("*.yaml")) + sorted(p.glob("*.yml"))
        candidates += sorted(p.glob("**/data.yaml"))
        if candidates:
            return candidates[0]
    return None


def _load_names(data_yaml):
    with open(data_yaml) as f:
        data = yaml.safe_load(f) or {}
    names = data.get("names")
    if isinstance(names, dict):  # {0: "white-pawn", ...}
        names = [names[k] for k in sorted(names, key=lambda x: int(x))]
    if not isinstance(names, list):
        raise SystemExit(f"Could not read a `names:` list from {data_yaml}")
    return data, names


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("dataset", type=Path, help="unzipped dataset folder (or path to data.yaml)")
    parser.add_argument("--fix", action="store_true", help="rewrite data.yaml names to canonical spelling")
    args = parser.parse_args()

    data_yaml = _find_data_yaml(args.dataset)
    if not data_yaml:
        raise SystemExit(f"No data.yaml found under {args.dataset}")
    print(f"data.yaml: {data_yaml.resolve()}\n")

    data, names = _load_names(data_yaml)

    mapping = {}     # index -> canonical (or None)
    print(f"{'idx':>3}  {'dataset name':<20} -> canonical")
    print("-" * 48)
    for i, raw in enumerate(names):
        canon = canonicalize(raw)
        mapping[i] = canon
        marker = "" if canon else "   <-- UNMAPPED"
        print(f"{i:>3}  {str(raw):<20} -> {canon or '?'}{marker}")

    mapped = [c for c in mapping.values() if c]
    unmapped = [names[i] for i, c in mapping.items() if not c]
    present = set(mapped)
    missing = [c for c in CANONICAL if c not in present]
    changes = {i: c for i, (raw, c) in enumerate(zip(names, mapping.values())) if c and c != _normalize(raw)}

    print("\nSummary")
    print("-------")
    print(f"  canonical classes present : {len(present)}/12")
    if missing:
        print(f"  MISSING classes           : {', '.join(missing)}")
    if unmapped:
        print(f"  UNMAPPED dataset classes  : {', '.join(map(str, unmapped))}")
        print("    -> hand-edit data.yaml `names:` for these, or add them to")
        print("       NAME_MAP in src/detect.py.")

    if not args.fix:
        if changes:
            print(f"\n{len(changes)} name(s) differ from canonical spelling. Re-run with --fix to rewrite data.yaml.")
        if not unmapped and not missing:
            print("\nAll classes map cleanly. You can train with:")
            print(f"  python training/train.py --data {data_yaml.resolve()}")
        return 0

    # --fix mode
    if unmapped:
        print("\nRefusing to --fix: some classes are unmapped (see above). Resolve those first.")
        return 1
    if not changes:
        print("\nNothing to fix -- names already canonical.")
        return 0

    backup = data_yaml.with_suffix(data_yaml.suffix + ".bak")
    backup.write_text(data_yaml.read_text())
    data["names"] = [mapping[i] for i in range(len(names))]
    with open(data_yaml, "w") as f:
        yaml.safe_dump(data, f, sort_keys=False)
    print(f"\nBacked up original to {backup.name}")
    print(f"Rewrote {len(changes)} class name(s) to canonical spelling in {data_yaml.name}.")
    print(f"\nNow train with:\n  python training/train.py --data {data_yaml.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
