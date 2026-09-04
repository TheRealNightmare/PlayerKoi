"""Event-gated board tracking loop.

Drives this flow: motion-gate (cheap, ML-free) -> a full 64-square
empty/white/black classification, sampled with multi-frame consensus (see
square_classifier.py) -> compute the observed delta against the tracker's
own state -> match it against every legal chess move (see
move_resolver.MoveResolver.resolve_from_deltas) -> accept, or flag for
manual correction via the web UI.

There is deliberately no automatic rescan fallback in this design. Since
vision only ever needs to answer empty/white/black per square (piece type
comes from the tracker's own maintained state -- see move_resolver.py's
docstring for why), a full-board read every settle is affordable, and both
the classifier and the delta match refuse to guess rather than produce a
wrong answer (see square_classifier.py's consensus/confidence handling).
When something can't be resolved with confidence, the loop leaves state
untouched and flags it; a human corrects it via the web UI
(apply_manual_correction), which is the sole recovery path.

This mirrors the product's own designed flow ("Board Settles -> Image
Captured -> Occupancy/Color Read -> Move Validated -> flag for manual
correction if no legal move matches"), and exploits the guaranteed-correct
starting position: the tracker seeds from the known standard position and
thereafter only needs to resolve *changes*, never re-deriving piece type
from vision.
"""

import threading
import time

from move_resolver import MoveResolver, standard_starting_matrix
from roi_diff import MAX_PLAUSIBLE_SQUARES, BoardMotionGate, to_gray_roi
from square_classifier import UNRESOLVED, read_settled_state
from square_geometry import board_roi_bbox, square_pixel_bboxes


def _matrix_color(matrix, square):
    file_idx, rank_idx = square
    label = matrix[rank_idx][file_idx]
    if label is None:
        return "empty"
    return label.split("-")[0]


class TrackingLoop:
    def __init__(
        self,
        capture_stream,
        calibration_matrix,
        image_size,
        classifier_model,
        on_update,
        poll_interval=0.12,
        consensus_samples=3,
        consensus_window_s=0.4,
        classifier_imgsz=64,
        classifier_min_conf=0.7,
        motion_thresh=None,
    ):
        """on_update(matrix, move_text, frame, flagged, reason) is called
        whenever the stable board state changes. move_text is a SAN move
        string (e.g. "Nf3") when the change resolved to a single legal
        move. When the settle couldn't be resolved with confidence,
        move_text is None, flagged is True, and reason is a short
        human-readable explanation -- the board is left untouched until a
        manual correction arrives via apply_manual_correction()."""
        self._capture_stream = capture_stream
        self._calibration_matrix = calibration_matrix
        self._classifier_model = classifier_model
        self._on_update = on_update
        self._poll_interval = poll_interval
        self._consensus_samples = consensus_samples
        self._consensus_window_s = consensus_window_s
        self._classifier_imgsz = classifier_imgsz
        self._classifier_min_conf = classifier_min_conf

        self._roi_bbox = board_roi_bbox(calibration_matrix, image_size)
        self._square_bboxes = square_pixel_bboxes(calibration_matrix, image_size)
        # motion_thresh=None keeps BoardMotionGate's own tuned default --
        # see its docstring for why this needs re-tuning per rig.
        self._gate = BoardMotionGate() if motion_thresh is None else BoardMotionGate(motion_thresh=motion_thresh)
        self._resolver = MoveResolver()
        # Reentrant: current_matrix (used internally by _handle_settle,
        # apply_manual_correction, etc. while already holding the lock) and
        # external callers both go through the same property.
        self._lock = threading.RLock()

        self._stable_matrix = standard_starting_matrix()
        self._stable_frame = None
        self._flag_reason = None

    @property
    def current_matrix(self):
        with self._lock:
            return [row[:] for row in self._stable_matrix]

    @property
    def turn(self):
        with self._lock:
            return self._resolver.turn

    def reset(self):
        """Re-seeds tracking at the standard starting position and resets
        the move resolver. Wire this to a 'New Game' control."""
        with self._lock:
            self._resolver.reset()
            self._stable_matrix = standard_starting_matrix()
            self._stable_frame = None
            self._flag_reason = None

    def apply_manual_correction(self, matrix, turn, frame):
        """Accepts a user-corrected board state from the web UI -- the sole
        recovery path when a settle can't be resolved with confidence.
        matrix is a full 8x8 type+color matrix (board_state convention),
        turn is "white" or "black"."""
        with self._lock:
            self._resolver.resync(matrix, turn=turn)
            self._stable_matrix = [row[:] for row in matrix]
            self._stable_frame = frame
            self._flag_reason = None
            self._on_update(self.current_matrix, None, frame, False, None)

    def run_forever(self):
        while True:
            self.tick()
            time.sleep(self._poll_interval)

    def tick(self):
        """Runs one cheap motion-gate poll. Only does real work (a full
        occupancy/color read plus legal-move matching) when the gate
        reports "settled" -- most calls just check the ROI's grayscale
        diff and return immediately."""
        frame, _timestamp = self._capture_stream.get_latest()
        if frame is None:
            return

        with self._lock:
            if self._stable_frame is None:
                self._stable_frame = frame

            roi_gray = to_gray_roi(frame, self._roi_bbox)
            if self._gate.update(roi_gray) != "settled":
                return

            self._handle_settle(frame)

    def _handle_settle(self, frame):
        """Called with self._lock held."""
        consensus = read_settled_state(
            self._classifier_model,
            self._capture_stream,
            self._square_bboxes,
            initial_frame=frame,
            num_samples=self._consensus_samples,
            window_s=self._consensus_window_s,
            imgsz=self._classifier_imgsz,
            min_conf=self._classifier_min_conf,
        )

        if any(state is UNRESOLVED for state in consensus.values()):
            self._flag_unresolved(frame, "low-confidence or inconsistent occupancy/color read on one or more squares")
            return

        observed_deltas = {
            square: state
            for square, state in consensus.items()
            if state != _matrix_color(self._stable_matrix, square)
        }

        if not observed_deltas:
            # Spurious settle trigger (nothing actually changed) -- refresh
            # the stable frame so the motion gate doesn't keep re-firing on
            # stale pixel drift, but this isn't a real update.
            self._stable_frame = frame
            return

        if len(observed_deltas) > MAX_PLAUSIBLE_SQUARES:
            self._flag_unresolved(
                frame, f"{len(observed_deltas)} squares changed at once -- more than any legal move touches"
            )
            return

        san, _move, patch = self._resolver.resolve_from_deltas(observed_deltas)
        if san is None:
            self._flag_unresolved(frame, "no unique legal move explains the observed change")
            return

        for (file_idx, rank_idx), label in patch.items():
            self._stable_matrix[rank_idx][file_idx] = label

        self._stable_frame = frame
        self._flag_reason = None
        self._on_update(self.current_matrix, san, frame, False, None)

    def _flag_unresolved(self, frame, reason):
        """Called with self._lock held. Leaves _stable_matrix and the
        resolver's board untouched -- last trusted state -- until a manual
        correction arrives. Refreshes _stable_frame so the motion gate
        doesn't keep re-triggering on the same static discrepancy."""
        self._stable_frame = frame
        self._flag_reason = reason
        self._on_update(self.current_matrix, None, frame, True, reason)
