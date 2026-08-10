"""Maps per-frame piece detections to a smoothed 8x8 board occupancy grid.

Uses the calibration homography (see calibrate.py) to convert each detected
piece's bounding-box bottom-center point into board coordinates, buckets it
into one of the 64 squares, and applies short temporal smoothing so a single
flickered/missed detection doesn't corrupt the reported state.
"""

import json
from collections import deque

import cv2
import numpy as np

BOARD_SIZE = 8
FILES = "abcdefgh"


def load_calibration(path):
    with open(path) as f:
        data = json.load(f)
    return np.array(data["perspective_matrix"], dtype=np.float64)


def bbox_bottom_center(bbox):
    """bbox is (x1, y1, x2, y2) in image pixels. Bottom-center is used
    instead of the box center because tall pieces (king/queen/bishop) show
    perspective offset even under an overhead camera -- their base, not
    their visual centroid, is what actually sits on a square."""
    x1, y1, x2, y2 = bbox
    return ((x1 + x2) / 2.0, y2)


def image_point_to_square(point, matrix):
    """Maps an image-pixel point to a (file_idx, rank_idx) pair, each 0..7,
    or None if the point falls outside the board."""
    src = np.array([[point]], dtype=np.float64)
    dst = cv2.perspectiveTransform(src, matrix)[0, 0]
    bx, by = dst

    if not (0 <= bx <= BOARD_SIZE and 0 <= by <= BOARD_SIZE):
        return None

    file_idx = min(int(bx), BOARD_SIZE - 1)
    rank_idx = min(int(by), BOARD_SIZE - 1)
    return file_idx, rank_idx


def square_name(file_idx, rank_idx):
    return f"{FILES[file_idx]}{rank_idx + 1}"


class BoardStateTracker:
    """matrix[rank_idx][file_idx] is None (empty) or a class label like
    "white-pawn". rank_idx=0 is rank 1, file_idx=0 is file a -- this
    matches the a1/h1/h8/a8 click order used during calibration."""

    def __init__(self, calibration_matrix, history=5, stable_frames=3):
        self._matrix = calibration_matrix
        self._history = deque(maxlen=history)
        self._stable_frames = stable_frames
        self._stable = [[None] * BOARD_SIZE for _ in range(BOARD_SIZE)]

    def _frame_grid(self, detections):
        grid = [[None] * BOARD_SIZE for _ in range(BOARD_SIZE)]
        best_conf = [[-1.0] * BOARD_SIZE for _ in range(BOARD_SIZE)]

        for det in detections:
            point = bbox_bottom_center(det["bbox"])
            square = image_point_to_square(point, self._matrix)
            if square is None:
                continue
            file_idx, rank_idx = square
            if det["confidence"] > best_conf[rank_idx][file_idx]:
                best_conf[rank_idx][file_idx] = det["confidence"]
                grid[rank_idx][file_idx] = det["class"]

        return grid

    def update(self, detections):
        """Feed one frame's detections in. Returns (matrix, changed)."""
        self._history.append(self._frame_grid(detections))

        changed = False
        if len(self._history) >= self._stable_frames:
            recent = list(self._history)[-self._stable_frames:]
            for rank_idx in range(BOARD_SIZE):
                for file_idx in range(BOARD_SIZE):
                    values = {frame[rank_idx][file_idx] for frame in recent}
                    if len(values) == 1:
                        (new_value,) = values
                        if new_value != self._stable[rank_idx][file_idx]:
                            self._stable[rank_idx][file_idx] = new_value
                            changed = True

        return self._stable, changed


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
