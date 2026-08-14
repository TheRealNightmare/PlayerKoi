"""Tests for occupancy_color.py's classical-CV thresholds and multi-frame
consensus wrapper -- run against synthetic flat-color numpy frames, no
camera required.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import numpy as np  # noqa: E402

import occupancy_color as oc  # noqa: E402

BOX = (0, 0, 10, 10)  # a single 10x10 bbox reused for both squares' crops
SQUARES = [(0, 0)]


def _flat_frame(bgr, size=20):
    frame = np.zeros((size, size, 3), dtype=np.uint8)
    frame[:, :] = bgr
    return frame


def _flat_crop(bgr, shape=(10, 10)):
    crop = np.zeros((shape[0], shape[1], 3), dtype=np.uint8)
    crop[:, :] = bgr
    return crop


def _make_baseline(empty_bgr=(40, 40, 40), white_lum=220.0, black_lum=40.0, occ_boundary=30.0, occ_scale=5.0,
                    color_std=5.0, background_bgr=None, background_shape=(10, 10), foreground_pixel_scale=5.0):
    # background_bgr defaults to empty_bgr -- in reality both describe the
    # same thing (the bare square's calibrated appearance), just read from
    # two differently-sized crops (footprint vs. full body).
    if background_bgr is None:
        background_bgr = empty_bgr
    return {
        "empty": {(0, 0): (np.array(empty_bgr, dtype=np.float64), 2.0)},
        "white_luminance_mean": white_lum,
        "black_luminance_mean": black_lum,
        "white_luminance_std": color_std,
        "black_luminance_std": color_std,
        "color_threshold": (white_lum + black_lum) / 2.0,
        "white_is_brighter": white_lum >= black_lum,
        "occupancy_boundary": occ_boundary,
        "occupancy_scale": occ_scale,
        "background_crops": {(0, 0): _flat_crop(background_bgr, background_shape)},
        "foreground_pixel_scale": foreground_pixel_scale,
    }


class TestClassifySquareOccupancy(unittest.TestCase):
    def setUp(self):
        self.baseline = _make_baseline()
        self.footprint_bboxes = {(0, 0): BOX}
        self.square_bboxes = {(0, 0): BOX}

    def test_matches_empty_baseline(self):
        frame = _flat_frame((40, 40, 40))  # exactly the empty baseline color
        state, _margin = oc.classify_square_occupancy(
            frame, (0, 0), self.footprint_bboxes, self.square_bboxes, self.baseline
        )
        self.assertEqual(state, oc.EMPTY)

    def test_clearly_occupied_white(self):
        # Footprint far from empty baseline (occupied); body luminance near
        # the white cluster mean (220).
        frame = _flat_frame((220, 220, 220))
        state, _margin = oc.classify_square_occupancy(
            frame, (0, 0), self.footprint_bboxes, self.square_bboxes, self.baseline
        )
        self.assertEqual(state, oc.WHITE)

    def test_clearly_occupied_black(self):
        frame = _flat_frame((40, 40, 40))
        # Same color as the empty baseline would read as EMPTY -- use a
        # color that's clearly occupied (far from baseline) but dark
        # (near the black luminance cluster mean, 40).
        frame = _flat_frame((45, 45, 30))  # far enough from (40,40,40) in norm, low luminance
        # Force a larger gap to make the occupancy decision unambiguous
        # for this synthetic test.
        baseline = _make_baseline(empty_bgr=(200, 200, 200), black_lum=45.0, white_lum=220.0)
        state, _margin = oc.classify_square_occupancy(
            frame, (0, 0), self.footprint_bboxes, self.square_bboxes, baseline
        )
        self.assertEqual(state, oc.BLACK)

    def test_boundary_read_is_unresolved(self):
        # A color sitting right on the occupancy decision boundary (neither
        # confidently empty nor confidently occupied).
        empty_bgr = np.array([40.0, 40.0, 40.0])
        boundary_bgr = empty_bgr + np.array([30.0, 0.0, 0.0])  # diff == occ_boundary exactly
        baseline = _make_baseline(empty_bgr=tuple(empty_bgr), occ_boundary=30.0, occ_scale=5.0)
        frame = _flat_frame(tuple(boundary_bgr))
        state, _margin = oc.classify_square_occupancy(
            frame, (0, 0), self.footprint_bboxes, self.square_bboxes, baseline
        )
        self.assertIs(state, oc.UNRESOLVED)

    def test_boundary_color_read_is_unresolved(self):
        # Confidently occupied, but body luminance sits right at the
        # white/black midpoint -- color can't be called confidently.
        baseline = _make_baseline(white_lum=220.0, black_lum=40.0, color_std=5.0)
        midpoint = (220.0 + 40.0) / 2.0
        frame = _flat_frame((midpoint, midpoint, midpoint))
        state, _margin = oc.classify_square_occupancy(
            frame, (0, 0), self.footprint_bboxes, self.square_bboxes, baseline
        )
        self.assertIs(state, oc.UNRESOLVED)


class TestBackgroundSubtractionForColor(unittest.TestCase):
    """Regression tests for the background-bleed bug: a piece's full body
    crop (square_bboxes) includes headroom above its footprint that, for a
    piece not filling its whole crop, shows mostly bare-square color. A
    plain crop average lets that square color dominate and can flip the
    read. These construct a crop where the piece is a minority of the
    pixels and the majority is bare-square color, and check the read is
    still correct -- and identical -- whether that square happens to be a
    light or a dark one.
    """

    def setUp(self):
        # footprint = fully piece-colored (bottom 10 rows); square_bboxes
        # (body crop) = the same 10 rows plus 20 rows of headroom above
        # that, mostly showing bare square -- a 2:1 background:piece ratio.
        self.footprint_bboxes = {(0, 0): (0, 20, 10, 30)}
        self.square_bboxes = {(0, 0): (0, 0, 10, 30)}

    def _frame(self, background_bgr, piece_bgr):
        frame = np.zeros((30, 10, 3), dtype=np.uint8)
        frame[0:20, :] = background_bgr  # headroom -- bare square, no piece here
        frame[20:30, :] = piece_bgr  # footprint -- piece's base
        return frame

    def test_black_piece_reads_black_regardless_of_light_or_dark_square(self):
        piece_bgr = (10, 10, 10)
        for square_name, square_bgr in [("light", (230, 230, 230)), ("dark", (50, 50, 50))]:
            with self.subTest(square=square_name):
                baseline = _make_baseline(empty_bgr=square_bgr, background_shape=(30, 10))
                frame = self._frame(square_bgr, piece_bgr)
                state, _margin = oc.classify_square_occupancy(
                    frame, (0, 0), self.footprint_bboxes, self.square_bboxes, baseline
                )
                self.assertEqual(state, oc.BLACK, f"misread on a {square_name} square")

    def test_white_piece_reads_white_regardless_of_light_or_dark_square(self):
        piece_bgr = (255, 255, 255)
        for square_name, square_bgr in [("light", (200, 200, 200)), ("dark", (50, 50, 50))]:
            with self.subTest(square=square_name):
                baseline = _make_baseline(empty_bgr=square_bgr, background_shape=(30, 10))
                frame = self._frame(square_bgr, piece_bgr)
                state, _margin = oc.classify_square_occupancy(
                    frame, (0, 0), self.footprint_bboxes, self.square_bboxes, baseline
                )
                self.assertEqual(state, oc.WHITE, f"misread on a {square_name} square")


class _FakeCaptureStream:
    """Replays a fixed list of frames, one per get_latest() call, for
    read_settled_state()'s consensus loop."""

    def __init__(self, frames):
        self._frames = list(frames)
        self._i = 0

    def get_latest(self):
        frame = self._frames[min(self._i, len(self._frames) - 1)]
        self._i += 1
        return frame, 0.0


class TestReadSettledState(unittest.TestCase):
    def setUp(self):
        self.baseline = _make_baseline()
        self.footprint_bboxes = {(0, 0): BOX}
        self.square_bboxes = {(0, 0): BOX}

    def test_consensus_confirms_agreeing_samples(self):
        white_frame = _flat_frame((220, 220, 220))
        stream = _FakeCaptureStream([white_frame, white_frame, white_frame])
        result = oc.read_settled_state(
            stream, SQUARES, self.footprint_bboxes, self.square_bboxes, self.baseline,
            initial_frame=white_frame, num_samples=3, window_s=0.0,
        )
        self.assertEqual(result[(0, 0)], oc.WHITE)

    def test_disagreeing_sample_forces_unresolved_for_that_square_only(self):
        white_frame = _flat_frame((220, 220, 220))
        empty_frame = _flat_frame((40, 40, 40))
        # initial_frame supplies sample 1 (white); with num_samples=3, only
        # 2 more are drawn from the stream below -- one agreeing (white),
        # one disagreeing (empty). Consensus must reject this square
        # rather than average the disagreement away.
        stream = _FakeCaptureStream([white_frame, empty_frame])
        result = oc.read_settled_state(
            stream, SQUARES, self.footprint_bboxes, self.square_bboxes, self.baseline,
            initial_frame=white_frame, num_samples=3, window_s=0.0,
        )
        self.assertIs(result[(0, 0)], oc.UNRESOLVED)


if __name__ == "__main__":
    unittest.main()
