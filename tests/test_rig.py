"""Tests for rig.orient -- how the board is seated under the gantry.

This is the one thing in rig.py that isn't a copy of the firmware, and
getting it wrong is not subtle: on this machine the carriage parks on a8
rather than the h1 the sketch assumes, so every square name has to be
rotated 180 degrees before it is sent. Uncorrected, the arm reaches for the
other side's pieces -- which is exactly the bug these tests exist to keep
fixed.
"""

import math
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


class TestGraveyardGeometry(unittest.TestCase):
    """The 32 parking slots ringing the board.

    These are coordinates the arm will drive to with a piece stuck to the
    coil, so "is it actually reachable" is the load-bearing assertion -- a
    slot outside the travel envelope is a stall or an ERR out of range
    mid-drag, which is exactly the failure that loses a piece.
    """

    def slots(self):
        return [rig.graveyard_slot_to_mm(s) for s in range(rig.GRAVEYARD_SLOTS)]

    def test_there_are_thirty_two_distinct_slots(self):
        # 8 per side on 4 sides. Distinctness matters: a duplicated slot means
        # the allocator would eventually stack two pieces in one place.
        self.assertEqual(rig.GRAVEYARD_SLOTS, 32)
        self.assertEqual(len(set(self.slots())), 32)

    def test_every_slot_is_reachable(self):
        for slot, (x, y) in enumerate(self.slots()):
            self.assertTrue(rig.within_travel(x, y),
                            f"slot {slot} at ({x}, {y}) is outside the travel envelope")

    def test_every_slot_sits_one_square_beyond_a_board_edge(self):
        """No slot is on the board, and none is further out than the ring."""
        lo_x, hi_x = rig.A1_X_MM - rig.SQUARE_MM, rig.SQUARE_MM
        lo_y, hi_y = -rig.SQUARE_MM, rig.BOARD_MM
        for slot, (x, y) in enumerate(self.slots()):
            on_a_rank_strip = y in (lo_y, hi_y) and rig.A1_X_MM <= x <= 0.0
            on_a_file_strip = x in (lo_x, hi_x) and rig.A1_Y_MM <= y <= 350.0
            self.assertTrue(on_a_rank_strip or on_a_file_strip,
                            f"slot {slot} at ({x}, {y}) is on neither strip")

    def test_the_corners_are_left_empty(self):
        """A corner slot would sit on no board centre line, so its printed dot
        would miss the grid. Nothing should be diagonally out on both axes."""
        for x, y in self.slots():
            off_x = x < rig.A1_X_MM or x > 0.0
            off_y = y < rig.A1_Y_MM or y > 350.0
            self.assertFalse(off_x and off_y, f"({x}, {y}) is a corner cell")

    def test_slots_are_numbered_around_the_ring(self):
        """Consecutive indices are physically adjacent, corners included.

        This is what lets anything walking the ring treat it as one sequence
        instead of four strips with seams. 50mm between neighbours along a
        strip; 70.7mm across a skipped corner, which is the 50/50 diagonal.
        """
        points = self.slots()
        for i, here in enumerate(points):
            there = points[(i + 1) % len(points)]
            gap = math.dist(here, there)
            self.assertAlmostEqual(gap, 50.0 * math.sqrt(2) if i % 8 == 7 else 50.0,
                                   places=6, msg=f"slot {i} -> {(i + 1) % len(points)}")

    def test_each_strip_owns_eight_consecutive_slots(self):
        for i, name in enumerate(rig.GRAVEYARD_STRIPS):
            for slot in range(i * 8, i * 8 + 8):
                self.assertEqual(rig.graveyard_strip(slot), name)

    def test_an_out_of_range_slot_is_refused(self):
        for slot in (-1, 32, 99):
            with self.assertRaises(ValueError):
                rig.graveyard_slot_to_mm(slot)


class TestOrientMM(unittest.TestCase):
    """orient_mm() is orient() for points that have no file/rank.

    A graveyard slot sits outside the 8x8, so there is no index to flip and
    orient() cannot be used. This rig is seated 180 degrees round, so getting
    this wrong sends every captured piece to the diagonally opposite slot --
    a full-width drag to the wrong place, with nothing to report it.
    """

    def test_it_agrees_with_orient_on_every_square(self):
        """The load-bearing one. Two independent routes to the same machine
        coordinate: rotate the indices then convert, or convert then mirror.
        They must not disagree anywhere, for any seating."""
        for origin in rig.SUPPORTED_ORIGINS:
            for file_ in range(8):
                for rank in range(8):
                    via_index = rig.square_to_mm(*rig.orient(file_, rank, origin=origin))
                    via_mm = rig.orient_mm(*rig.square_to_mm(file_, rank), origin=origin)
                    self.assertAlmostEqual(via_index[0], via_mm[0], places=9,
                                           msg=f"{origin} file {file_} rank {rank}")
                    self.assertAlmostEqual(via_index[1], via_mm[1], places=9,
                                           msg=f"{origin} file {file_} rank {rank}")

    def test_it_is_its_own_inverse(self):
        for origin in rig.SUPPORTED_ORIGINS:
            for point in ((-350.0, 0.0), (0.0, 350.0), (-400.0, -50.0), (50.0, 400.0)):
                self.assertEqual(rig.orient_mm(*rig.orient_mm(*point, origin=origin),
                                               origin=origin), point)

    def test_h1_leaves_everything_alone(self):
        """The firmware's own assumption: no rotation at all."""
        self.assertEqual(rig.orient_mm(-123.0, 45.0, origin="h1"), (-123.0, 45.0))

    def test_every_slot_is_still_reachable_after_mirroring(self):
        """A slot the arm cannot reach is an ERR out of range mid-drag, with
        the coil live and a piece stuck to it."""
        for origin in rig.SUPPORTED_ORIGINS:
            for slot in range(rig.GRAVEYARD_SLOTS):
                x, y = rig.orient_mm(*rig.graveyard_slot_to_mm(slot), origin=origin)
                self.assertTrue(rig.within_travel(x, y),
                                f"{origin} slot {slot} -> ({x}, {y})")

    def test_it_preserves_distance(self):
        """It is a reflection, so 'nearest slot' means the same thing before
        and after -- which is what lets the planner choose a slot in board
        space and hand the machine the mirrored coordinate."""
        a, b = rig.square_to_mm(0, 0), rig.graveyard_slot_to_mm(0)
        before = math.dist(a, b)
        after = math.dist(rig.orient_mm(*a, origin="a8"),
                          rig.orient_mm(*b, origin="a8"))
        self.assertAlmostEqual(before, after, places=9)

    def test_an_unknown_origin_is_refused(self):
        with self.assertRaises(ValueError):
            rig.orient_mm(0.0, 0.0, origin="e4")


if __name__ == "__main__":
    unittest.main()
