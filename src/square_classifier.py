"""Wraps an NCNN-exported per-square classifier: 13 classes (empty + 6
piece types x 2 colors), run only on the handful of square crops that
roi_diff.diff_changed_squares flags as changed -- this is what replaces a
full-frame YOLO pass for routine per-move recognition, exploiting the fact
that the starting position is already known and only *changes* need
resolving each turn.

Expects a model trained with training/train_classifier.py on crops
produced by training/prepare_square_crops.py, whose class folder names
follow the same "{color}-{piece}" convention detect.py's NAME_MAP produces,
plus "empty" for a vacant square.
"""

import os

# Same rationale as detect.py: skip ultralytics' network requirements
# check for the NCNN backend, which is slow/hangy on the Pi.
os.environ.setdefault("ULTRALYTICS_SKIP_REQUIREMENTS_CHECKS", "1")

EMPTY = "empty"

# Distinct from a genuine "empty" classification -- marks a crop whose
# top-1 confidence fell below min_conf, so callers can force a fallback
# instead of silently trusting (or silently treating as vacant) a guess
# the model itself wasn't confident about.
UNRESOLVED = object()


def load_classifier(model_path):
    from ultralytics import YOLO

    return YOLO(model_path, task="classify")


def classify_squares(model, frame, squares, square_bboxes, imgsz=64, min_conf=0.0):
    """squares: iterable of (file_idx, rank_idx) keys into square_bboxes,
    typically the shortlist from roi_diff.diff_changed_squares. Crops each
    from `frame` and runs one batched predict() call (at most a handful of
    crops per move, so batching cost is negligible).

    Returns {(file_idx, rank_idx): (class_name, confidence)}, where
    class_name is None for a genuine "empty" square (matching
    board_state's matrix convention of None = empty), or the UNRESOLVED
    sentinel if the crop's top-1 confidence fell below min_conf -- callers
    should treat UNRESOLVED as a reason to fall back rather than trusting
    a low-confidence guess.
    """
    crops = []
    kept_squares = []
    for square in squares:
        x1, y1, x2, y2 = square_bboxes[square]
        if x2 <= x1 or y2 <= y1:
            continue
        crops.append(frame[y1:y2, x1:x2])
        kept_squares.append(square)

    if not crops:
        return {}

    results = model.predict(crops, imgsz=imgsz, verbose=False)

    output = {}
    for square, result in zip(kept_squares, results):
        class_id = int(result.probs.top1)
        confidence = float(result.probs.top1conf)
        class_name = result.names[class_id]
        if confidence < min_conf:
            output[square] = (UNRESOLVED, confidence)
        else:
            output[square] = (None if class_name == EMPTY else class_name, confidence)

    return output
