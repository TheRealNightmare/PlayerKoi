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
    machine mm  what the firmware speaks. The ORIGIN is the corner of travel
                beyond h1 (not a square centre, and not a1), and x runs
                NEGATIVE toward the a-file. So h1 is at (-65, 60) and a1 is
                at (-415, 60).

square_to_mm() converts. In legacy mode nothing on the Python side sends mm
at all -- the firmware takes square names and does its own arithmetic -- so
this conversion is for bench work and for the dormant native path.
"""

# Board geometry. V2: 400x400mm playing area on a 50mm pitch, BOTH axes -- the
# earlier 200mm Y span was belt slip and was fixed mechanically, so there is no
# separate Y pitch.
SQUARE_MM = 50.0
BOARD_MM = SQUARE_MM * 8        # 400, the playing area

# a1's centre in machine coordinates. The origin (0, 0) is the corner of
# travel beyond h1 -- as far toward the h-file and rank 1 as the carriage
# goes -- which is also the park position. So every square is at negative x
# and positive y: h1 is (-65, 60), a8 is (-415, 410).
A1_X_MM = -415.0
A1_Y_MM = 60.0

# Travel limits, MEASURED on the machine by jogging to the frame: 480 x 470mm,
# not the 500 x 500 the frame was drawn for. The graveyard ring's slot centres
# span 450mm each way, centred in that reach (15mm clear at each X limit, 10mm
# at each Y limit), and the board sits one square pitch inside the ring.
GRAVEYARD_DEPTH_MM = 50.0
MIN_X_MM, MAX_X_MM = -480.0, 0.0
MIN_Y_MM, MAX_Y_MM = 0.0, 470.0

# Where captured pieces go: a ring of parking slots in the 50mm margin, one
# strip on each of the four sides, eight slots per strip on the board's own
# file/rank centre lines. 32 in total, shared between both colours.
#
# The four 50x50mm corner cells are deliberately left empty. A corner slot
# would sit on no centre line at all, so its dot would not line up with the
# printed grid, and 32 already exceeds the 30 pieces that can ever be
# captured -- there is nothing to gain by squeezing in four more.
GRAVEYARD_Y_LOW_MM = A1_Y_MM - SQUARE_MM             # 10, beyond rank 1
GRAVEYARD_Y_HIGH_MM = A1_Y_MM + BOARD_MM             # 460, beyond rank 8
GRAVEYARD_X_LOW_MM = A1_X_MM - SQUARE_MM             # -465, beyond the a-file
GRAVEYARD_X_HIGH_MM = A1_X_MM + BOARD_MM             # -15, beyond the h-file

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

# Letting go of a piece. The pieces hold permanent magnets, so a full-strength
# reverse does not release one so much as punch it -- it jumps and rattles as
# it lands. So the release fades the grip to nothing first, lets the piece
# settle, and only then gives a WEAK reverse to clear residual magnetism from
# the core. The kick can be weak because the piece is already seated by then;
# dropping it entirely is not an option, since a magnetised core tows the
# piece along when the carriage leaves.
MAG_KICK = 110

# How long that fade takes. Tunable live (RELEASE, and the slider in the web
# UI) because the right value depends on the piece and the coil; 0 restores
# the old instant kick.
DEFAULT_RELEASE_MS = 200
MIN_RELEASE_MS, MAX_RELEASE_MS = 0, 2000

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


# The square whose outer corner is the origin: where the carriage rests, and
# where a human must park it before HOME -- in the corner of travel beyond
# this square, not on its centre. There are no limit switches: HOME drives to
# the assumed origin rather than seeking anything, so if the carriage isn't
# actually in that corner when the board powers up, every coordinate that
# follows is silently wrong.
PARK_SQUARE = "h1"

BAUD = 115200
READY_BANNER = "READY ChessBot-V1"

# The protocol revision this code expects from the sketch. Bumped whenever the
# host starts relying on something a older board doesn't do.
#
#   r1  the original: no polarity suffix. It accepts "MOVE e2e4 w" anyway --
#       parsePly reads four characters and ignores the rest -- and silently
#       attracts for every move, which shoves every white piece off its
#       square. That silence is why this check exists.
#   r2  w|b suffix on MOVE/KNIGHT, plus POL and POLTEST.
#   r3  the faded release, plus RELEASE to tune it.
#   r4  BURY, for parking a captured piece on a graveyard slot. The first
#       command that drives to a raw machine coordinate in normal play, so an
#       r3 board cannot fake it -- it has no verb that reaches off the board.
#   r5  re-measured geometry: the origin moved from h1's centre to the corner
#       of travel beyond h1, and a1 to (-415, 60). No new verbs, but an r4
#       board takes the same square names and BURY coordinates and drives
#       somewhere else -- so it has to be refused just the same.
FIRMWARE_REV = 5


def banner_rev(banner):
    """The revision from a READY line. A banner with no revision is r1 -- that
    is precisely what the original sketch prints."""
    for token in (banner or "").split():
        if token.startswith("r") and token[1:].isdigit():
            return int(token[1:])
    return 1


# ---------------------------------------------------------------- orientation
#
# The one value in this file that is NOT mirrored from the firmware, because
# the firmware has no concept of it: chessbot_v1 parses square names
# arithmetically (charAt(0) - 'a') and assumes the carriage parks beyond h1.
#
# On this machine it doesn't. The corner the gantry actually rests on -- the
# origin, (0, 0) -- is beyond a8, so the whole machine is rotated 180 degrees against
# every square name it is sent. Uncorrected, asking for e2 drives to d7, which
# is why the arm reached for Black's pieces on White's turn.
#
# Correcting it here rather than in the sketch keeps the geometry measurements
# and the orientation separable: the mm constants above stay true of the
# hardware, and this stays true of how the board is seated under it.
#
# The four values are the real square the carriage parks beyond:
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


# The board's centre in board-space mm -- the point every orientation flip is
# symmetric about. a1 and h8 centres are 7 squares apart, so the middle is 3.5.
BOARD_CENTRE_X_MM = A1_X_MM + 3.5 * SQUARE_MM
BOARD_CENTRE_Y_MM = A1_Y_MM + 3.5 * SQUARE_MM


def orient_mm(x_mm, y_mm, origin=None):
    """Board-space mm -> machine mm, applying how the board is seated.

    The mm counterpart of orient(). It exists because orient() can only rotate
    file/rank indices, and a graveyard slot has neither -- it sits outside the
    8x8, so there is no index to flip. Reflecting the millimetres about the
    board centre does the same job and works for any point, on the board or
    off it.

    This is NOT optional on this rig. ORIGIN_SQUARE is "a8", a 180 degree
    rotation, so a slot handed to the firmware unmirrored lands on the
    diagonally opposite slot -- the arm drives a captured piece the full width
    of the board to the wrong place, and nothing reports it.

    Like orient(), it is its own inverse for every supported origin.
    """
    origin = ORIGIN_SQUARE if origin is None else origin
    try:
        flip_file, flip_rank = _ORIENTATIONS[origin]
    except KeyError:
        raise ValueError(
            f"unknown board origin {origin!r} -- expected one of {', '.join(_ORIENTATIONS)}"
        )
    if flip_file:
        x_mm = 2 * BOARD_CENTRE_X_MM - x_mm
    if flip_rank:
        y_mm = 2 * BOARD_CENTRE_Y_MM - y_mm
    return (x_mm, y_mm)


def within_travel(x_mm, y_mm):
    """Whether a machine coordinate is physically reachable."""
    return MIN_X_MM <= x_mm <= MAX_X_MM and MIN_Y_MM <= y_mm <= MAX_Y_MM


# The four strips, named for the board edge each sits beyond. Slots are
# numbered counter-clockwise from SOUTH's a-file end, 8 per strip:
#
#              NORTH  16..23  (right to left)
#           +-------------------+
#   WEST    |                   |   EAST
#   24..31  |      the 8x8      |   8..15
#   (top    |                   |   (bottom
#    down)  +-------------------+    up)
#              SOUTH  0..7  (left to right)
#
# Counter-clockwise rather than "all of one edge, then all of the next in
# reading order" so that consecutive indices are always physically adjacent,
# including across the corner joins. Anything that walks the ring -- filling
# it in order, finding the next free slot near a full one -- then gets
# adjacency for free instead of special-casing four seams.
SOUTH, EAST, NORTH, WEST = "south", "east", "north", "west"
GRAVEYARD_STRIPS = (SOUTH, EAST, NORTH, WEST)

SLOTS_PER_STRIP = 8
GRAVEYARD_SLOTS = SLOTS_PER_STRIP * len(GRAVEYARD_STRIPS)   # 32


def graveyard_strip(slot):
    """Which of the four strips a slot index falls on."""
    if not 0 <= slot < GRAVEYARD_SLOTS:
        raise ValueError(f"graveyard slot must be 0..{GRAVEYARD_SLOTS - 1}, not {slot}")
    return GRAVEYARD_STRIPS[slot // SLOTS_PER_STRIP]


def graveyard_slot_to_mm(slot):
    """Where to park a captured piece, in machine mm.

    `slot` is 0..31 around the ring (see the diagram above). Every slot sits
    on a board file or rank centre line, so the printed sticker's graveyard
    dots line up with the board's own columns and rows.

    There is no colour argument: all 32 slots are a single shared pool, and
    choosing between them is policy rather than geometry -- see
    graveyard.nearest_free_slot(). This only says where slot n is.

    Mirrors square_to_mm(), and carries the same caveat: the result is in
    board space. This rig is seated 180 degrees round (ORIGIN_SQUARE), and
    orient() only rotates file/rank indices, which a graveyard slot does not
    have -- it sits outside the 8x8. So on a rotated rig, mirror the result
    about the board centre rather than reaching for orient().
    """
    strip = graveyard_strip(slot)
    i = slot % SLOTS_PER_STRIP
    if strip == SOUTH:                      # a-file end to h-file end
        return (A1_X_MM + i * SQUARE_MM, GRAVEYARD_Y_LOW_MM)
    if strip == EAST:                       # rank 1 up to rank 8
        return (GRAVEYARD_X_HIGH_MM, A1_Y_MM + i * SQUARE_MM)
    if strip == NORTH:                      # h-file end back to a-file end
        return (A1_X_MM + (7 - i) * SQUARE_MM, GRAVEYARD_Y_HIGH_MM)
    return (GRAVEYARD_X_LOW_MM, A1_Y_MM + (7 - i) * SQUARE_MM)   # WEST, top down
