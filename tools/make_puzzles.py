"""Cut the Lichess puzzle database down to data/puzzles.csv for puzzle mode.

The source is https://database.lichess.org/#puzzles -- CC0, about 5 million
puzzles, ~300 MB zstd-compressed. This streams it (from the URL, or from a
file already downloaded) and keeps a small, even spread:

    * White to solve only. Lichess stores the position BEFORE the opponent's
      setup move, so that means rows whose FEN has Black to move. The human
      then sits on their usual side of the rig and the arm always plays Black.
    * Well-tested puzzles only (popularity, play count, rating deviation), so
      the stored line is one people agree on.
    * An even spread of ratings: reservoir-sampled per 50-point bucket, so
      adaptive mode finds something near any rating from 400 to 2800.

Off-Pi tool: needs `pip install zstandard` (not in requirements.txt, the Pi
never runs this).

    python3 tools/make_puzzles.py                          # download + write
    python3 tools/make_puzzles.py --source lichess_db_puzzle.csv.zst
"""

import argparse
import csv
import io
import random
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "data" / "puzzles.csv"
DEFAULT_URL = "https://database.lichess.org/lichess_db_puzzle.csv.zst"

RATING_MIN = 400
RATING_MAX = 2800
BUCKET = 50


def _open_source(source):
    import zstandard

    if source.startswith(("http://", "https://")):
        raw = urllib.request.urlopen(source)
    else:
        raw = open(source, "rb")
    reader = zstandard.ZstdDecompressor().stream_reader(raw)
    return io.TextIOWrapper(reader, encoding="utf-8", newline="")


def _piece_count(fen):
    placement = fen.split()[0]
    return sum(1 for ch in placement if ch.isalpha())


def select(rows, per_bucket, min_popularity, min_plays, max_deviation, seed):
    """Reservoir-samples `per_bucket` rows per rating bucket. Pure, so the
    filtering can be checked without the real 300 MB file."""
    rng = random.Random(seed)
    buckets = {}
    seen = {}
    for row in rows:
        fen = row["FEN"]
        if fen.split()[1] != "b":
            continue
        try:
            rating = int(row["Rating"])
            if (int(row["Popularity"]) < min_popularity
                    or int(row["NbPlays"]) < min_plays
                    or int(row["RatingDeviation"]) > max_deviation):
                continue
        except (KeyError, ValueError):
            continue
        if not RATING_MIN <= rating < RATING_MAX:
            continue
        moves = row["Moves"].split()
        if len(moves) < 2:
            continue
        key = rating // BUCKET
        seen[key] = seen.get(key, 0) + 1
        entry = {
            "id": row["PuzzleId"],
            "fen": fen,
            "moves": " ".join(moves),
            "rating": rating,
            "themes": row.get("Themes", ""),
            "pieces": _piece_count(fen),
        }
        bucket = buckets.setdefault(key, [])
        if len(bucket) < per_bucket:
            bucket.append(entry)
        else:
            slot = rng.randrange(seen[key])
            if slot < per_bucket:
                bucket[slot] = entry
    picked = [entry for key in sorted(buckets) for entry in buckets[key]]
    picked.sort(key=lambda e: (e["rating"], e["id"]))
    return picked


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", default=DEFAULT_URL,
                        help="URL or local path of lichess_db_puzzle.csv.zst")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--per-bucket", type=int, default=125,
                        help=f"puzzles kept per {BUCKET}-point rating band")
    parser.add_argument("--min-popularity", type=int, default=85)
    parser.add_argument("--min-plays", type=int, default=500)
    parser.add_argument("--max-deviation", type=int, default=90)
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()

    print(f"reading {args.source} ...", file=sys.stderr)
    with _open_source(args.source) as handle:
        picked = select(csv.DictReader(handle), args.per_bucket, args.min_popularity,
                        args.min_plays, args.max_deviation, args.seed)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=["id", "fen", "moves", "rating", "themes", "pieces"])
        writer.writeheader()
        writer.writerows(picked)
    print(f"wrote {len(picked)} puzzles to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
