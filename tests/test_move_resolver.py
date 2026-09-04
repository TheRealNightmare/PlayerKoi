"""Tests for move_resolver.MoveResolver.resolve_from_deltas -- the core
matching logic behind the occupancy/color redesign (see that module's
docstring and src/square_classifier.py). Runs entirely against
python-chess, no camera/calibration/OpenCV required.

Castling and en-passant FEN fixtures below were independently checked
against python-chess (is_valid() + the exact move present in legal_moves)
before being hardcoded here, rather than derived from the code under test.
Expected delta squares for castling/en-passant/promotion are hand-computed
from well-known board coordinates (e1/f1/g1/h1 etc.), not by calling
MoveResolver._expected_delta -- that's the method under test.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import chess  # noqa: E402

from move_resolver import MoveResolver  # noqa: E402


def sq(file_idx, rank_idx):
    return (file_idx, rank_idx)


class TestQuietMovesAndCaptures(unittest.TestCase):
    def setUp(self):
        self.resolver = MoveResolver()

    def test_quiet_move(self):
        # 1. e4 -- e2 empties, e4 becomes white. No other square involved.
        san, move, patch = self.resolver.resolve_from_deltas({sq(4, 1): "empty", sq(4, 3): "white"})
        self.assertEqual(san, "e4")
        self.assertEqual(move.uci(), "e2e4")
        self.assertEqual(patch, {sq(4, 1): None, sq(4, 3): "white-pawn"})
        self.assertEqual(self.resolver.board.piece_at(chess.E4), chess.Piece(chess.PAWN, chess.WHITE))
        self.assertIsNone(self.resolver.board.piece_at(chess.E2))

    def test_capture_chained_after_quiet_moves(self):
        # 1. e4 d5 2. exd5 -- a full mini-game played entirely through
        # resolve_from_deltas, ending in a capture (d5 goes straight from
        # black to white, no intervening "empty" state).
        san1, _move1, _patch1 = self.resolver.resolve_from_deltas({sq(4, 1): "empty", sq(4, 3): "white"})
        self.assertEqual(san1, "e4")

        san2, _move2, _patch2 = self.resolver.resolve_from_deltas({sq(3, 6): "empty", sq(3, 4): "black"})
        self.assertEqual(san2, "d5")

        san3, move3, patch3 = self.resolver.resolve_from_deltas({sq(4, 3): "empty", sq(3, 4): "white"})
        self.assertEqual(san3, "exd5")
        self.assertEqual(move3.uci(), "e4d5")
        self.assertEqual(patch3, {sq(4, 3): None, sq(3, 4): "white-pawn"})
        self.assertEqual(self.resolver.board.piece_at(chess.D5), chess.Piece(chess.PAWN, chess.WHITE))

    def test_unexplained_delta_returns_none(self):
        # a1 holds a white rook at the start -- no legal move makes it
        # "become black" on move 1. Nothing should match.
        san, move, patch = self.resolver.resolve_from_deltas({sq(0, 0): "black"})
        self.assertIsNone(san)
        self.assertIsNone(move)
        self.assertIsNone(patch)

    def test_too_few_squares_for_any_move_returns_none(self):
        # A single square becoming empty with no matching destination isn't
        # a legal move's delta on the starting position.
        san, move, patch = self.resolver.resolve_from_deltas({sq(4, 1): "empty"})
        self.assertIsNone(san)
        self.assertIsNone(move)
        self.assertIsNone(patch)


class TestSpecialMoves(unittest.TestCase):
    def _resolver_at(self, fen):
        resolver = MoveResolver()
        resolver.board = chess.Board(fen)
        return resolver

    def test_white_kingside_castle(self):
        # Verified independently: chess.Board(fen).is_valid() is True and
        # e1g1 is in board.legal_moves.
        fen = "r1bqk2r/pppp1ppp/2n2n2/2b1p3/2B1P3/2N2N2/PPPP1PPP/R1BQK2R w KQkq - 4 5"
        resolver = self._resolver_at(fen)
        # King e1->g1, rook h1->f1 -- 4 squares, hand-computed.
        deltas = {sq(4, 0): "empty", sq(6, 0): "white", sq(7, 0): "empty", sq(5, 0): "white"}
        san, move, patch = resolver.resolve_from_deltas(deltas)
        self.assertEqual(san, "O-O")
        self.assertEqual(move.uci(), "e1g1")
        self.assertEqual(
            patch, {sq(4, 0): None, sq(6, 0): "white-king", sq(7, 0): None, sq(5, 0): "white-rook"}
        )

    def test_black_queenside_castle(self):
        # Verified independently: chess.Board(fen).is_valid() is True and
        # e8c8 is in board.legal_moves.
        fen = "r3kbnr/pppqpppp/2n5/3p4/3P4/2N2N2/PPPQPPPP/R1B1KB1R b KQkq - 6 6"
        resolver = self._resolver_at(fen)
        # King e8->c8, rook a8->d8 -- 4 squares, hand-computed.
        deltas = {sq(4, 7): "empty", sq(2, 7): "black", sq(0, 7): "empty", sq(3, 7): "black"}
        san, move, patch = resolver.resolve_from_deltas(deltas)
        self.assertEqual(san, "O-O-O")
        self.assertEqual(move.uci(), "e8c8")
        self.assertEqual(
            patch, {sq(4, 7): None, sq(2, 7): "black-king", sq(0, 7): None, sq(3, 7): "black-rook"}
        )

    def test_en_passant(self):
        # White pawn e5, black just played d7-d5 (ep target d6). Verified
        # independently: e5d6 is legal and board.is_en_passant() is True.
        fen = "rnbqkbnr/ppp1pppp/8/3pP3/8/8/PPPP1PPP/RNBQKBNR w KQkq d6 0 3"
        resolver = self._resolver_at(fen)
        # Mover e5->d6, captured pawn's square d5 empties too -- neither
        # the mover's `from` nor `to` square. 3 squares, hand-computed.
        deltas = {sq(4, 4): "empty", sq(3, 5): "white", sq(3, 4): "empty"}
        san, move, patch = resolver.resolve_from_deltas(deltas)
        self.assertEqual(san, "exd6")
        self.assertEqual(move.uci(), "e5d6")
        self.assertEqual(patch, {sq(4, 4): None, sq(3, 5): "white-pawn", sq(3, 4): None})

    def test_promotion_defaults_to_queen_despite_four_legal_variants(self):
        # White pawn a7, a8 empty. Verified independently: a7a8q/r/b/n are
        # all legal (4 distinct promotion moves share this exact delta).
        fen = "1nbqkbnr/P1pppppp/8/8/8/8/1PPPPPPP/RNBQKBNR w KQk - 0 5"
        resolver = self._resolver_at(fen)
        deltas = {sq(0, 6): "empty", sq(0, 7): "white"}
        san, move, patch = resolver.resolve_from_deltas(deltas)
        self.assertEqual(san, "a8=Q")
        self.assertEqual(move.promotion, chess.QUEEN)
        self.assertEqual(patch, {sq(0, 6): None, sq(0, 7): "white-queen"})


class TestOnlyMove(unittest.TestCase):
    """only_move is how an engine-dictated move is enforced: anything else
    physically played must be rejected without touching the board."""

    def setUp(self):
        self.resolver = MoveResolver()

    def test_accepts_the_expected_move(self):
        expected = chess.Move.from_uci("e2e4")
        san, move, patch = self.resolver.resolve_from_deltas(
            {sq(4, 1): "empty", sq(4, 3): "white"}, only_move=expected
        )
        self.assertEqual(san, "e4")
        self.assertEqual(move, expected)
        self.assertEqual(patch, {sq(4, 1): None, sq(4, 3): "white-pawn"})

    def test_rejects_a_different_legal_move_without_touching_the_board(self):
        # d2d4 is perfectly legal, but the engine asked for e2e4.
        before = self.resolver.board.fen()
        san, move, patch = self.resolver.resolve_from_deltas(
            {sq(3, 1): "empty", sq(3, 3): "white"}, only_move=chess.Move.from_uci("e2e4")
        )
        self.assertIsNone(san)
        self.assertIsNone(move)
        self.assertIsNone(patch)
        # Critically: nothing was pushed and then rolled back -- the
        # position must be untouched.
        self.assertEqual(self.resolver.board.fen(), before)

    def test_rejects_an_illegal_expected_move(self):
        san, _move, _patch = self.resolver.resolve_from_deltas(
            {sq(4, 1): "empty", sq(4, 3): "white"}, only_move=chess.Move.from_uci("e2e5")
        )
        self.assertIsNone(san)

    def test_honours_underpromotion_which_the_normal_path_cannot(self):
        # Without only_move, every promotion collapses to queen because
        # vision can't tell them apart. With the engine dictating, the
        # exact move must survive.
        self.resolver.board = chess.Board("1nbqkbnr/P1pppppp/8/8/8/8/1PPPPPPP/RNBQKBNR w KQk - 0 5")
        san, move, patch = self.resolver.resolve_from_deltas(
            {sq(0, 6): "empty", sq(0, 7): "white"}, only_move=chess.Move.from_uci("a7a8n")
        )
        self.assertEqual(san, "a8=N")
        self.assertEqual(move.promotion, chess.KNIGHT)
        self.assertEqual(patch[sq(0, 7)], "white-knight")


class TestResync(unittest.TestCase):
    def test_infers_surviving_castling_rights_from_home_squares(self):
        resolver = MoveResolver()
        # Build a matrix with both white rooks/king on home squares (rights
        # should survive) but black's kingside rook moved away (that right
        # should not).
        matrix = [[None] * 8 for _ in range(8)]
        matrix[0][4] = "white-king"
        matrix[0][0] = "white-rook"
        matrix[0][7] = "white-rook"
        matrix[7][4] = "black-king"
        matrix[7][0] = "black-rook"
        # h8 left empty -- black's rook is not there anymore.

        resolver.resync(matrix, turn="white")
        self.assertTrue(resolver.board.has_kingside_castling_rights(chess.WHITE))
        self.assertTrue(resolver.board.has_queenside_castling_rights(chess.WHITE))
        self.assertTrue(resolver.board.has_queenside_castling_rights(chess.BLACK))
        self.assertFalse(resolver.board.has_kingside_castling_rights(chess.BLACK))
        self.assertEqual(resolver.board.turn, chess.WHITE)


if __name__ == "__main__":
    unittest.main()
