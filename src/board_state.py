"""Board-state matrix helpers shared across the tracking pipeline.

matrix[rank_idx][file_idx] is None (empty) or a class label like
"white-pawn". rank_idx=0 is rank 1, file_idx=0 is file a -- this matches the
a1/h1/h8/a8 click order used during calibration. The matrix itself is
maintained by move_resolver.MoveResolver (seeded from the standard starting
position, updated by applying resolved legal moves) rather than re-derived
from vision each turn -- see that module's docstring.
"""

import json
from pathlib import Path

import numpy as np

BOARD_SIZE = 8
FILES = "abcdefgh"

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"


def calibration_paths(env=None):
    """Paths for one environment's calibration files: (json, preview, reference).

    Camera height/position is one of the things that varies between
    environments, so each needs its own board geometry. env=None keeps the
    original unsuffixed filenames, so existing defaults keep working.

    Lives here rather than in calibrate.py because calibrate.py imports
    picamera2 at module scope and so can't be imported off the Pi.
    """
    suffix = "" if env is None else f"-env{env}"
    return (
        CONFIG_DIR / f"calibration{suffix}.json",
        CONFIG_DIR / f"calibration{suffix}_preview.jpg",
        CONFIG_DIR / f"reference{suffix}_frame.jpg",
    )


def load_calibration(path):
    with open(path) as f:
        data = json.load(f)
    return np.array(data["perspective_matrix"], dtype=np.float64)


def format_matrix(matrix):
    """Pretty-prints the matrix with rank 8 on top, like a real board."""
    lines = []
    for rank_idx in reversed(range(BOARD_SIZE)):
        cells = []
        for file_idx in range(BOARD_SIZE):
            label = matrix[rank_idx][file_idx]
            cells.append("." if label is None else _abbreviate(label))
        lines.append(f"{rank_idx + 1}  " + " ".join(cells))
    lines.append("   " + " ".join(FILES.upper()))
    return "\n".join(lines)


_PIECE_LETTERS = {
    "king": "K",
    "queen": "Q",
    "rook": "R",
    "bishop": "B",
    "knight": "N",
    "pawn": "P",
}


def _abbreviate(label):
    color, piece = label.split("-")
    letter = _PIECE_LETTERS.get(piece, "?")
    return letter if color == "white" else letter.lower()


def matrix_to_fen_placement(matrix):
    """Converts a stable board matrix (matrix[rank_idx][file_idx], rank_idx=0
    is rank 1) into the piece-placement field of a FEN string -- FEN lists
    ranks top-down (rank 8 first), the reverse of our matrix's indexing."""
    rows = []
    for rank_idx in reversed(range(BOARD_SIZE)):
        row = ""
        empty_run = 0
        for file_idx in range(BOARD_SIZE):
            label = matrix[rank_idx][file_idx]
            if label is None:
                empty_run += 1
                continue
            if empty_run:
                row += str(empty_run)
                empty_run = 0
            row += _abbreviate(label)
        if empty_run:
            row += str(empty_run)
        rows.append(row)
    return "/".join(rows)
