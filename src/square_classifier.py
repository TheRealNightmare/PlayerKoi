"""ML per-square occupancy/color classifier: empty / white / black.

Replaces occupancy_color.py's classical pixel-threshold approach, which
proved too brittle for this board/piece/lighting combination across
several rounds of real-hardware tuning (see training/NOTES.md). Under the
design in move_resolver.py, vision only ever needs to answer, per square:
empty, or occupied by a white piece, or occupied by a black piece -- piece
*type* comes entirely from the tracker's own maintained state. That's a
far easier learning problem than the retired 13-class (type+color)
classifier this project used to have, needs much less training data, and
-- trained on real photos from this exact rig via
src/collect_square_crops.py -- can learn to disregard shadows, glossy
highlights, and low-contrast pieces the way hand-derived pixel thresholds
couldn't.

There is no automatic fallback left in this design (see tracking_loop.py)
-- a misread here can only be caught by a human via the web UI's
manual-correction flow. So, same as the classical-CV module it replaces,
a low-confidence classification returns UNRESOLVED rather than a guess,
and read_settled_state() requires several consensus-agreeing frames before
trusting a square's state at all.
"""

import os
import time
from collections import Counter

from square_geometry import BOARD_SIZE

# Same rationale as the old detect.py/square_classifier.py: skip
# ultralytics' network requirements check for the NCNN backend, which is
# slow/hangy on the Pi.
os.environ.setdefault("ULTRALYTICS_SKIP_REQUIREMENTS_CHECKS", "1")

EMPTY = "empty"
WHITE = "white"
BLACK = "black"

# Distinct from any real state -- marks a low-confidence classification
# (or an inconsistent one across consensus samples). Callers must treat
# this as "don't guess," not as a fourth state.
UNRESOLVED = object()

ALL_SQUARES = [(file_idx, rank_idx) for rank_idx in range(BOARD_SIZE) for file_idx in range(BOARD_SIZE)]

# The trained model sits at 0.9-1.0 confidence on nearly every square, so
# this mainly exists to reject genuinely ambiguous predictions. Kept
# deliberately below the model's normal operating range: correct-but-
# marginal reads (a real rig showed an empty square at 0.61) shouldn't be
# discarded, since majority voting below and legal-move matching in
# move_resolver.py both sit downstream as stronger guards.
DEFAULT_MIN_CONF = 0.5


def load_classifier(model_path):
    from ultralytics import YOLO

    return YOLO(model_path, task="classify")


def classify_board(model, frame, square_bboxes, imgsz=64, min_conf=DEFAULT_MIN_CONF):
    """Classifies every square in a single frame with one batched
    predict() call. Returns {square: state}, state being EMPTY, WHITE,
    BLACK, or UNRESOLVED (top-1 confidence below min_conf)."""
    squares = list(square_bboxes.keys())
    crops = []
    kept_squares = []
    for square in squares:
        x1, y1, x2, y2 = square_bboxes[square]
        if x2 <= x1 or y2 <= y1:
            continue
        crops.append(frame[y1:y2, x1:x2])
        kept_squares.append(square)

    results = {}
    if crops:
        predictions = model.predict(crops, imgsz=imgsz, verbose=False)
        for square, result in zip(kept_squares, predictions):
            class_id = int(result.probs.top1)
            confidence = float(result.probs.top1conf)
            class_name = result.names[class_id]
            results[square] = UNRESOLVED if confidence < min_conf else class_name

    return {square: results.get(square, UNRESOLVED) for square in squares}


def read_settled_state(
    model,
    capture_stream,
    square_bboxes,
    initial_frame=None,
    num_samples=3,
    window_s=0.4,
    imgsz=64,
    min_conf=DEFAULT_MIN_CONF,
):
    """Samples num_samples frames (the first being initial_frame if given,
    the rest freshly captured spaced across window_s) and classifies every
    square in each, then takes a **majority vote** per square: a state is
    reported only if it holds a strict majority of the samples (2 of 3),
    otherwise UNRESOLVED.

    Majority rather than unanimity because these are 64 squares x
    num_samples predictions per settle -- demanding every one of them
    agree means a single flaky frame anywhere unresolves a square, and
    (before this) unresolved anywhere flagged the whole board. See
    tracking_loop.MAX_UNRESOLVED_SQUARES for how unresolved squares are
    tolerated downstream.

    Returns {square: state}.
    """
    frames = [initial_frame] if initial_frame is not None else []
    interval = window_s / max(1, num_samples - 1) if num_samples > 1 else 0.0
    while len(frames) < num_samples:
        if frames:
            time.sleep(interval)
        frame, _timestamp = capture_stream.get_latest()
        frames.append(frame)

    per_frame = [classify_board(model, frame, square_bboxes, imgsz=imgsz, min_conf=min_conf) for frame in frames]

    needed = len(per_frame) // 2 + 1  # strict majority: 2 of 3, 3 of 4, 3 of 5
    consensus = {}
    for square in square_bboxes:
        votes = Counter(
            result[square] for result in per_frame if result[square] is not UNRESOLVED
        )
        winner, count = votes.most_common(1)[0] if votes else (UNRESOLVED, 0)
        consensus[square] = winner if count >= needed else UNRESOLVED
    return consensus
