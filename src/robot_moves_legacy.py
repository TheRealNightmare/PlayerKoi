"""Turning a chess move into ChessBot-V1 commands.

The counterpart to robot_moves.py, and much shorter than it, because this
firmware does the hard part. robot_moves has to route a knight through the
gaps between pieces itself, which is where its clearance arithmetic and
lattice-line routing come from; ChessBot-V1 takes "KNIGHT b1c3" and weaves on
its own, with the magnet duties and edge folding already measured into it.

So what is left here is chess rules, and only chess rules:

    which squares move        MOVE vs KNIGHT, and castling's second command
    what has to come off      captures, including en passant's offset victim
    what a human must do      remove a captured piece, swap in a queen

Same Step type and same plan() signature as robot_moves, so Robot can take
either module as its planner.

Captures are handled the way RunChess handles them: there is no graveyard --
the gantry's travel is exactly the 8x8 board, with nothing outside it -- and
no topple, so a human lifts the captured piece off before the attacker is
dragged in. That makes the prompt BLOCKING, unlike the promotion prompt which
is merely advisory and comes last.
"""

import chess

import rig
from robot_moves import _PIECE_WORDS, Step


def _describe(square, piece):
    name = chess.square_name(square)
    word = _PIECE_WORDS[piece.piece_type] if piece is not None else "piece"
    colour = "white" if piece is not None and piece.color == chess.WHITE else "black"
    return name, word, colour


def _remove_prompt(square, piece):
    """A blocking prompt: nothing else happens until a human confirms the
    captured piece is off the board. Dragging the attacker onto an occupied
    square would just shove two pieces around."""
    name, word, colour = _describe(square, piece)
    return Step(
        kind="prompt",
        blocking=True,
        note=f"waiting for the {colour} {word} on {name} to be removed",
        prompt=f"Take the {colour} {word.upper()} off {name}, then confirm",
    )


def plan(board, move, topple_delay_s=None, origin_square=None):
    """Gantry commands to physically play `move` in `board` -- the position
    *before* the move, exactly like robot_moves.plan() and describe_move().

    Returns a list of Step. `topple_delay_s` is accepted and ignored: this
    firmware has no TOPPLE, and the capture wait is a human confirmation
    rather than a fixed delay. It stays in the signature so the two planners
    remain interchangeable.

    `origin_square` is which real square the carriage parks on; it rotates the
    squares in the emitted commands to match how the board is seated under the
    gantry. Defaults to rig.ORIGIN_SQUARE, the measured value for this machine.
    Only Step.command is rotated -- see the comment on the MOVE step.

    Raises ValueError if the move isn't legal here. Not defensive noise:
    is_capture/is_en_passant answer for the side to move, so a move for the
    wrong side yields a plan that asks a human to remove the wrong piece.
    """
    if move not in board.legal_moves:
        raise ValueError(f"{move.uci()} is not legal in this position ({board.fen()})")

    steps = []

    # Captures first -- the destination has to be clear before anything is
    # dragged onto it. En passant's victim is BESIDE the destination, not on
    # it, which is the case hand-written rules get wrong, so ask python-chess
    # rather than reasoning about it here.
    if board.is_en_passant(move):
        victim = chess.square(
            chess.square_file(move.to_square), chess.square_rank(move.from_square)
        )
        steps.append(_remove_prompt(victim, board.piece_at(victim)))
    elif board.is_capture(move):
        steps.append(_remove_prompt(move.to_square, board.piece_at(move.to_square)))

    mover = board.piece_at(move.from_square)
    origin, word, _ = _describe(move.from_square, mover)
    target = chess.square_name(move.to_square)
    uci = origin + target

    # A knight can't take the straight diagonal between centres without
    # clipping whatever it's jumping, so it weaves along the gridlines
    # instead. The firmware owns that path; we just pick the verb.
    knight = mover is not None and mover.piece_type == chess.KNIGHT
    steps.append(
        Step(
            # The command is rotated into machine orientation; the note is
            # not. Step keeps the two apart precisely so the firmware and the
            # human can be told different things about the same move, and the
            # human is looking at a real board in real notation.
            f"{'KNIGHT' if knight else 'MOVE'} {rig.orient_uci(uci, origin_square)}",
            f"{word} {origin} to {target}",
        )
    )

    if board.is_castling(move):
        # The rook's squares aren't in the move at all, and it has to pass
        # under the king that just landed between them -- so it always
        # weaves, never drags straight.
        kingside = chess.square_file(move.to_square) > chess.square_file(move.from_square)
        rank = chess.square_rank(move.from_square)
        rook_from = chess.square_name(chess.square(7 if kingside else 0, rank))
        rook_to = chess.square_name(chess.square(5 if kingside else 3, rank))
        steps.append(
            Step(
                f"KNIGHT {rig.orient_uci(rook_from + rook_to, origin_square)}",
                f"rook {rook_from} to {rook_to}, weaving past the king",
            )
        )

    if move.promotion is not None:
        # The arm can't fetch a queen from the box, and vision can't tell a
        # queen from a pawn anyway (it reads empty/white/black only), so
        # internal state already records the promotion -- the physical board
        # just has to be made to match. Advisory, not blocking: the move is
        # already complete and the next one can't start until the camera
        # settles regardless.
        _, _, colour = _describe(move.from_square, mover)
        new_word = _PIECE_WORDS[move.promotion]
        steps.append(
            Step(
                kind="prompt",
                note=f"replace the {colour} pawn on {target} with a {new_word}",
                prompt=f"Replace the {colour} pawn on {target} with a {new_word.upper()}",
            )
        )

    return steps
