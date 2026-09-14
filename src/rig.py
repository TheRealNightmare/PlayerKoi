"""Measured constants for the physical machine.

Every number here was measured on the rig and is mirrored from
firmware/chessbot_v1/chessbot_v1.ino, which is the single source of truth.
This module exists so the Python side, the docs and the bench sheet can cite
one place instead of re-declaring geometry that then drifts apart.

If a value here disagrees with the firmware, the FIRMWARE is right and this
file is stale -- it is the thing that actually drives the motors.

A warning about the other numbers in this repo:

* RunChess/chess.txt has SQUARE_MM = 25.7 and a different a1 offset. That is
  an older calibration for the 200x200 frame, from before the belt slip was
  fixed by re-belting. It is superseded. Do not use it.
* firmware/chess_gantry/ declares its own constants (20 steps/mm, 75mm/s,
  MAG_MAX_PWM 170). Those belong to a different, unbuilt machine and are not
  this rig's -- see that sketch's header.

COORDINATES

Two systems meet here, so be explicit about which one is in play:

    file/rank   0..7 on each axis, a1 = (0, 0), h8 = (7, 7). What the chess
                code (robot_moves, move_resolver) speaks. Fractions are legal
                and mean the lattice line between two squares.
    machine mm  what the firmware speaks. The ORIGIN IS h1, not a1, and x
                runs NEGATIVE toward the a-file. So a1 is at (-210, 0).

square_to_mm() converts. In legacy mode nothing on the Python side sends mm
at all -- the firmware takes square names and does its own arithmetic -- so
this conversion is for bench work and for the dormant native path.
"""

# Board geometry. 30mm pitch on BOTH axes: the earlier 200mm Y span was belt
# slip and was fixed mechanically, so there is no separate Y pitch.
SQUARE_MM = 30.0

# a1's centre in machine coordinates. The origin (0, 0) is h1's centre, which
# is also the park position, so the a-file is 7 squares negative in x.
A1_X_MM = -210.0
A1_Y_MM = 0.0

# The measured corners ARE the travel limits -- there is no room beyond them
# in any direction. This is why knight and castling weaves have to fold back
# inward at the board edge rather than stepping half a square outside it
# (clampWeave in the firmware, _fold_to_travel in robot_moves).
MIN_X_MM, MAX_X_MM = -210.0, 0.0
MIN_Y_MM, MAX_Y_MM = 0.0, 210.0

# Motion scale: 200 steps/rev at 1/2 microstepping over a 20-tooth GT2 pulley
# on 2mm belt = 400 steps per 40mm = 10 steps/mm. The microstepping jumpers
# matter: a CNC Shield defaults to 1/8, which would make every distance 4x
# too large. See docs/CONNECTION.md troubleshooting.
MICROSTEPS = 2
MOTOR_STEPS_PER_REV = 200.0
PULLEY_TEETH = 20.0
BELT_PITCH_MM = 2.0
MM_PER_REV = PULLEY_TEETH * BELT_PITCH_MM          # 40.0
STEPS_PER_MM = (MOTOR_STEPS_PER_REV * MICROSTEPS) / MM_PER_REV   # 10.0

# Proven feed rate. The firmware accepts SPEED 1-100 mm/s.
FEED_MMS = 40.0

# Magnet duty cycles, as PWM counts. FULL drags a piece square-to-square;
# DIAG is the weaker duty used along the gridlines of a weave, where the
# piece rides offset from the pole face and full strength snatches it
# sideways. These are the firmware's measured values for this coil and rail
# -- chess_gantry's 170 cap is for different hardware and does not apply.
MAG_FULL = 255
MAG_DIAG = 155

# The white pieces on this set were built with their magnets the other way up,
# so they need the opposite coil polarity: attract holds a black piece, repel
# holds a white one. Measured at the bench with MAG 1 / MAG 2.
#
# The firmware owns the actual coil switching (WHITE_IS_REVERSED in
# chessbot_v1.ino); this side only has to say which colour is being carried,
# which it does with a w|b suffix on MOVE and KNIGHT. Mirrored here because
# every other measured fact about the machine is in this file.
WHITE_IS_REVERSED = True


def colour_token(is_white):
    """The w|b suffix for MOVE/KNIGHT. The firmware maps it to a polarity."""
    return "w" if is_white else "b"


# Where the carriage rests, and what a human must park it on before HOME.
# There are no limit switches: HOME drives to the assumed origin rather than
# seeking anything, so if the carriage isn't actually on h1 when the board
# powers up, every coordinate that follows is silently wrong.
PARK_SQUARE = "h1"

BAUD = 115200
READY_BANNER = "READY ChessBot-V1"


# ---------------------------------------------------------------- orientation
#
# The one value in this file that is NOT mirrored from the firmware, because
# the firmware has no concept of it: chessbot_v1 parses square names
# arithmetically (charAt(0) - 'a') and assumes the carriage parks on h1.
#
# On this machine it doesn't. The corner the gantry actually rests on -- the
# origin, (0, 0) -- is a8, so the whole machine is rotated 180 degrees against
# every square name it is sent. Uncorrected, asking for e2 drives to d7, which
# is why the arm reached for Black's pieces on White's turn.
#
# Correcting it here rather than in the sketch keeps the geometry measurements
# and the orientation separable: the mm constants above stay true of the
# hardware, and this stays true of how the board is seated under it.
#
# The four values are the real square the carriage parks on:
#
#     "h1"   what the firmware assumes -- no correction
#     "a8"   rotated 180 degrees: file AND rank flip     <- this rig
#     "h8"   ranks flip, files don't (a mirror, from a reversed Y axis)
#     "a1"   files flip, ranks don't
ORIGIN_SQUARE = "a8"

# (flip_file, flip_rank) for each supported origin.
_ORIENTATIONS = {
    "h1": (False, False),
    "a8": (True, True),
    "h8": (False, True),
    "a1": (True, False),
}

SUPPORTED_ORIGINS = tuple(_ORIENTATIONS)


def orient(file_, rank, origin=None):
    """(file, rank) as chess means it -> (file, rank) as the machine means it.

    Its own inverse for every supported origin, so a round trip is the
    identity and the helper can be used in either direction.
    """
    origin = ORIGIN_SQUARE if origin is None else origin
    try:
        flip_file, flip_rank = _ORIENTATIONS[origin]
    except KeyError:
        raise ValueError(
            f"unknown board origin {origin!r} -- expected one of {', '.join(_ORIENTATIONS)}"
        )
    return (7 - file_ if flip_file else file_, 7 - rank if flip_rank else rank)


def orient_square(name, origin=None):
    """"e2" -> "d7" on this rig. For square names bound for the firmware."""
    file_, rank = orient(ord(name[0]) - ord("a"), int(name[1]) - 1, origin)
    return f"{chr(ord('a') + file_)}{rank + 1}"


def orient_uci(uci, origin=None):
    """"e2e4" -> "d7d5". Only the four square characters -- a promotion
    suffix is dropped, since MOVE/KNIGHT take squares and the firmware has no
    idea what a promotion is."""
    return orient_square(uci[:2], origin) + orient_square(uci[2:4], origin)


def square_to_mm(file_, rank):
    """(file, rank) in 0..7 -> (x, y) in machine mm. Fractions are fine.

    Mirrors gotoSquareF() in the firmware.
    """
    return (A1_X_MM + file_ * SQUARE_MM, A1_Y_MM + rank * SQUARE_MM)


def within_travel(x_mm, y_mm):
    """Whether a machine coordinate is physically reachable."""
    return MIN_X_MM <= x_mm <= MAX_X_MM and MIN_Y_MM <= y_mm <= MAX_Y_MM
