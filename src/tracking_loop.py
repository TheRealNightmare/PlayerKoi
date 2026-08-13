"""Event-gated board tracking loop.

Replaces main.py's and web_ui.py's near-identical
`while True: capture; full-frame detect; sleep(interval)` loops, which ran
unconditional full-model inference every tick regardless of whether the
board had actually changed. This drives the flow instead:

    motion-gate (cheap, ML-free) -> per-square pixel diff -> classify only
    the changed squares -> match the result against every legal chess move
    -> accept, or fall back to a full-frame YOLO rescan if the diff was
    unreliable or didn't resolve to a legal move.

This mirrors the product's own designed flow ("Board Settles -> Image
Captured -> Pieces Identified -> Move Validated -> re-scan if no legal
move matches"), and exploits the guaranteed-correct starting position: the
tracker seeds from the known standard position and thereafter only needs
to resolve *changes*, not re-derive all 64 squares from scratch each turn.
"""

import time

from board_state import BoardStateTracker
from detect import detect
from move_resolver import MoveResolver, standard_starting_matrix
from roi_diff import MAX_PLAUSIBLE_SQUARES, BoardMotionGate, diff_changed_squares, to_gray_roi
from square_classifier import UNRESOLVED, classify_squares
from square_geometry import board_roi_bbox, square_pixel_bboxes


class TrackingLoop:
    def __init__(
        self,
        capture_stream,
        calibration_matrix,
        image_size,
        fallback_model,
        classifier_model,
        on_update,
        poll_interval=0.12,
        fallback_conf=0.4,
        fallback_imgsz=480,
        classifier_imgsz=64,
        classifier_min_conf=0.6,
        fallback_stable_frames=3,
        fallback_poll_interval=0.1,
    ):
        """on_update(matrix, move_text, frame, flagged) is called whenever
        the stable board state changes. move_text is a SAN move string
        (e.g. "Nf3") when the change resolved to a single legal move, or
        None when a fallback rescan couldn't resolve one either (flagged
        is True in that case -- see MoveResolver.resync's docstring for
        why special-move rights get reset when this happens)."""
        self._capture_stream = capture_stream
        self._calibration_matrix = calibration_matrix
        self._fallback_model = fallback_model
        self._classifier_model = classifier_model
        self._on_update = on_update
        self._poll_interval = poll_interval
        self._fallback_conf = fallback_conf
        self._fallback_imgsz = fallback_imgsz
        self._classifier_imgsz = classifier_imgsz
        self._classifier_min_conf = classifier_min_conf
        self._fallback_stable_frames = fallback_stable_frames
        self._fallback_poll_interval = fallback_poll_interval

        self._roi_bbox = board_roi_bbox(calibration_matrix, image_size)
        self._square_bboxes = square_pixel_bboxes(calibration_matrix, image_size)
        self._gate = BoardMotionGate()
        self._resolver = MoveResolver()

        self._stable_matrix = standard_starting_matrix()
        self._stable_frame = None

    @property
    def current_matrix(self):
        return [row[:] for row in self._stable_matrix]

    def reset(self):
        """Re-seeds tracking at the standard starting position and resets
        the move resolver. Wire this to a 'New Game' control (the
        product's design already calls for a momentary push button for
        this)."""
        self._resolver.reset()
        self._stable_matrix = standard_starting_matrix()
        self._stable_frame = None

    def run_forever(self):
        while True:
            self.tick()
            time.sleep(self._poll_interval)

    def tick(self):
        """Runs one cheap motion-gate poll. Only does real work (a
        settle's worth of diffing/classification/legality-checking, or a
        full rescan) when the gate reports "settled" -- most calls just
        check the ROI's grayscale diff and return immediately."""
        frame, _timestamp = self._capture_stream.get_latest()
        if frame is None:
            return

        if self._stable_frame is None:
            self._stable_frame = frame

        roi_gray = to_gray_roi(frame, self._roi_bbox)
        if self._gate.update(roi_gray) != "settled":
            return

        self._handle_settle(frame)

    def _handle_settle(self, frame):
        changed = diff_changed_squares(self._stable_frame, frame, self._square_bboxes)

        if changed and len(changed) <= MAX_PLAUSIBLE_SQUARES:
            proposed = self._apply_classifier(frame, [square for square, _score in changed])
            if proposed is not None:
                san, _move = self._resolver.resolve(proposed)
                if san is not None:
                    self._accept(proposed, frame, san)
                    return

        # The diff found nothing, found too many squares to trust, hit an
        # unresolved (low-confidence) crop, or didn't match a legal move.
        # Fall back to a full-frame rescan rather than guess.
        self._fallback_rescan(frame)

    def _apply_classifier(self, frame, squares):
        results = classify_squares(
            self._classifier_model,
            frame,
            squares,
            self._square_bboxes,
            imgsz=self._classifier_imgsz,
            min_conf=self._classifier_min_conf,
        )
        proposed = [row[:] for row in self._stable_matrix]
        for square, (class_name, _confidence) in results.items():
            if class_name is UNRESOLVED:
                return None
            file_idx, rank_idx = square
            proposed[rank_idx][file_idx] = class_name
        return proposed

    def _fallback_rescan(self, frame):
        tracker = BoardStateTracker(
            self._calibration_matrix,
            history=self._fallback_stable_frames + 2,
            stable_frames=self._fallback_stable_frames,
        )
        matrix = self._stable_matrix
        for attempt in range(self._fallback_stable_frames):
            if attempt:
                time.sleep(self._fallback_poll_interval)
            live_frame, _timestamp = self._capture_stream.get_latest()
            detections = detect(
                self._fallback_model, live_frame, conf=self._fallback_conf, imgsz=self._fallback_imgsz
            )
            matrix, _changed = tracker.update(detections)

        san, _move = self._resolver.resolve(matrix)
        if san is not None:
            self._accept(matrix, frame, san)
            return

        # Still unresolved even after a full rescan -- resync so the game
        # can continue, but flag it: no single legal move explains what
        # the camera sees, and special-move rights reset (known
        # limitation, see MoveResolver.resync).
        self._resolver.resync(matrix)
        self._stable_matrix = [row[:] for row in matrix]
        self._stable_frame = frame
        self._on_update(self._stable_matrix, None, frame, True)

    def _accept(self, matrix, frame, move_text):
        self._stable_matrix = [row[:] for row in matrix]
        self._stable_frame = frame
        self._on_update(self._stable_matrix, move_text, frame, False)
