"""Player Koi's web UI -- the menu, and both ways of playing behind it.

One launch serves a mode menu; src/session.py owns which mode is running and
what it takes to swap one for another. The two are very different stacks:

    Play the engine   camera + classifier + tracking_loop: you move White by
                      hand, vision reads the board, the arm answers for Black.
                      The board diagram is driven by tracking_loop's
                      event-gated occupancy-read/delta/legal-move pipeline
                      rather than a fixed-interval detection loop -- the feed
                      updates continuously and cheaply, but the board only
                      re-evaluates once something has settled.

    AI vs AI          headless_loop: no camera at all. The engine plays both
                      sides and the arm places every move, so the position is
                      known rather than observed.

    Guided game       Play the engine, plus the coach (src/coach.py): White's
                      best move drawn on the board every turn, each of your
                      moves graded, Black's explained, and the arm held back
                      after a Mistake or Blunder so it can be taken back.

    Puzzles           The same camera stack, driven by PuzzleController over
                      src/puzzles.py: show a layout, check it with the camera,
                      have Black's moves from the stored line placed, judge
                      White's.

Moves are real algebraic notation (SAN) via python-chess, not a physical
before/after description. There is no automatic rescan in this design -- when
a settle can't be resolved with confidence the UI flags it and offers a manual
"Fix board" correction instead (POST /board/correct).

    python3 src/web_ui.py --robot auto         # http://<this-pi>:8000/
    python3 src/web_ui.py --ai-vs-ai --robot auto    # skip the menu

The page itself is src/ui/ -- real .html/.css/.js files, served by
read_ui_asset() rather than held in a string literal here. src/ui/neoretro.css
is a hand-written reimplementation of the neo-retro design system
(https://neo-retro.zurat.dev) in the gruvbox-light theme; the real thing is a
Svelte component library and this rig has no build step.

Nothing is required to reach the menu. "Play the engine" additionally needs
config/calibration.json (run calibrate.py) and a trained, NCNN-exported
empty/white/black classifier (see src/collect_square_crops.py and
training/train_classifier.py); the menu greys it out and says so when they are
missing, rather than the process refusing to start.
"""

import argparse
import json
import time
from collections import deque
from pathlib import Path
from threading import Event, Lock, Thread

import chess
import cv2

import admin_moves
import coach
from board_state import load_calibration, matrix_to_fen_placement
from engine import DEFAULT_SKILL, DEFAULT_THINK_S, ChessEngine, describe_move
from headless_loop import HeadlessLoop, NullStream
import move_policy
import puzzles
import rig
import rig_config
from robot import GantryError, RobotController, open_gantry
from robot_moves import DEFAULT_TOPPLE_DELAY_S
from session import AI_VS_AI, MENU, NORMAL, ModeError, Session
from square_classifier import DEFAULT_MIN_CONF

# picamera2 (capture) and the ncnn classifier loader are imported inside
# main()'s tracking path rather than here, so --ai-vs-ai runs off the Pi and
# without a trained model -- it needs neither.

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CALIBRATION = REPO_ROOT / "config" / "calibration.json"
DEFAULT_CLASSIFIER = REPO_ROOT / "models" / "square_classifier_ncnn_model"
DEFAULT_HARVEST = REPO_ROOT / "training" / "datasets" / "harvested"

_VALID_LABELS = {
    f"{color}-{piece}"
    for color in ("white", "black")
    for piece in ("king", "queen", "rook", "bishop", "knight", "pawn")
}

UI_DIR = Path(__file__).resolve().parent / "ui"

# Served content types, which is also the allowlist: a request for anything
# not ending in one of these extensions is refused outright.
_UI_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
}


def read_ui_asset(name):
    """Bytes of one file from src/ui, or None if it isn't a servable asset.

    The page lives on disk rather than in a string literal here, because
    writing JS through a Python string means every backslash escape in a JS
    string has to be doubled -- and getting that wrong produces a real
    newline inside a JS literal, which is a syntax error the Python side is
    perfectly happy to emit.

    resolve() before the containment check, so ".." and symlinks are both
    normalised away before it is made.
    """
    path = (UI_DIR / name).resolve()
    if path.suffix not in _UI_TYPES:
        return None, None
    if not path.is_relative_to(UI_DIR) or not path.is_file():
        return None, None
    return path.read_bytes(), _UI_TYPES[path.suffix]


class BoardBuffer:
    """Holds the latest board matrix, move text, flagged status/reason, and
    JPEG frame for the HTTP handler to read -- one lock guards the board
    fields since tracking_loop's on_update sets them together; the frame is
    set separately and far more often (every capture tick, for a live
    feed)."""

    def __init__(self):
        self._lock = Lock()
        self._matrix = None
        self._updated_at = 0.0
        self._last_move = None
        self._move_seq = 0
        self._flagged = False
        self._flag_reason = None
        self._jpeg = None

    def set_board(self, matrix, move_text, flagged, reason):
        with self._lock:
            self._matrix = [row[:] for row in matrix]
            self._updated_at = time.time()
            self._last_move = move_text
            self._flagged = flagged
            self._flag_reason = reason
            self._move_seq += 1

    def set_frame(self, frame):
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if ok:
            with self._lock:
                self._jpeg = buf.tobytes()

    def get_board(self):
        with self._lock:
            return self._matrix, self._updated_at, self._last_move, self._move_seq, self._flagged, self._flag_reason

    def get_frame(self):
        with self._lock:
            return self._jpeg


def _game_over_reason(board):
    """Why a finished game finished, in words. python-chess's result() gives
    the score but not the cause, and "1/2-1/2" alone leaves you guessing
    whether the arm stalled or the position is genuinely drawn."""
    if board.is_checkmate():
        return "checkmate"
    if board.is_stalemate():
        return "stalemate"
    if board.is_insufficient_material():
        return "insufficient material"
    if board.is_seventyfive_moves():
        return "75-move rule"
    if board.is_fivefold_repetition():
        return "fivefold repetition"
    return "no legal moves"


class EngineController:
    """Runs the engine on its own thread and publishes the move to place.

    Deliberately not driven inline from TrackingLoop.on_update: that fires
    with the loop's lock held, so a ~0.5s search there would stall every
    /board.json poll. on_update just pokes the event; this thread does the
    thinking.
    """

    kind = "engine"

    def __init__(self, loop, engine, think_s=DEFAULT_THINK_S, robot=None,
                 both_sides=False, move_delay_s=0.0, noob=False, coach_think_s=None):
        self._loop = loop
        self._engine = engine
        self._think_s = think_s
        self._robot = robot
        # AI vs AI: one engine plays itself and the arm places every move, so
        # the colour gate comes off and each completed move re-pokes the
        # thread. move_delay_s is purely so a human can follow along.
        self._both_sides = both_sides
        self._move_delay_s = move_delay_s
        # How a move gets picked. Plain best_move, or the beginner-ish,
        # weave-averse policy -- same signature either way.
        self._noob = noob
        self._pick_move = move_policy.choose_move if noob else (
            lambda engine, board, think_s: engine.best_move(board, think_s)
        )
        self._lock = Lock()
        self._wake = Event()
        self._enabled = False
        self._thinking = False
        self._headline = None
        self._extra = None
        self._message = None if engine.available else engine.error
        # Guided mode. The coach reads every position at full strength
        # (analyse() ignores Skill Level), hints White's best move, grades
        # White's move once played, and explains Black's. On a Mistake or a
        # Blunder it holds Black's reply until the human decides whether to
        # take the move back -- a take-back after the arm has answered would
        # mean undoing the arm's move too, including a capture it cannot
        # un-bury.
        self._coach = coach_think_s is not None
        self._coach_think_s = coach_think_s
        self._analysis = {}          # fen -> analyse() result, this game
        self._reviewed = set()       # fens whose arriving move has been judged
        self._hint_fen = None
        self._coach_eval = None
        self._hint = None
        self._review = None
        self._opponent_note = None
        self._awaiting_fen = None    # set while a Mistake/Blunder waits on a decision
        self._takeback = None
        # Set by close() to end _run. A mode switch builds a new controller
        # around the same shared engine, so without this each switch would
        # leak a thread that goes on driving the old mode's loop.
        self._stopped = Event()
        self._thread = Thread(target=self._run, daemon=True)
        self._thread.start()

    def state(self):
        with self._lock:
            expected = self._loop.expected_move
            return {
                "available": self._engine.available,
                "enabled": self._enabled,
                "thinking": self._thinking,
                "instruction": self._headline,
                "extra": self._extra,
                "expected_uci": expected.uci() if expected is not None else None,
                "skill": self._engine.skill,
                "style": "noob" if self._noob else "normal",
                "message": self._message,
                "coach": self._coach_state() if self._coach else None,
            }

    def _coach_state(self):
        """Called with self._lock held."""
        evaluation = None
        if self._coach_eval is not None:
            evaluation = {"cp": self._coach_eval.get("score_cp"),
                          "mate": self._coach_eval.get("mate")}
        return {
            "eval": evaluation,
            "hint": self._hint,
            "review": self._review,
            "opponent_note": self._opponent_note,
            "awaiting_decision": self._awaiting_fen is not None,
            "takeback": self._takeback,
        }
    def configure(self, enabled=None, skill=None):
        with self._lock:
            if skill is not None:
                self._engine.set_skill(skill)
            if enabled is not None and enabled != self._enabled:
                self._enabled = bool(enabled) and self._engine.available
                if not self._enabled:
                    self._headline = self._extra = None
                    self._loop.set_expected_move(None)
        self.notify()

    def attach_robot(self, robot):
        """Hands the controller its arm after the fact.

        Session builds this controller first (it binds to the loop) and the
        RobotController second (it binds to the same loop), so the link has
        to be made from outside rather than in either constructor.
        """
        with self._lock:
            self._robot = robot

    def notify(self):
        self._wake.set()

    def _run(self):
        while not self._stopped.is_set():
            self._wake.wait(timeout=1.0)
            self._wake.clear()
            if self._stopped.is_set():
                return
            try:
                self._maybe_move()
            except Exception as exc:
                with self._lock:
                    self._thinking = False
                    self._message = f"engine error: {exc}"

    def _maybe_move(self):
        if not self._engine.available:
            return
        # The coach runs whether or not the engine is switched on to play:
        # hints and grades are useful with Black moved by hand, too.
        if self._coach and self._coach_step():
            return  # a Mistake/Blunder is waiting on take back / continue
        with self._lock:
            if not self._enabled:
                return
        # Engine plays Black -- unless it's playing both sides -- and only
        # when nothing is already pending.
        if self._loop.expected_move is not None:
            return
        if not self._both_sides and self._loop.turn != "black":
            return

        board = self._loop.board_copy
        if board.is_game_over():
            with self._lock:
                self._headline, self._extra = None, None
                self._message = f"game over -- {board.result()} ({_game_over_reason(board)})"
            return

        with self._lock:
            self._thinking = True
            self._message = None
        move = self._pick_move(self._engine, board, self._think_s)
        headline, extra = describe_move(board, move) if move is not None else (None, None)

        with self._lock:
            self._thinking = False
            self._headline, self._extra = headline, extra
        if move is None:
            return

        # Arm verification before the arm moves: whatever ends up on the
        # board next -- robot or human -- must match this move or it flags.
        self._loop.set_expected_move(move)

        if self._both_sides and (self._robot is None or not self._robot.ready):
            # Nobody else is going to place this move, so leaving it armed
            # would wedge the game: _maybe_move returns early for as long as
            # an expected move is pending. Clear it and say why, so homing
            # the arm is enough to get going again.
            self._loop.set_expected_move(None)
            with self._lock:
                state = self._robot.state() if self._robot is not None else {}
                self._message = (state.get("message")
                                 or "the arm is not homed -- press Home / re-enable")
            return

        if self._robot is not None and self._robot.ready:
            # Blocking, but this is the engine's own thread with no lock
            # held, which is exactly why the search lives here too.
            ok, error = self._robot.execute(board, move)
            if not ok:
                with self._lock:
                    self._message = f"robot stopped: {error}"
                return
            if self._both_sides:
                # Nobody else is going to wake us: in AI vs AI the arm's own
                # move *is* the board update, so the chain has to re-poke
                # itself. A failed move deliberately doesn't -- the robot is
                # halted and a human has to look at it.
                time.sleep(self._move_delay_s)
                self.notify()

    # ------------------------------------------------------------- the coach

    def _analyse(self, board):
        """analyse(), cached per position for the life of this game. The hint
        for a position and the grade of the move played from it need the same
        search, and a take-back revisits a position already searched."""
        key = board.fen()
        if key not in self._analysis:
            if len(self._analysis) > 512:
                self._analysis.clear()
            self._analysis[key] = self._engine.analyse(board, self._coach_think_s)
        return self._analysis[key]

    def _coach_step(self):
        """One pass of the coach over the current position. Returns True if
        Black must hold its reply."""
        board = self._loop.board_copy
        fen = board.fen()

        with self._lock:
            if self._awaiting_fen is not None:
                if self._awaiting_fen == fen:
                    return True
                # Undo, Edit board or Reset moved the game on without an
                # answer; the question no longer applies.
                self._awaiting_fen = None
            if not board.move_stack and self._takeback is None:
                self._review = self._opponent_note = None

        if board.move_stack and fen not in self._reviewed:
            self._reviewed.add(fen)
            played = board.move_stack[-1]
            before = board.copy()
            before.pop()
            after_info = self._analyse(board)
            if before.turn == chess.WHITE:
                best_info = self._analyse(before)
                review = coach.review(before, played, best_info, after_info) if best_info else None
                with self._lock:
                    self._review = review
                    self._takeback = None
                    self._hint = None
                    if after_info is not None:
                        self._coach_eval = after_info
                    if review is not None and review["label"] in coach.NEEDS_DECISION:
                        self._awaiting_fen = fen
                        return True
            else:
                note = f"Black played {before.san(played)}: {coach.explain(before, played, after_info)}"
                with self._lock:
                    self._opponent_note = note
                    if after_info is not None:
                        self._coach_eval = after_info

        if board.turn == chess.WHITE and self._hint_fen != fen:
            self._hint_fen = fen
            if board.is_game_over():
                with self._lock:
                    self._hint = None
                return False
            info = self._analyse(board)
            with self._lock:
                self._hint = coach.hint(board, info)
                if info is not None:
                    self._coach_eval = info
        return False

    def coach_decision(self, decision):
        """Answers the Mistake/Blunder question. Returns (ok, error).

        "takeback" undoes the move in software and says how to undo it on the
        board; the tracker needs nothing more, since putting the piece back
        makes the board match the restored position and so reads as no change.
        "continue" lets Black reply.
        """
        if not self._coach:
            return False, "the coach is not running in this mode"
        if decision not in ("takeback", "continue"):
            return False, "decision must be \"takeback\" or \"continue\""
        with self._lock:
            if self._awaiting_fen is None:
                return False, "nothing is waiting for a decision"
            self._awaiting_fen = None

        if decision == "takeback":
            board = self._loop.board_copy
            if not board.move_stack:
                return False, "nothing to take back"
            move = board.move_stack[-1]
            with self._lock:
                # Forget the grade, so replaying the same move is judged again.
                self._reviewed.discard(board.fen())
            board.pop()
            if self._loop.undo_last_move() is None:
                return False, "nothing to take back"
            with self._lock:
                self._takeback = coach.takeback_instruction(board, move)
                self._review = None
            # The restored position gets a fresh hint.
            self._hint_fen = None
        self.notify()
        return True, None

    def close(self, timeout=2.0):
        """Stops the thread. Deliberately does NOT close the engine: the
        Stockfish process is shared across modes and owned by whoever built
        it. A move already in flight finishes -- the arm is mid-sequence and
        cutting it loose is worse than waiting."""
        self._stopped.set()
        with self._lock:
            self._enabled = False
        self._wake.set()
        self._thread.join(timeout=timeout)


class PuzzleController:
    """Runs puzzle mode: shows a layout, checks it with the camera, has Black's
    moves from the line placed, and judges White's.

    Sits in the slot Session keeps for EngineController and answers the same
    calls (configure/attach_robot/notify/close/state), so Session needs no
    special case for it. The puzzle logic proper -- picking, scoring, the
    state machine -- is puzzles.py; this is the part that touches the loop and
    the arm.

    Black's replies are the stored line, never Stockfish: the puzzle is only a
    puzzle if the defence is the one it was built around.
    """

    kind = "puzzle"

    def __init__(self, loop, bank, progress, filters=None, robot=None, graveyard=None,
                 rng=None):
        self._loop = loop
        self._bank = bank
        self._progress = progress
        self._filters = dict(filters or {})
        self._robot = robot
        # The Robot (not the controller), whose graveyard ring the human has to
        # empty before a new puzzle -- None without an arm.
        self._graveyard = graveyard
        self._rng = rng
        self._lock = Lock()
        self._wake = Event()
        self._stopped = Event()

        self._run = None
        self._mismatches = None
        self._message = None
        self._instruction = None
        self._extra = None
        self._hint_square = None
        self._solution = None
        self._solution_uci = None
        self._rating_delta = None
        self._base_len = 0          # move_stack length when the current step began
        self._opponent_armed = False

        self._next_puzzle()
        self._thread = Thread(target=self._run_thread, daemon=True)
        self._thread.start()

    # -------------------------------------------------------- Session's calls

    def configure(self, enabled=None, skill=None):
        """Session calls configure(enabled=False) on Reset and on Stop. For a
        puzzle that means: stop whatever the line was doing and go back to
        setting this same puzzle up."""
        if enabled is False:
            with self._lock:
                run = self._run
            if run is not None:
                self._setup(run.puzzle)
        self.notify()

    def attach_robot(self, robot):
        with self._lock:
            self._robot = robot

    def notify(self):
        self._wake.set()

    def close(self, timeout=2.0):
        self._stopped.set()
        self._wake.set()
        self._thread.join(timeout=timeout)
        self._loop.set_paused(False)

    def state(self):
        with self._lock:
            run = self._run
            board = self._loop.board_copy
            expected = self._loop.expected_move
            phase = run.phase if run else None
            target = run.puzzle.target_matrix() if run and phase == puzzles.SETUP else None
            # Live rather than stored: Reset re-seeds the tracked board after
            # the setup began, and the list has to be against what's there.
            diff = puzzles.setup_diff(self._loop.current_matrix, target) if target else None
            return {
                "phase": phase,
                "puzzle": run.puzzle.info() if run else None,
                "target": target,
                "diff": diff,
                "mismatches": self._mismatches,
                "graveyard_pieces": len(self._graveyard.graveyard) if self._graveyard else 0,
                "to_move": "white" if board.turn == chess.WHITE else "black",
                "instruction": self._instruction,
                "extra": self._extra,
                "expected_uci": expected.uci() if expected is not None else None,
                "hint_square": self._hint_square,
                "solution": self._solution,
                "solution_uci": self._solution_uci,
                "wrong_note": run.wrong_note if run else None,
                "result": run.result() if run and run.finished else None,
                "rating_delta": self._rating_delta,
                "progress": self._progress.summary(),
                "message": self._message,
            }

    # ------------------------------------------------------- the UI's actions

    def action(self, name):
        """One of ready/hint/solution/skip/next. Returns (ok, payload) where
        payload is an error string on failure, else extra fields for the
        reply."""
        with self._lock:
            run = self._run
        if run is None:
            if name in ("next", "skip"):
                self._next_puzzle()
                return True, {}
            return False, self._message or "no puzzle loaded"

        if name == "ready":
            return self._ready(run)
        if name == "hint":
            if run.phase != puzzles.SOLVING:
                return False, "a hint is only for your own move"
            with self._lock:
                self._hint_square = run.hint()
            return True, {}
        if name == "solution":
            if run.finished or run.phase == puzzles.SETUP:
                return False, "no puzzle in progress"
            board = self._loop.board_copy
            remaining = run.solution[run.index:]
            with self._lock:
                run.reveal()
                self._solution = coach.line_san(board, remaining, limit=len(remaining))
                self._solution_uci = [m.uci() for m in remaining]
            self._finish(run)
            return True, {}
        if name in ("skip", "next"):
            if not run.finished and not run.recorded and run.phase != puzzles.SETUP:
                # Walking away mid-line: a miss already made still counts.
                self._record(run, puzzles.FAILED if run.failed_once else puzzles.SKIPPED)
            elif run.phase == puzzles.SETUP and not run.recorded and name == "skip":
                self._record(run, puzzles.SKIPPED)
            self._next_puzzle()
            return True, {}
        return False, f"unknown puzzle action {name!r}"

    def _ready(self, run):
        """The human says the board is set up. Make the camera agree first."""
        if run.phase != puzzles.SETUP:
            return False, "the puzzle has already started"
        read = self._loop.read_board()
        if read is None:
            return False, "no camera frame yet -- try again in a moment"
        mismatches = puzzles.color_mismatches(read, run.puzzle.target_matrix())
        with self._lock:
            self._mismatches = mismatches or None
        if mismatches:
            return False, (f"{len(mismatches)} square(s) don't match the puzzle: "
                           + ", ".join(m["square"] for m in mismatches[:8]))

        self._loop.set_position(run.puzzle.board())
        if self._graveyard is not None:
            # Whatever the ring held belongs to the last game; the human has
            # just been told to clear it along with the rest of the setup.
            self._graveyard.clear_graveyard()
        with self._lock:
            self._message = None
            run.begin()
            self._opponent_armed = False
        # Only after begin(): the thread re-pauses on every pass it still
        # sees SETUP, and would otherwise hold tracking for a full lapse.
        self._loop.set_paused(False)
        self.notify()
        return True, {}

    # ---------------------------------------------------------------- driving

    def _run_thread(self):
        while not self._stopped.is_set():
            self._wake.wait(timeout=1.0)
            self._wake.clear()
            if self._stopped.is_set():
                return
            try:
                self._step()
            except Exception as exc:
                with self._lock:
                    self._message = f"puzzle error: {exc}"

    def _step(self):
        with self._lock:
            run = self._run
        if run is None:
            return
        if run.phase == puzzles.SETUP:
            # Held paused while pieces are being moved around, so the tracker
            # doesn't flag every half-built position. Refreshed here once a
            # second; if this thread dies the pause lapses on its own.
            self._loop.set_paused(True)
            return
        board = self._loop.board_copy
        if len(board.move_stack) < self._base_len:
            # Undo or Edit board went behind us. Follow rather than fight.
            self._base_len = len(board.move_stack)
        if run.phase == puzzles.OPPONENT:
            self._step_opponent(run, board)
        elif run.phase == puzzles.SOLVING:
            self._step_solving(run, board)

    def _step_opponent(self, run, board):
        move = run.next_move
        if self._opponent_armed:
            if len(board.move_stack) > self._base_len:
                # The tracker accepted it -- camera-confirmed after the arm, or
                # placed by hand and matched against expected_move.
                with self._lock:
                    self._opponent_armed = False
                    self._instruction = self._extra = None
                    run.opponent_done()
                    self._base_len = len(board.move_stack)
                    self._hint_square = None
                if run.finished:
                    self._finish(run)
            return

        if move not in board.legal_moves:
            with self._lock:
                self._message = "the board no longer matches the puzzle -- press Next or Reset"
            return
        headline, extra = describe_move(board, move)
        with self._lock:
            self._opponent_armed = True
            self._base_len = len(board.move_stack)
            self._instruction, self._extra = headline, extra
        # Whatever lands on the board next must be this move, arm or hand.
        self._loop.set_expected_move(move)
        robot = self._robot
        if robot is not None and robot.ready:
            ok, error = robot.execute(board, move)
            if not ok:
                with self._lock:
                    self._message = f"robot stopped: {error} -- place Black's move by hand"
        self.notify()  # look for the accepted move straight away

    def _step_solving(self, run, board):
        if len(board.move_stack) <= self._base_len:
            return
        played = board.move_stack[-1]
        before = board.copy()
        before.pop()
        if run.player_moved(before, played):
            with self._lock:
                self._base_len = len(board.move_stack)
                self._hint_square = None
            if run.finished:
                self._finish(run)
            else:
                self.notify()
            return

        # Wrong. Take it back in software and say how to take it back on the
        # board; putting the piece back then reads as no change at all.
        self._loop.undo_last_move()
        with self._lock:
            run.wrong_note = (f"{before.san(played)} isn't it -- "
                              f"{coach.takeback_instruction(before, played)}, then try again")
            self._base_len = len(board.move_stack) - 1

    def _finish(self, run):
        if not run.recorded:
            self._record(run, run.result())

    def _record(self, run, result):
        run.recorded = True
        delta = self._progress.record(run.puzzle, result)
        with self._lock:
            self._rating_delta = delta

    def _next_puzzle(self):
        f = self._filters
        rating_range = None
        target = None
        if f.get("rating_mode") == "range":
            rating_range = (int(f.get("rating_min") or 0), int(f.get("rating_max") or 4000))
        else:
            target = self._progress.rating
        kwargs = {} if self._rng is None else {"rng": self._rng}
        puzzle = self._bank.pick(
            rating_range=rating_range, target=target,
            themes=f.get("themes") or (), max_pieces=f.get("max_pieces") or None,
            exclude=self._progress.recent_ids(), **kwargs,
        )
        if puzzle is None:
            with self._lock:
                self._run = None
                self._message = "no puzzle matches those filters -- widen them on the menu"
            return
        self._setup(puzzle)

    def _setup(self, puzzle):
        self._loop.set_expected_move(None)
        self._loop.set_paused(True)
        with self._lock:
            self._run = puzzles.PuzzleRun(puzzle)
            self._mismatches = None
            self._message = None
            self._instruction = self._extra = None
            self._hint_square = None
            self._solution = None
            self._solution_uci = None
            self._rating_delta = None
            self._opponent_armed = False


def _puzzle_filters(settings):
    """The menu's puzzle settings, cleaned. Anything malformed is dropped
    rather than refused -- a bad filter just means a wider choice."""
    filters = {"rating_mode": "range" if settings.get("puzzle_rating_mode") == "range" else "adaptive"}
    for key, name in (("puzzle_min", "rating_min"), ("puzzle_max", "rating_max"),
                      ("puzzle_max_pieces", "max_pieces")):
        try:
            value = int(settings.get(key))
        except (TypeError, ValueError):
            continue
        if value > 0:
            filters[name] = value
    themes = settings.get("puzzle_themes") or []
    if isinstance(themes, list):
        filters["themes"] = [t for t in themes if t in puzzles.MENU_THEMES]
    return filters


def _validate_correction(body):
    """Returns an error string, or None if body is well-formed enough to
    attempt (matrix shape/labels valid, turn valid, and the resulting
    position parses as a legal chess.Board)."""
    matrix = body.get("matrix")
    turn = body.get("turn")

    if turn not in ("white", "black"):
        return "turn must be \"white\" or \"black\""
    if not isinstance(matrix, list) or len(matrix) != 8:
        return "matrix must be an 8x8 array"
    for row in matrix:
        if not isinstance(row, list) or len(row) != 8:
            return "matrix must be an 8x8 array"
        for label in row:
            if label is not None and label not in _VALID_LABELS:
                return f"invalid piece label: {label!r}"

    placement = matrix_to_fen_placement(matrix)
    turn_char = "w" if turn == "white" else "b"
    try:
        board = chess.Board(f"{placement} {turn_char} - - 0 1")
    except ValueError as exc:
        return f"invalid position: {exc}"
    if not board.is_valid():
        return "position is not a legal chess position (check kings/pawns)"
    return None


class AdminConsole:
    """What /admin does: drive the arm by hand, square to square.

    Works on the menu and in a running game. In a game the arm is shared with
    the engine and the board is shared with the tracker, so every action that
    moves anything first turns the engine off (and leaves it off -- the human
    resumes from the game screen) and holds tracking paused while it runs. A
    piece move then rewrites the tracked position to match, through the same
    correction path the board editor uses; otherwise the camera would see a
    piece jump and flag it, or the software board in AI vs AI would simply be
    wrong from then on.
    """

    LOG_SIZE = 100

    def __init__(self, session, frame_source=lambda: None):
        self._session = session
        self._frame = frame_source
        self._lock = Lock()
        self._log = deque(maxlen=self.LOG_SIZE)
        self._pos = None
        self._engine_paused = False

    def state(self):
        session = self._session
        robot = session.robot
        loop = session.loop
        with self._lock:
            log = list(self._log)
            pos = self._pos
            engine_paused = self._engine_paused
        return {
            "mode": session.mode,
            "running": session.running,
            "park_square": rig.ORIGIN_SQUARE,
            "robot": None if robot is None else {
                "port": robot.port,
                "homed": robot.homed,
                "halted": robot.halted,
                "busy": robot.busy,
                "message": robot.message,
                "white_polarity": robot.white_polarity,
                "supported": robot.speaks_squares,
            },
            "pos": None if pos is None else {"x_mm": pos[0], "y_mm": pos[1]},
            "matrix": loop.current_matrix if loop is not None else None,
            "turn": loop.turn if loop is not None else None,
            "engine_paused": engine_paused and session.running,
            "log": log[::-1],  # newest first
        }

    def _record(self, action, command, ok, detail):
        entry = {"t": time.time(), "action": action, "command": command, "ok": ok}
        entry["reply" if ok else "error"] = detail
        with self._lock:
            self._log.append(entry)

    def action(self, body):
        """Returns (http_status, payload). Never raises."""
        name = body.get("action")
        robot = self._session.robot
        if robot is None:
            return 400, {"error": "no robot attached -- start with --robot"}
        if name != "halt" and not robot.speaks_squares:
            return 400, {"error": "/admin speaks square names, which only the legacy "
                                  "(chessbot_v1) firmware understands"}
        handler = {
            "goto": self._goto, "move": self._move, "mag": self._mag,
            "pos": self._query_pos, "home": self._home, "halt": self._halt,
        }.get(name)
        if handler is None:
            return 400, {"error": "action must be one of goto/move/mag/pos/home/halt"}
        try:
            status, payload = handler(robot, body)
        except ValueError as exc:  # a bad square, colour or magnet mode
            return 400, {"error": str(exc), **self.state()}
        return status, {**payload, **self.state()}

    # ---- actions ---------------------------------------------------------

    def _send(self, robot, action, command, require_homed=True):
        try:
            reply = robot.admin_send(command, require_homed=require_homed)
        except GantryError as exc:
            self._record(action, command, False, str(exc))
            return False, str(exc)
        self._record(action, command, True, reply)
        return True, reply

    def _query_pos(self, robot, body=None):
        ok, reply = self._send(robot, "pos", "POS", require_homed=False)
        if not ok:
            return 400, {"error": reply}
        pos = admin_moves.parse_pos(reply)
        with self._lock:
            self._pos = pos
        return 200, {"ok": True}

    def _quiet_game(self):
        """Engine off, tracking held. Returns a release callable, or raises
        GantryError if the engine's own move still holds the arm."""
        session = self._session
        if session.robot.busy:
            raise GantryError("arm is busy -- wait for the current move to finish")
        controller = session.engine_controller
        if controller is not None:
            controller.configure(enabled=False)
            with self._lock:
                self._engine_paused = True
        loop = session.loop
        if loop is None:
            return lambda: None
        # Long enough for a full-board drag; released the moment it's done.
        loop.set_paused(True, lapse_s=60.0)
        return lambda: loop.set_paused(False)

    def _run(self, robot, action, command, require_homed=True):
        """Sends one moving command, quieting a running game around it."""
        try:
            release = self._quiet_game()
        except GantryError as exc:
            self._record(action, command, False, str(exc))
            return False, str(exc)
        try:
            return self._send(robot, action, command, require_homed)
        finally:
            release()

    def _goto(self, robot, body):
        command = admin_moves.goto_command(body.get("square"))
        ok, reply = self._run(robot, "goto", command)
        if not ok:
            return 409 if "busy" in reply else 400, {"error": reply}
        self._query_pos(robot)
        return 200, {"ok": True}

    def _move(self, robot, body):
        frm, to = body.get("from"), body.get("to")
        command = admin_moves.move_command(frm, to, body.get("colour"),
                                           bool(body.get("weave")))
        loop = self._session.loop
        moved = None
        if loop is not None:
            # Checked before the arm moves: a refusal after the drag would
            # leave the real board and the tracked one disagreeing.
            moved, error = admin_moves.apply_to_matrix(loop.current_matrix, frm, to)
            if error is None:
                error = _validate_correction({"matrix": moved, "turn": loop.turn})
            if error is not None:
                self._record("move", command, False, error)
                return 400, {"error": error}

        ok, reply = self._run(robot, "move", command)
        if not ok:
            return 409 if "busy" in reply else 400, {"error": reply}
        if moved is not None and self._session.loop is loop:
            loop.apply_manual_correction(moved, loop.turn, self._frame())
        self._query_pos(robot)
        return 200, {"ok": True}

    def _mag(self, robot, body):
        command = admin_moves.mag_command(body.get("mode"))
        # Switching the coil OFF is a safety action and must work on a halted
        # arm; energising it goes through the same quieting as a move.
        if body.get("mode") == "off":
            ok, reply = self._send(robot, "mag", command, require_homed=False)
        else:
            ok, reply = self._run(robot, "mag", command, require_homed=False)
        if not ok:
            return 409 if "busy" in reply else 400, {"error": reply}
        return 200, {"ok": True}

    def _home(self, robot, body):
        try:
            release = self._quiet_game()
        except GantryError as exc:
            self._record("home", "HOME", False, str(exc))
            return 409, {"error": str(exc)}
        try:
            ok, error = self._session.park()
        finally:
            release()
        self._record("home", "HOME", ok, None if ok else error)
        if not ok:
            return 400, {"error": error}
        with self._lock:
            self._pos = None  # parked at the origin; ask rather than assume
        self._query_pos(robot)
        return 200, {"ok": True}

    def _halt(self, robot, body):
        controller = self._session.robot_controller
        if controller is not None:
            controller.halt("halted from /admin")
        else:
            robot.halt("halted from /admin")
        self._record("halt", "!", True, "halted -- home to re-enable")
        return 200, {"ok": True}


def start_server(host, port, buffer, session):
    """HTTP server that reads `session` live rather than closing over one
    mode's objects, so a mode switch is visible to the very next request.

    Every game route answers 409 when nothing is running: the menu is a real
    state, not a degenerate game, and a stale tab polling /board.json after a
    stop must get a clear answer rather than an exception.
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    def latest_frame():
        stream = session.capture_stream
        return None if stream is None else stream.get_latest()[0]

    admin = AdminConsole(session, frame_source=latest_frame)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path, _, query = self.path.partition("?")
            if path == "/" or path in ("/index.html",):
                self._send_asset("index.html")
            elif path in ("/admin", "/admin/"):
                self._send_asset("admin.html")
            elif path == "/admin/state.json":
                self._send_json(200, admin.state())
            elif path == "/state.json":
                self._send_json(200, self._state_payload())
            elif path == "/board.json":
                loop = session.loop
                if loop is None:
                    # On the menu: answer with the session state alone, so
                    # one poll drives both screens.
                    self._send_json(200, self._state_payload())
                    return
                # The UI polls with ?editing=1 while its board editor is
                # open; that refreshes the pause so tracking stays held.
                # Stop polling (close the tab) and the pause lapses.
                if "editing=1" in query:
                    loop.set_paused(True)
                matrix, updated_at, last_move, move_seq, flagged, flag_reason = buffer.get_board()
                rows = matrix if matrix is not None else [[None] * 8 for _ in range(8)]
                robot_controller = session.robot_controller
                engine_controller = session.engine_controller
                payload = self._state_payload()
                payload.update(
                    {
                        "matrix": rows,
                        "updated_at": updated_at,
                        "last_move": last_move,
                        "move_seq": move_seq,
                        "flagged": flagged,
                        "flag_reason": flag_reason,
                        "turn": loop.turn,
                        "paused": loop.is_paused,
                        "engine": (engine_controller.state()
                                   if engine_controller and engine_controller.kind == "engine"
                                   else None),
                        "puzzle": (engine_controller.state()
                                   if engine_controller and engine_controller.kind == "puzzle"
                                   else None),
                        "robot": robot_controller.state() if robot_controller else None,
                    }
                )
                self._send_json(200, payload)
            elif path == "/stream.mjpg":
                if session.capture_stream is None:
                    # No camera in this mode. Answering rather than streaming
                    # nothing forever keeps a stray <img> from holding a
                    # ThreadingHTTPServer thread open for the whole game.
                    self.send_error(503, "no camera in this mode")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.end_headers()
                try:
                    while session.capture_stream is not None:
                        jpeg = buffer.get_frame()
                        if jpeg is None:
                            time.sleep(0.05)
                            continue
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n")
                        self.wfile.write(jpeg)
                        self.wfile.write(b"\r\n")
                        time.sleep(0.03)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # viewer closed the tab
            elif self._send_asset(path.lstrip("/")):
                pass
            else:
                self.send_error(404)

        def _send_asset(self, name):
            body, content_type = read_ui_asset(name)
            if body is None:
                return False
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            # The page is edited in place during development and the Pi is
            # reached over a LAN, so a cached stylesheet is a real nuisance.
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
            return True

        def _state_payload(self):
            state = session.state()
            # The park corner belongs in the payload, not baked into PAGE:
            # --board-origin is applied after this module is imported, so a
            # page built at import time would name the wrong square.
            state["park_square"] = rig.ORIGIN_SQUARE
            return state

        def do_POST(self):
            path, _, _query = self.path.partition("?")
            known = ("/board/correct", "/board/undo", "/board/pause", "/engine",
                     "/robot", "/mode", "/mode/stop", "/reset", "/coach", "/puzzle",
                     "/admin")
            if path not in known:
                self.send_error(404)
                return

            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                self._send_json(400, {"error": "invalid JSON body"})
                return

            # ---- /admin: valid on the menu and in a game alike -------------

            if path == "/admin":
                status, payload = admin.action(body)
                self._send_json(status, payload)
                return

            # ---- mode control: valid on the menu, unlike everything else ----

            if path == "/mode":
                try:
                    self._send_json(200, {"ok": True, **session.start(
                        body.get("mode"), body.get("settings"))})
                except ModeError as exc:
                    self._send_json(400, {"error": str(exc)})
                except Exception as exc:  # a camera or model that won't load
                    self._send_json(500, {"error": f"could not start: {exc}"})
                return

            if path == "/mode/stop":
                self._send_json(200, {"ok": True, **session.stop()})
                return

            if path == "/reset":
                ok, error = session.reset()
                payload = {"ok": ok, **self._state_payload()}
                if not ok:
                    payload["error"] = error
                self._send_json(200 if ok else 400, payload)
                return

            # ---- everything below needs a running mode --------------------

            loop = session.loop
            engine_controller = session.engine_controller
            robot_controller = session.robot_controller
            if loop is None:
                self._send_json(409, {"error": "no mode is running -- pick one first"})
                return

            if path == "/coach":
                decide = getattr(engine_controller, "coach_decision", None)
                if decide is None:
                    self._send_json(400, {"error": "the coach is not running in this mode"})
                    return
                ok, error = decide(body.get("decision"))
                if not ok:
                    self._send_json(400, {"error": error})
                    return
                self._send_json(200, {"ok": True, "engine": engine_controller.state()})
                return

            if path == "/puzzle":
                if engine_controller is None or engine_controller.kind != "puzzle":
                    self._send_json(400, {"error": "puzzle mode is not running"})
                    return
                ok, result = engine_controller.action(body.get("action"))
                if not ok:
                    self._send_json(400, {"error": result, "puzzle": engine_controller.state()})
                    return
                self._send_json(200, {"ok": True, **result, "puzzle": engine_controller.state()})
                return

            if path == "/engine":
                if engine_controller.kind != "engine":
                    self._send_json(400, {"error": "there is no engine opponent in this mode"})
                    return
                engine_controller.configure(
                    enabled=body.get("enabled"), skill=body.get("skill")
                )
                self._send_json(200, {"ok": True, "engine": engine_controller.state()})
                return

            if path == "/robot":
                if robot_controller is None:
                    self._send_json(400, {"error": "no robot attached -- start with --robot"})
                    return
                if body.get("halt"):
                    robot_controller.halt()
                    self._send_json(200, {"ok": True, "robot": robot_controller.state()})
                    return
                if body.get("confirm"):
                    # The human has lifted the captured piece. Releases the
                    # robot thread, which is blocked mid-sequence.
                    if not robot_controller.confirm():
                        self._send_json(400, {"error": "nothing is waiting for confirmation"})
                        return
                    self._send_json(200, {"ok": True, "robot": robot_controller.state()})
                    return
                if body.get("cancel"):
                    if not robot_controller.cancel():
                        self._send_json(400, {"error": "nothing is waiting for confirmation"})
                        return
                    self._send_json(200, {"ok": True, "robot": robot_controller.state()})
                    return
                if body.get("white_polarity"):
                    polarity = body["white_polarity"]
                    if polarity not in rig_config.POLARITIES:
                        self._send_json(400, {"error": f"polarity must be one of "
                                                       f"{list(rig_config.POLARITIES)}"})
                        return
                    try:
                        robot_controller.set_white_polarity(polarity)
                    except (GantryError, ValueError) as exc:
                        self._send_json(400, {"error": str(exc),
                                              "robot": robot_controller.state()})
                        return
                    self._send_json(200, {"ok": True, "robot": robot_controller.state()})
                    return
                if body.get("release_ms") is not None:
                    try:
                        robot_controller.set_release_ms(body["release_ms"])
                    except (GantryError, ValueError) as exc:
                        self._send_json(400, {"error": str(exc),
                                              "robot": robot_controller.state()})
                        return
                    self._send_json(200, {"ok": True, "robot": robot_controller.state()})
                    return
                if body.get("home"):
                    # Homing crosses the board, so it must not race a settle.
                    loop.set_paused(True, lapse_s=60.0)
                    try:
                        ok, error = robot_controller.home()
                    finally:
                        loop.set_paused(False)
                    if not ok:
                        self._send_json(400, {"error": error, "robot": robot_controller.state()})
                        return
                    self._send_json(200, {"ok": True, "robot": robot_controller.state()})
                    return
                self._send_json(400, {"error": "expected one of home/halt/confirm/cancel"})
                return

            if path == "/board/pause":
                loop.set_paused(bool(body.get("paused")))
                self._send_json(200, {"ok": True, "paused": loop.is_paused})
                return

            if path == "/board/undo":
                san = loop.undo_last_move(self._frame())
                if san is None:
                    self._send_json(400, {"error": "nothing to undo"})
                else:
                    self._send_json(200, {"ok": True, "undone": san})
                return

            error = _validate_correction(body)
            if error is not None:
                self._send_json(400, {"error": error})
                return

            loop.apply_manual_correction(body["matrix"], body["turn"], self._frame())
            # Saving the editor ends the edit session, so lift the pause.
            loop.set_paused(False)

            # A correction can put captured pieces back on the board. The arm
            # has already parked those on graveyard slots, and it will not go
            # and fetch them -- so free the slots for reuse and tell the human
            # which ones still have a piece sitting on them.
            payload = {"ok": True}
            robot = session.robot
            if robot is not None:
                on_board = sum(1 for row in body["matrix"] for label in row if label)
                freed = robot.reconcile_graveyard(on_board)
                if freed:
                    payload["retrieve"] = [
                        {"slot": slot, "x_mm": x, "y_mm": y} for slot, (x, y) in freed
                    ]
            self._send_json(200, payload)

        def _frame(self):
            """The latest camera frame, or None in a mode that has none."""
            stream = session.capture_stream
            if stream is None:
                return None
            frame, _timestamp = stream.get_latest()
            return frame

        def _send_json(self, status, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass  # keep the terminal clean

    server = ThreadingHTTPServer((host, port), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    return server


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--poll-interval", type=float, default=0.12,
                        help="seconds between cheap motion-gate polls (not a detection interval)")
    parser.add_argument("--calibration", type=Path, default=DEFAULT_CALIBRATION)
    parser.add_argument("--classifier", type=Path, default=DEFAULT_CLASSIFIER, help="per-square classifier")
    parser.add_argument("--min-conf", type=float, default=DEFAULT_MIN_CONF,
                        help="classifier confidence threshold")
    parser.add_argument("--motion-thresh", type=float, default=None,
                        help="board-ROI motion threshold; tune with debug_classifier.py --watch")
    parser.add_argument("--engine-command", default="stockfish",
                        help="UCI engine binary for the Black side")
    parser.add_argument("--engine-skill", type=int, default=DEFAULT_SKILL,
                        help="Stockfish Skill Level 0-20 (adjustable live in the UI)")
    parser.add_argument("--engine-think", type=float, default=DEFAULT_THINK_S,
                        help="seconds the engine may think per move")
    parser.add_argument("--ai-vs-ai", action="store_true",
                        help="Stockfish plays itself and the arm places every move. No "
                             "camera, no calibration and no classifier are used -- the "
                             "position is tracked in software, so the board MUST start "
                             "with all 32 pieces in the standard setup. Requires --robot")
    parser.add_argument("--noob", action=argparse.BooleanOptionalAction, default=None,
                        help="play like a beginner: prefer pawn moves, and move a knight "
                             "or castle only when nothing else is legal (those are the "
                             "moves that make the arm weave, which is its riskiest "
                             "motion). Costs about 2x --engine-think per move. "
                             "Default: on with --ai-vs-ai, off otherwise")
    parser.add_argument("--white-polarity", choices=rig_config.POLARITIES, default=None,
                        help="what the coil must do to HOLD a white piece. The white "
                             "magnets are fitted the other way up on this set, so "
                             "'repel' holds them and 'attract' shoves them off the "
                             "square. Saved to config/rig.json; omit to use the saved "
                             "value. Changeable live in the UI")
    parser.add_argument("--release-ms", type=int, default=None,
                        metavar="MS",
                        help=f"how long the grip takes to fade when a piece is set "
                             f"down ({rig.MIN_RELEASE_MS}-{rig.MAX_RELEASE_MS}). A hard "
                             "release makes the piece jump, so it eases off and then "
                             "gives a weak kick to clear the core; 0 restores the old "
                             "instant kick. Saved to config/rig.json; adjustable live "
                             "in the UI")
    parser.add_argument("--board-origin", default=rig.ORIGIN_SQUARE,
                        choices=rig.SUPPORTED_ORIGINS,
                        help="which real square the carriage parks on, i.e. how the board "
                             f"is seated under the gantry (default: {rig.ORIGIN_SQUARE}, "
                             "measured on this rig). 'h1' is the firmware's own "
                             "assumption and applies no rotation")
    parser.add_argument("--move-delay", type=float, default=1.0,
                        help="--ai-vs-ai only: seconds to pause after each completed move, "
                             "so the game is watchable and you have time to hit Halt")
    parser.add_argument("--robot", default=None, metavar="PORT",
                        help="serial port of the gantry Arduino (e.g. /dev/ttyACM0), 'auto' "
                             "to detect it, or 'mock' for a dry run. Omitted: no arm, you "
                             "place Black's moves by hand as before")
    parser.add_argument("--robot-protocol", default="legacy", choices=("legacy", "native"),
                        help="which firmware is flashed. 'legacy' (default) is "
                             "firmware/chessbot_v1, what the machine actually runs; "
                             "'native' is firmware/chess_gantry, the unbuilt limit-switch rig")
    parser.add_argument("--topple-delay", type=float, default=DEFAULT_TOPPLE_DELAY_S,
                        help="native protocol only: seconds to wait after toppling a "
                             "captured piece, for you to lift it off. The legacy firmware "
                             "waits for you to confirm instead, with no time limit")
    parser.add_argument("--harvest", type=Path, nargs="?", const=DEFAULT_HARVEST, default=None,
                        help="save labelled crops from every resolved move, to grow the training "
                             f"set as you play (default dir: {DEFAULT_HARVEST})")
    parser.add_argument("--coach-think", type=float, default=1.0,
                        help="Guided game: seconds the coach analyses each position "
                             "(at full strength, whatever the opponent's skill)")
    parser.add_argument("--puzzles", type=Path, default=puzzles.DEFAULT_BANK,
                        help="puzzle file, made by tools/make_puzzles.py")
    parser.add_argument("--puzzle-progress", type=Path, default=puzzles.DEFAULT_PROGRESS,
                        help="where your puzzle rating and history are kept")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="0.0.0.0")
    return parser.parse_args()


def _open_robot(args):
    """Opens the gantry, or exits with a readable reason. Shared by both
    modes so the no-limit-switch warning is only written once."""
    # How the board is seated under the gantry is a property of the machine,
    # not of a single move, so it lives on rig rather than being threaded
    # through every planner call. Both planners read it from there.
    rig.ORIGIN_SQUARE = args.board_origin
    if args.white_polarity or args.release_ms is not None:
        rig_config.save(white_polarity=args.white_polarity, release_ms=args.release_ms)
    try:
        robot = open_gantry(args.robot, topple_delay_s=args.topple_delay,
                            protocol=args.robot_protocol,
                            white_polarity=args.white_polarity,
                            release_ms=args.release_ms)
    except GantryError as exc:
        raise SystemExit(f"Robot: {exc}")
    except ImportError:
        raise SystemExit("Robot: pyserial is missing -- pip install -r requirements.txt")

    print(f"Robot: gantry on {robot.port} ({args.robot_protocol} protocol), "
          f"firmware r{robot.firmware_rev}.")
    if robot.stale_firmware:
        # Loud, and it will refuse to move -- an r1 board silently attracts
        # for every move, so the first white move shoves a piece off the board.
        print(f"Robot: !! {robot.message}")
    else:
        print(f"Robot: white pieces are held by {robot.white_polarity.upper()}, "
              f"release fades over {robot.release_ms}ms "
              "(both adjustable in the UI).")
    if args.robot_protocol == "legacy":
        # No limit switches on this build: HOME drives to the assumed origin
        # rather than seeking it, so "homed" is a promise the human makes,
        # not something the machine measured.
        #
        # Name the square the human can actually see. rig.PARK_SQUARE is the
        # firmware's idea of the origin; ORIGIN_SQUARE is which real corner
        # that is on this rig, and telling them the wrong one is how the
        # whole board ends up rotated.
        print(f"Robot: park the carriage in the corner of travel beyond "
              f"{rig.ORIGIN_SQUARE} before homing -- there are no limit switches "
              "to find it.")
        if rig.ORIGIN_SQUARE != rig.PARK_SQUARE:
            print(f"Robot: board origin {rig.ORIGIN_SQUARE} -- squares are rotated "
                  "before they reach the firmware.")
    return robot


def _make_builders(args, engine, buffer, session_ref, progress=None):
    """The per-mode stacks, as callables for Session.

    Both are closures rather than methods so session.py stays free of the
    camera stack entirely -- importing picamera2 or ncnn is what would stop
    --ai-vs-ai running off the Pi, and that import must not happen until
    normal mode is actually asked for.

    Each returns (loop, capture_stream, engine_controller, tick). Returns
    (build_normal, build_ai, build_guided, build_puzzle).
    """

    def on_update_factory(harvester=None):
        def on_update(matrix, move_text, frame, flagged, reason):
            buffer.set_board(matrix, move_text, flagged, reason)
            if harvester is not None and move_text is not None and not flagged:
                harvester.record(matrix, frame)
            session = session_ref()
            # An unresolvable settle right after the arm moved means the
            # physical board and the tracked position have diverged. Stop the
            # arm before it stacks another move on top.
            if flagged and session is not None and session.robot_controller is not None:
                session.robot_controller.note_flag(reason)
            # Just a poke -- the engine thinks on its own thread, since this
            # runs with the loop's lock held.
            if session is not None and session.engine_controller is not None:
                session.engine_controller.notify()

        return on_update

    def setting(settings, name, fallback):
        value = settings.get(name)
        return fallback if value is None else value

    def build_ai(settings):
        """No camera anywhere in this path: HeadlessLoop is the position, so
        the physical board must start in the standard 32-piece setup or
        everything after the first move is a lie. Nothing verifies that."""
        loop = HeadlessLoop(on_update=on_update_factory())
        buffer.set_board(loop.current_matrix, None, False, None)  # seed the UI
        controller = EngineController(
            loop, engine,
            think_s=setting(settings, "think", args.engine_think),
            robot=None,  # attached below, once Session has built it
            both_sides=True,
            move_delay_s=setting(settings, "move_delay", args.move_delay),
            noob=bool(setting(settings, "noob", True)),
        )
        return loop, None, controller, None

    def build_tracking():
        """The camera half every camera mode shares: (loop, stream, tick)."""
        # Imported here, not at module scope: picamera2 and the ncnn loader
        # are the reason this mode can't run off the Pi, and AI vs AI must not
        # pay for them.
        from capture import Camera, CaptureStream
        from harvest import CropHarvester
        from square_classifier import load_classifier
        from square_geometry import square_pixel_bboxes
        from tracking_loop import TrackingLoop

        calibration_matrix = load_calibration(args.calibration)
        classifier_model = load_classifier(str(args.classifier))

        camera = Camera()
        camera.open()
        stream = CaptureStream(camera)
        stream.start()
        frame = None
        while frame is None:
            frame, _timestamp = stream.get_latest()
        image_size = (frame.shape[1], frame.shape[0])

        harvester = None
        if args.harvest is not None:
            harvester = CropHarvester(
                args.harvest, square_pixel_bboxes(calibration_matrix, image_size)
            )

        loop = TrackingLoop(
            capture_stream=stream,
            calibration_matrix=calibration_matrix,
            image_size=image_size,
            classifier_model=classifier_model,
            on_update=on_update_factory(harvester),
            poll_interval=args.poll_interval,
            classifier_min_conf=args.min_conf,
            motion_thresh=args.motion_thresh,
        )
        buffer.set_board(loop.current_matrix, None, False, None)

        def tick():
            live_frame, _timestamp = stream.get_latest()
            if live_frame is not None:
                buffer.set_frame(live_frame)
            loop.tick()

        # CaptureStream.stop() is the teardown hook; Session calls .close().
        stream.close = lambda: (stream.stop(), camera.close())
        return loop, stream, tick

    def build_normal(settings, coach_on=False):
        loop, stream, tick = build_tracking()
        think = setting(settings, "think", args.engine_think)
        controller = EngineController(
            loop, engine,
            think_s=think,
            robot=None,
            both_sides=False,
            noob=bool(setting(settings, "noob", False)),
            # The coach thinks longer than the opponent: its answer is the one
            # the human learns from, and it runs at full strength regardless.
            coach_think_s=max(float(think), args.coach_think) if coach_on else None,
        )
        return loop, stream, controller, tick

    def build_guided(settings):
        return build_normal(settings, coach_on=True)

    def build_puzzle(settings):
        bank = puzzles.PuzzleBank.load(args.puzzles)
        loop, stream, tick = build_tracking()
        session = session_ref()
        controller = PuzzleController(
            loop, bank, progress or puzzles.PuzzleProgress.load(args.puzzle_progress),
            filters=_puzzle_filters(settings),
            robot=None,  # attached by Session once the RobotController exists
            graveyard=session.robot if session is not None else None,
        )
        return loop, stream, controller, tick

    return build_normal, build_ai, build_guided, build_puzzle


def main():
    args = parse_args()

    engine = ChessEngine(command=args.engine_command, skill=args.engine_skill)
    print("Engine: Stockfish ready." if engine.available else f"Engine: {engine.error}")

    robot = _open_robot(args) if args.robot else None
    if robot is not None and robot.stale_firmware:
        print("Robot: not homing -- reflash first.")
    elif robot is not None:
        # Once, here: the port stays open for the life of the process, so a
        # mode switch never costs a re-home. The human has already parked the
        # carriage -- this is where that promise is cashed in.
        print("Homing the gantry -- keep hands clear...")
        try:
            robot.home()
            print("Robot: homed and ready.")
        except GantryError as exc:
            print(f"Robot: {exc}")

    buffer = BoardBuffer()
    holder = {}
    progress = puzzles.PuzzleProgress.load(args.puzzle_progress)
    build_normal, build_ai, build_guided, build_puzzle = _make_builders(
        args, engine, buffer, lambda: holder.get("session"), progress=progress)

    session = Session(
        engine, robot=robot,
        calibration=args.calibration, classifier=args.classifier,
        build_normal=build_normal, build_ai=build_ai,
        build_guided=build_guided, build_puzzle=build_puzzle,
        puzzles=args.puzzles, puzzle_summary=progress.summary,
        poll_interval=args.poll_interval,
        defaults={
            "skill": args.engine_skill,
            "think": args.engine_think,
            "move_delay": args.move_delay,
            "noob": True if args.noob is None else args.noob,
        },
    )
    holder["session"] = session

    try:
        start_server(args.host, args.port, buffer, session)
        print(f"Serving at http://<this-pi>:{args.port}/")

        if args.ai_vs_ai:
            # The old command line still lands straight in the game.
            session.start(AI_VS_AI, {"noob": True if args.noob is None else args.noob,
                                     "move_delay": args.move_delay,
                                     "think": args.engine_think})
            print("Started AI vs AI. Set up all 32 pieces, then press play.")
        else:
            print("Open the page and pick a mode.")

        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("Stopped.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
