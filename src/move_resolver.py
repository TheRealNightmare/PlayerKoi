"""Legal-move resolution via python-chess.

Wraps a chess.Board() seeded at the standard starting position -- the
product's own "Setup Verification" step already guarantees the physical
board starts there, so there's no need to derive the start state from
vision -- and matches a proposed board update against every legal move.
This is the "Move Validated" step described in the project's own design
("the board is matched against every legal move to identify the ply"),
which didn't exist in the code before this module: previously
board_state.py / web_ui.py only diffed two raw matrices into a
human-readable description, with no legality checking or turn tracking.
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


def standard_starting_matrix():
    """Returns board_state's matrix[rank_idx][file_idx] representation of
    the standard chess starting position -- used to seed a tracker without
    needing vision to derive it, since the physical board is guaranteed to
    start there (see the product's own Setup Verification step)."""
    matrix = [[None] * BOARD_SIZE for _ in range(BOARD_SIZE)]
    for square, piece in chess.Board().piece_map().items():
        file_idx = chess.square_file(square)
        rank_idx = chess.square_rank(square)
        color = "white" if piece.color == chess.WHITE else "black"
        matrix[rank_idx][file_idx] = f"{color}-{_PIECE_NAMES[piece.piece_type]}"
    return matrix


class MoveResolver:
    def __init__(self):
        self.board = chess.Board()

    def resolve(self, proposed_matrix):
        """proposed_matrix is a full 8x8 matrix (board_state's
        matrix[rank_idx][file_idx] convention) representing what the
        camera currently believes the board looks like -- typically the
        current stable matrix with roi_diff's changed squares patched in
        from square_classifier's output.

        Tries every legal move from the current position; if exactly one
        produces this placement, plays it for real and returns
        (san, move). Returns (None, None) if zero or more than one legal
        move matches -- ambiguous, the caller should fall back to a full
        rescan rather than guess.
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

    def resync(self, matrix):
        """Forces the resolver's internal board to match a freshly
        rescanned matrix (used after a full-YOLO fallback re-derives raw
        occupancy rather than a legal-move match). Whoever's turn it was
        carries over unchanged, but castling rights and the en-passant
        target can't be recovered from a bare snapshot and reset to
        defaults -- a known, accepted limitation. Callers should surface
        "board re-synced, special-move rights reset" to the user rather
        than claiming full FEN fidelity.
        """
        placement = matrix_to_fen_placement(matrix)
        turn = "w" if self.board.turn == chess.WHITE else "b"
        self.board = chess.Board(f"{placement} {turn} - - 0 1")

    def reset(self):
        self.board = chess.Board()
