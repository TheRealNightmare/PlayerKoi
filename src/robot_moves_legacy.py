"""Turning a chess move into ChessBot-V1 commands.

The counterpart to robot_moves.py, and much shorter than it, because this
firmware does the hard part. robot_moves has to route a knight through the
gaps between pieces itself, which is where its clearance arithmetic and
lattice-line routing come from; ChessBot-V1 takes "KNIGHT b1c3" and weaves on
its own, with the magnet duties and edge folding already measured into it.

So what is left here is chess rules, and only chess rules:

    which squares move        MOVE vs KNIGHT, and castling's second command
    which colour is carried   the w|b suffix -- white magnets are reversed on
                              this set, so the coil polarity follows it
    what has to come off      captures, including en passant's offset victim
    which slot it goes to     the pile is host-side state; the firmware only
                              drives to the coordinate it is handed
    what a human must do      swap in a queen

Same Step type and same plan() signature as robot_moves, so Robot can take
either module as its planner.

CAPTURES

The V1 rig had no graveyard -- travel was exactly the 8x8, with nothing
outside it -- so a capture stopped the machine and a human lifted the piece
off before the attacker was dragged in. The V2 frame reaches a full square
past every edge, so the arm now parks the captured piece itself, on one of the
32 slots ringing the board (src/graveyard.py), with the firmware's BURY.

That removes the only blocking prompt in an ordinary move. The promotion
prompt stays, because no amount of travel lets the arm fetch a queen.

Two things about BURY are easy to get wrong and are handled in _bury():

  * the destination is a RAW MACHINE COORDINATE, not a square, because slots
    sit outside the 8x8 and have no name. It therefore needs rig.orient_mm(),
    not rig.orient() -- see the note there.
  * which slot depends on what is already in the pile, so plan() has to be
    told. It cannot work that out from `board`: python-chess knows a piece was
    captured, not where the arm put it.
"""

import chess

import graveyard
import rig
from robot_moves import _PIECE_WORDS, Step


def _describe(square, piece):
    name = chess.square_name(square)
    word = _PIECE_WORDS[piece.piece_type] if piece is not None else "piece"
    colour = "white" if piece is not None and piece.color == chess.WHITE else "black"
    return name, word, colour


def _bury(square, piece, slot, origin_square=None):
    """Lift the captured piece off `square` and park it on graveyard `slot`.

    Returns (Step, slot) so the caller can add the slot to the pile -- plan()
    is pure, and the pile belongs to whoever is running the game.

    The square in the command is rotated like every other square bound for the
    firmware. The COORDINATE is rotated too, but by orient_mm(), because a
    slot has no file/rank for orient() to flip. Both have to happen: rotating
    one and not the other sends the arm to the right slot from the wrong
    square, or the wrong slot from the right one.
    """
    name, word, colour = _describe(square, piece)
    x_mm, y_mm = rig.orient_mm(*rig.graveyard_slot_to_mm(slot), origin=origin_square)
    token = rig.colour_token(piece is not None and piece.color == chess.WHITE)
    return (
        Step(
            kind="command",
            command=f"BURY {rig.orient_square(name, origin_square)} "
                    f"{x_mm:.2f} {y_mm:.2f} {token}",
            note=f"park the captured {colour} {word} from {name} on slot {slot}",
        ),
        slot,
    )


def _full_graveyard_prompt(square, piece):
    """The fallback when all 32 slots are taken.

    It cannot happen in a legal game -- 30 pieces can be captured and there
    are 32 slots -- but plan() is also handed positions set up by hand from
    the web UI's correction flow, and a made-up position with a stale pile
    can reach it. Better a prompt than an exception mid-move.
    """
    name, word, colour = _describe(square, piece)
    return Step(
        kind="prompt",
        blocking=True,
        note=f"graveyard full -- waiting for the {colour} {word} on {name}",
        prompt=f"Graveyard is full. Take the {colour} {word.upper()} off {name}, "
               f"then confirm",
    )


def plan(board, move, topple_delay_s=None, origin_square=None, occupied=()):
    """Gantry commands to physically play `move` in `board` -- the position
    *before* the move, exactly like robot_moves.plan() and describe_move().

    Returns a list of Step. `topple_delay_s` is accepted and ignored: this
    firmware has no TOPPLE, and a capture is now driven to a graveyard slot
    rather than waited on. It stays in the signature so the two planners
    remain interchangeable.

    `origin_square` is which real square the carriage parks on; it rotates the
    squares in the emitted commands to match how the board is seated under the
    gantry. Defaults to rig.ORIGIN_SQUARE, the measured value for this machine.
    Only Step.command is rotated -- see the comment on the MOVE step.

    `occupied` is the graveyard slots already holding a piece. plan() cannot
    derive it: python-chess knows a piece was captured, not where the arm put
    it. Defaults to empty, which is right for a fresh game and for every
    caller that never captures.

    Use plan_with_slots() instead if you need to know which slot was used --
    this returns only the steps, so that the signature keeps matching
    robot_moves.plan().

    Raises ValueError if the move isn't legal here. Not defensive noise:
    is_capture/is_en_passant answer for the side to move, so a move for the
    wrong side yields a plan that buries the wrong piece.
    """
    return plan_with_slots(board, move, origin_square=origin_square,
                           occupied=occupied)[0]


def plan_with_slots(board, move, origin_square=None, occupied=()):
    """(steps, slots_used). The pile is the caller's to keep, so the slots a
    plan consumes have to come back out with it -- see Session."""
    if move not in board.legal_moves:
        raise ValueError(f"{move.uci()} is not legal in this position ({board.fen()})")

    steps = []
    pile = set(occupied)
    used = []

    # Captures first -- the destination has to be clear before anything is
    # dragged onto it. En passant's victim is BESIDE the destination, not on
    # it, which is the case hand-written rules get wrong, so ask python-chess
    # rather than reasoning about it here.
    victim = None
    if board.is_en_passant(move):
        victim = chess.square(
            chess.square_file(move.to_square), chess.square_rank(move.from_square)
        )
    elif board.is_capture(move):
        victim = move.to_square

    if victim is not None:
        # Nearest free slot to where the piece actually stands, so the drag
        # off the board is as short as it can be. Board space, not machine
        # space: _bury() applies the orientation.
        slot = graveyard.nearest_free_slot_for_square(
            chess.square_file(victim), chess.square_rank(victim), occupied=pile)
        if slot is None:
            steps.append(_full_graveyard_prompt(victim, board.piece_at(victim)))
        else:
            step, slot = _bury(victim, board.piece_at(victim), slot, origin_square)
            steps.append(step)
            pile.add(slot)
            used.append(slot)

    mover = board.piece_at(move.from_square)
    origin, word, _ = _describe(move.from_square, mover)
    target = chess.square_name(move.to_square)
    uci = origin + target

    # A knight can't take the straight diagonal between centres without
    # clipping whatever it's jumping, so it weaves along the gridlines
    # instead. The firmware owns that path; we just pick the verb.
    knight = mover is not None and mover.piece_type == chess.KNIGHT
    # Which colour the coil is about to pick up. The white pieces' magnets are
    # reversed on this set, so the firmware flips polarity on this token --
    # holding a white piece with the black polarity shoves it off the square.
    # Named apart from the `colour` the promotion branch below binds to a
    # word ("white"), so the two can never be confused if this file is
    # reordered -- one is a protocol token, the other is prose.
    polarity = rig.colour_token(mover is not None and mover.color == chess.WHITE)
    steps.append(
        Step(
            # The command is rotated into machine orientation; the note is
            # not. Step keeps the two apart precisely so the firmware and the
            # human can be told different things about the same move, and the
            # human is looking at a real board in real notation.
            f"{'KNIGHT' if knight else 'MOVE'} {rig.orient_uci(uci, origin_square)} {polarity}",
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
                # Same colour as the king, so the same polarity.
                f"KNIGHT {rig.orient_uci(rook_from + rook_to, origin_square)} {polarity}",
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

    return steps, used
