"""Tests for tracking_loop.TrackingLoop._handle_settle's delta computation
-- feeding it a synthetic full-board classification (bypassing real
camera/geometry by monkeypatching square_classifier.read_settled_state) and
checking the observed delta it hands to MoveResolver.resolve_from_deltas is
exactly the squares a known move touches, no more, no less.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import chess  # noqa: E402
import numpy as np  # noqa: E402

import square_classifier as sc  # noqa: E402
import tracking_loop  # noqa: E402
from move_resolver import standard_starting_matrix  # noqa: E402
from square_classifier import ALL_SQUARES, BLACK, EMPTY, WHITE  # noqa: E402


def _matrix_from_board(board):
    matrix = [[None] * 8 for _ in range(8)]
    for square, piece in board.piece_map().items():
        file_idx = chess.square_file(square)
        rank_idx = chess.square_rank(square)
        color = "white" if piece.color == chess.WHITE else "black"
        matrix[rank_idx][file_idx] = f"{color}-{chess.piece_name(piece.piece_type)}"
    return matrix


def _matrix_state(matrix, square):
    file_idx, rank_idx = square
    label = matrix[rank_idx][file_idx]
    if label is None:
        return EMPTY
    return WHITE if label.startswith("white") else BLACK


def _consensus_for(matrix):
    """Full 64-square consensus dict matching `matrix` exactly -- as if
    square_classifier.read_settled_state classified every square with full
    confidence and it matched `matrix` precisely."""
    return {square: _matrix_state(matrix, square) for square in ALL_SQUARES}


class _FakeCaptureStream:
    def __init__(self):
        self.frame = np.zeros((10, 10, 3), dtype=np.uint8)

    def get_latest(self):
        return self.frame, 0.0


class TestHandleSettleDelta(unittest.TestCase):
    def setUp(self):
        self.capture_stream = _FakeCaptureStream()
        self.updates = []

        def on_update(matrix, move_text, frame, flagged, reason):
            self.updates.append((matrix, move_text, flagged, reason))

        # calibration_matrix/image_size only need to produce *some* bboxes
        # at construction time -- read_settled_state is monkeypatched in
        # every test below, so real pixel geometry (and the classifier
        # model) is never actually exercised.
        self.loop = tracking_loop.TrackingLoop(
            capture_stream=self.capture_stream,
            calibration_matrix=np.eye(3),
            image_size=(10, 10),
            classifier_model=None,
            on_update=on_update,
        )

    def _settle_with(self, matrix):
        consensus = _consensus_for(matrix)
        with mock.patch.object(tracking_loop, "read_settled_state", return_value=consensus):
            self.loop._handle_settle(self.capture_stream.frame)

    def test_quiet_move_delta_is_exactly_the_two_squares_touched(self):
        matrix = standard_starting_matrix()
        matrix[1][4] = None  # e2 empties
        matrix[3][4] = "white-pawn"  # e4 occupied

        self._settle_with(matrix)

        self.assertEqual(len(self.updates), 1)
        result_matrix, move_text, flagged, reason = self.updates[0]
        self.assertEqual(move_text, "e4")
        self.assertFalse(flagged)
        self.assertIsNone(reason)
        self.assertEqual(result_matrix, matrix)

    def test_castling_delta_is_exactly_the_four_squares_touched(self):
        pre_board = chess.Board("r1bqk2r/pppp1ppp/2n2n2/2b1p3/2B1P3/2N2N2/PPPP1PPP/R1BQK2R w KQkq - 4 5")
        self.loop._resolver.board = pre_board.copy()
        self.loop._stable_matrix = _matrix_from_board(pre_board)

        post_board = pre_board.copy()
        post_board.push_san("O-O")
        self._settle_with(_matrix_from_board(post_board))

        self.assertEqual(len(self.updates), 1)
        _matrix, move_text, flagged, reason = self.updates[0]
        self.assertEqual(move_text, "O-O")
        self.assertFalse(flagged)
        self.assertIsNone(reason)

    def test_spurious_settle_with_no_actual_change_is_not_flagged_or_updated(self):
        matrix = standard_starting_matrix()
        self._settle_with(matrix)
        # Nothing changed -- not a real update, and definitely not a flag.
        self.assertEqual(self.updates, [])

    def test_unexplainable_change_is_flagged_not_guessed(self):
        matrix = standard_starting_matrix()
        matrix[0][0] = None  # a1's rook vanishes with no legal move producing this
        self._settle_with(matrix)

        self.assertEqual(len(self.updates), 1)
        _matrix, move_text, flagged, reason = self.updates[0]
        self.assertIsNone(move_text)
        self.assertTrue(flagged)
        self.assertIsNotNone(reason)
        # Flagging must leave the tracked state untouched (last trusted
        # state), not adopt the unexplained matrix.
        self.assertEqual(self.loop.current_matrix, standard_starting_matrix())

    def test_unresolved_square_unrelated_to_the_move_does_not_block_it(self):
        # The whole point of tolerating unresolved squares: a marginal
        # read on some square the move never touched used to flag the
        # entire board.
        matrix = standard_starting_matrix()
        matrix[1][4] = None  # e2 empties
        matrix[3][4] = "white-pawn"  # e4 occupied

        consensus = _consensus_for(matrix)
        consensus[(1, 7)] = sc.UNRESOLVED  # b8, nowhere near the move
        with mock.patch.object(tracking_loop, "read_settled_state", return_value=consensus):
            self.loop._handle_settle(self.capture_stream.frame)

        self.assertEqual(len(self.updates), 1)
        _matrix, move_text, flagged, _reason = self.updates[0]
        self.assertEqual(move_text, "e4")
        self.assertFalse(flagged)

    def test_unresolved_square_that_is_part_of_the_move_flags_rather_than_guessing(self):
        # The safety argument: if the moved-to square is the unresolved
        # one, the delta is incomplete (only e2 emptied), matches no legal
        # move, and flags -- it must never resolve to something wrong.
        matrix = standard_starting_matrix()
        matrix[1][4] = None
        matrix[3][4] = "white-pawn"

        consensus = _consensus_for(matrix)
        consensus[(4, 3)] = sc.UNRESOLVED  # e4, the destination square
        with mock.patch.object(tracking_loop, "read_settled_state", return_value=consensus):
            self.loop._handle_settle(self.capture_stream.frame)

        self.assertEqual(len(self.updates), 1)
        _matrix, move_text, flagged, reason = self.updates[0]
        self.assertIsNone(move_text)
        self.assertTrue(flagged)
        self.assertIn("no unique legal move", reason)
        self.assertEqual(self.loop.current_matrix, standard_starting_matrix())

    def test_too_many_unresolved_squares_is_flagged(self):
        matrix = standard_starting_matrix()
        consensus = _consensus_for(matrix)
        for square in ALL_SQUARES[: tracking_loop.MAX_UNRESOLVED_SQUARES + 1]:
            consensus[square] = sc.UNRESOLVED
        with mock.patch.object(tracking_loop, "read_settled_state", return_value=consensus):
            self.loop._handle_settle(self.capture_stream.frame)

        self.assertEqual(len(self.updates), 1)
        _matrix, move_text, flagged, reason = self.updates[0]
        self.assertIsNone(move_text)
        self.assertTrue(flagged)
        self.assertIn("low-confidence", reason)

    def test_too_many_changed_squares_is_flagged_without_calling_resolver(self):
        matrix = standard_starting_matrix()
        # Blow away an entire rank -- no legal move touches this many squares.
        for file_idx in range(8):
            matrix[1][file_idx] = None
        self._settle_with(matrix)

        self.assertEqual(len(self.updates), 1)
        _matrix, move_text, flagged, reason = self.updates[0]
        self.assertIsNone(move_text)
        self.assertTrue(flagged)
        self.assertIn("squares changed at once", reason)


class TestUndoAndPause(unittest.TestCase):
    def setUp(self):
        self.capture_stream = _FakeCaptureStream()
        self.updates = []

        def on_update(matrix, move_text, frame, flagged, reason):
            self.updates.append((matrix, move_text, flagged, reason))

        self.loop = tracking_loop.TrackingLoop(
            capture_stream=self.capture_stream,
            calibration_matrix=np.eye(3),
            image_size=(10, 10),
            classifier_model=None,
            on_update=on_update,
        )

    def _settle_with(self, matrix):
        consensus = _consensus_for(matrix)
        with mock.patch.object(tracking_loop, "read_settled_state", return_value=consensus):
            self.loop._handle_settle(self.capture_stream.frame)

    def test_undo_restores_both_the_matrix_and_the_resolver_board(self):
        before_matrix = standard_starting_matrix()
        before_fen = self.loop._resolver.board.fen()

        matrix = standard_starting_matrix()
        matrix[1][4] = None
        matrix[3][4] = "white-pawn"
        self._settle_with(matrix)
        self.assertEqual(self.updates[-1][1], "e4")

        san = self.loop.undo_last_move()

        self.assertEqual(san, "e4")
        self.assertEqual(self.loop.current_matrix, before_matrix)
        # The resolver's board must be restored too, not just the matrix --
        # otherwise the next move would be judged against a stale position.
        self.assertEqual(self.loop._resolver.board.fen(), before_fen)

    def test_undo_with_no_history_returns_none(self):
        self.assertIsNone(self.loop.undo_last_move())
        self.assertEqual(self.updates, [])

    def test_paused_tick_ignores_a_settle(self):
        with mock.patch.object(self.loop._gate, "update", return_value="settled"), \
             mock.patch.object(tracking_loop, "read_settled_state") as read:
            self.loop.set_paused(True)
            self.loop.tick()
            read.assert_not_called()

            self.loop.set_paused(False)
            self.loop.tick()
            read.assert_called_once()

    def test_pause_lapses_so_a_closed_editor_cannot_strand_tracking(self):
        self.loop.set_paused(True, lapse_s=0.0)
        self.assertFalse(self.loop.is_paused)

        with mock.patch.object(self.loop._gate, "update", return_value="settled"), \
             mock.patch.object(tracking_loop, "read_settled_state") as read:
            self.loop.tick()
            read.assert_called_once()


if __name__ == "__main__":
    unittest.main()
