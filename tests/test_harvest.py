"""Tests for harvest.CropHarvester -- auto-collected training data from
real play. The risk here is silently poisoning the dataset with wrong
labels, so the label derivation and the crop geometry both get checked
directly against what's on disk.

Also covers the collect_square_crops filename bug these changes fix: two
collection sessions must accumulate, not overwrite each other.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

import harvest  # noqa: E402
from move_resolver import standard_starting_matrix  # noqa: E402
from square_classifier import ALL_SQUARES, BLACK, EMPTY, WHITE  # noqa: E402


def _bboxes():
    # One distinct 4x4 region per square, laid out in an 8x8 grid.
    return {(f, r): (f * 4, r * 4, f * 4 + 4, r * 4 + 4) for f, r in ALL_SQUARES}


def _frame():
    return np.full((32, 32, 3), 128, dtype=np.uint8)


class TestLabelFor(unittest.TestCase):
    def test_derives_labels_from_the_starting_position(self):
        matrix = standard_starting_matrix()
        self.assertEqual(harvest.label_for(matrix, (0, 0)), WHITE)   # a1 rook
        self.assertEqual(harvest.label_for(matrix, (4, 1)), WHITE)   # e2 pawn
        self.assertEqual(harvest.label_for(matrix, (4, 3)), EMPTY)   # e4
        self.assertEqual(harvest.label_for(matrix, (3, 6)), BLACK)   # d7 pawn
        self.assertEqual(harvest.label_for(matrix, (4, 7)), BLACK)   # e8 king


class TestCropHarvester(unittest.TestCase):
    def test_writes_one_correctly_labelled_crop_per_square(self):
        matrix = standard_starting_matrix()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            h = harvest.CropHarvester(out, _bboxes(), session="s1", seed=0)
            written = h.record(matrix, _frame())

            self.assertEqual(written, 64)
            files = list(out.rglob("*.jpg"))
            self.assertEqual(len(files), 64)

            # 32 pieces at the start: 16 white, 16 black, 32 empty squares.
            by_label = {}
            for path in files:
                by_label[path.parent.name] = by_label.get(path.parent.name, 0) + 1
            self.assertEqual(by_label[WHITE], 16)
            self.assertEqual(by_label[BLACK], 16)
            self.assertEqual(by_label[EMPTY], 32)

    def test_crops_use_the_given_square_geometry(self):
        # Paint one square's region a unique colour and confirm that exact
        # region is what lands in the file -- a geometry mixup would train
        # the model on the wrong part of the board.
        matrix = standard_starting_matrix()
        frame = _frame()
        frame[4:8, 0:4] = (10, 20, 30)  # square (0, 1) == a2, a white pawn

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            h = harvest.CropHarvester(out, _bboxes(), session="s1", seed=0)
            h.record(matrix, frame)

            match = list(out.rglob("*_a2.jpg"))
            self.assertEqual(len(match), 1)
            self.assertEqual(match[0].parent.name, WHITE)
            crop = cv2.imread(str(match[0]))
            self.assertEqual(crop.shape, (4, 4, 3))
            self.assertLess(abs(int(crop[0, 0][0]) - 10), 3)  # JPEG is lossy

    def test_no_frame_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            h = harvest.CropHarvester(out, _bboxes(), session="s1", seed=0)
            self.assertEqual(h.record(standard_starting_matrix(), None), 0)
            self.assertEqual(list(out.rglob("*.jpg")), [])

    def test_repeated_records_accumulate_rather_than_overwrite(self):
        matrix = standard_starting_matrix()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            h = harvest.CropHarvester(out, _bboxes(), session="s1", seed=0)
            h.record(matrix, _frame())
            h.record(matrix, _frame())
            self.assertEqual(len(list(out.rglob("*.jpg"))), 128)


class TestCollectSessionNaming(unittest.TestCase):
    """The bug this fixes: collect_square_crops used round_idx alone, which
    restarts at 0 each run, so a second session overwrote the first."""

    def test_two_sessions_do_not_overwrite_each_other(self):
        import collect_square_crops as collect

        squares = [(0, 0), (1, 0)]
        bboxes = _bboxes()
        frames = [_frame(), _frame()]

        with tempfile.TemporaryDirectory() as tmp:
            split_dir = Path(tmp) / "train"
            first = collect.save_crops(frames, squares, WHITE, bboxes, split_dir, "sessionA", 0)
            second = collect.save_crops(frames, squares, WHITE, bboxes, split_dir, "sessionB", 0)

            # Same round index in both runs -- previously these collided.
            self.assertEqual(first, second)
            self.assertEqual(len(list((split_dir / WHITE).glob("*.jpg"))), first + second)


if __name__ == "__main__":
    unittest.main()
