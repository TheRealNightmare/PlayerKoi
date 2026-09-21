"""Choosing which graveyard slot a captured piece goes to.

rig.py says where the 32 slots ARE. This says which one to use, which is a
different kind of fact: the geometry is measured off the machine and changes
only when the machine does, whereas this is a policy that could reasonably be
something else tomorrow. rig.py's own docstring draws that line, so the policy
lives here rather than there.

It is also not in robot_moves.py. That module routes a single move across a
board it can see, and it is already the largest thing in src/. Slot choice
needs something robot_moves has no notion of -- what is already in the pile --
so keeping it separate stops "where do I put this" leaking into "how do I get
there".

The pool is SHARED: all 32 slots are open to either colour, and a capture goes
to whichever free slot is nearest. That keeps the arm's trip off the board
short, which matters more here than tidiness -- at 50mm per square the ring is
1.6m around, and the far corner is a long drag with a piece stuck to the coil.
The cost is that the two colours intermix, so the pile is not a scoreboard.

No state of its own. Occupancy is passed in and the caller owns the pile, which
means this is a pure function of its arguments and can be tested without
standing up a game.
"""

import math

import rig


def slot_positions():
    """All 32 slot centres in machine mm, indexed by slot number."""
    return [rig.graveyard_slot_to_mm(s) for s in range(rig.GRAVEYARD_SLOTS)]


def nearest_free_slot(from_x_mm, from_y_mm, occupied=()):
    """The free slot nearest (from_x_mm, from_y_mm), or None if all are full.

    `occupied` is any container of slot indices already holding a piece; it is
    membership-tested, so a set is the sensible thing to pass for a pile of
    any size.

    Distance is straight-line from the capture square to the slot, not the
    path the arm will actually drive. The arm's route is decided later by
    robot_moves, and it cannot be known here without duplicating that routing
    -- but the ring is convex enough that the nearest slot as the crow flies
    is the nearest slot to drive to in every case that comes up.

    Ties go to the lowest index. That is not arbitrary: a capture from the
    centre files is genuinely equidistant from two slots, and a randomised or
    dict-ordered tie-break would make the same position plan differently
    between runs, which is miserable to debug and would make the tests flaky.
    """
    best, best_d2 = None, None
    for slot in range(rig.GRAVEYARD_SLOTS):
        if slot in occupied:
            continue
        x, y = rig.graveyard_slot_to_mm(slot)
        # Compare squared distances -- same ordering, no sqrt, and no float
        # fuzz to make two genuinely-equal distances compare unequal and
        # silently break the lowest-index tie-break above.
        d2 = (x - from_x_mm) ** 2 + (y - from_y_mm) ** 2
        if best_d2 is None or d2 < best_d2:
            best, best_d2 = slot, d2
    return best


def nearest_free_slot_for_square(file_, rank, occupied=()):
    """nearest_free_slot() addressed by board square instead of by mm.

    The convenient entry point for the planner, which thinks in file/rank:
    `nearest_free_slot_for_square(4, 3, pile)` for a piece captured on e4.
    """
    return nearest_free_slot(*rig.square_to_mm(file_, rank), occupied=occupied)


def slot_distance_mm(slot, from_x_mm, from_y_mm):
    """Straight-line distance from a point to a slot, for reporting."""
    x, y = rig.graveyard_slot_to_mm(slot)
    return math.hypot(x - from_x_mm, y - from_y_mm)
