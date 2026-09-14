"""Serial link for the ChessBot-V1 firmware (firmware/chessbot_v1).

This is the protocol the real machine speaks, and it sits a lot higher than
the one robot.GantryLink was written for. Where chess_gantry takes low-level
"GOTO 3.5 4" waypoints and lets the Pi plan the path, ChessBot-V1 takes
"MOVE e2e4" and does the whole thing itself -- attract, drag, pulse -- with
the measured magnet duties and the edge-fold logic living in firmware.

That division is deliberate and worth preserving: the geometry was measured
on the machine, so the machine is where it should stay. The Python side's job
in legacy mode is chess rules only (see robot_moves_legacy).

What this class actually does is small: reuse GantryLink's framing (write a
line, block for one OK/ERR) and translate the handful of verbs the layers
above emit into their ChessBot-V1 spellings.
"""

import rig
from robot import GantryError, GantryLink

# Commands the upper layers send that this firmware spells differently.
# Anything not in here goes through verbatim, which covers MOVE, KNIGHT,
# GOTO, PULSE, SPEED and the bench-only MM/JOG.
_TRANSLATIONS = {
    "OFF": "MAG 0",      # no bare "coil off" verb; MAG 0 is it
    "STATUS": "POS",     # replies "OK POS x y" -- no homed flag, see homed()
}


def autodetect_port():
    """Find the Uno. Ported from RunChess/arduino.py, which had this and
    MicroChess didn't -- src/robot.py hardcoded /dev/ttyACM0."""
    import serial.tools.list_ports

    candidates = []
    for port in serial.tools.list_ports.comports():
        blob = f"{port.description} {port.manufacturer} {port.device}".lower()
        if any(k in blob for k in ("arduino", "ch340", "usb-serial", "wch", "acm")):
            candidates.append(port.device)

    if not candidates:
        seen = [p.device for p in serial.tools.list_ports.comports()]
        raise GantryError(
            "No Arduino found. Ports seen: "
            + (", ".join(seen) or "none")
            + ". Pass the port explicitly instead of 'auto'."
        )
    return candidates[0]


class LegacyGantryLink(GantryLink):
    """GantryLink speaking ChessBot-V1. Same interface, so Robot and
    RobotController can't tell the difference."""

    def __init__(self, port, baud=rig.BAUD, timeout=None):
        if port == "auto":
            port = autodetect_port()
        # Motion is blocking and a full-board drag at 40mm/s takes a while,
        # so the inherited 40s budget is the right order of magnitude.
        if timeout is None:
            super().__init__(port, baud)
        else:
            super().__init__(port, baud, timeout)

    def send(self, command):
        # Both translated verbs are bare words, so a whole-command lookup is
        # enough -- and it can't accidentally rewrite something like
        # "SPEED 40" that merely starts with a translated word.
        return super().send(_TRANSLATIONS.get(command.strip(), command))

    def abort(self):
        """No-op, and that is a real limitation worth stating plainly.

        ChessBot-V1 has no '!' soft e-stop and its motion is blocking, so
        there is no point in the firmware's main loop where an abort byte
        would be noticed. A halt requested mid-move therefore only takes
        effect once the current command finishes on its own.

        Callers still get halted state correctly -- Robot._fail() runs
        regardless -- so the arm will refuse the NEXT move. It just cannot
        stop the one in flight. If that matters, the answer is a hardware
        e-stop cutting driver enable (D8), not more code here.
        """

    def position(self):
        """(x, y) in machine mm. Parses "OK POS -30.0 60.0"; send() has
        already stripped the "OK"."""
        parts = self.send("POS").split()
        try:
            return float(parts[-2]), float(parts[-1])
        except (IndexError, ValueError):
            raise GantryError(f"could not parse POS reply: {parts!r}")
