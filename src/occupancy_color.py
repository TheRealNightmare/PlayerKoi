"""Classical-CV occupancy/color reader.

Replaces square_classifier.py's ML-based per-square type+color read. Under
the design in move_resolver.py, vision never needs piece *type* -- the
tracker already knows what type of piece is on a square from its own
maintained state, since it only has to resolve deltas against legal moves
(see MoveResolver.resolve_from_deltas). Vision only needs to answer, per
square: empty, or occupied by a white piece, or occupied by a black piece.
That's a much easier signal than piece type -- easy enough to read with
plain pixel-color comparisons against a calibration-time baseline, no model
inference at all.

Two-stage read per square:
  1. Occupancy: compare the square's footprint crop (the flat area a piece
     actually stands on, margin_up_px=0 -- see square_geometry.py) against
     that square's own calibrated empty-color baseline. The board's own
     light/dark checkerboard pattern cancels out here by construction,
     since each square is only ever compared against its own baseline.
  2. Color: when occupied, read luminance from the square's full crop
     (extra headroom above the footprint, since a standing piece's body
     extends upward) -- but NOT as a plain crop average. That crop mixes
     in background square-color around/behind the piece (worse for short
     pieces, or the checkerboard-colored headroom above them), which would
     otherwise bias a piece's apparent color toward whatever square color
     it happens to be standing on. Instead, each pixel is compared against
     the calibrated empty-board reference at that exact position, and only
     pixels that clearly differ (the piece silhouette) are averaged --
     simple background subtraction, so the square's own color drops out
     rather than being averaged in as noise.

There is no automatic ML fallback left in this design (see
tracking_loop.py) -- a misread here can only be caught by a human via the
web UI's manual-correction flow, not by a second model. So both stages
require a confidence margin (several calibration-time standard deviations
from the decision boundary) before committing to a result; a boundary-line
read returns UNRESOLVED rather than guessing. read_settled_state() adds a
second layer on top of that: it samples several frames after a settle and
only trusts a square's state once every sample agrees, rejecting transient
hand shadows, reflections, or a settle trigger that fired a beat too early.
"""

import time
from pathlib import Path

import cv2
import numpy as np

from square_geometry import BOARD_SIZE

EMPTY = "empty"
WHITE = "white"
BLACK = "black"

# Distinct from any real state -- marks a read too close to a decision
# boundary (or an inconsistent one across consensus samples) to trust.
# Callers must treat this as "don't guess," not as a fourth state.
UNRESOLVED = object()

ALL_SQUARES = [(file_idx, rank_idx) for rank_idx in range(BOARD_SIZE) for file_idx in range(BOARD_SIZE)]

# Default confidence margin (calibrated standard deviations from a decision
# boundary) required before trusting a read -- shared between the live
# classifier and calibrate.py's own separation warning, so "confident
# enough to trust live" and "confident enough to calibrate without warning"
# mean the same thing.
DEFAULT_MIN_MARGIN_STD = 2.5

# Minimum fraction of a piece-body crop that must be confidently foreground
# (background-subtracted) before its masked luminance is trusted at all --
# guards against a near-empty mask (e.g. a very small piece, or a piece
# whose color happens to be very close to the background) producing a
# meaningless average over a handful of pixels. Tune alongside
# square_geometry.py's margin_up_px during physical bring-up.
DEFAULT_MIN_FOREGROUND_FRAC = 0.05

EMPTY_BOARD_REFERENCE_FILENAME = "empty_board_reference.png"


def _mean_bgr(frame, bbox):
    x1, y1, x2, y2 = bbox
    if x2 <= x1 or y2 <= y1:
        return None
    crop = frame[y1:y2, x1:x2]
    return crop.reshape(-1, 3).astype(np.float64).mean(axis=0)


def _luminance(mean_bgr):
    b, g, r = mean_bgr
    return 0.114 * b + 0.587 * g + 0.299 * r


def average_frames(frames):
    """Averages a burst of BGR frames into a single uint8 reference frame
    -- used to build the empty-board background reference that
    _masked_luminance() subtracts against."""
    stacked = np.stack([frame.astype(np.float64) for frame in frames], axis=0)
    return np.clip(stacked.mean(axis=0), 0, 255).astype(np.uint8)


def _pixel_diff(crop, background_crop):
    """Per-pixel Euclidean BGR distance between two same-shaped crops.
    Returns an (H, W) float64 array."""
    return np.linalg.norm(crop.astype(np.float64) - background_crop.astype(np.float64), axis=2)


def _masked_luminance(crop, background_crop, pixel_threshold, min_foreground_frac=DEFAULT_MIN_FOREGROUND_FRAC):
    """Background-subtracted mean luminance: only pixels that differ from
    the calibrated background by more than pixel_threshold are treated as
    piece (foreground) and averaged. This is what keeps the board's own
    light/dark square color from biasing a piece's apparent color, since
    background-matching pixels are excluded rather than averaged in.
    Returns None (not UNRESOLVED -- this is a plain helper, callers map to
    UNRESOLVED) if the crops don't match in shape or too few pixels
    qualify as foreground to trust the result.
    """
    if crop.shape != background_crop.shape:
        return None
    diff = _pixel_diff(crop, background_crop)
    mask = diff > pixel_threshold
    if mask.mean() < min_foreground_frac:
        return None
    foreground_mean_bgr = crop[mask].astype(np.float64).mean(axis=0)
    return _luminance(foreground_mean_bgr)


def classify_square_occupancy(frame, square, footprint_bboxes, square_bboxes, baseline, min_margin_std=DEFAULT_MIN_MARGIN_STD):
    """Classifies one square's occupancy/color for a single frame.

    baseline is the dict returned by load_baseline(). Returns (state,
    margin_std) where state is EMPTY, WHITE, BLACK, or UNRESOLVED, and
    margin_std is a rough confidence score (how many calibrated standard
    deviations the read was from the nearest decision boundary) useful for
    diagnostics.
    """
    mean_bgr = _mean_bgr(frame, footprint_bboxes[square])
    if mean_bgr is None:
        return UNRESOLVED, 0.0

    empty_mean, empty_std = baseline["empty"][square]
    diff = float(np.linalg.norm(mean_bgr - empty_mean))
    scale = max(baseline["occupancy_scale"], 1e-6)
    occupancy_margin = (diff - baseline["occupancy_boundary"]) / scale

    if occupancy_margin <= -min_margin_std:
        return EMPTY, -occupancy_margin
    if occupancy_margin < min_margin_std:
        return UNRESOLVED, abs(occupancy_margin)

    # Confidently occupied -- now read color from the full (piece-body)
    # crop, background-subtracted against this square's calibrated empty
    # appearance so the board's own light/dark color doesn't bias the
    # result (see module docstring).
    x1, y1, x2, y2 = square_bboxes[square]
    if x2 <= x1 or y2 <= y1:
        return UNRESOLVED, 0.0
    body_crop = frame[y1:y2, x1:x2]
    background_crop = baseline["background_crops"][square]
    pixel_threshold = min_margin_std * baseline["foreground_pixel_scale"]
    luminance = _masked_luminance(body_crop, background_crop, pixel_threshold)
    if luminance is None:
        return UNRESOLVED, 0.0

    signed = luminance - baseline["color_threshold"]
    if not baseline["white_is_brighter"]:
        signed = -signed
    color_std = max((baseline["white_luminance_std"] + baseline["black_luminance_std"]) / 2.0, 1e-6)
    color_margin = abs(signed) / color_std

    if color_margin < min_margin_std:
        return UNRESOLVED, color_margin

    return (WHITE if signed > 0 else BLACK), color_margin


def classify_board_occupancy(frame, squares, footprint_bboxes, square_bboxes, baseline, min_margin_std=DEFAULT_MIN_MARGIN_STD):
    """classify_square_occupancy() for every square in `squares`. Returns
    {square: state}."""
    return {
        square: classify_square_occupancy(frame, square, footprint_bboxes, square_bboxes, baseline, min_margin_std)[0]
        for square in squares
    }


def read_settled_state(
    capture_stream,
    squares,
    footprint_bboxes,
    square_bboxes,
    baseline,
    initial_frame=None,
    num_samples=3,
    window_s=0.4,
    min_margin_std=DEFAULT_MIN_MARGIN_STD,
):
    """Samples num_samples frames (the first being initial_frame if given,
    the rest freshly captured spaced across window_s) and reads occupancy
    for every square in each. A square's state is only reported as
    EMPTY/WHITE/BLACK if every sample agreed and none of them were
    UNRESOLVED -- otherwise it's reported as UNRESOLVED, forcing the caller
    to flag rather than trust a transient or boundary-line read.

    Returns {square: state}.
    """
    frames = [initial_frame] if initial_frame is not None else []
    interval = window_s / max(1, num_samples - 1) if num_samples > 1 else 0.0
    while len(frames) < num_samples:
        if frames:
            time.sleep(interval)
        frame, _timestamp = capture_stream.get_latest()
        frames.append(frame)

    per_frame = [
        classify_board_occupancy(frame, squares, footprint_bboxes, square_bboxes, baseline, min_margin_std)
        for frame in frames
    ]

    consensus = {}
    for square in squares:
        states = {result[square] for result in per_frame}
        if len(states) == 1 and UNRESOLVED not in states:
            (state,) = states
            consensus[square] = state
        else:
            consensus[square] = UNRESOLVED
    return consensus


def load_baseline(calibration, calibration_path):
    """Converts the JSON-friendly calibration.json fields (see
    calibrate.py) into the dict shape classify_square_occupancy() expects.

    calibration_path is calibration.json's own path -- used to locate the
    sibling empty-board reference image (EMPTY_BOARD_REFERENCE_FILENAME)
    calibrate.py saves alongside it, needed for the background-subtracted
    color read (see module docstring). The returned dict still needs
    finalize_baseline() called on it once per-square pixel bboxes are
    known (see TrackingLoop.__init__), which slices that image into
    per-square background crops.
    """
    empty = {}
    for key, entry in calibration["empty_baseline"].items():
        file_idx_str, rank_idx_str = key.split(",")
        square = (int(file_idx_str), int(rank_idx_str))
        empty[square] = (np.array(entry["mean_bgr"], dtype=np.float64), float(entry["std"]))

    color = calibration["piece_color_baseline"]
    occupancy = calibration["occupancy_diff_threshold"]
    foreground = calibration["foreground_pixel_diff_threshold"]

    background_path = Path(calibration_path).resolve().parent / EMPTY_BOARD_REFERENCE_FILENAME
    background_image = cv2.imread(str(background_path))
    if background_image is None:
        raise FileNotFoundError(
            f"missing {background_path} -- re-run calibrate.py (it now saves an empty-board "
            "reference image alongside calibration.json)"
        )

    return {
        "empty": empty,
        "white_luminance_mean": color["white_luminance_mean"],
        "black_luminance_mean": color["black_luminance_mean"],
        "white_luminance_std": color["white_luminance_std"],
        "black_luminance_std": color["black_luminance_std"],
        "color_threshold": color["threshold"],
        "white_is_brighter": color["white_is_brighter"],
        "occupancy_boundary": occupancy["boundary"],
        "occupancy_scale": occupancy["scale"],
        "background_image": background_image,
        "foreground_pixel_scale": foreground["scale"],
    }


def finalize_baseline(baseline, square_bboxes):
    """Slices baseline["background_image"] into a per-square crop dict
    (using the same piece-body bboxes the color read uses), so
    classify_square_occupancy doesn't re-slice the same full-resolution
    image on every read. Call once, after square_bboxes are known (see
    TrackingLoop.__init__) -- mutates and returns baseline."""
    background_image = baseline["background_image"]
    crops = {}
    for square, (x1, y1, x2, y2) in square_bboxes.items():
        if x2 > x1 and y2 > y1:
            crops[square] = background_image[y1:y2, x1:x2]
    baseline["background_crops"] = crops
    return baseline


def compute_baselines(empty_frames, start_frames, footprint_bboxes, square_bboxes, starting_matrix,
                       min_margin_std=DEFAULT_MIN_MARGIN_STD):
    """Builds the three JSON-friendly calibration.json fields this module
    needs, from raw calibration-time captures:

      empty_frames: several BGR frames of the cleared board.
      start_frames: several BGR frames of the standard starting position.
      starting_matrix: board_state-style matrix (see
        move_resolver.standard_starting_matrix()) describing which square
        is white/black/empty in start_frames.

    Returns (empty_baseline, piece_color_baseline, occupancy_diff_threshold,
    foreground_pixel_diff_threshold, background_image, warning) --
    background_image is the averaged empty-board frame (calibrate.py saves
    it to EMPTY_BOARD_REFERENCE_FILENAME); warning is a human-readable
    string when white/black luminance separation, or foreground-pixel
    coverage, looks too unreliable to trust (None otherwise), meant to be
    surfaced immediately by calibrate.py rather than silently deploying an
    unreliable threshold.

    The occupancy boundary/scale are set at the midpoint (and averaged
    spread) between two distributions gathered directly from these same
    captures: how far a genuinely empty square's samples drift from its own
    baseline (should be small), and how far a genuinely occupied square (in
    start_frames) reads from that same empty baseline (should be large).
    This ties the live decision threshold to the actual camera/lighting
    conditions rather than a guessed constant. The color statistics below
    use the same idea, applied per-pixel: white/black luminance is measured
    only from pixels that clearly differ from the empty-board reference
    (see _masked_luminance), not a raw crop average -- otherwise the
    checkerboard-colored background around/behind each piece would bias
    these calibration-time statistics the same way it would bias a live
    read, defeating the point of masking at read time (see module
    docstring for the full rationale).
    """
    empty_baseline = {}
    empty_lookup = {}
    for square, bbox in footprint_bboxes.items():
        samples = [m for m in (_mean_bgr(frame, bbox) for frame in empty_frames) if m is not None]
        if not samples:
            continue
        mean = np.mean(samples, axis=0)
        distances = [float(np.linalg.norm(s - mean)) for s in samples]
        std = float(np.std(distances)) if len(distances) > 1 else 0.0
        file_idx, rank_idx = square
        empty_baseline[f"{file_idx},{rank_idx}"] = {"mean_bgr": mean.tolist(), "std": std}
        empty_lookup[square] = mean

    empty_diffs = []
    for square, mean in empty_lookup.items():
        for frame in empty_frames:
            sample = _mean_bgr(frame, footprint_bboxes[square])
            if sample is not None:
                empty_diffs.append(float(np.linalg.norm(sample - mean)))

    background_image = average_frames(empty_frames)

    # Pure camera/lighting noise: how far each empty-board burst frame's
    # pixels drift from the averaged background, with nothing actually
    # there to cause a real difference. Scaling this by min_margin_std
    # gives the per-pixel foreground/background split the same
    # statistically-grounded margin as every other threshold here, rather
    # than a guessed constant.
    noise_diffs = []
    for square, bbox in square_bboxes.items():
        x1, y1, x2, y2 = bbox
        if x2 <= x1 or y2 <= y1:
            continue
        background_crop = background_image[y1:y2, x1:x2]
        for frame in empty_frames:
            crop = frame[y1:y2, x1:x2]
            if crop.shape == background_crop.shape:
                noise_diffs.append(_pixel_diff(crop, background_crop).ravel())
    foreground_pixel_scale = float(np.std(np.concatenate(noise_diffs))) if noise_diffs else 1.0
    foreground_pixel_scale = max(foreground_pixel_scale, 1e-6)
    pixel_threshold = min_margin_std * foreground_pixel_scale

    occupied_diffs = []
    white_luminances = []
    black_luminances = []
    masked_attempts = 0
    masked_failures = 0
    for rank_idx in range(BOARD_SIZE):
        for file_idx in range(BOARD_SIZE):
            square = (file_idx, rank_idx)
            label = starting_matrix[rank_idx][file_idx]
            if label is None or square not in empty_lookup:
                continue
            mean = empty_lookup[square]
            x1, y1, x2, y2 = square_bboxes[square]
            background_crop = background_image[y1:y2, x1:x2] if x2 > x1 and y2 > y1 else None
            for frame in start_frames:
                footprint_sample = _mean_bgr(frame, footprint_bboxes[square])
                if footprint_sample is not None:
                    occupied_diffs.append(float(np.linalg.norm(footprint_sample - mean)))
                if background_crop is None:
                    continue
                masked_attempts += 1
                body_crop = frame[y1:y2, x1:x2]
                lum = _masked_luminance(body_crop, background_crop, pixel_threshold)
                if lum is None:
                    masked_failures += 1
                    continue
                (white_luminances if label.startswith("white") else black_luminances).append(lum)

    empty_mean_diff = float(np.mean(empty_diffs)) if empty_diffs else 0.0
    empty_std_diff = float(np.std(empty_diffs)) if len(empty_diffs) > 1 else 1.0
    occupied_mean_diff = float(np.mean(occupied_diffs)) if occupied_diffs else empty_mean_diff + 1.0
    occupied_std_diff = float(np.std(occupied_diffs)) if len(occupied_diffs) > 1 else 1.0

    occupancy_diff_threshold = {
        "boundary": (empty_mean_diff + occupied_mean_diff) / 2.0,
        "scale": max((empty_std_diff + occupied_std_diff) / 2.0, 1e-6),
    }

    white_mean = float(np.mean(white_luminances)) if white_luminances else 0.0
    white_std = float(np.std(white_luminances)) if len(white_luminances) > 1 else 1.0
    black_mean = float(np.mean(black_luminances)) if black_luminances else 0.0
    black_std = float(np.std(black_luminances)) if len(black_luminances) > 1 else 1.0
    separation = abs(white_mean - black_mean)
    avg_std = max((white_std + black_std) / 2.0, 1e-6)

    piece_color_baseline = {
        "white_luminance_mean": white_mean,
        "white_luminance_std": white_std,
        "black_luminance_mean": black_mean,
        "black_luminance_std": black_std,
        "threshold": (white_mean + black_mean) / 2.0,
        "white_is_brighter": white_mean >= black_mean,
    }

    foreground_pixel_diff_threshold = {"scale": foreground_pixel_scale}

    warnings = []
    if not white_luminances or not black_luminances or (separation / avg_std) < 2 * min_margin_std:
        warnings.append(
            f"white/black piece luminance separation is only {separation:.1f} "
            f"(avg spread {avg_std:.1f}) -- color reads may be unreliable. "
            "Check lighting, piece contrast, and camera exposure before relying on this calibration."
        )
    if masked_attempts and (masked_failures / masked_attempts) > 0.2:
        warnings.append(
            f"background-subtracted color masking found too few foreground pixels on "
            f"{masked_failures}/{masked_attempts} starting-position crops -- pieces may be too "
            "similar in color to the board, or square_geometry.py's margin_up_px may need tuning."
        )
    warning = " ".join(warnings) if warnings else None

    return (
        empty_baseline,
        piece_color_baseline,
        occupancy_diff_threshold,
        foreground_pixel_diff_threshold,
        background_image,
        warning,
    )
