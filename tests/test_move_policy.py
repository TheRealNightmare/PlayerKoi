"""Tests for move_policy.choose_move -- the beginner-ish, weave-averse
move chooser used by --ai-vs-ai.

No engine binary is involved: ChessEngine is replaced by a stub that records
what it was asked and answers from a script. What is being tested is the
policy's arithmetic -- which moves it allows, which it prefers -- not
Stockfish.

The property with real teeth is the knight/castling exclusion, because it is
there for the hardware: those are the only moves that make the arm weave
along the gridlines at reduced magnet duty, which is where pieces get
dropped.
"""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import chess  # noqa: E402

import move_policy  # noqa: E402


class StubEngine:
    """Answers like ChessEngine but from a script, and remembers the
    root_moves it was handed so the restriction can be asserted."""

    def __init__(self, shortlist=None):
        self._shortlist = shortlist
        self.analyse_roots = None
        self.play_roots = None

    def top_moves(self, board, think_s, count=5, root_moves=None):
        self.analyse_roots = root_moves
        if self._shortlist is None:
            # Stand in for a build with no MultiPV: nothing comes back.
            return []
        return [m for m in self._shortlist if root_moves is None or m in root_moves][:count]

    def best_move(self, board, think_s, root_moves=None):
        self.play_roots = root_moves
        return root_moves[0] if root_moves else None


def uci(*names):
    return [chess.Move.from_uci(n) for n in names]


class TestSparingTheArm(unittest.TestCase):
    def test_knights_are_excluded_from_the_start(self):
        board = chess.Board()
        allowed = move_policy.allowed_moves(board)
        self.assertNotIn(chess.Move.from_uci("b1c3"), allowed)
        self.assertNotIn(chess.Move.from_uci("g1f3"), allowed)
        self.assertIn(chess.Move.from_uci("e2e4"), allowed)

    def test_castling_is_excluded_too(self):
        """It sends a second KNIGHT command for the rook, so it weaves."""
        board = chess.Board("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
        allowed = move_policy.allowed_moves(board)
        self.assertNotIn(chess.Move.from_uci("e1g1"), allowed)
        self.assertNotIn(chess.Move.from_uci("e1c1"), allowed)
        self.assertIn(chess.Move.from_uci("e1f1"), allowed)  # plain king move is fine

    def test_a_knight_is_played_when_nothing_else_is_legal(self):
        """'Only when forced' is exactly this fallback -- the arm weaves
        rather than the game stalling.

        White's king is boxed in by its own knight and the two black pawns,
        so the knight's three moves are the entire legal move list.
        """
        board = chess.Board("7k/8/8/8/8/6pp/8/6NK w - - 0 1")
        self.assertEqual(len(list(board.legal_moves)), 3)
        allowed = move_policy.allowed_moves(board)
        self.assertEqual(allowed, list(board.legal_moves))

        engine = StubEngine(shortlist=uci("g1f3"))
        chosen = move_policy.choose_move(engine, board, 0.1)
        self.assertIn(chosen, board.legal_moves)

    def test_a_forced_move_skips_the_search_entirely(self):
        """One legal move means nothing to choose between -- don't spend two
        engine searches confirming it."""
        board = chess.Board("7k/8/8/8/8/8/6q1/7K w - - 0 1")
        self.assertEqual(len(list(board.legal_moves)), 1)
        only = move_policy.allowed_moves(board)

        engine = StubEngine(shortlist=only)
        self.assertEqual(move_policy.choose_move(engine, board, 0.1), only[0])
        self.assertIsNone(engine.analyse_roots)  # never asked
        self.assertIsNone(engine.play_roots)

    def test_the_shortlist_is_asked_for_within_the_allowed_set(self):
        board = chess.Board()
        engine = StubEngine(shortlist=uci("e2e4", "d2d4"))
        move_policy.choose_move(engine, board, 0.1)
        self.assertEqual(engine.analyse_roots, move_policy.allowed_moves(board))
        for m in engine.analyse_roots:
            self.assertNotEqual(board.piece_at(m.from_square).piece_type, chess.KNIGHT)


class TestPreferringPawns(unittest.TestCase):
    def test_a_pawn_in_the_shortlist_wins(self):
        board = chess.Board("rnbqkbnr/pppppppp/8/8/8/5N2/PPPPPPPP/RNBQKB1R w KQkq - 0 1")
        engine = StubEngine(shortlist=uci("h1g1", "e2e4", "d2d4"))
        move_policy.choose_move(engine, board, 0.1)
        # The rook move is dropped; only the pawn moves survive to play().
        self.assertEqual(engine.play_roots, uci("e2e4", "d2d4"))

    def test_without_a_pawn_the_whole_shortlist_survives(self):
        """The preference is soft. If the engine's good moves are all piece
        moves, it plays a piece move rather than digging up a bad pawn push."""
        board = chess.Board("4k3/8/8/8/8/8/8/R3K2R w KQ - 0 1")
        shortlist = uci("a1a8", "h1h8")
        engine = StubEngine(shortlist=shortlist)
        move_policy.choose_move(engine, board, 0.1)
        self.assertEqual(engine.play_roots, shortlist)

    def test_the_final_choice_comes_from_play_not_analyse(self):
        """play() honours Skill Level and analyse() doesn't, which is the
        whole reason there are two calls."""
        board = chess.Board()
        engine = StubEngine(shortlist=uci("e2e4", "d2d4"))
        chosen = move_policy.choose_move(engine, board, 0.1)
        self.assertIsNotNone(engine.play_roots)
        self.assertIn(chosen, engine.play_roots)


class TestDegradingGracefully(unittest.TestCase):
    def test_no_multipv_still_keeps_the_knight_filter(self):
        """An engine that can't do MultiPV loses the pawn preference, but
        must not lose the hardware protection."""
        board = chess.Board()
        engine = StubEngine(shortlist=None)  # top_moves returns []
        chosen = move_policy.choose_move(engine, board, 0.1)
        self.assertEqual(engine.play_roots, move_policy.allowed_moves(board))
        self.assertNotEqual(board.piece_at(chosen.from_square).piece_type, chess.KNIGHT)

    def test_a_move_outside_the_narrowed_set_is_not_passed_on(self):
        """Some builds ignore root_moves. The arm must never be handed a
        move the policy excluded."""
        board = chess.Board()

        class IgnoresRootMoves(StubEngine):
            def best_move(self, board, think_s, root_moves=None):
                self.play_roots = root_moves
                return chess.Move.from_uci("b1c3")  # a knight, excluded

        engine = IgnoresRootMoves(shortlist=uci("e2e4", "d2d4"))
        chosen = move_policy.choose_move(engine, board, 0.1)
        self.assertIn(chosen, engine.play_roots)
        self.assertNotEqual(chosen, chess.Move.from_uci("b1c3"))

    def test_a_finished_game_returns_none(self):
        """Same contract as ChessEngine.best_move, so the two are
        interchangeable as a policy."""
        board = chess.Board("7k/5Q2/6K1/8/8/8/8/8 b - - 0 1")
        self.assertTrue(board.is_game_over())
        self.assertIsNone(move_policy.choose_move(StubEngine(), board, 0.1))


class TestOverAWholeGame(unittest.TestCase):
    def test_the_policy_always_returns_a_legal_move(self):
        board = chess.Board()
        random.seed(5)
        for _ in range(120):
            if board.is_game_over():
                break
            legal = list(board.legal_moves)
            engine = StubEngine(shortlist=random.sample(legal, min(4, len(legal))))
            move = move_policy.choose_move(engine, board, 0.0)
            self.assertIn(move, board.legal_moves)
            board.push(move)

    def test_knights_and_castling_stay_rare(self):
        """The point of the whole exercise: far fewer KNIGHT weaves than
        unrestricted play would send."""
        board = chess.Board()
        random.seed(6)
        weaves = 0
        plies = 0
        while plies < 120 and not board.is_game_over():
            legal = list(board.legal_moves)
            engine = StubEngine(shortlist=random.sample(legal, min(4, len(legal))))
            move = move_policy.choose_move(engine, board, 0.0)
            if move_policy._is_weave(board, move):
                weaves += 1
            board.push(move)
            plies += 1
        self.assertLess(weaves, plies * 0.05, f"{weaves} weaves in {plies} plies")


if __name__ == "__main__":
    unittest.main()
