"""Hand-driven gantry commands for the /admin page.

The game path turns a chess move into commands through a planner, which
knows the rules: captures, castling, which colour is being carried. /admin
is the opposite -- a human names two squares and says what is on them -- so
there are no rules to apply here, only spelling and orientation.

Orientation is the one thing that must not be skipped. rig.ORIGIN_SQUARE
rotates every square bound for the firmware (see rig.orient); a click on e4
that went out unrotated would drive the arm to a different real square.

Pure: no serial, no session. The route in web_ui.py does the sending.
"""

import re

import rig

_SQUARE = re.compile(r"^[a-h][1-8]$")

MAG_MODES = {"off": 0, "attract": 1, "repel": 2}


def check_square(name):
    """The square, lower-cased, or raises ValueError."""
    if not isinstance(name, str) or not _SQUARE.match(name.strip().lower()):
        raise ValueError(f"not a square: {name!r} (expected a1-h8)")
    return name.strip().lower()


def goto_command(square):
    """Carriage to `square`, magnet untouched."""
    return f"GOTO {rig.orient_square(check_square(square))}"


def move_command(from_square, to_square, colour, weave):
    """Drag the piece on `from_square` to `to_square`.

    `colour` is "white" or "black" -- the coil polarity follows it, and the
    wrong one shoves the piece off the square instead of holding it.
    `weave` picks KNIGHT (along the gridlines, clear of neighbours) over MOVE
    (straight between centres).
    """
    frm, to = check_square(from_square), check_square(to_square)
    if frm == to:
        raise ValueError("from and to are the same square")
    if colour not in ("white", "black"):
        raise ValueError(f"colour must be white or black, not {colour!r}")
    verb = "KNIGHT" if weave else "MOVE"
    return f"{verb} {rig.orient_uci(frm + to)} {rig.colour_token(colour == 'white')}"


def mag_command(mode):
    if mode not in MAG_MODES:
        raise ValueError(f"magnet mode must be one of {list(MAG_MODES)}")
    return f"MAG {MAG_MODES[mode]}"


def _cell(square):
    """(row, col) in the board matrix -- board_state's matrix[rank_idx][file_idx],
    so row 0 is rank 1 and col 0 is file a."""
    return int(square[1]) - 1, ord(square[0]) - ord("a")


def apply_to_matrix(matrix, from_square, to_square):
    """The tracked board after the arm moves a piece. Returns (matrix, None)
    or (None, reason).

    Refuses a move onto an occupied square: the arm would drag one piece into
    another, and there is no capture here to clear it first.
    """
    frm, to = check_square(from_square), check_square(to_square)
    fr, fc = _cell(frm)
    tr, tc = _cell(to)
    if matrix[fr][fc] is None:
        return None, f"there is no piece on {frm}"
    if matrix[tr][tc] is not None:
        return None, f"{to} is occupied -- clear it first"
    moved = [row[:] for row in matrix]
    moved[tr][tc], moved[fr][fc] = moved[fr][fc], None
    return moved, None


def parse_pos(reply):
    """(x, y) from a POS reply ("POS -30.0 60.0"), or None if it has no
    numbers -- the mock answers with an empty string."""
    parts = (reply or "").split()
    try:
        return float(parts[-2]), float(parts[-1])
    except (IndexError, ValueError):
        return None
