"""Tests for rig.orient -- how the board is seated under the gantry.

This is the one thing in rig.py that isn't a copy of the firmware, and
getting it wrong is not subtle: on this machine the carriage parks on a8
rather than the h1 the sketch assumes, so every square name has to be
rotated 180 degrees before it is sent. Uncorrected, the arm reaches for the
other side's pieces -- which is exactly the bug these tests exist to keep
fixed.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import rig  # noqa: E402

ALL_SQUARES = [f"{chr(ord('a') + f)}{r + 1}" for f in range(8) for r in range(8)]


class TestThisRig(unittest.TestCase):
    """The measured default: origin a8, a 180 degree rotation."""

    def test_the_default_is_the_measured_a8_origin(self):
        self.assertEqual(rig.ORIGIN_SQUARE, "a8")

    def test_the_park_corner_maps_to_the_origin_the_firmware_assumes(self):
        # The firmware drives to h1 for (0, 0); the carriage is really on a8.
        self.assertEqual(rig.orient_square("a8"), "h1")
        self.assertEqual(rig.orient_square("h1"), "a8")

    def test_both_file_and_rank_flip(self):
        self.assertEqual(rig.orient_square("e2"), "d7")
        self.assertEqual(rig.orient_square("a1"), "h8")
        self.assertEqual(rig.orient_square("d4"), "e5")

    def test_a_uci_move_rotates_both_squares(self):
        self.assertEqual(rig.orient_uci("e2e4"), "d7d5")
        self.assertEqual(rig.orient_uci("b1c3"), "g8f6")

    def test_a_promotion_suffix_is_dropped(self):
        """MOVE/KNIGHT take squares; the firmware has no idea what a
        promotion is."""
        self.assertEqual(rig.orient_uci("a7a8q"), "h2h1")


class TestEveryOrientation(unittest.TestCase):
    def test_h1_is_the_identity(self):
        """The firmware's own assumption -- no correction at all."""
        for name in ALL_SQUARES:
            self.assertEqual(rig.orient_square(name, origin="h1"), name)

    def test_h8_flips_ranks_only(self):
        self.assertEqual(rig.orient_square("e2", origin="h8"), "e7")
        self.assertEqual(rig.orient_square("a1", origin="h8"), "a8")

    def test_a1_flips_files_only(self):
        self.assertEqual(rig.orient_square("e2", origin="a1"), "d2")
        self.assertEqual(rig.orient_square("a1", origin="a1"), "h1")

    def test_every_orientation_is_its_own_inverse(self):
        """A round trip has to be the identity, or the helper can't be
        trusted to answer 'which square did the machine actually go to?'."""
        for origin in rig.SUPPORTED_ORIGINS:
            for name in ALL_SQUARES:
                rotated = rig.orient_square(name, origin=origin)
                self.assertEqual(rig.orient_square(rotated, origin=origin), name)

    def test_every_orientation_is_a_bijection(self):
        for origin in rig.SUPPORTED_ORIGINS:
            mapped = {rig.orient_square(n, origin=origin) for n in ALL_SQUARES}
            self.assertEqual(mapped, set(ALL_SQUARES))

    def test_an_unknown_origin_is_refused(self):
        with self.assertRaises(ValueError):
            rig.orient_square("e2", origin="e4")


class TestCoordinateForm(unittest.TestCase):
    def test_orient_takes_and_returns_file_rank(self):
        self.assertEqual(rig.orient(4, 1, origin="a8"), (3, 6))

    def test_fractions_survive(self):
        """Weave waypoints sit on lattice lines between squares, so the
        native planner rotates half-square coordinates."""
        self.assertEqual(rig.orient(0.5, 1.5, origin="a8"), (6.5, 5.5))

    def test_the_travel_range_is_preserved(self):
        """Rotation is symmetric about the board centre, so a coordinate
        already folded into 0..7 stays there -- which is why the native
        planner can fold first and rotate after."""
        for origin in rig.SUPPORTED_ORIGINS:
            for value in (0.0, 0.5, 3.5, 7.0):
                x, y = rig.orient(value, value, origin=origin)
                self.assertTrue(0.0 <= x <= 7.0 and 0.0 <= y <= 7.0)


if __name__ == "__main__":
    unittest.main()
