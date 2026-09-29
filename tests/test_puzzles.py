"""Tests for puzzles.py and web_ui.PuzzleController.

The controller is driven over a HeadlessLoop with a fake camera read bolted
on, so the whole puzzle cycle -- setup check, Black's move, a wrong answer
taken back, the right one, the rating change -- runs with no camera, no arm
and no Stockfish.
"""

import os
import random
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import chess  # noqa: E402

import make_puzzles  # noqa: E402
import puzzles  # noqa: E402
import web_ui  # noqa: E402
from headless_loop import HeadlessLoop  # noqa: E402
from move_resolver import matrix_from_board, standard_starting_matrix  # noqa: E402
from square_classifier import ALL_SQUARES, UNRESOLVED  # noqa: E402

# Lichess puzzle Mgguk: Black's Qe3 blunder, then White mates in 2.
MATE2 = puzzles.Puzzle(
    id="Mgguk",
    fen="1r5k/p3p2p/4Nbp1/8/8/5P1P/P1Qq2P1/2R4K b - - 0 30",
    moves=("d2e3", "c2c8", "b8c8", "c1c8"),
    rating=400,
    themes=("mateIn2", "short"),
    pieces=16,
)


def make(id, rating, themes=("fork",), pieces=20):
    return puzzles.Puzzle(id=id, fen=MATE2.fen, moves=MATE2.moves, rating=rating,
                          themes=tuple(themes), pieces=pieces)


def temp_path():
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.unlink(path)  # start with no file, as on a first run
    return Path(path)


class TestBank(unittest.TestCase):
    def setUp(self):
        self.bank = puzzles.PuzzleBank([
            make("a", 800, ("fork",), 10),
            make("b", 1200, ("pin",), 20),
            make("c", 1250, ("fork", "short"), 30),
            make("d", 2000, ("mateIn1",), 8),
        ])

    def test_a_range_is_a_hard_filter(self):
        for _ in range(20):
            self.assertIn(self.bank.pick(rating_range=(1100, 1300)).id, ("b", "c"))
        self.assertIsNone(self.bank.pick(rating_range=(3000, 3100)))

    def test_adaptive_picks_near_the_rating(self):
        for _ in range(20):
            self.assertIn(self.bank.pick(target=1220).id, ("b", "c"))

    def test_adaptive_widens_until_something_fits(self):
        self.assertEqual(self.bank.pick(target=1700, themes=["mateIn1"]).id, "d")

    def test_themes_match_any_ticked(self):
        ids = {self.bank.pick(themes=["fork"]).id for _ in range(40)}
        self.assertEqual(ids, {"a", "c"})

    def test_max_pieces(self):
        ids = {self.bank.pick(max_pieces=10).id for _ in range(40)}
        self.assertEqual(ids, {"a", "d"})

    def test_recent_puzzles_are_avoided_until_nothing_else_is_left(self):
        self.assertEqual(self.bank.pick(themes=["pin"], exclude=["a"]).id, "b")
        self.assertEqual(self.bank.pick(themes=["pin"], exclude=["b"]).id, "b")

    def test_nothing_matching_is_none(self):
        self.assertIsNone(self.bank.pick(themes=["skewer"]))

    def test_the_bundled_file_loads_and_is_white_to_solve(self):
        bank = puzzles.PuzzleBank.load(puzzles.DEFAULT_BANK)
        self.assertGreater(len(bank), 1000)
        for puzzle in bank.puzzles[::97]:
            board = puzzle.board()
            self.assertEqual(board.turn, chess.BLACK, puzzle.id)
            for move in puzzle.solution():
                self.assertIn(move, board.legal_moves, puzzle.id)
                board.push(move)


class TestProgress(unittest.TestCase):
    def setUp(self):
        self.path = temp_path()
        self.addCleanup(lambda: self.path.exists() and self.path.unlink())
        self.progress = puzzles.PuzzleProgress.load(self.path)

    def test_a_first_run_starts_at_the_default(self):
        self.assertEqual(self.progress.rating, puzzles.START_RATING)

    def test_solving_an_even_puzzle_gains_half_k(self):
        delta = self.progress.record(make("x", 1200), puzzles.SOLVED)
        self.assertEqual(delta, puzzles.ELO_K // 2)
        self.assertEqual(self.progress.streak, 1)

    def test_failing_loses_and_breaks_the_streak(self):
        self.progress.record(make("x", 1200), puzzles.SOLVED)
        delta = self.progress.record(make("y", 1200), puzzles.FAILED)
        self.assertLess(delta, 0)
        self.assertEqual(self.progress.streak, 0)
        self.assertEqual(self.progress.best_streak, 1)

    def test_an_easy_solve_still_counts_for_something(self):
        """Pure Elo rounds a 400 puzzle at 1200 to +0, which reads as broken."""
        self.assertEqual(self.progress.record(make("x", 400), puzzles.SOLVED), 1)
        self.assertEqual(self.progress.record(make("y", 2800), puzzles.FAILED), -1)

    def test_a_hint_is_worth_half(self):
        delta = self.progress.record(make("x", 1200), puzzles.HINTED)
        self.assertEqual(delta, puzzles.ELO_K // 4)

    def test_a_skip_changes_nothing_but_the_history(self):
        delta = self.progress.record(make("x", 1200), puzzles.SKIPPED)
        self.assertEqual(delta, 0)
        self.assertEqual(self.progress.recent_ids(), ["x"])

    def test_it_survives_a_restart(self):
        self.progress.record(make("x", 1500), puzzles.SOLVED)
        again = puzzles.PuzzleProgress.load(self.path)
        self.assertEqual(again.rating, self.progress.rating)
        self.assertEqual(again.recent_ids(), ["x"])

    def test_a_mangled_file_starts_fresh(self):
        self.path.write_text("{not json")
        self.assertEqual(puzzles.PuzzleProgress.load(self.path).rating, puzzles.START_RATING)


class TestRules(unittest.TestCase):
    def test_the_stored_move_is_correct(self):
        board = MATE2.board()
        board.push_uci("d2e3")
        self.assertTrue(puzzles.is_correct(board, chess.Move.from_uci("c2c8"),
                                           chess.Move.from_uci("c2c8")))

    def test_any_mate_is_correct(self):
        board = chess.Board("6k1/5ppp/8/8/8/8/5PPP/R3R1K1 w - - 0 1")
        expected = chess.Move.from_uci("a1a8")
        self.assertTrue(puzzles.is_correct(board, chess.Move.from_uci("e1e8"), expected))
        self.assertFalse(puzzles.is_correct(board, chess.Move.from_uci("e1e2"), expected))

    def test_setup_diff_lists_removals_swaps_then_placements(self):
        current = standard_starting_matrix()
        target = standard_starting_matrix()
        target[1][4] = None                 # e2 pawn goes
        target[3][4] = "white-pawn"         # ... to e4
        target[0][1] = "white-bishop"       # b1 knight becomes a bishop
        diff = puzzles.setup_diff(current, target)
        self.assertEqual(diff["remove"], ["e2"])
        self.assertEqual(diff["swap"], ["b1"])
        self.assertEqual(diff["add"], ["e4"])
        self.assertEqual(diff["steps"], [
            "remove the white pawn from e2",
            "b1: replace the white knight with a white bishop",
            "put a white pawn on e4",
        ])

    def test_matching_boards_need_nothing(self):
        m = standard_starting_matrix()
        self.assertEqual(puzzles.setup_diff(m, m)["steps"], [])

    def test_color_mismatches(self):
        target = MATE2.target_matrix()
        read = {sq: _color(target, sq) for sq in ALL_SQUARES}
        self.assertEqual(puzzles.color_mismatches(read, target), [])
        read[(4, 3)] = "white"      # e4 should be empty
        read[(7, 7)] = UNRESOLVED   # h8 king unreadable
        bad = puzzles.color_mismatches(read, target)
        self.assertEqual([b["square"] for b in bad], ["e4", "h8"])
        self.assertEqual(bad[1]["seen"], "unclear")


def _color(matrix, square):
    file_idx, rank_idx = square
    label = matrix[rank_idx][file_idx]
    return "empty" if label is None else label.split("-")[0]


class TestRun(unittest.TestCase):
    def test_the_full_line(self):
        run = puzzles.PuzzleRun(MATE2)
        board = MATE2.board()
        self.assertEqual(run.phase, puzzles.SETUP)
        run.begin()
        self.assertEqual(run.phase, puzzles.OPPONENT)
        board.push(run.next_move)
        run.opponent_done()
        self.assertEqual(run.phase, puzzles.SOLVING)
        self.assertTrue(run.player_moved(board, chess.Move.from_uci("c2c8")))
        board.push_uci("c2c8")
        self.assertEqual(run.phase, puzzles.OPPONENT)
        board.push(run.next_move)
        run.opponent_done()
        self.assertTrue(run.player_moved(board, chess.Move.from_uci("c1c8")))
        self.assertEqual(run.phase, puzzles.SOLVED_PHASE)
        self.assertEqual(run.result(), puzzles.SOLVED)

    def test_a_miss_then_a_find_still_fails(self):
        run = puzzles.PuzzleRun(MATE2)
        board = MATE2.board()
        run.begin()
        board.push(run.next_move)
        run.opponent_done()
        self.assertFalse(run.player_moved(board, chess.Move.from_uci("c2c3")))
        self.assertEqual(run.phase, puzzles.SOLVING)
        self.assertTrue(run.player_moved(board, chess.Move.from_uci("c2c8")))
        self.assertEqual(run.result(), puzzles.FAILED)

    def test_a_hint_names_the_piece_and_marks_the_run(self):
        run = puzzles.PuzzleRun(MATE2)
        run.begin()
        self.assertIsNone(run.hint())  # not the human's turn yet
        run.opponent_done()
        self.assertEqual(run.hint(), "c2")
        self.assertEqual(run.result(), puzzles.HINTED)

    def test_revealing_gives_the_rest_and_fails(self):
        run = puzzles.PuzzleRun(MATE2)
        run.begin()
        run.opponent_done()
        self.assertEqual(run.reveal(), ["c2c8", "b8c8", "c1c8"])
        self.assertTrue(run.finished)
        self.assertEqual(run.result(), puzzles.FAILED)


# --------------------------------------------------------------- controller

class CameraLoop(HeadlessLoop):
    """HeadlessLoop plus the one camera call puzzle setup needs. `physical`
    is what the fake camera sees; None means no frame yet."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.physical = None

    def read_board(self):
        if self.physical is None:
            return None
        return {sq: _color(self.physical, sq) for sq in ALL_SQUARES}


class TestPuzzleController(unittest.TestCase):
    def setUp(self):
        self.path = temp_path()
        self.addCleanup(lambda: self.path.exists() and self.path.unlink())
        self.progress = puzzles.PuzzleProgress.load(self.path)
        self.loop = CameraLoop()
        self.ctl = web_ui.PuzzleController(
            self.loop, puzzles.PuzzleBank([MATE2]), self.progress, rng=random.Random(0))
        self.ctl.close()  # driven by hand below

    def state(self):
        return self.ctl.state()

    def set_up_correctly(self):
        self.loop.physical = MATE2.target_matrix()
        return self.ctl.action("ready")

    def through_blacks_move(self):
        self.set_up_correctly()
        self.ctl._step()                  # arms Black's move
        self.loop.force_settle()          # ... which the human places (no arm)
        self.ctl._step()

    def white_plays(self, uci):
        self.loop.set_expected_move(chess.Move.from_uci(uci))
        self.loop.force_settle()
        self.ctl._step()

    def test_it_opens_on_the_setup_with_a_to_do_list(self):
        state = self.state()
        self.assertEqual(state["phase"], puzzles.SETUP)
        self.assertEqual(state["target"], MATE2.target_matrix())
        self.assertIn("h8: replace the black rook with a black king", state["diff"]["steps"])
        self.assertIn("put a white knight on e6", state["diff"]["steps"])

    def test_tracking_is_held_during_setup_and_released_on_close(self):
        loop = CameraLoop()
        ctl = web_ui.PuzzleController(loop, puzzles.PuzzleBank([MATE2]), self.progress)
        self.assertTrue(loop.is_paused)
        ctl.close()
        self.assertFalse(loop.is_paused)

    def test_a_wrong_setup_is_refused_and_marked(self):
        self.loop.physical = standard_starting_matrix()
        ok, error = self.ctl.action("ready")
        self.assertFalse(ok)
        self.assertIn("don't match", error)
        self.assertEqual(self.state()["phase"], puzzles.SETUP)
        self.assertTrue(self.state()["mismatches"])

    def test_no_camera_frame_is_refused(self):
        self.assertFalse(self.ctl.action("ready")[0])

    def test_a_confirmed_setup_adopts_the_exact_position(self):
        self.assertEqual(self.set_up_correctly(), (True, {}))
        self.assertEqual(self.loop.board_copy.fen(), MATE2.fen)
        self.assertEqual(self.state()["phase"], puzzles.OPPONENT)
        self.assertFalse(self.loop.is_paused)

    def test_blacks_move_is_armed_and_described(self):
        self.set_up_correctly()
        self.ctl._step()
        self.assertEqual(self.loop.expected_move, chess.Move.from_uci("d2e3"))
        self.assertEqual(self.state()["instruction"], "d2 → e3")

    def test_solving_it(self):
        self.through_blacks_move()
        self.assertEqual(self.state()["phase"], puzzles.SOLVING)
        self.white_plays("c2c8")
        self.ctl._step()                  # arms Black's recapture
        self.loop.force_settle()
        self.ctl._step()
        self.white_plays("c1c8")
        state = self.state()
        self.assertEqual(state["phase"], puzzles.SOLVED_PHASE)
        self.assertEqual(state["result"], puzzles.SOLVED)
        self.assertGreater(state["rating_delta"], 0)
        self.assertEqual(self.progress.history[-1]["result"], puzzles.SOLVED)

    def test_a_wrong_move_is_taken_back_and_retried(self):
        self.through_blacks_move()
        before = self.loop.board_copy.fen()
        self.white_plays("c2c3")
        self.assertEqual(self.loop.board_copy.fen(), before)
        self.assertIn("put the queen back c3 → c2", self.state()["wrong_note"])
        self.assertEqual(self.state()["phase"], puzzles.SOLVING)
        self.white_plays("c2c8")
        self.assertIsNone(self.state()["wrong_note"])
        self.assertEqual(self.state()["phase"], puzzles.OPPONENT)

    def test_hint_then_solution(self):
        self.through_blacks_move()
        self.assertEqual(self.ctl.action("hint"), (True, {}))
        self.assertEqual(self.state()["hint_square"], "c2")
        self.assertEqual(self.ctl.action("solution"), (True, {}))
        state = self.state()
        self.assertEqual(state["solution"], ["Qc8+", "Rxc8", "Rxc8#"])
        self.assertEqual(state["result"], puzzles.FAILED)

    def test_the_result_is_recorded_once(self):
        self.through_blacks_move()
        self.ctl.action("solution")
        self.ctl.action("next")
        self.assertEqual(len(self.progress.history), 1)

    def test_next_sets_up_again_against_the_current_board(self):
        self.through_blacks_move()
        self.ctl.action("skip")
        state = self.state()
        self.assertEqual(state["phase"], puzzles.SETUP)
        self.assertEqual(self.progress.history[-1]["result"], puzzles.SKIPPED)
        # The board now holds the puzzle after Black's move, so only that
        # move needs undoing.
        self.assertEqual(sorted(state["diff"]["steps"]), sorted([
            "remove the black queen from e3", "put a black queen on d2"]))

    def test_reset_goes_back_to_setting_up_the_same_puzzle(self):
        self.through_blacks_move()
        self.ctl.configure(enabled=False)
        self.assertEqual(self.state()["phase"], puzzles.SETUP)
        self.assertEqual(self.state()["puzzle"]["id"], MATE2.id)

    def test_no_match_says_to_widen_the_filters(self):
        ctl = web_ui.PuzzleController(CameraLoop(), puzzles.PuzzleBank([MATE2]),
                                      self.progress, filters={"themes": ["skewer"]})
        ctl.close()
        self.assertIsNone(ctl.state()["phase"])
        self.assertIn("filters", ctl.state()["message"])


class TestFilters(unittest.TestCase):
    def test_menu_settings_are_cleaned(self):
        filters = web_ui._puzzle_filters({
            "puzzle_rating_mode": "range", "puzzle_min": "900", "puzzle_max": 1300,
            "puzzle_max_pieces": 0, "puzzle_themes": ["fork", "nonsense"],
        })
        self.assertEqual(filters, {"rating_mode": "range", "rating_min": 900,
                                   "rating_max": 1300, "themes": ["fork"]})

    def test_anything_else_is_adaptive(self):
        self.assertEqual(web_ui._puzzle_filters({})["rating_mode"], "adaptive")


class TestMakePuzzles(unittest.TestCase):
    def row(self, id, fen, rating=1000, moves="d2e3 c2c8", pop=95, plays=1000, dev=75):
        return {"PuzzleId": id, "FEN": fen, "Moves": moves, "Rating": str(rating),
                "RatingDeviation": str(dev), "Popularity": str(pop), "NbPlays": str(plays),
                "Themes": "fork"}

    def test_only_white_to_solve_and_well_tested(self):
        rows = [
            self.row("keep", MATE2.fen),
            self.row("white-to-move", MATE2.fen.replace(" b ", " w ")),
            self.row("unpopular", MATE2.fen, pop=10),
            self.row("unplayed", MATE2.fen, plays=3),
            self.row("uncertain", MATE2.fen, dev=300),
            self.row("too-easy", MATE2.fen, rating=100),
        ]
        picked = make_puzzles.select(rows, 10, 85, 500, 90, seed=1)
        self.assertEqual([p["id"] for p in picked], ["keep"])
        self.assertEqual(picked[0]["pieces"], 16)

    def test_each_band_is_capped(self):
        rows = [self.row(f"p{i}", MATE2.fen, rating=1010) for i in range(50)]
        self.assertEqual(len(make_puzzles.select(rows, 7, 85, 500, 90, seed=1)), 7)


if __name__ == "__main__":
    unittest.main()
