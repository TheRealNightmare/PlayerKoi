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
from pathlib import Path
from threading import Event, Lock, Thread

import chess
import cv2

from board_state import load_calibration, matrix_to_fen_placement
from engine import DEFAULT_SKILL, DEFAULT_THINK_S, ChessEngine, describe_move
from headless_loop import HeadlessLoop, NullStream
import move_policy
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

    def __init__(self, loop, engine, think_s=DEFAULT_THINK_S, robot=None,
                 both_sides=False, move_delay_s=0.0, noob=False):
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
        with self._lock:
            if not self._enabled or not self._engine.available:
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


def start_server(host, port, buffer, session):
    """HTTP server that reads `session` live rather than closing over one
    mode's objects, so a mode switch is visible to the very next request.

    Every game route answers 409 when nothing is running: the menu is a real
    state, not a degenerate game, and a stale tab polling /board.json after a
    stop must get a clear answer rather than an exception.
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path, _, query = self.path.partition("?")
            if path == "/" or path in ("/index.html",):
                self._send_asset("index.html")
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
                        "engine": engine_controller.state() if engine_controller else None,
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
                     "/robot", "/mode", "/mode/stop", "/reset")
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

            if path == "/engine":
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
            self._send_json(200, {"ok": True})

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
    if args.white_polarity:
        rig_config.save(args.white_polarity)
    try:
        robot = open_gantry(args.robot, topple_delay_s=args.topple_delay,
                            protocol=args.robot_protocol,
                            white_polarity=args.white_polarity)
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
        print(f"Robot: white pieces are held by {robot.white_polarity.upper()} "
              "(change it in the UI, or with --white-polarity).")
    if args.robot_protocol == "legacy":
        # No limit switches on this build: HOME drives to the assumed origin
        # rather than seeking it, so "homed" is a promise the human makes,
        # not something the machine measured.
        #
        # Name the square the human can actually see. rig.PARK_SQUARE is the
        # firmware's idea of the origin; ORIGIN_SQUARE is which real corner
        # that is on this rig, and telling them the wrong one is how the
        # whole board ends up rotated.
        print(f"Robot: park the carriage on {rig.ORIGIN_SQUARE} before homing -- "
              "there are no limit switches to find it.")
        if rig.ORIGIN_SQUARE != rig.PARK_SQUARE:
            print(f"Robot: board origin {rig.ORIGIN_SQUARE} -- squares are rotated "
                  "before they reach the firmware.")
    return robot


def _make_builders(args, engine, buffer, session_ref):
    """The two per-mode stacks, as callables for Session.

    Both are closures rather than methods so session.py stays free of the
    camera stack entirely -- importing picamera2 or ncnn is what would stop
    --ai-vs-ai running off the Pi, and that import must not happen until
    normal mode is actually asked for.

    Each returns (loop, capture_stream, engine_controller, tick).
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

    def build_normal(settings):
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
        controller = EngineController(
            loop, engine,
            think_s=setting(settings, "think", args.engine_think),
            robot=None,
            both_sides=False,
            noob=bool(setting(settings, "noob", False)),
        )

        def tick():
            live_frame, _timestamp = stream.get_latest()
            if live_frame is not None:
                buffer.set_frame(live_frame)
            loop.tick()

        # CaptureStream.stop() is the teardown hook; Session calls .close().
        stream.close = lambda: (stream.stop(), camera.close())
        return loop, stream, controller, tick

    return build_normal, build_ai


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
    build_normal, build_ai = _make_builders(args, engine, buffer, lambda: holder.get("session"))

    session = Session(
        engine, robot=robot,
        calibration=args.calibration, classifier=args.classifier,
        build_normal=build_normal, build_ai=build_ai,
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
