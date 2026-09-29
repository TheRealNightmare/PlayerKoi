"""Tests for coach.py -- grading, hints and the rule-based explanations -- and
for the guided game's EngineController, which holds Black's reply while a
Mistake or Blunder waits on take back / continue.

No Stockfish: the engine is a stub that answers analyse() from a table keyed
by FEN, so every grade here comes from a known evaluation.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import chess  # noqa: E402

import coach  # noqa: E402
import web_ui  # noqa: E402
from headless_loop import HeadlessLoop  # noqa: E402


def ev(cp=None, mate=None, pv=()):
    return {"score_cp": cp, "mate": mate, "pv": [chess.Move.from_uci(m) for m in pv]}


class TestWinPercent(unittest.TestCase):
    def test_level_is_even(self):
        self.assertAlmostEqual(coach.win_percent(ev(0)), 50.0)

    def test_mate_is_certain_either_way(self):
        self.assertEqual(coach.win_percent(ev(mate=3)), 100.0)
        self.assertEqual(coach.win_percent(ev(mate=-2)), 0.0)

    def test_it_saturates(self):
        """300cp matters at +0, barely at +900 -- the reason for using it."""
        near_even = coach.win_percent(ev(300)) - coach.win_percent(ev(0))
        already_won = coach.win_percent(ev(1200)) - coach.win_percent(ev(900))
        self.assertGreater(near_even, 5 * already_won)


class TestClassify(unittest.TestCase):
    def test_the_engines_own_move_is_best(self):
        self.assertEqual(coach.classify(ev(30), ev(-900), chess.WHITE, played_is_best=True), coach.BEST)

    def test_a_small_drop_is_good(self):
        self.assertEqual(coach.classify(ev(30), ev(10), chess.WHITE), coach.GOOD)

    def test_hanging_the_queen_is_a_blunder(self):
        self.assertEqual(coach.classify(ev(30), ev(-900), chess.WHITE), coach.BLUNDER)

    def test_the_labels_climb_with_the_drop(self):
        labels = [coach.classify(ev(0), ev(-cp), chess.WHITE) for cp in (20, 70, 140, 400)]
        self.assertEqual(labels, [coach.GOOD, coach.INACCURACY, coach.MISTAKE, coach.BLUNDER])

    def test_black_is_judged_from_its_own_side(self):
        # White-POV numbers: Black was better (-50) and is now lost (+500).
        self.assertEqual(coach.classify(ev(-50), ev(500), chess.BLACK), coach.BLUNDER)
        self.assertEqual(coach.classify(ev(-50), ev(-60), chess.BLACK), coach.GOOD)

    def test_throwing_away_a_forced_mate_is_a_blunder(self):
        self.assertEqual(coach.classify(ev(mate=2), ev(0), chess.WHITE), coach.BLUNDER)


class TestExplain(unittest.TestCase):
    def explain(self, fen, uci, after=None):
        return coach.explain(chess.Board(fen), chess.Move.from_uci(uci), after)

    def test_checkmate_says_only_that(self):
        fen = "r1bqkb1r/pppp1ppp/2n2n2/4p2Q/2B1P3/8/PPPP1PPP/RNB1K1NR w KQkq - 4 4"
        self.assertEqual(self.explain(fen, "h5f7"), "checkmate")

    def test_a_knight_fork_on_king_and_rook(self):
        text = self.explain("r3k3/8/8/1N6/8/8/8/4K3 w - - 0 1", "b5c7")
        self.assertIn("gives check", text)
        self.assertIn("forks the king and the rook on a8", text)

    def test_a_hung_queen_is_called_out(self):
        text = self.explain("7k/8/8/5p2/8/8/8/3QK3 w - - 0 1", "d1g4")
        self.assertIn("attacks the pawn on f5", text)
        self.assertIn("but leaves the queen on g4 hanging", text)

    def test_pressure_that_was_already_there_is_not_news(self):
        # The queen already hits d5 down the open file from d1.
        text = self.explain("7k/8/8/3p4/8/8/8/3QK3 w - - 0 1", "d1d2")
        self.assertNotIn("attacks the pawn", text)

    def test_a_hang_on_its_own_reads_as_a_sentence(self):
        text = self.explain("7k/8/8/5p2/8/8/8/4R2K w - - 0 1", "e1e4")
        self.assertEqual(text, "leaves the rook on e4 hanging")

    def test_castling(self):
        text = self.explain("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "e1g1")
        self.assertIn("castles kingside", text)

    def test_promotion(self):
        self.assertIn("promotes to a queen", self.explain("8/P6k/8/8/8/8/8/K7 w - - 0 1", "a7a8q"))

    def test_development(self):
        self.assertIn("develops the knight", self.explain(chess.STARTING_FEN, "g1f3"))

    def test_a_capture_names_the_victim(self):
        fen = "rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"
        self.assertIn("captures the black pawn", self.explain(fen, "e4d5"))

    def test_a_mate_threat_comes_from_the_evaluation(self):
        text = self.explain(chess.STARTING_FEN, "e2e4", after=ev(mate=4))
        self.assertIn("sets up mate in 4", text)

    def test_a_centre_pawn(self):
        self.assertIn("claims the centre", self.explain(chess.STARTING_FEN, "e2e4"))

    def test_something_is_always_said(self):
        self.assertTrue(self.explain(chess.STARTING_FEN, "a2a3"))


class TestHintAndReview(unittest.TestCase):
    def test_the_hint_says_what_and_where(self):
        hint = coach.hint(chess.Board(), ev(30, pv=["e2e4", "e7e5"]))
        self.assertEqual(hint["uci"], "e2e4")
        self.assertEqual(hint["san"], "e4")
        self.assertEqual(hint["headline"], "e2 → e4")

    def test_no_line_no_hint(self):
        self.assertIsNone(coach.hint(chess.Board(), None))
        self.assertIsNone(coach.hint(chess.Board(), ev(0)))

    def test_a_blunder_review_names_the_better_line(self):
        board = chess.Board()
        review = coach.review(board, chess.Move.from_uci("f2f3"),
                              ev(30, pv=["e2e4", "e7e5", "g1f3"]), ev(-400))
        self.assertEqual(review["label"], coach.BLUNDER)
        self.assertEqual(review["better_san"], "e4")
        self.assertEqual(review["better_line"], ["e4", "e5", "Nf3"])

    def test_the_best_move_has_nothing_better(self):
        review = coach.review(chess.Board(), chess.Move.from_uci("e2e4"),
                              ev(30, pv=["e2e4"]), ev(25))
        self.assertEqual(review["label"], coach.BEST)
        self.assertIsNone(review["better_san"])

    def test_a_mating_move_is_best_without_an_evaluation_after(self):
        board = chess.Board("r1bqkb1r/pppp1ppp/2n2n2/4p2Q/2B1P3/8/PPPP1PPP/RNB1K1NR w KQkq - 4 4")
        review = coach.review(board, chess.Move.from_uci("h5f7"), None, None)
        self.assertEqual(review["label"], coach.BEST)


class TestTakeback(unittest.TestCase):
    def instruction(self, fen, uci):
        return coach.takeback_instruction(chess.Board(fen), chess.Move.from_uci(uci))

    def test_a_quiet_move_goes_back(self):
        self.assertEqual(self.instruction(chess.STARTING_FEN, "e2e4"), "put the pawn back e4 → e2")

    def test_a_capture_returns_the_victim(self):
        fen = "rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"
        text = self.instruction(fen, "e4d5")
        self.assertIn("put the pawn back d5 → e4", text)
        self.assertIn("return the black pawn to d5", text)

    def test_castling_brings_the_rook_home_too(self):
        text = self.instruction("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "e1g1")
        self.assertIn("and the rook f1 → h1", text)

    def test_en_passant_restores_the_pawn_beside(self):
        fen = "rnbqkbnr/ppp1p1pp/8/3pPp2/8/8/PPPP1PPP/RNBQKBNR w KQkq f6 0 3"
        self.assertIn("return the black pawn to f5", self.instruction(fen, "e5f6"))

    def test_promotion_swaps_the_pawn_back(self):
        text = self.instruction("8/P6k/8/8/8/8/8/K7 w - - 0 1", "a7a8q")
        self.assertIn("take the queen off a8 and put the pawn back on a7", text)


# --------------------------------------------------------------- controller

class StubEngine:
    """analyse() from a FEN table; best_move() records that Black was asked."""

    available = True
    error = None
    skill = 3

    def __init__(self, evals, default=None, black_move="e7e5"):
        self.evals = evals
        self.default = default or ev(0)
        self.black_move = chess.Move.from_uci(black_move)
        self.black_asked = 0

    def set_skill(self, n):
        self.skill = n

    def analyse(self, board, think_s):
        return self.evals.get(board.fen(), self.default)

    def best_move(self, board, think_s, root_moves=None):
        self.black_asked += 1
        return self.black_move if self.black_move in board.legal_moves else None

    def close(self):
        pass


def play(loop, uci):
    """A move the tracker resolved -- HeadlessLoop commits the expected one."""
    loop.set_expected_move(chess.Move.from_uci(uci))
    assert loop.force_settle()


class TestGuidedController(unittest.TestCase):
    def setUp(self):
        start = chess.Board()
        after_blunder = chess.Board()
        after_blunder.push_uci("f2f3")
        after_good = chess.Board()
        after_good.push_uci("e2e4")
        self.engine = StubEngine({
            start.fen(): ev(30, pv=["e2e4", "e7e5"]),
            after_blunder.fen(): ev(-400, pv=["e7e5"]),
            after_good.fen(): ev(30, pv=["e7e5"]),
        })
        self.loop = HeadlessLoop()
        self.ctl = web_ui.EngineController(self.loop, self.engine, think_s=0.01, coach_think_s=0.01)
        # Stop its thread and drive it by hand, so nothing here is timing.
        self.ctl.close()
        self.ctl._enabled = True

    def coach_state(self):
        return self.ctl.state()["coach"]

    def test_the_hint_is_the_engines_best_move(self):
        self.ctl._maybe_move()
        state = self.coach_state()
        self.assertEqual(state["hint"]["uci"], "e2e4")
        self.assertEqual(state["eval"], {"cp": 30, "mate": None})

    def test_a_good_move_is_answered_at_once(self):
        self.ctl._maybe_move()
        play(self.loop, "e2e4")
        self.ctl._maybe_move()
        self.assertEqual(self.coach_state()["review"]["label"], coach.BEST)
        self.assertFalse(self.coach_state()["awaiting_decision"])
        self.assertEqual(self.engine.black_asked, 1)
        self.assertEqual(self.loop.expected_move, chess.Move.from_uci("e7e5"))

    def test_a_blunder_holds_black(self):
        self.ctl._maybe_move()
        play(self.loop, "f2f3")
        self.ctl._maybe_move()
        self.ctl._maybe_move()
        state = self.coach_state()
        self.assertEqual(state["review"]["label"], coach.BLUNDER)
        self.assertEqual(state["review"]["better_san"], "e4")
        self.assertTrue(state["awaiting_decision"])
        self.assertEqual(self.engine.black_asked, 0)

    def test_continue_lets_black_reply(self):
        self.ctl._maybe_move()
        play(self.loop, "f2f3")
        self.ctl._maybe_move()
        self.assertEqual(self.ctl.coach_decision("continue"), (True, None))
        self.ctl._maybe_move()
        self.assertFalse(self.coach_state()["awaiting_decision"])
        self.assertEqual(self.engine.black_asked, 1)

    def test_take_back_restores_the_position_and_says_how(self):
        self.ctl._maybe_move()
        play(self.loop, "f2f3")
        self.ctl._maybe_move()
        self.assertEqual(self.ctl.coach_decision("takeback"), (True, None))
        self.assertEqual(self.loop.board_copy.fen(), chess.Board().fen())
        self.assertEqual(self.coach_state()["takeback"], "put the pawn back f3 → f2")
        self.ctl._maybe_move()
        self.assertEqual(self.coach_state()["hint"]["uci"], "e2e4")
        self.assertEqual(self.engine.black_asked, 0)

    def test_the_same_blunder_again_is_judged_again(self):
        self.ctl._maybe_move()
        play(self.loop, "f2f3")
        self.ctl._maybe_move()
        self.ctl.coach_decision("takeback")
        self.ctl._maybe_move()
        play(self.loop, "f2f3")
        self.ctl._maybe_move()
        self.assertTrue(self.coach_state()["awaiting_decision"])
        self.assertEqual(self.engine.black_asked, 0)

    def test_undo_elsewhere_drops_the_question(self):
        self.ctl._maybe_move()
        play(self.loop, "f2f3")
        self.ctl._maybe_move()
        self.loop.undo_last_move()  # the UI's own Undo button
        self.ctl._maybe_move()
        self.assertFalse(self.coach_state()["awaiting_decision"])

    def test_a_decision_with_nothing_waiting_is_refused(self):
        ok, error = self.ctl.coach_decision("continue")
        self.assertFalse(ok)
        self.assertIn("nothing", error)

    def test_black_is_explained_too(self):
        self.ctl._maybe_move()
        play(self.loop, "e2e4")
        self.ctl._maybe_move()          # grades e4, arms Black's e5
        self.loop.force_settle()        # Black's move is placed
        self.ctl._maybe_move()
        self.assertTrue(self.coach_state()["opponent_note"].startswith("Black played e5"))

    def test_normal_mode_has_no_coach(self):
        plain = web_ui.EngineController(HeadlessLoop(), self.engine, think_s=0.01)
        plain.close()
        self.assertIsNone(plain.state()["coach"])
        self.assertFalse(plain.coach_decision("continue")[0])


if __name__ == "__main__":
    unittest.main()
