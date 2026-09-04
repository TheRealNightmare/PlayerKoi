"""Stockfish opponent, and the physical instructions for playing its moves.

The engine plays Black. It reads the live position straight off
MoveResolver's internal chess.Board (see TrackingLoop.board_copy), so
there's no separate game state to keep in sync.

describe_move() is the part that matters at the board: a bare "e8 g8"
isn't enough to actually play a move by hand, because castling also moves
a rook, en passant removes a pawn that isn't on the destination square,
and promotion needs a different piece put down. Every one of those gets
spelled out.
"""

import chess

SKILL_MIN = 0
SKILL_MAX = 20
DEFAULT_SKILL = 3
DEFAULT_THINK_S = 0.5

_PIECE_WORDS = {
    chess.PAWN: "pawn",
    chess.KNIGHT: "knight",
    chess.BISHOP: "bishop",
    chess.ROOK: "rook",
    chess.QUEEN: "queen",
    chess.KING: "king",
}


def describe_move(board, move):
    """Physical instructions for playing `move` in `board` (the position
    *before* the move). Returns (headline, extra) -- headline is the
    from/to to show large, extra is any additional physical action, or
    None. Pure python-chess; no engine needed."""
    origin = chess.square_name(move.from_square)
    target = chess.square_name(move.to_square)
    headline = f"{origin} → {target}"
    mover = board.piece_at(move.from_square)
    color_word = "white" if mover is not None and mover.color == chess.WHITE else "black"

    if board.is_castling(move):
        # The rook's squares aren't in the move at all, so they have to be
        # named explicitly or the position ends up wrong.
        kingside = chess.square_file(move.to_square) > chess.square_file(move.from_square)
        rank = chess.square_rank(move.from_square)
        rook_from = chess.square_name(chess.square(7 if kingside else 0, rank))
        rook_to = chess.square_name(chess.square(5 if kingside else 3, rank))
        side = "kingside" if kingside else "queenside"
        return headline, f"castling {side} — ALSO move the rook {rook_from} → {rook_to}"

    if board.is_en_passant(move):
        # The captured pawn sits beside the destination, not on it.
        captured = chess.square(chess.square_file(move.to_square), chess.square_rank(move.from_square))
        return headline, f"en passant — ALSO remove the pawn on {chess.square_name(captured)}"

    extras = []
    victim = board.piece_at(move.to_square)
    if victim is not None:
        victim_color = "white" if victim.color == chess.WHITE else "black"
        extras.append(f"capture — remove the {victim_color} {_PIECE_WORDS[victim.piece_type]} on {target} first")
    if move.promotion is not None:
        extras.append(f"promotion — replace it with a {color_word} {_PIECE_WORDS[move.promotion]}")

    return headline, "; ".join(extras) if extras else None


class ChessEngine:
    """Stockfish over UCI. Never raises on a missing binary -- `available`
    goes False instead, so the web UI keeps working as a plain tracker."""

    def __init__(self, command="stockfish", skill=DEFAULT_SKILL):
        self._engine = None
        self.error = None
        self._skill = skill
        try:
            import chess.engine

            self._engine = chess.engine.SimpleEngine.popen_uci(command)
        except Exception as exc:  # binary missing, not executable, bad UCI handshake
            self.error = f"{command} not available ({exc.__class__.__name__}) -- try: sudo apt install stockfish"
            return
        self.set_skill(skill)

    @property
    def available(self):
        return self._engine is not None

    @property
    def skill(self):
        return self._skill

    def set_skill(self, skill):
        self._skill = max(SKILL_MIN, min(SKILL_MAX, int(skill)))
        if self._engine is not None:
            try:
                self._engine.configure({"Skill Level": self._skill})
            except Exception:
                pass  # some builds name it differently; strength just stays default

    def best_move(self, board, think_s=DEFAULT_THINK_S):
        """Returns a chess.Move, or None if unavailable or the position has
        no legal moves (checkmate/stalemate)."""
        if self._engine is None or board.is_game_over():
            return None
        import chess.engine

        result = self._engine.play(board, chess.engine.Limit(time=think_s))
        return result.move

    def close(self):
        if self._engine is not None:
            try:
                self._engine.quit()
            except Exception:
                pass
            self._engine = None
