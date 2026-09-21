"""Tests for graveyard.nearest_free_slot -- which slot a capture goes to.

The pool is shared between both colours and the arm takes the nearest free
slot, so the properties that matter are: it really is the nearest, it never
hands back a slot that already holds a piece, and it gives the same answer
twice for the same inputs. That last one is easy to lose to a set iteration
order or a float comparison and miserable to debug afterwards, so it is
asserted directly.

Pure geometry -- no serial port, no board, no python-chess.
"""

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import graveyard  # noqa: E402
import rig  # noqa: E402


ALL_SLOTS = range(rig.GRAVEYARD_SLOTS)


def brute_force_nearest(x, y, occupied=()):
    """The answer worked out the obvious way, for the allocator to agree with.

    Deliberately not the same shape as the implementation: it sorts every
    candidate rather than tracking a running best, so a bug in the running
    comparison shows up as a disagreement instead of being reproduced here.
    """
    free = [s for s in ALL_SLOTS if s not in occupied]
    if not free:
        return None
    return min(free, key=lambda s: (math.dist(rig.graveyard_slot_to_mm(s), (x, y)), s))


class TestNearestFreeSlot(unittest.TestCase):
    def test_picks_the_nearest_slot_from_every_square(self):
        for file_ in range(8):
            for rank in range(8):
                x, y = rig.square_to_mm(file_, rank)
                self.assertEqual(graveyard.nearest_free_slot(x, y),
                                 brute_force_nearest(x, y),
                                 f"disagreed for file {file_} rank {rank}")

    def test_a_corner_piece_goes_to_an_adjacent_slot(self):
        """a1's nearest slot is one square away, not across the board."""
        x, y = rig.square_to_mm(0, 0)
        slot = graveyard.nearest_free_slot(x, y)
        self.assertAlmostEqual(graveyard.slot_distance_mm(slot, x, y), rig.SQUARE_MM)

    def test_occupied_slots_are_skipped(self):
        x, y = rig.square_to_mm(0, 0)
        first = graveyard.nearest_free_slot(x, y)
        second = graveyard.nearest_free_slot(x, y, occupied={first})
        self.assertNotEqual(first, second)
        self.assertEqual(second, brute_force_nearest(x, y, {first}))

    def test_filling_the_ring_never_repeats_a_slot(self):
        """Capture 32 pieces from the middle of the board and every one gets
        its own slot -- the failure this guards against is an allocator that
        keeps handing back the same nearest slot because the caller's pile
        isn't consulted."""
        x, y = rig.square_to_mm(4, 3)
        pile = set()
        for _ in ALL_SLOTS:
            slot = graveyard.nearest_free_slot(x, y, occupied=pile)
            self.assertIsNotNone(slot)
            self.assertNotIn(slot, pile)
            pile.add(slot)
        self.assertEqual(pile, set(ALL_SLOTS))

    def test_a_full_ring_returns_none(self):
        # Rather than raising, or silently returning slot 0 on top of a piece.
        self.assertIsNone(graveyard.nearest_free_slot(0, 0, occupied=set(ALL_SLOTS)))

    def test_ties_go_to_the_lowest_index(self):
        """h8 is exactly one square from both the east and the north strip.

        Whichever way that resolves, it has to resolve the SAME way every
        time, or the same position plans differently between runs.
        """
        x, y = rig.square_to_mm(7, 7)
        east, north = 15, 16
        self.assertAlmostEqual(graveyard.slot_distance_mm(east, x, y),
                               graveyard.slot_distance_mm(north, x, y))
        self.assertEqual(graveyard.nearest_free_slot(x, y), min(east, north))

    def test_is_deterministic(self):
        x, y = rig.square_to_mm(3, 4)
        pile = {0, 5, 11, 17, 23, 30}
        answers = {graveyard.nearest_free_slot(x, y, occupied=pile) for _ in range(20)}
        self.assertEqual(len(answers), 1)

    def test_accepts_a_board_square_directly(self):
        x, y = rig.square_to_mm(2, 6)
        self.assertEqual(graveyard.nearest_free_slot_for_square(2, 6),
                         graveyard.nearest_free_slot(x, y))

    def test_occupied_defaults_to_empty(self):
        # The common call is a first capture with no pile to pass.
        self.assertIsNotNone(graveyard.nearest_free_slot(0, 0))


class TestSlotPositions(unittest.TestCase):
    def test_matches_rig(self):
        self.assertEqual(graveyard.slot_positions(),
                         [rig.graveyard_slot_to_mm(s) for s in ALL_SLOTS])

    def test_every_slot_is_reachable_with_a_piece(self):
        for slot, (x, y) in enumerate(graveyard.slot_positions()):
            self.assertTrue(rig.within_travel(x, y), f"slot {slot} unreachable")


if __name__ == "__main__":
    unittest.main()
