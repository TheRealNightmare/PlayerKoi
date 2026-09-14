"""Playing badly on purpose, and playing gently for the arm's sake.

A style layer over ChessEngine rather than part of it: engine.py's job is
talking UCI, and none of this is about that.

Two things are being asked for at once, and they pull in the same direction:

    look like a beginner     pawn-heavy, pieces barely developed
    spare the gantry         knight moves and castling are the only moves
                             that send a KNIGHT weave, which is the riskiest
                             motion the rig makes -- the piece rides offset
                             from the pole face at reduced magnet duty
                             (rig.MAG_DIAG), so it is the one most likely to
                             be dropped or dragged crooked

So knights and castling are excluded outright unless nothing else is legal,
and pawn moves are preferred among whatever the engine thinks is reasonable.

The pawn preference is deliberately soft. A hard "always a pawn if one is
legal" rule sounds like what a beginner does, but it isn't: it would march
all eight pawns down the board before a bishop ever moved, and the games stop
resembling chess at all. Picking the pawn move out of the engine's shortlist
keeps every move defensible while still being visibly pawn-happy.

Hence two engine calls per move, which is the real cost here:

    analyse()   ignores Skill Level -> an honest shortlist of good moves
    play()      honours Skill Level -> blunders within what we allowed

They cannot be collapsed into one call, because a single analyse() would play
too well and a single play() would give no shortlist to prefer a pawn from.
Budget roughly 2x --engine-think per move.
"""

import chess

DEFAULT_CANDIDATES = 5


def _is_weave(board, move):
    """Whether playing this move makes the arm weave along the gridlines
    rather than drag straight square-to-square."""
    piece = board.piece_at(move.from_square)
    if piece is not None and piece.piece_type == chess.KNIGHT:
        return True
    # Castling sends a second KNIGHT command for the rook, which has to weave
    # under the king that just landed between its squares.
    return board.is_castling(move)


def allowed_moves(board):
    """Legal moves with the weaving ones removed -- or every legal move, if
    removing them would leave nothing. "Only when forced" is exactly that
    fallback: a knight move that is the sole way out of check is still
    played."""
    gentle = [m for m in board.legal_moves if not _is_weave(board, m)]
    return gentle or list(board.legal_moves)


def _pawn_moves(board, moves):
    return [
        m for m in moves
        if (piece := board.piece_at(m.from_square)) is not None
        and piece.piece_type == chess.PAWN
    ]


def choose_move(engine, board, think_s, candidates=DEFAULT_CANDIDATES):
    """A beginner-ish, weave-averse move. Returns a chess.Move, or None when
    the engine is unavailable or the game is over -- same contract as
    ChessEngine.best_move, so the two are interchangeable as a policy."""
    if board.is_game_over():
        return None

    allowed = allowed_moves(board)
    if len(allowed) == 1:
        return allowed[0]  # no point spending two searches on a forced move

    shortlist = engine.top_moves(board, think_s, count=candidates, root_moves=allowed)
    # An engine with no MultiPV support gives nothing back; fall through to a
    # plain restricted search rather than losing the knight filter too.
    narrowed = _pawn_moves(board, shortlist) or shortlist or allowed

    move = engine.best_move(board, think_s, root_moves=narrowed)
    # Some builds ignore root_moves. Don't hand back a move we excluded --
    # the whole point is that the arm never sees it.
    if move is not None and move not in narrowed:
        return narrowed[0]
    return move
