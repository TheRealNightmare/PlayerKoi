"""Tests for training/run_paths.py's weights resolution -- pure pathlib
against a temporary fake runs/ tree, no ultralytics or GPU required.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "training"))

import run_paths  # noqa: E402


def make_run(root, name, kind="pt", mtime=None):
    """Creates a fake run directory holding the given artifact."""
    weights = root / name / "weights"
    weights.mkdir(parents=True, exist_ok=True)
    if kind == "pt":
        artifact = weights / "best.pt"
        artifact.write_bytes(b"fake")
    else:
        artifact = weights / "best_ncnn_model"
        artifact.mkdir(exist_ok=True)
        (artifact / "model.ncnn.param").write_bytes(b"fake")
    if mtime is not None:
        os.utime(artifact, (mtime, mtime))
    return artifact


class RunPathsTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)


class ListRunsTest(RunPathsTestCase):
    def test_newest_first(self):
        make_run(self.root, "train", mtime=1000)
        make_run(self.root, "train-3", mtime=3000)
        make_run(self.root, "train-2", mtime=2000)
        names = [name for name, _, _ in run_paths.list_runs("pt", self.root)]
        self.assertEqual(names, ["train-3", "train-2", "train"])

    def test_skips_dirs_without_weights(self):
        # Ultralytics creates runs/classify/val, val-2 as scratch dirs for each
        # test-split scoring pass. They hold no weights and must never be
        # offered as a run to export or deploy.
        make_run(self.root, "train")
        (self.root / "val").mkdir()
        (self.root / "val-2" / "weights").mkdir(parents=True)
        names = [name for name, _, _ in run_paths.list_runs("pt", self.root)]
        self.assertEqual(names, ["train"])

    def test_missing_runs_dir_is_empty_not_an_error(self):
        self.assertEqual(run_paths.list_runs("pt", self.root / "nope"), [])

    def test_ncnn_kind_finds_export_dir(self):
        make_run(self.root, "train", kind="ncnn")
        names = [name for name, _, _ in run_paths.list_runs("ncnn", self.root)]
        self.assertEqual(names, ["train"])

    def test_kinds_do_not_cross_match(self):
        make_run(self.root, "train", kind="pt")
        self.assertEqual(run_paths.list_runs("ncnn", self.root), [])

    def test_renamed_ncnn_dir_still_found(self):
        weights = self.root / "train" / "weights"
        weights.mkdir(parents=True)
        (weights / "square_classifier_ncnn_model").mkdir()
        names = [name for name, _, _ in run_paths.list_runs("ncnn", self.root)]
        self.assertEqual(names, ["train"])


class ResolveTest(RunPathsTestCase):
    def test_explicit_path_wins(self):
        best = make_run(self.root, "train")
        make_run(self.root, "train-2", mtime=9999)
        self.assertEqual(run_paths.resolve(best, None, "pt", True, self.root), best)

    def test_run_name(self):
        make_run(self.root, "train")
        target = make_run(self.root, "train-2")
        got = run_paths.resolve(None, "train-2", "pt", True, self.root)
        self.assertEqual(got, target)

    def test_newest_when_nothing_given(self):
        make_run(self.root, "train", mtime=1000)
        newest = make_run(self.root, "train-2", mtime=2000)
        got = run_paths.resolve(None, None, "pt", True, self.root)
        self.assertEqual(got, newest)

    def test_placeholder_is_named_in_the_error(self):
        # The docs printed `train-N` inside copy-pasteable blocks, so it got
        # pasted verbatim. A bare "file not found" gave no clue why.
        make_run(self.root, "train")
        bad = self.root / "train-N" / "weights" / "best.pt"
        with self.assertRaises(SystemExit) as ctx:
            run_paths.resolve(bad, None, "pt", True, self.root)
        message = str(ctx.exception)
        self.assertIn("placeholder", message)
        self.assertIn("train", message)

    def test_missing_path_lists_real_runs(self):
        make_run(self.root, "train-7")
        with self.assertRaises(SystemExit) as ctx:
            run_paths.resolve(self.root / "absent.pt", None, "pt", True, self.root)
        self.assertIn("train-7", str(ctx.exception))

    def test_unknown_run_name_lists_real_runs(self):
        make_run(self.root, "train")
        with self.assertRaises(SystemExit) as ctx:
            run_paths.resolve(None, "train-9", "pt", True, self.root)
        self.assertIn("train", str(ctx.exception))

    def test_no_runs_at_all(self):
        with self.assertRaises(SystemExit) as ctx:
            run_paths.resolve(None, None, "pt", True, self.root)
        self.assertIn("train first", str(ctx.exception))

    def test_non_tty_without_yes_raises_rather_than_hanging(self):
        # input() on a non-terminal would block a script or CI job forever,
        # so this must fail fast with both ways out.
        make_run(self.root, "train")
        with mock.patch.object(sys, "stdin") as stdin:
            stdin.isatty.return_value = False
            with self.assertRaises(SystemExit) as ctx:
                run_paths.resolve(None, None, "pt", False, self.root)
        self.assertIn("--yes", str(ctx.exception))

    def test_tty_confirmation_accepted(self):
        best = make_run(self.root, "train")
        with mock.patch.object(sys, "stdin") as stdin, \
                mock.patch("builtins.input", return_value="y"):
            stdin.isatty.return_value = True
            self.assertEqual(run_paths.resolve(None, None, "pt", False, self.root), best)

    def test_tty_confirmation_declined_aborts(self):
        make_run(self.root, "train")
        with mock.patch.object(sys, "stdin") as stdin, \
                mock.patch("builtins.input", return_value=""):
            stdin.isatty.return_value = True
            with self.assertRaises(SystemExit) as ctx:
                run_paths.resolve(None, None, "pt", False, self.root)
        self.assertIn("Aborted", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
