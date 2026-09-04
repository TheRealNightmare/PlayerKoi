"""Tests for engine.describe_move -- the physical instructions shown at
the board. These are what stand between the engine's move and the user
putting the wrong thing down, so the special moves (where the from/to
alone is NOT enough to play the move) get explicit coverage.

Pure python-chess; no Stockfish needed.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import chess  # noqa: E402

import engine as eng  # noqa: E402


class TestDescribeMove(unittest.TestCase):
    def test_quiet_move_is_just_from_to(self):
        board = chess.Board()
        headline, extra = eng.describe_move(board, chess.Move.from_uci("e2e4"))
        self.assertEqual(headline, "e2 → e4")
        self.assertIsNone(extra)

    def test_capture_says_what_to_remove_first(self):
        board = chess.Board()
        board.push_san("e4")
        board.push_san("d5")
        headline, extra = eng.describe_move(board, chess.Move.from_uci("e4d5"))
        self.assertEqual(headline, "e4 → d5")
        self.assertIn("remove", extra)
        self.assertIn("black pawn", extra)
        self.assertIn("d5", extra)

    def test_castling_names_the_rook_move(self):
        # Verified position: e8g8 is legal here.
        board = chess.Board("r1bqk2r/pppp1ppp/2n2n2/2b1p3/2B1P3/2N2N2/PPPP1PPP/R1BQK2R b KQkq - 5 5")
        headline, extra = eng.describe_move(board, chess.Move.from_uci("e8g8"))
        self.assertEqual(headline, "e8 → g8")
        self.assertIn("kingside", extra)
        self.assertIn("h8", extra)
        self.assertIn("f8", extra)

    def test_queenside_castling_names_the_other_rook(self):
        board = chess.Board("r3kbnr/pppqpppp/2n5/3p4/3P4/2N2N2/PPPQPPPP/R1B1KB1R b KQkq - 6 6")
        headline, extra = eng.describe_move(board, chess.Move.from_uci("e8c8"))
        self.assertEqual(headline, "e8 → c8")
        self.assertIn("queenside", extra)
        self.assertIn("a8", extra)
        self.assertIn("d8", extra)

    def test_en_passant_names_the_pawn_to_remove(self):
        # White pawn e5, black just played d7-d5; the captured pawn ends up
        # on d5, NOT on the destination d6 -- the whole reason this needs
        # spelling out.
        board = chess.Board("rnbqkbnr/ppp1pppp/8/3pP3/8/8/PPPP1PPP/RNBQKBNR w KQkq d6 0 3")
        headline, extra = eng.describe_move(board, chess.Move.from_uci("e5d6"))
        self.assertEqual(headline, "e5 → d6")
        self.assertIn("en passant", extra)
        self.assertIn("d5", extra)
        self.assertNotIn("d6", extra)

    def test_promotion_says_which_piece_to_put_down(self):
        board = chess.Board("1nbqkbnr/P1pppppp/8/8/8/8/1PPPPPPP/RNBQKBNR w KQk - 0 5")
        headline, extra = eng.describe_move(board, chess.Move.from_uci("a7a8q"))
        self.assertEqual(headline, "a7 → a8")
        self.assertIn("promotion", extra)
        self.assertIn("white queen", extra)

    def test_underpromotion_names_the_actual_piece(self):
        board = chess.Board("1nbqkbnr/P1pppppp/8/8/8/8/1PPPPPPP/RNBQKBNR w KQk - 0 5")
        _headline, extra = eng.describe_move(board, chess.Move.from_uci("a7a8n"))
        self.assertIn("white knight", extra)


class TestEngineUnavailable(unittest.TestCase):
    def test_missing_binary_degrades_instead_of_raising(self):
        # The web UI must still start and track when Stockfish isn't
        # installed, so construction can't blow up.
        e = eng.ChessEngine(command="definitely-not-a-real-engine-binary")
        self.assertFalse(e.available)
        self.assertIn("not available", e.error)
        self.assertIsNone(e.best_move(chess.Board()))
        e.close()  # must be safe even with no subprocess


if __name__ == "__main__":
    unittest.main()
