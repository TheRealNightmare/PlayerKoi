"""Legal-move resolution via python-chess.

Wraps a chess.Board() seeded at the standard starting position -- the
tracker's own maintained state already guarantees the internal board starts
there, so there's no need to derive the start state from vision -- and
resolves each settle event to a single legal move.

resolve_from_deltas() is the hot path (see tracking_loop.py):
square_classifier.py only ever tells us which squares' *occupancy/color*
(empty/white/black) changed, never full piece type, so matching happens
against that lighter signal rather than a full-board FEN comparison. The
piece type of whatever moved is never read from vision at all -- it comes
from this resolver's own already-correct internal board state, which is
exactly the redesign's central idea: since the tracker knows the full
starting position and has applied every legal move since, it always
knows piece identity in software.

resolve() (full-matrix FEN matching) is kept as a standalone utility -- it's
no longer called from the hot path, but is still the simplest way to
validate a complete proposed board state if one is ever available (e.g. in
tests).
"""

import chess

from board_state import BOARD_SIZE, matrix_to_fen_placement

_PIECE_NAMES = {
    chess.PAWN: "pawn",
    chess.KNIGHT: "knight",
    chess.BISHOP: "bishop",
    chess.ROOK: "rook",
    chess.QUEEN: "queen",
    chess.KING: "king",
}


def _color_name(color):
    return "white" if color == chess.WHITE else "black"


def standard_starting_matrix():
    """Returns board_state's matrix[rank_idx][file_idx] representation of
    the standard chess starting position -- used to seed a tracker without
    needing vision to derive it, since the physical board is guaranteed to
    start there (see the product's own Setup Verification step)."""
    matrix = [[None] * BOARD_SIZE for _ in range(BOARD_SIZE)]
    for square, piece in chess.Board().piece_map().items():
        file_idx = chess.square_file(square)
        rank_idx = chess.square_rank(square)
        matrix[rank_idx][file_idx] = f"{_color_name(piece.color)}-{_PIECE_NAMES[piece.piece_type]}"
    return matrix


class MoveResolver:
    def __init__(self):
        self.board = chess.Board()

    @property
    def turn(self):
        return _color_name(self.board.turn)

    def resolve(self, proposed_matrix):
        """proposed_matrix is a full 8x8 matrix (board_state's
        matrix[rank_idx][file_idx] convention) representing a complete
        proposed board state (full piece type + color, not just
        occupancy/color).

        Tries every legal move from the current position; if exactly one
        produces this placement, plays it for real and returns
        (san, move). Returns (None, None) if zero or more than one legal
        move matches. Not used by the live tracking loop (see
        resolve_from_deltas) -- kept as a standalone utility for anywhere a
        complete proposed board state is available, e.g. tests.
        """
        target_placement = matrix_to_fen_placement(proposed_matrix)

        matches = []
        for move in self.board.legal_moves:
            self.board.push(move)
            if self.board.board_fen() == target_placement:
                matches.append(move)
            self.board.pop()

        if len(matches) != 1:
            return None, None

        move = matches[0]
        san = self.board.san(move)
        self.board.push(move)
        return san, move

    def _expected_delta(self, move):
        """Returns {(file_idx, rank_idx): "white"|"black"|"empty"} for
        every square whose occupancy/color would change if `move` were
        played -- derived by diffing python-chess's own piece_map() before
        and after a scratch push(), rather than hand-written per-move-type
        rules. This gets captures, castling (king + rook, 4 squares), and
        en passant (the captured pawn's square, which is neither the
        mover's `from` nor `to`) exactly right for free, since push()
        already implements chess's special-move side effects correctly.
        """
        scratch = self.board.copy(stack=False)
        before = scratch.piece_map()
        scratch.push(move)
        after = scratch.piece_map()

        delta = {}
        for square in set(before) | set(after):
            before_color = _color_name(before[square].color) if square in before else "empty"
            after_color = _color_name(after[square].color) if square in after else "empty"
            if before_color != after_color:
                key = (chess.square_file(square), chess.square_rank(square))
                delta[key] = after_color
        return delta

    def resolve_from_deltas(self, observed_deltas):
        """observed_deltas: {(file_idx, rank_idx): "white"|"black"|"empty"}
        for every square whose confirmed occupancy/color (as classified by
        square_classifier.py) differs from this resolver's own tracked
        state. Never full piece type -- vision doesn't supply that under
        this design (see module docstring).

        Enumerates every legal move and computes its expected delta via
        _expected_delta(). Promotion is the one case color alone can't
        disambiguate (e7e8=Q/R/B/N all produce an identical delta), so
        candidates are grouped by (from, to, is_en_passant, is_castling)
        and each group is collapsed to its queen-promotion member before
        matching, per the product decision to always assume queen
        promotion.

        Accepts the unique legal move (after collapsing) whose expected
        delta exactly matches observed_deltas -- same squares, same
        resulting colors. Returns (None, None, None) on zero or multiple
        matches: ambiguous or unexplained, the caller must not guess and
        should flag this for manual correction instead.

        On a match, plays the move for real and returns (san, move,
        patch), where patch is {(file_idx, rank_idx): "{color}-{piece}" |
        None} for exactly the delta squares -- built by reading this
        resolver's own board *after* the push, so the mover's piece type
        comes from the resolver's already-correct internal state, never
        from vision.
        """
        groups = {}
        for move in self.board.legal_moves:
            key = (move.from_square, move.to_square, self.board.is_en_passant(move), self.board.is_castling(move))
            if key not in groups or move.promotion == chess.QUEEN:
                groups[key] = move

        matches = [move for move in groups.values() if self._expected_delta(move) == observed_deltas]
        if len(matches) != 1:
            return None, None, None

        move = matches[0]
        san = self.board.san(move)
        self.board.push(move)

        patch = {}
        for file_idx, rank_idx in observed_deltas:
            piece = self.board.piece_at(chess.square(file_idx, rank_idx))
            patch[(file_idx, rank_idx)] = f"{_color_name(piece.color)}-{_PIECE_NAMES[piece.piece_type]}" if piece else None

        return san, move, patch

    def resync(self, matrix, turn=None):
        """Forces the resolver's internal board to match a manually
        corrected matrix (the sole recovery path once resolve_from_deltas
        can't find a unique match -- there's no automatic ML rescan in
        this design, see tracking_loop.py; the web UI surfaces a
        correction affordance to the user instead).

        Castling rights are inferred from the corrected matrix itself: a
        right survives only if the relevant king and rook are still on
        their home squares. This can occasionally be wrong (can't
        distinguish "never moved" from "moved away and back"), but is
        strictly better than always discarding rights, since manual
        correction is now an everyday recovery path rather than a rare
        last resort. The en-passant target is always cleared regardless
        (low-stakes -- affects at most one ply).

        `turn` ("white" or "black") overrides whoever's turn the resolver
        otherwise assumed; defaults to carrying over the current turn.
        """
        placement = matrix_to_fen_placement(matrix)
        current_turn = "white" if self.board.turn == chess.WHITE else "black"
        turn_char = "w" if (turn or current_turn) == "white" else "b"

        castling = ""
        if matrix[0][4] == "white-king" and matrix[0][7] == "white-rook":
            castling += "K"
        if matrix[0][4] == "white-king" and matrix[0][0] == "white-rook":
            castling += "Q"
        if matrix[7][4] == "black-king" and matrix[7][7] == "black-rook":
            castling += "k"
        if matrix[7][4] == "black-king" and matrix[7][0] == "black-rook":
            castling += "q"

        self.board = chess.Board(f"{placement} {turn_char} {castling or '-'} - 0 1")

    def reset(self):
        self.board = chess.Board()
