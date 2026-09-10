"""Tests for collect_square_crops.py's split assignment and environment
tagging -- pure logic, no camera or picamera2 required.
"""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "training"))

import collect_square_crops as csc  # noqa: E402
from board_state import calibration_paths  # noqa: E402
from eval_by_env import UNTAGGED, env_of  # noqa: E402

from pathlib import Path  # noqa: E402


class AssignSplitsTest(unittest.TestCase):
    def test_every_round_gets_exactly_one_split(self):
        splits = csc.assign_splits(random.Random(0), 12)
        self.assertEqual(len(splits), 12)
        self.assertTrue(set(splits) <= set(csc.SPLITS))

    def test_all_three_splits_populated_for_a_normal_session(self):
        # The bug this guards: independent per-round coin flips at p=0.15
        # frequently produce zero val or zero test rounds in a 12-round run.
        for seed in range(50):
            splits = csc.assign_splits(random.Random(seed), 12)
            for name in csc.SPLITS:
                self.assertIn(name, splits, f"seed {seed} produced no {name} rounds")

    def test_train_keeps_the_majority(self):
        splits = csc.assign_splits(random.Random(1), 20)
        self.assertGreater(splits.count("train"), splits.count("val") + splits.count("test"))

    def test_small_sessions_do_not_starve_train(self):
        splits = csc.assign_splits(random.Random(2), 3)
        self.assertEqual(sorted(splits), sorted(csc.SPLITS))

    def test_degenerate_round_counts(self):
        self.assertEqual(csc.assign_splits(random.Random(3), 0), [])
        self.assertEqual(csc.assign_splits(random.Random(3), 1), ["train"])
        two = csc.assign_splits(random.Random(3), 2)
        self.assertEqual(len(two), 2)
        self.assertIn("train", two)

    def test_seed_is_reproducible(self):
        self.assertEqual(
            csc.assign_splits(random.Random(7), 12),
            csc.assign_splits(random.Random(7), 12),
        )


class DatasetCountsTest(unittest.TestCase):
    def test_counts_cover_all_three_splits(self):
        counts = csc.dataset_counts(Path("/nonexistent-dataset"))
        for split in csc.SPLITS:
            for label in ("empty", "white", "black"):
                self.assertEqual(counts[f"{split}/{label}"], 0)


class EnvTaggingTest(unittest.TestCase):
    def test_calibration_paths_are_per_environment(self):
        default_json, _, _ = calibration_paths()
        env2_json, env2_preview, _ = calibration_paths("2")
        self.assertEqual(default_json.name, "calibration.json")
        self.assertEqual(env2_json.name, "calibration-env2.json")
        self.assertEqual(env2_preview.name, "calibration-env2_preview.jpg")
        # Distinct environments must never collide -- overwriting one
        # environment's board geometry with another's is the failure this
        # whole tagging scheme exists to prevent.
        self.assertNotEqual(env2_json, calibration_paths("3")[0])

    def test_env_recovered_from_crop_filename(self):
        self.assertEqual(env_of(Path("env2-20260911-142030_r003_e4_2.jpg")), "2")
        self.assertEqual(env_of(Path("envkitchen-20260911-142030_r000_a1_0.jpg")), "kitchen")

    def test_untagged_crops_are_grouped(self):
        self.assertEqual(env_of(Path("20260904-101500_r003_e4_2.jpg")), UNTAGGED)
        self.assertEqual(env_of(Path("greenboard_r003_e4_2.jpg")), UNTAGGED)

    def test_save_crops_filenames_carry_the_session(self):
        # save_crops() prefixes with the session id, which is what makes the
        # env tag recoverable per-image; assert the two ends line up.
        session = "env2-20260911-142030"
        name = f"{session}_r003_{csc.square_name((4, 3))}_2.jpg"
        self.assertEqual(env_of(Path(name)), "2")


if __name__ == "__main__":
    unittest.main()
