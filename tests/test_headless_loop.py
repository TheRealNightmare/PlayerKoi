"""Tests for headless_loop.HeadlessLoop -- the vision-free stand-in for
TrackingLoop used by --ai-vs-ai.

The interesting property is that force_settle() is what advances the game:
RobotController calls it after the arm parks and halts the robot if it
returns False (see robot.py). So these cover both that it commits the right
position for the awkward moves (castling, en passant, promotion) and that it
still refuses when nothing was armed.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import chess  # noqa: E402

from headless_loop import HeadlessLoop, NullStream  # noqa: E402
from move_resolver import matrix_from_board  # noqa: E402


def _play(loop, uci):
    """Arms a move and lets the 'arm' finish it, as the engine thread does."""
    loop.set_expected_move(chess.Move.from_uci(uci))
    return loop.force_settle()


class HeadlessLoopTest(unittest.TestCase):
    def test_starts_at_the_standard_position(self):
        loop = HeadlessLoop()
        self.assertEqual(loop.turn, "white")
        self.assertEqual(loop.current_matrix, matrix_from_board(chess.Board()))
        self.assertIsNone(loop.expected_move)

    def test_force_settle_commits_the_expected_move(self):
        loop = HeadlessLoop()
        self.assertTrue(_play(loop, "e2e4"))
        self.assertEqual(loop.turn, "black")
        self.assertIsNone(loop.expected_move)  # consumed
        self.assertEqual(loop.current_matrix[3][4], "white-pawn")
        self.assertIsNone(loop.current_matrix[1][4])

    def test_force_settle_refuses_with_nothing_armed(self):
        """RobotController treats False as 'the arm moved and nothing
        followed it' and halts, which is the behaviour we want to keep."""
        loop = HeadlessLoop()
        self.assertFalse(loop.force_settle())
        self.assertEqual(loop.current_matrix, matrix_from_board(chess.Board()))

    def test_force_settle_refuses_an_illegal_armed_move(self):
        loop = HeadlessLoop()
        loop.set_expected_move(chess.Move.from_uci("e2e5"))
        self.assertFalse(loop.force_settle())
        self.assertEqual(loop.turn, "white")

    def test_on_update_gets_san_and_the_new_matrix(self):
        seen = []
        loop = HeadlessLoop(on_update=lambda *args: seen.append(args))
        _play(loop, "g1f3")
        (matrix, move_text, frame, flagged, reason), = seen
        self.assertEqual(move_text, "Nf3")
        self.assertIsNone(frame)
        self.assertFalse(flagged)
        self.assertIsNone(reason)
        self.assertEqual(matrix[2][5], "white-knight")

    def test_castling_moves_the_rook_too(self):
        loop = HeadlessLoop()
        for uci in ("e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "g8f6"):
            self.assertTrue(_play(loop, uci))
        self.assertTrue(_play(loop, "e1g1"))
        self.assertEqual(loop.current_matrix[0][6], "white-king")
        self.assertEqual(loop.current_matrix[0][5], "white-rook")
        self.assertIsNone(loop.current_matrix[0][7])
        self.assertIsNone(loop.current_matrix[0][4])

    def test_en_passant_clears_the_square_beside_the_destination(self):
        loop = HeadlessLoop()
        for uci in ("e2e4", "a7a6", "e4e5", "d7d5"):
            self.assertTrue(_play(loop, uci))
        self.assertTrue(_play(loop, "e5d6"))
        self.assertEqual(loop.current_matrix[5][3], "white-pawn")
        self.assertIsNone(loop.current_matrix[4][3])  # the captured pawn on d5

    def test_promotion_records_the_new_piece(self):
        loop = HeadlessLoop()
        loop.apply_manual_correction(
            matrix_from_board(chess.Board("4k3/P7/8/8/8/8/8/4K3 w - - 0 1")), "white"
        )
        self.assertTrue(_play(loop, "a7a8q"))
        self.assertEqual(loop.current_matrix[7][0], "white-queen")

    def test_undo_reverts_the_position_and_returns_san(self):
        loop = HeadlessLoop()
        _play(loop, "e2e4")
        self.assertEqual(loop.undo_last_move(), "e4")
        self.assertEqual(loop.turn, "white")
        self.assertEqual(loop.current_matrix, matrix_from_board(chess.Board()))
        self.assertIsNone(loop.undo_last_move())  # nothing left

    def test_undo_clears_an_armed_move(self):
        """The position changed underneath it, so the engine has to rethink
        rather than have the arm play a move for the old position."""
        loop = HeadlessLoop()
        _play(loop, "e2e4")
        loop.set_expected_move(chess.Move.from_uci("e7e5"))
        loop.undo_last_move()
        self.assertIsNone(loop.expected_move)

    def test_manual_correction_adopts_the_position_and_turn(self):
        loop = HeadlessLoop()
        board = chess.Board("8/8/8/4k3/8/8/4P3/4K3 b - - 0 1")
        loop.apply_manual_correction(matrix_from_board(board), "black")
        self.assertEqual(loop.turn, "black")
        self.assertEqual(loop.current_matrix[4][4], "black-king")
        self.assertTrue(_play(loop, "e5e4"))

    def test_reset_returns_to_the_start(self):
        loop = HeadlessLoop()
        _play(loop, "e2e4")
        loop.reset()
        self.assertEqual(loop.turn, "white")
        self.assertEqual(loop.current_matrix, matrix_from_board(chess.Board()))

    def test_pause_is_recorded_but_gates_nothing(self):
        """RobotController pauses and keeps refreshing the pause while the
        arm moves. There is no tracking to hold still here, so a paused loop
        must still accept moves rather than stalling the game."""
        loop = HeadlessLoop()
        loop.set_paused(True)
        self.assertTrue(loop.is_paused)
        self.assertTrue(_play(loop, "e2e4"))
        loop.set_paused(False)
        self.assertFalse(loop.is_paused)

    def test_board_copy_is_a_copy(self):
        loop = HeadlessLoop()
        board = loop.board_copy
        board.push(chess.Move.from_uci("e2e4"))
        self.assertEqual(loop.turn, "white")

    def test_null_stream_has_no_frame(self):
        self.assertEqual(NullStream().get_latest(), (None, None))


if __name__ == "__main__":
    unittest.main()
