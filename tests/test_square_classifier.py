"""Tests for square_classifier.py's multi-frame consensus wrapper -- run
against a fake model (no real NCNN/ultralytics inference), no camera
required.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import square_classifier as sc  # noqa: E402

BOX = (0, 0, 10, 10)
SQUARES = [(0, 0), (1, 0)]


class _FakeProbs:
    def __init__(self, class_id, conf):
        self.top1 = class_id
        self.top1conf = conf


class _FakeResult:
    def __init__(self, class_id, conf, names):
        self.probs = _FakeProbs(class_id, conf)
        self.names = names


class _FakeModel:
    """Returns a scripted sequence of per-crop predictions, one call's
    worth of results per predict() invocation -- lets tests control
    exactly what each square "sees" across consensus samples without a
    real model."""

    NAMES = {0: sc.EMPTY, 1: sc.WHITE, 2: sc.BLACK}

    def __init__(self, responses):
        # responses: list of lists of (class_id, conf), one outer entry
        # per predict() call, inner entries matching the crops passed in.
        self._responses = list(responses)
        self._call = 0

    def predict(self, crops, imgsz=64, verbose=False):
        batch = self._responses[self._call]
        self._call += 1
        assert len(batch) == len(crops)
        return [_FakeResult(class_id, conf, self.NAMES) for class_id, conf in batch]


def _frame():
    import numpy as np

    return np.zeros((10, 10, 3), dtype=np.uint8)


class _FakeCaptureStream:
    def __init__(self, num_calls):
        self._num_calls = num_calls

    def get_latest(self):
        return _frame(), 0.0


class TestClassifyBoard(unittest.TestCase):
    def test_high_confidence_predictions_pass_through(self):
        model = _FakeModel([[(1, 0.95), (2, 0.9)]])  # square 0 -> white, square 1 -> black
        square_bboxes = {SQUARES[0]: BOX, SQUARES[1]: BOX}
        result = sc.classify_board(model, _frame(), square_bboxes, min_conf=0.7)
        self.assertEqual(result[SQUARES[0]], sc.WHITE)
        self.assertEqual(result[SQUARES[1]], sc.BLACK)

    def test_low_confidence_prediction_is_unresolved(self):
        model = _FakeModel([[(1, 0.5), (2, 0.9)]])
        square_bboxes = {SQUARES[0]: BOX, SQUARES[1]: BOX}
        result = sc.classify_board(model, _frame(), square_bboxes, min_conf=0.7)
        self.assertIs(result[SQUARES[0]], sc.UNRESOLVED)
        self.assertEqual(result[SQUARES[1]], sc.BLACK)


class TestReadSettledState(unittest.TestCase):
    def test_consensus_confirms_agreeing_samples(self):
        # 3 samples (all from predict() calls, no initial_frame), all agree white.
        model = _FakeModel([[(1, 0.9)], [(1, 0.9)], [(1, 0.9)]])
        square_bboxes = {SQUARES[0]: BOX}
        result = sc.read_settled_state(
            model, _FakeCaptureStream(3), square_bboxes, num_samples=3, window_s=0.0, min_conf=0.7
        )
        self.assertEqual(result[SQUARES[0]], sc.WHITE)

    def test_disagreeing_sample_forces_unresolved(self):
        # Two agree white, one says black -- consensus must reject rather
        # than average the disagreement away.
        model = _FakeModel([[(1, 0.9)], [(1, 0.9)], [(2, 0.9)]])
        square_bboxes = {SQUARES[0]: BOX}
        result = sc.read_settled_state(
            model, _FakeCaptureStream(3), square_bboxes, num_samples=3, window_s=0.0, min_conf=0.7
        )
        self.assertIs(result[SQUARES[0]], sc.UNRESOLVED)

    def test_any_low_confidence_sample_forces_unresolved(self):
        model = _FakeModel([[(1, 0.9)], [(1, 0.4)], [(1, 0.9)]])
        square_bboxes = {SQUARES[0]: BOX}
        result = sc.read_settled_state(
            model, _FakeCaptureStream(3), square_bboxes, num_samples=3, window_s=0.0, min_conf=0.7
        )
        self.assertIs(result[SQUARES[0]], sc.UNRESOLVED)


if __name__ == "__main__":
    unittest.main()
