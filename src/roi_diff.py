"""Cheap, ML-free motion gating and per-square change localization.

Exploits the fact that a chess move is a discrete, mostly-static event: the
board sits still, a hand enters and moves a piece, then the board sits
still again. Full detection only needs to run once per settle, not on
every frame -- this module decides *when* that is (BoardMotionGate) and
*which squares* plausibly changed (diff_changed_squares), using plain
grayscale frame differencing over the calibrated board ROI (see
square_geometry.py). No model is involved at all -- this is what replaces
the current "run full YOLO every 0.75s regardless of state" loop.
"""

import cv2
import numpy as np

# A legal chess move touches at most 4 squares (castling: king from/to +
# rook from/to); en passant touches 3. More changed squares than this means
# the diff is unreliable (hand still over the board, lighting glitch,
# calibration drift) -- a full YOLO rescan should be trusted instead of a
# partial per-square update built on top of it.
MAX_PLAUSIBLE_SQUARES = 6


def to_gray_roi(frame, roi_bbox):
    x1, y1, x2, y2 = roi_bbox
    crop = frame[y1:y2, x1:x2]
    return cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)


class BoardMotionGate:
    """Feed successive grayscale board-ROI crops via update(). Returns one
    of:
      "idle"    -- no meaningful change since the last settle.
      "moving"  -- diff is currently elevated (a hand is over the board).
      "settled" -- diff just dropped back down after being elevated for at
                   least min_moving_frames frames, and has now stayed quiet
                   for settle_frames frames -- fires exactly once per move.
                   This is the signal to run per-square diffing/detection.
    """

    def __init__(self, motion_thresh=12.0, settle_frames=3, min_moving_frames=1):
        self._motion_thresh = motion_thresh
        self._settle_frames = settle_frames
        self._min_moving_frames = min_moving_frames
        self._prev_gray = None
        self._moving_streak = 0
        self._quiet_streak = 0
        self._was_moving = False

    def update(self, roi_gray):
        if self._prev_gray is None:
            self._prev_gray = roi_gray
            return "idle"

        score = float(np.mean(cv2.absdiff(roi_gray, self._prev_gray)))
        self._prev_gray = roi_gray
        moving = score > self._motion_thresh

        if moving:
            self._moving_streak += 1
            self._quiet_streak = 0
            if self._moving_streak >= self._min_moving_frames:
                self._was_moving = True
            return "moving"

        self._moving_streak = 0
        self._quiet_streak += 1

        if self._was_moving and self._quiet_streak >= self._settle_frames:
            self._was_moving = False
            self._quiet_streak = 0
            return "settled"

        return "idle"


def diff_changed_squares(prev_frame, curr_frame, square_bboxes, thresh=18.0, min_area_frac=0.15):
    """Per-square grayscale mean-abs-diff between prev_frame (the last
    known-stable frame) and curr_frame (a newly-settled frame), restricted
    to each square's crop from square_pixel_bboxes(). A square only counts
    as changed if the fraction of its crop exceeding `thresh` per-pixel is
    at least min_area_frac -- this guards against a global-mean trigger
    from small specular highlights or shadow flicker rather than an actual
    occupancy change.

    Returns a list of (square, changed_frac) sorted by changed_frac
    descending, where square is a (file_idx, rank_idx) key matching
    square_bboxes.
    """
    prev_gray = cv2.cvtColor(prev_frame, cv2.COLOR_BGR2GRAY)
    curr_gray = cv2.cvtColor(curr_frame, cv2.COLOR_BGR2GRAY)

    scored = []
    for square, (x1, y1, x2, y2) in square_bboxes.items():
        if x2 <= x1 or y2 <= y1:
            continue
        prev_crop = prev_gray[y1:y2, x1:x2]
        curr_crop = curr_gray[y1:y2, x1:x2]
        diff = cv2.absdiff(prev_crop, curr_crop)

        changed_frac = float(np.mean(diff > thresh))
        if changed_frac >= min_area_frac:
            scored.append((square, changed_frac))

    scored.sort(key=lambda item: item[1], reverse=True)
    return scored
