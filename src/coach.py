"""The coach: what to play, how good the move you did play was, and why.

Pure python-chess over whatever ChessEngine.analyse() returned -- no threads,
no engine calls of its own -- so every rule here can be tested against a
fixed position with canned evaluations (tests/test_coach.py).

Three jobs, all phrased for someone standing at the board:

    hint()       the best move for the side to move, with where to put it
    review()     a quality label for the move just played, and what was better
    explain()    a short reason for a move -- the same rules for yours and for
                 the arm's

Move quality is judged the way Lichess does it: not on the raw centipawn loss,
which overstates mistakes in positions that are already won or lost, but on
the drop in *win chance*. Losing 300cp at +9 changes nothing; losing it at +1
throws the game.

explain() is deliberately rule-based rather than written by a language model:
it runs offline on the Pi, instantly, and says only things that are provably
true of the position. It will miss subtle ideas; it will not invent them.
"""

import math

import chess

from engine import _PIECE_WORDS, describe_move

BEST = "Best"
GOOD = "Good"
INACCURACY = "Inaccuracy"
MISTAKE = "Mistake"
BLUNDER = "Blunder"

# Win-chance drop, in percentage points, at which each label starts.
INACCURACY_DROP = 5.0
MISTAKE_DROP = 10.0
BLUNDER_DROP = 20.0

# The labels that hold the arm back and offer a take-back.
NEEDS_DECISION = (MISTAKE, BLUNDER)

# How many moves of the engine's line "Better was..." spells out.
LINE_PLIES = 4

PIECE_VALUES = {
    chess.PAWN: 1,
    chess.KNIGHT: 3,
    chess.BISHOP: 3,
    chess.ROOK: 5,
    chess.QUEEN: 9,
    chess.KING: 100,
}


def _color_word(color):
    return "white" if color == chess.WHITE else "black"


def win_percent(info):
    """White's chance of winning, 0-100, from an analyse() result.

    The Lichess curve: 50 + 50 * (2 / (1 + e^(-0.00368 * cp)) - 1). A forced
    mate is certainty either way.
    """
    if info.get("mate") is not None:
        return 100.0 if info["mate"] > 0 else 0.0
    cp = info.get("score_cp") or 0
    return 50.0 + 50.0 * (2.0 / (1.0 + math.exp(-0.00368 * cp)) - 1.0)


def classify(best_info, played_info, mover, played_is_best=False):
    """Label a move from the win chance it gave away.

    `best_info` is the evaluation of the position before the move (what best
    play keeps), `played_info` of the position after it; both from White's
    point of view, as ChessEngine.analyse returns them.
    """
    if played_is_best:
        return BEST
    before = win_percent(best_info)
    after = win_percent(played_info)
    if mover == chess.BLACK:
        before, after = 100.0 - before, 100.0 - after
    drop = before - after
    if drop >= BLUNDER_DROP:
        return BLUNDER
    if drop >= MISTAKE_DROP:
        return MISTAKE
    if drop >= INACCURACY_DROP:
        return INACCURACY
    return GOOD


def _piece_phrase(piece, square):
    return f"{_PIECE_WORDS[piece.piece_type]} on {chess.square_name(square)}"


def _mate_for(info, color):
    """Moves to mate if `info` says `color` is mating, else None."""
    if not info or info.get("mate") is None:
        return None
    mate = info["mate"]
    if color == chess.WHITE and mate > 0:
        return mate
    if color == chess.BLACK and mate < 0:
        return -mate
    return None


def explain(board, move, after_info=None):
    """A short, true description of what `move` does in `board` (the position
    before it). `after_info` -- the evaluation after the move -- adds "sets up
    mate" when it shows a forced mate for the mover.

    Returns one string of phrases joined by "; ".
    """
    mover = board.piece_at(move.from_square)
    if mover is None:
        return ""
    color = mover.color
    enemy = not color
    after = board.copy(stack=False)
    after.push(move)

    if after.is_checkmate():
        return "checkmate"

    phrases = []
    if board.is_castling(move):
        side = "kingside" if chess.square_file(move.to_square) > chess.square_file(move.from_square) else "queenside"
        phrases.append(f"castles {side}, tucking the king away")
    elif board.is_en_passant(move):
        phrases.append("captures the pawn en passant")
    else:
        victim = board.piece_at(move.to_square)
        if victim is not None:
            phrases.append(f"captures the {_color_word(victim.color)} {_PIECE_WORDS[victim.piece_type]}")

    if move.promotion is not None:
        phrases.append(f"promotes to a {_PIECE_WORDS[move.promotion]}")

    if after.is_check():
        phrases.append("gives check")

    home_rank = 0 if color == chess.WHITE else 7
    if (mover.piece_type in (chess.KNIGHT, chess.BISHOP)
            and chess.square_rank(move.from_square) == home_rank
            and board.piece_at(move.to_square) is None):
        phrases.append(f"develops the {_PIECE_WORDS[mover.piece_type]}")

    centre = (chess.D4, chess.E4, chess.D5, chess.E5)
    if mover.piece_type == chess.PAWN and move.to_square in centre and board.piece_at(move.to_square) is None:
        phrases.append("claims the centre")

    # New targets: enemy pieces the moved piece hits now and did not before,
    # kept only if they are worth something -- more valuable than the
    # attacker, or undefended. The king is left out; check says that already.
    moved = after.piece_at(move.to_square)
    before_hits = board.attacks(move.from_square)
    targets = []
    for square in after.attacks(move.to_square):
        target = after.piece_at(square)
        if target is None or target.color != enemy or target.piece_type == chess.KING:
            continue
        if square in before_hits:
            continue  # already under this piece's fire from where it stood
        valuable = PIECE_VALUES[target.piece_type] > PIECE_VALUES[moved.piece_type]
        undefended = not after.is_attacked_by(enemy, square)
        if valuable or undefended:
            targets.append((PIECE_VALUES[target.piece_type], target, square))
    targets.sort(key=lambda t: -t[0])
    gives_check = after.is_check()
    if len(targets) >= 2 or (targets and gives_check):
        names = [_piece_phrase(t, s) for _v, t, s in targets[:2]]
        if gives_check and len(targets) == 1:
            phrases.append(f"forks the king and the {names[0]}")
        else:
            phrases.append(f"forks the {names[0]} and the {names[1]}")
    elif targets:
        phrases.append(f"attacks the {_piece_phrase(targets[0][1], targets[0][2])}")

    # En prise: the piece just moved can be taken for free, or taken by
    # something cheaper than itself.
    if moved.piece_type != chess.KING and after.is_attacked_by(enemy, move.to_square):
        attackers = [after.piece_at(sq) for sq in after.attackers(enemy, move.to_square)]
        cheapest = min(PIECE_VALUES[p.piece_type] for p in attackers)
        defended = after.is_attacked_by(color, move.to_square)
        if not defended or cheapest < PIECE_VALUES[moved.piece_type]:
            lead = "but leaves" if phrases else "leaves"
            phrases.append(f"{lead} the {_PIECE_WORDS[moved.piece_type]} on "
                           f"{chess.square_name(move.to_square)} hanging")

    mate_in = _mate_for(after_info, color)
    if mate_in is not None:
        phrases.append(f"sets up mate in {mate_in}")

    return "; ".join(phrases) if phrases else f"a quiet {_PIECE_WORDS[mover.piece_type]} move"


def line_san(board, moves, limit=LINE_PLIES):
    """The first `limit` moves of a line, as SAN from `board`."""
    scratch = board.copy(stack=False)
    sans = []
    for move in moves[:limit]:
        if move not in scratch.legal_moves:
            break
        sans.append(scratch.san(move))
        scratch.push(move)
    return sans


def hint(board, info):
    """The coach's suggestion for the side to move, or None without a line.

    Returns {uci, san, text, headline, extra}: headline/extra are
    describe_move's physical instructions, so the hint says where to put the
    piece as well as what it achieves.
    """
    if not info or not info.get("pv"):
        return None
    best = info["pv"][0]
    if best not in board.legal_moves:
        return None
    headline, extra = describe_move(board, best)
    text = explain(board, best)
    mate_in = _mate_for(info, board.turn)
    if mate_in is not None and text != "checkmate":
        text = f"{text}; mate in {mate_in}"
    return {
        "uci": best.uci(),
        "san": board.san(best),
        "text": text,
        "headline": headline,
        "extra": extra,
    }


def _mates(board, move):
    scratch = board.copy(stack=False)
    scratch.push(move)
    return scratch.is_checkmate()


def review(board, played, best_info, after_info):
    """Judge `played` in `board` (the position before it).

    `best_info` is the analysis of `board` -- its pv[0] is the best move --
    and `after_info` the analysis after `played`. Returns {san, label,
    better_san, better_line, text}; better_* are None when the move was the
    engine's own choice.
    """
    mover = board.turn
    best = best_info["pv"][0] if best_info and best_info.get("pv") else None
    played_is_best = best is not None and best == played
    if played_is_best:
        label = BEST
    elif best_info and after_info:
        label = classify(best_info, after_info, mover)
    elif after_info is None and _mates(board, played):
        label = BEST  # analyse() has nothing to say about a finished game
    else:
        label = GOOD
    better_san = better_line = None
    if best is not None and not played_is_best and label != GOOD and best in board.legal_moves:
        better_san = board.san(best)
        better_line = line_san(board, best_info["pv"])
    return {
        "san": board.san(played),
        "uci": played.uci(),
        "label": label,
        "better_san": better_san,
        "better_line": better_line,
        "text": explain(board, played, after_info),
    }


def takeback_instruction(board, move):
    """How to physically undo `move`, given `board` -- the position *before*
    it, i.e. after the software undo. The reverse of describe_move: the piece
    goes home, and anything it took comes back."""
    piece = board.piece_at(move.from_square)
    origin = chess.square_name(move.from_square)
    target = chess.square_name(move.to_square)
    word = _PIECE_WORDS[piece.piece_type] if piece else "piece"
    steps = []
    if move.promotion is not None:
        steps.append(f"take the {_PIECE_WORDS[move.promotion]} off {target} and put the pawn back on {origin}")
    else:
        steps.append(f"put the {word} back {target} → {origin}")

    if board.is_castling(move):
        kingside = chess.square_file(move.to_square) > chess.square_file(move.from_square)
        rank = chess.square_rank(move.from_square)
        rook_from = chess.square_name(chess.square(7 if kingside else 0, rank))
        rook_to = chess.square_name(chess.square(5 if kingside else 3, rank))
        steps.append(f"and the rook {rook_to} → {rook_from}")
    elif board.is_en_passant(move):
        captured = chess.square(chess.square_file(move.to_square), chess.square_rank(move.from_square))
        steps.append(f"and return the {_color_word(not board.turn)} pawn to {chess.square_name(captured)}")
    else:
        victim = board.piece_at(move.to_square)
        if victim is not None:
            steps.append(f"and return the {_color_word(victim.color)} "
                         f"{_PIECE_WORDS[victim.piece_type]} to {target}")
    return " ".join(steps)
