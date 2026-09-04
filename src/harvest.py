"""Collect labelled training crops from real play.

Whenever the tracker resolves a move, it knows the true contents of all 64
squares *and* has the settled frame that produced them -- 64 correctly
labelled crops, free, per move. Feeding those back into training is how
the classifier improves just by using the board.

Two deliberate constraints, both about not poisoning the dataset:

  * Callers must only invoke this for a *resolved move*, not for undo.
    undo_last_move reports the reverted position while the physical board
    still shows the post-move one, so harvesting there would write
    confidently mislabelled crops.
  * Output goes to its own directory tree, never straight into the curated
    dataset. Auto-labels are only as good as the tracker was that day, so
    merging them in stays a deliberate act (and a bad run can just be
    deleted).

Layout matches collect_square_crops.py -- {train,val}/{empty,white,black}/
-- so merging is a copy and training can point straight at it.
"""

import datetime as dt
import random
from pathlib import Path

import cv2

from board_state import FILES
from square_classifier import ALL_SQUARES, BLACK, EMPTY, WHITE

VAL_FRACTION = 0.15


def label_for(matrix, square):
    """empty / white / black for a square of a board_state matrix."""
    file_idx, rank_idx = square
    piece = matrix[rank_idx][file_idx]
    if piece is None:
        return EMPTY
    return WHITE if piece.startswith("white") else BLACK


class CropHarvester:
    def __init__(self, out_dir, square_bboxes, session=None, val_fraction=VAL_FRACTION, seed=None):
        self.out_dir = Path(out_dir)
        self._square_bboxes = square_bboxes
        self._session = session or dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        self._val_fraction = val_fraction
        self._rng = random.Random(seed)
        self._index = 0
        self.saved = 0

    def record(self, matrix, frame):
        """Writes one crop per square, labelled from `matrix`. Returns how
        many were written (0 if there's no frame to crop)."""
        if frame is None:
            return 0

        split = "val" if self._rng.random() < self._val_fraction else "train"
        written = 0
        for square in ALL_SQUARES:
            x1, y1, x2, y2 = self._square_bboxes[square]
            if x2 <= x1 or y2 <= y1:
                continue
            label = label_for(matrix, square)
            directory = self.out_dir / split / label
            directory.mkdir(parents=True, exist_ok=True)
            file_idx, rank_idx = square
            name = f"{self._session}_{self._index:04d}_{FILES[file_idx]}{rank_idx + 1}.jpg"
            cv2.imwrite(str(directory / name), frame[y1:y2, x1:x2])
            written += 1

        self._index += 1
        self.saved += written
        return written
