"""Turning a chess move into gantry commands.

This is the whole "brain" of the robot arm, and it deliberately contains no
serial code, no hardware and no threads -- it's a pure function from
(position, move) to a list of steps, so the interesting part (routing a
knight through the gaps, toppling the right pawn on en passant, getting the
castling rook past the king) is unit-testable off the Pi with nothing but
python-chess. See tests/test_robot_moves.py.

Coordinates match the firmware: x = file (0 = a .. 7 = h), y = rank
(0 = rank 1 .. 7 = rank 8), and fractions are legal -- 3.5 is the lattice
line between files d and e. Routing pieces along those coordinates, through
the gaps between squares rather than over their centres, is what lets a
knight cross an occupied board without shoving anything.

CLEARANCE, and why the routing line moves around

On this board a square is 30mm, so half a square -- the midline of a gap --
is only 15mm from the centre of the piece on either side of it. Against
13-15mm piece bases that leaves between 2mm and nothing, and the 25mm magnet
is worse still: its pole face reaches to within 2.5mm of a flanking piece's
centre, easily close enough to drag it along.

So _route() does not simply split the gap. When one flank of a traversal is
verified empty, the line shifts LATTICE_BIAS toward it, putting 21mm between
the piece being carried and the one it's passing on the long traversal leg
(and 8.5mm of daylight between that piece and the magnet's edge). The short
diagonal onto the lattice corner then becomes the tightest point at 17.4mm,
so a biased move's true worst case is 17.4mm rather than 15.0mm -- a smaller
win than the traversal figure alone suggests, but a real one, and it applies
to ~2 moves in 3. Only when both flanks are occupied -- or the empty one is
off the board -- does it fall back to the 15mm midline, because at that
point 15mm is the geometric maximum and no amount of cleverness improves
on it.

tests/test_robot_moves.py asserts the 15mm floor as an invariant over whole
random games; it has never been breached in ~32,000 planned moves, and only
about 2.5% of moves reach it at all.

That fallback is not hypothetical: in the opening every gap in the rank-7
pawn wall has a pawn on both sides, so a knight coming off the back rank
crosses at 15mm no matter what. If pieces catch there, the fix is mechanical
(narrower bases) or a weaker MAG_EDGE -- not more routing code.

Special-move handling is derived from python-chess (board.is_castling,
is_en_passant, ...) rather than hand-written rules, for the same reason
move_resolver._expected_delta diffs piece_map() instead: push() already
implements chess's side effects correctly, and re-deriving them here is how
you end up with a robot that plays en passant into the wrong square.
"""

import chess

# Board geometry. Keep these in step with the firmware's own SQUARE_MM --
# they exist here so the clearance arithmetic above can be re-derived rather
# than taken on trust.
SQUARE_MM = 30.0        # 240x240mm playing area
PIECE_BASE_MM = 15.0    # worst case of the measured 13-15mm bases
MAGNET_MM = 25.0        # pole face diameter

# How far to shift a traversal line off the midline when one side is known
# to be empty, in squares. 0.2 -> 21mm from the occupied flank instead of
# 15mm. Raising it buys more clearance from that flank but pushes the magnet
# further under the empty square, so it can't exceed 0.5 (which would put
# the carriage over the neighbouring square's centre).
LATTICE_BIAS = 0.2

# Magnet duty cycles. HOLD drags a piece across a square; EDGE is the lower
# duty used while riding the gap between squares, where the piece is offset
# from the pole face and a full-strength pull tends to snatch it sideways
# into a neighbouring square. Both are clamped again by the firmware's
# MAG_MAX_PWM, which is what actually protects the 5V coil.
MAG_HOLD = 170
MAG_EDGE = 110

# Where the carriage rests between moves. Travel is exactly the board, so
# this is a1's centre with the coil de-energised.
PARK = (0.0, 0.0)

DEFAULT_TOPPLE_DELAY_S = 5.0

_PIECE_WORDS = {
    chess.PAWN: "pawn",
    chess.KNIGHT: "knight",
    chess.BISHOP: "bishop",
    chess.ROOK: "rook",
    chess.QUEEN: "queen",
    chess.KING: "king",
}


class Step:
    """One item in a plan. `command` is a firmware line to send, or None for
    a step the Pi handles itself (WAIT, PROMPT). `note` is what the web UI
    shows while it's happening."""

    __slots__ = ("command", "note", "kind", "seconds", "prompt")

    def __init__(self, command=None, note="", kind="command", seconds=0.0, prompt=None):
        self.command = command
        self.note = note
        self.kind = kind  # "command" | "wait" | "prompt"
        self.seconds = seconds
        self.prompt = prompt

    def __repr__(self):
        return f"Step({self.kind}, {self.command!r}, {self.note!r})"

    def __eq__(self, other):
        if not isinstance(other, Step):
            return NotImplemented
        return (
            self.command == other.command
            and self.kind == other.kind
            and self.seconds == other.seconds
            and self.prompt == other.prompt
        )


def _xy(square):
    return chess.square_file(square), chess.square_rank(square)


def _goto(x, y, note):
    # Two decimals is enough for half-square routing and keeps the serial
    # line short; the firmware parses with atof.
    return Step(f"GOTO {x:.2f} {y:.2f}", note)


def _sign(value):
    return (value > 0) - (value < 0)


def _between(from_square, to_square):
    """The squares a straight-line move passes over, exclusive of both ends.
    chess.SquareSet.between() returns empty for non-line moves (knights)."""
    return list(chess.SquareSet.between(from_square, to_square))


def _is_straight(dx, dy):
    return dx == 0 or dy == 0 or abs(dx) == abs(dy)


def _interior_sign(value):
    """Which way is 'toward the middle of the board' from a given file or
    rank index -- used to pick the perpendicular offset for a move that has
    no component on that axis."""
    return 1 if value <= 3 else -1


def _flanking(gap, start, end, vertical):
    """The two lines of squares a traversal passes between.

    `gap` is the fixed half-integer coordinate of the line (1.5 = between
    files b and c); `start`/`end` bound the coordinate that varies. Returns
    (low, high) lists of squares, where an empty list means that side is off
    the board.

    Only squares strictly inside the swept range are included, and that is
    exact rather than approximate: a square whose centre sits at or beyond
    the range's ends is already at least 0.583 squares (17.5mm) away, which
    is further than the midline clearance this function exists to improve.
    """
    low_idx = int(round(gap - 0.5))
    crossed = [v for v in range(8) if min(start, end) < v < max(start, end)]

    def line(idx):
        if not 0 <= idx <= 7:
            return []  # off the board: nothing there, but nowhere to go either
        return [chess.square(idx, v) if vertical else chess.square(v, idx) for v in crossed]

    return line(low_idx), line(low_idx + 1)


def _bias(low, high, board, ignore):
    """How far to shift a traversal off the midline, in squares.

    Negative shifts toward `low`, positive toward `high`, zero stays put.
    A side is only worth moving toward if it is on the board *and* empty for
    the whole traversal -- an off-board side is empty in the useless sense,
    since the carriage can't go there.

    Both sides clear means there is nothing to dodge; both blocked means the
    midline is already the best available position. Either way, stay.
    """

    def clear(squares):
        return bool(squares) and all(
            square in ignore or board.piece_at(square) is None for square in squares
        )

    low_clear, high_clear = clear(low), clear(high)
    if low_clear == high_clear:
        return 0.0
    return -LATTICE_BIAS if low_clear else LATTICE_BIAS


def _route(from_square, to_square, board, label):
    """The waypoints for dragging one piece, magnet already on.

    A straight-line move over empty squares goes centre to centre -- chess
    legality already guarantees sliding pieces have a clear path, so this
    covers most moves.

    Anything else rides the lattice: step off centre, travel through the gaps
    between squares, then drop into the destination centre. That's what
    knights need, and what the castling rook needs to get past the king it
    just jumped over. When the move has no component on one axis (the rook's
    h8->f8), the perpendicular offset is still applied and held for the whole
    traverse -- without it the rook would track straight down the middle of
    rank 8 and hit the king on g8.

    The traversal is always axis-aligned (knights offset on both axes but
    consume one of them at each end; a rook-style move offsets only
    perpendicular), which is what makes the clearance question tractable:
    exactly two lines of squares flank it, and _bias() shifts the line toward
    whichever of them is empty. See this module's docstring for why 15mm of
    midline clearance isn't enough on a 30mm board.
    """
    fx, fy = _xy(from_square)
    tx, ty = _xy(to_square)
    dx, dy = tx - fx, ty - fy

    blocked = any(board.piece_at(square) is not None for square in _between(from_square, to_square))
    if _is_straight(dx, dy) and not blocked:
        return [_goto(tx, ty, f"slide {label} to {chess.square_name(to_square)}")]

    ox = 0.5 * _sign(dx) if dx else 0.5 * _interior_sign(fx)
    oy = 0.5 * _sign(dy) if dy else 0.5 * _interior_sign(fy)

    # Offsets on axes the move travels are consumed on arrival; the
    # perpendicular one is held all the way across and only released at the
    # end, when the piece drops into the destination centre.
    sx, sy = fx + ox, fy + oy
    ex = tx - ox if dx else tx + ox
    ey = ty - oy if dy else ty + oy

    # The square being departed is empty by now -- the piece is on the magnet.
    # A captured piece on the destination is deliberately *not* forgiven: it
    # was toppled, not necessarily removed yet, so keep clear of it.
    ignore = {from_square}
    if sx == ex:  # vertical traversal, running between two files
        low, high = _flanking(sx, sy, ey, vertical=True)
        sx = ex = sx + _bias(low, high, board, ignore)
    else:  # horizontal, between two ranks
        low, high = _flanking(sy, sx, ex, vertical=False)
        sy = ey = sy + _bias(low, high, board, ignore)

    return [
        _goto(sx, sy, f"ease {label} off {chess.square_name(from_square)}"),
        Step(f"MAG {MAG_EDGE}", "ride the gap between squares"),
        _goto(ex, ey, f"route {label} around {chess.square_name(to_square)}"),
        Step(f"MAG {MAG_HOLD}", "regrip"),
        _goto(tx, ty, f"drop {label} on {chess.square_name(to_square)}"),
    ]


def _carry(from_square, to_square, board, label):
    """Pick a piece up, route it, put it down centred."""
    fx, fy = _xy(from_square)
    steps = [
        _goto(fx, fy, f"go to {chess.square_name(from_square)}"),
        Step(f"MAG {MAG_HOLD}", f"grip the {label}"),
    ]
    steps += _route(from_square, to_square, board, label)
    steps.append(Step("PULSE", f"centre the {label} on {chess.square_name(to_square)}"))
    return steps


def _topple(square, victim, topple_delay_s):
    """Knock a captured piece over and give the human time to lift it off.

    Toppling leaves the piece lying on its own square, so the capturing
    piece cannot be slid in until it's gone -- hence the wait. The delay is
    fixed rather than vision-gated by choice; if it elapses and the piece is
    still there, the incoming move will disturb it and the camera will flag
    the settle rather than committing a wrong position.
    """
    x, y = _xy(square)
    name = chess.square_name(square)
    word = _PIECE_WORDS[victim.piece_type] if victim is not None else "piece"
    return [
        _goto(x, y, f"go to {name}"),
        Step("TOPPLE", f"topple the {word} on {name}"),
        _goto(*PARK, "stand clear"),
        Step(
            kind="wait",
            seconds=topple_delay_s,
            note=f"take the {word} off {name}",
            prompt=f"Remove the toppled {word} on {name}",
        ),
    ]


def plan(board, move, topple_delay_s=DEFAULT_TOPPLE_DELAY_S):
    """Gantry steps to physically play `move` in `board` -- the position
    *before* the move, exactly like engine.describe_move().

    Returns a list of Step. The caller is expected to walk it in order,
    sending `command` for "command" steps, sleeping for "wait" steps, and
    surfacing `prompt` for "prompt" steps.

    Raises ValueError if the move isn't legal in this position. That isn't
    defensive noise: python-chess's is_capture/is_en_passant answer for the
    side to move, so handing in a move for the *other* side yields a plan
    that quietly topples the wrong piece rather than failing. Better to
    refuse than to have the arm act on it.
    """
    if move not in board.legal_moves:
        raise ValueError(f"{move.uci()} is not legal in this position ({board.fen()})")

    steps = []

    # Captures come first: the victim has to be off the destination square
    # before anything is dragged onto it. En passant's victim is beside the
    # destination, not on it, which is exactly the case hand-written rules
    # get wrong -- so ask python-chess.
    if board.is_en_passant(move):
        victim_square = chess.square(
            chess.square_file(move.to_square), chess.square_rank(move.from_square)
        )
        steps += _topple(victim_square, board.piece_at(victim_square), topple_delay_s)
    elif board.is_capture(move):
        steps += _topple(move.to_square, board.piece_at(move.to_square), topple_delay_s)

    mover = board.piece_at(move.from_square)
    label = _PIECE_WORDS[mover.piece_type] if mover is not None else "piece"
    steps += _carry(move.from_square, move.to_square, board, label)

    if board.is_castling(move):
        # The rook's squares aren't in the move at all. It also has to pass
        # under the king, which is now sitting on the square between -- so
        # this is always an edge route, and _route works that out on its own
        # from the post-king-move occupancy.
        kingside = chess.square_file(move.to_square) > chess.square_file(move.from_square)
        rank = chess.square_rank(move.from_square)
        rook_from = chess.square(7 if kingside else 0, rank)
        rook_to = chess.square(5 if kingside else 3, rank)

        after_king = board.copy(stack=False)
        after_king.set_piece_at(move.to_square, after_king.piece_at(move.from_square))
        after_king.remove_piece_at(move.from_square)
        steps += _carry(rook_from, rook_to, after_king, "rook")

    steps.append(Step("MAG 0", "release"))
    steps.append(_goto(*PARK, "park"))

    if move.promotion is not None:
        # The robot can't fetch a queen from the box, and vision can't tell
        # a queen from a pawn anyway (it only reads empty/white/black), so
        # internal state already records the promotion -- the board just has
        # to be made to match it by hand.
        colour = "white" if mover is not None and mover.color == chess.WHITE else "black"
        square = chess.square_name(move.to_square)
        word = _PIECE_WORDS[move.promotion]
        steps.append(
            Step(
                kind="prompt",
                note=f"replace the {colour} pawn on {square} with a {word}",
                prompt=f"Replace the {colour} pawn on {square} with a {word.upper()}",
            )
        )

    return steps
