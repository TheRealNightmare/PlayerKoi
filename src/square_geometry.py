"""Per-square pixel geometry derived from the calibration homography.

board_state.py's calibration matrix maps image pixels -> board space (a
0..8 x 0..8 grid). This module inverts that to answer the opposite
question: given a square (or the whole board), what pixel region does it
occupy in the raw camera frame? Both live inference (roi_diff.py,
square_classifier.py) and the ChessReD training-crop generator
(training/prepare_square_crops.py) import from here, so the region a piece
is expected to occupy is defined exactly once and used identically for
training and inference.
"""

import cv2
import numpy as np

BOARD_SIZE = 8


def _cell_corners_board_space(file_idx, rank_idx):
    return np.array(
        [
            [file_idx, rank_idx],
            [file_idx + 1, rank_idx],
            [file_idx + 1, rank_idx + 1],
            [file_idx, rank_idx + 1],
        ],
        dtype=np.float64,
    ).reshape(-1, 1, 2)


def square_pixel_bboxes(matrix, image_size, margin_px=6, margin_up_px=40):
    """matrix maps image pixels -> board space. image_size is (width, height).

    Returns {(file_idx, rank_idx): (x1, y1, x2, y2)} in image pixels,
    clipped to the frame. Padded by margin_px on every side plus extra
    margin_up_px on the top edge only -- standing pieces extend well above
    their square's flat footprint even under a near-overhead camera, so the
    crop needs headroom above the square, not left/right/below. Tune
    margin_up_px during physical bring-up against a live capture or
    config/calibration_preview.jpg.
    """
    width, height = image_size
    inv_matrix = np.linalg.inv(matrix)

    bboxes = {}
    for rank_idx in range(BOARD_SIZE):
        for file_idx in range(BOARD_SIZE):
            board_corners = _cell_corners_board_space(file_idx, rank_idx)
            image_corners = cv2.perspectiveTransform(board_corners, inv_matrix).reshape(-1, 2)

            x1 = image_corners[:, 0].min() - margin_px
            x2 = image_corners[:, 0].max() + margin_px
            y1 = image_corners[:, 1].min() - margin_up_px
            y2 = image_corners[:, 1].max() + margin_px

            x1 = int(max(0, round(x1)))
            y1 = int(max(0, round(y1)))
            x2 = int(min(width, round(x2)))
            y2 = int(min(height, round(y2)))
            bboxes[(file_idx, rank_idx)] = (x1, y1, x2, y2)

    return bboxes


def board_roi_bbox(matrix, image_size, margin_px=20):
    """Whole-board pixel bounding box (image px), for the cheap motion gate
    that watches for any change before running per-square work."""
    width, height = image_size
    inv_matrix = np.linalg.inv(matrix)

    board_corners = np.array(
        [[0, 0], [BOARD_SIZE, 0], [BOARD_SIZE, BOARD_SIZE], [0, BOARD_SIZE]], dtype=np.float64
    ).reshape(-1, 1, 2)
    image_corners = cv2.perspectiveTransform(board_corners, inv_matrix).reshape(-1, 2)

    x1 = int(max(0, round(image_corners[:, 0].min() - margin_px)))
    y1 = int(max(0, round(image_corners[:, 1].min() - margin_px)))
    x2 = int(min(width, round(image_corners[:, 0].max() + margin_px)))
    y2 = int(min(height, round(image_corners[:, 1].max() + margin_px)))
    return x1, y1, x2, y2
