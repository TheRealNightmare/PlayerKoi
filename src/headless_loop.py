"""The game state, without a camera.

A drop-in stand-in for TrackingLoop for the AI-vs-AI mode: same surface, but
the board is *known* rather than observed. That works because of the same
argument MoveResolver rests on -- the physical board starts at the standard
position and every move since has been a legal move the software chose -- with
one extra step. In AI vs AI nothing but the gantry ever touches the board, so
there is no human move to read back, and the position can simply be pushed.

The substitution that makes the rest of the stack work unchanged is
force_settle(): RobotController calls it after the arm parks to make vision
confirm what it just did (robot.py). Here there is nothing to confirm against,
so it commits the expected move instead. That is the honest version of what
this mode is: the software's position is trusted, and a slipped belt or a
knocked-over piece will NOT be caught. Hence --move-delay and the Halt button.

What is deliberately kept:

* expected_move is still armed before the arm moves and still required here.
  A force_settle() with nothing pending returns False, so RobotController's
  fail-closed path (robot.py:425) still fires rather than silently passing.
* set_paused/tick stay as no-ops rather than being removed, so the pause
  keep-alive thread in RobotController needs no special-casing.
"""

from threading import Lock

from move_resolver import MoveResolver, matrix_from_board, standard_starting_matrix


class NullStream:
    """A CaptureStream that never has a frame. Lets web_ui's /undo and
    /correct handlers keep passing a frame through without knowing whether a
    camera exists."""

    def get_latest(self):
        return None, None


class HeadlessLoop:
    """Owns a chess.Board and publishes it. No camera, no classifier.

    on_update(matrix, move_text, frame, flagged, reason) is called with the
    same signature TrackingLoop uses -- frame is always None here -- so
    web_ui's callback is shared between the two modes.
    """

    def __init__(self, on_update=None):
        self._lock = Lock()
        # Same ownership as TrackingLoop: the resolver owns the board, so
        # reset() and resync() are shared rather than reimplemented here.
        self._resolver = MoveResolver()
        self._matrix = standard_starting_matrix()
        self._expected_move = None
        self._paused = False
        self._on_update = on_update or (lambda *args: None)

    # -- read-only state, as TrackingLoop exposes it ----------------------

    @property
    def board_copy(self):
        with self._lock:
            return self._resolver.board.copy()

    @property
    def current_matrix(self):
        with self._lock:
            return [row[:] for row in self._matrix]

    @property
    def turn(self):
        with self._lock:
            return self._resolver.turn

    @property
    def expected_move(self):
        with self._lock:
            return self._expected_move

    @property
    def is_paused(self):
        with self._lock:
            return self._paused

    # -- control ---------------------------------------------------------

    def set_expected_move(self, move):
        with self._lock:
            self._expected_move = move

    def set_paused(self, paused, lapse_s=None):
        """Recorded so the UI can show it, but nothing is gated on it: there
        is no tracking to hold still. lapse_s is accepted and ignored for the
        same reason -- an unattended pause here costs nothing."""
        with self._lock:
            self._paused = bool(paused)

    def tick(self):
        """Nothing to poll."""

    def force_settle(self, frame=None):
        """Commits the move the arm has just finished making.

        Returns False -- refusing to advance -- when no move was armed, which
        keeps RobotController's "the arm moved and nothing followed it" check
        meaningful instead of rubber-stamping every execute().
        """
        with self._lock:
            board = self._resolver.board
            move = self._expected_move
            if move is None or move not in board.legal_moves:
                return False
            san = board.san(move)
            board.push(move)
            self._matrix = matrix_from_board(board)
            self._expected_move = None
            matrix = [row[:] for row in self._matrix]
        self._on_update(matrix, san, None, False, None)
        return True

    def reset(self):
        with self._lock:
            self._resolver.reset()
            self._matrix = standard_starting_matrix()
            self._expected_move = None
            matrix = [row[:] for row in self._matrix]
        self._on_update(matrix, None, None, False, None)

    def undo_last_move(self, frame=None):
        """Reverts the last move. The physical board will NOT follow -- a
        human has to put the piece back by hand, which is why this is a
        UI-only button and never called from the engine thread."""
        with self._lock:
            board = self._resolver.board
            if not board.move_stack:
                return None
            move = board.pop()
            san = board.san(move)  # legal again in the position before it
            self._matrix = matrix_from_board(board)
            self._expected_move = None  # position changed; the engine must re-think
            matrix = [row[:] for row in self._matrix]
        self._on_update(matrix, None, None, False, None)
        return san

    def apply_manual_correction(self, matrix, turn, frame=None):
        """Adopts a position typed into the web UI's board editor.
        MoveResolver.resync() does the work, including re-inferring castling
        rights from the placement. The move history is gone afterwards, so
        undo stops working until the next move."""
        with self._lock:
            self._resolver.resync(matrix, turn=turn)
            self._matrix = matrix_from_board(self._resolver.board)
            self._expected_move = None
            published = [row[:] for row in self._matrix]
        self._on_update(published, None, None, False, None)
