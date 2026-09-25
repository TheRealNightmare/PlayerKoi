"""The two sketches and rig.py must agree about the machine.

There are three copies of the board geometry in this repo, for reasons that
are each defensible on their own:

  firmware/chessbot_v1/     the real one. The firmware owns these numbers --
                            they were measured on the machine.
  firmware/chessbot_walker/ the bring-up walker. Copied, because Arduino has
                            no good way to share a header between two sketch
                            folders and it has to compile alone.
  src/rig.py                the Python mirror, so the host, the docs and the
                            bench sheet can cite one place.

Three copies drift. That is not a risk, it is a certainty given enough edits,
and each drift has its own quiet failure: a walker that has drifted confirms a
machine that no longer exists, and a rig.py that has drifted makes the host
plan for a board the firmware will not drive.

So this test exists to make the drift loud. It does not care what the numbers
ARE -- only that all three say the same thing. Change geometry in one place and
this will name the others.

Pure text parsing; no Arduino toolchain needed (there isn't one on this
machine, which is also why nothing here claims the sketches compile).
"""

import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import rig  # noqa: E402

FIRMWARE = Path(__file__).resolve().parent.parent / "firmware"
GAME_SKETCH = FIRMWARE / "chessbot_v1" / "chessbot_v1.ino"
WALKER_SKETCH = FIRMWARE / "chessbot_walker" / "chessbot_walker.ino"

# Every constant both sketches must carry, and agree on. Anything whose value
# is an expression rather than a literal (MM_PER_REV, stepsPerMM) is left out:
# it is derived from these, so pinning these pins those too.
SHARED = [
    # pin map -- wrong here and the walker drives the wrong motor
    "stepPinA", "dirPinA", "stepPinB", "dirPinB", "enablePin", "AIN1", "AIN2",
    # motion scale -- wrong here and every distance is wrong by a constant
    "MICROSTEPS", "MOTOR_STEPS_PER_REV", "PULLEY_TEETH", "BELT_PITCH_MM",
    "invertX", "invertY",
    # board geometry
    "SQUARE_MM", "A1_X_MM", "A1_Y_MM",
    "MIN_X", "MAX_X", "MIN_Y", "MAX_Y",
    "GRAVEYARD_Y_LOW", "GRAVEYARD_Y_HIGH",
    "GRAVEYARD_X_LOW", "GRAVEYARD_X_HIGH",
]

# Sketch name -> the attribute in rig.py that mirrors it. Pins are absent on
# purpose: the Python side never touches a pin.
MIRRORED_IN_RIG = {
    "SQUARE_MM": "SQUARE_MM",
    "A1_X_MM": "A1_X_MM",
    "A1_Y_MM": "A1_Y_MM",
    "MIN_X": "MIN_X_MM",
    "MAX_X": "MAX_X_MM",
    "MIN_Y": "MIN_Y_MM",
    "MAX_Y": "MAX_Y_MM",
    "GRAVEYARD_Y_LOW": "GRAVEYARD_Y_LOW_MM",
    "GRAVEYARD_Y_HIGH": "GRAVEYARD_Y_HIGH_MM",
    "GRAVEYARD_X_LOW": "GRAVEYARD_X_LOW_MM",
    "GRAVEYARD_X_HIGH": "GRAVEYARD_X_HIGH_MM",
    "MICROSTEPS": "MICROSTEPS",
    "MOTOR_STEPS_PER_REV": "MOTOR_STEPS_PER_REV",
    "PULLEY_TEETH": "PULLEY_TEETH",
    "BELT_PITCH_MM": "BELT_PITCH_MM",
}

_DECL = re.compile(r"^\s*const\s+(?:float|int|bool)\s+(.+?);", re.M)

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT = re.compile(r"//[^\n]*")


def strip_comments(text):
    """Code only.

    The safety checks below are about what the sketch DOES, and the walker's
    header comment deliberately spells out what it does not do -- naming
    analogWrite and magHold in prose to say they are absent. Searching raw
    text would make that documentation fail its own test, and the obvious
    "fix" would be to delete the explanation, which is backwards.
    """
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", text))


def parse_consts(path):
    """{name: number|bool} for every `const` with a literal value.

    Handles the two-on-one-line form the sketches use for the travel limits
    (`const float MIN_X = -480.0, MAX_X = 0.0;`). Declarations whose value is
    an expression are skipped rather than guessed at.
    """
    found = {}
    for body in _DECL.findall(path.read_text()):
        for part in body.split(","):
            if "=" not in part:
                continue
            name, _, value = part.partition("=")
            name, value = name.strip(), value.strip()
            if value in ("true", "false"):
                found[name] = (value == "true")
                continue
            try:
                found[name] = float(value)
            except ValueError:
                pass          # an expression, e.g. PULLEY_TEETH * BELT_PITCH_MM
    return found


class TestTheSketchesAgree(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.game = parse_consts(GAME_SKETCH)
        cls.walker = parse_consts(WALKER_SKETCH)

    def test_the_parser_found_something(self):
        """Guards the rest of the file. A regex that silently matches nothing
        would make every comparison below pass vacuously."""
        self.assertGreater(len(self.game), 15)
        self.assertGreater(len(self.walker), 15)

    def test_both_sketches_declare_every_shared_constant(self):
        for name in SHARED:
            self.assertIn(name, self.game, f"{GAME_SKETCH.name} is missing {name}")
            self.assertIn(name, self.walker, f"{WALKER_SKETCH.name} is missing {name}")

    def test_the_two_sketches_agree(self):
        """The whole point. If this fails, say which file you changed and fix
        the other -- the firmware is the source of truth, not the walker."""
        for name in SHARED:
            self.assertEqual(self.walker.get(name), self.game.get(name),
                             f"{name} differs: walker says {self.walker.get(name)}, "
                             f"chessbot_v1 says {self.game.get(name)}")

    def test_rig_py_mirrors_the_firmware(self):
        for sketch_name, rig_name in MIRRORED_IN_RIG.items():
            self.assertAlmostEqual(
                float(getattr(rig, rig_name)), self.game[sketch_name], places=6,
                msg=f"rig.{rig_name} disagrees with {sketch_name} in the sketch "
                    f"-- the FIRMWARE is right and rig.py is stale")

    def test_the_board_really_is_400mm_on_50mm_squares(self):
        """One absolute check, so a consistent change to the wrong numbers
        everywhere still gets noticed. These are the measured V2 numbers:
        origin in the corner of travel beyond h1, 480 x 470mm of reach."""
        self.assertEqual(self.game["SQUARE_MM"], 50.0)
        self.assertEqual(self.game["A1_X_MM"], -415.0)
        self.assertEqual(self.game["A1_Y_MM"], 60.0)
        self.assertEqual((self.game["MIN_X"], self.game["MAX_X"]), (-480.0, 0.0))
        self.assertEqual((self.game["MIN_Y"], self.game["MAX_Y"]), (0.0, 470.0))

    def within_sketch(self, x, y):
        return (self.game["MIN_X"] <= x <= self.game["MAX_X"]
                and self.game["MIN_Y"] <= y <= self.game["MAX_Y"])

    def test_every_square_and_every_weave_waypoint_is_reachable(self):
        """Knight and castling weaves step half a square past the board edge
        (FOLD_EDGE_WEAVE is off), so check the whole half-square lattice from
        -0.5 to 7.5, not just the 64 centres."""
        halves = [i / 2 for i in range(-1, 16)]
        for f in halves:
            for r in halves:
                x, y = rig.square_to_mm(f, r)
                self.assertTrue(self.within_sketch(x, y),
                                f"file {f} rank {r} at ({x}, {y}) is outside the firmware's travel")

    def test_every_graveyard_slot_is_inside_the_travel_limits(self):
        """Checked against the SKETCH's limits, not rig.py's -- the firmware
        is what refuses the move."""
        for slot in range(rig.GRAVEYARD_SLOTS):
            x, y = rig.graveyard_slot_to_mm(slot)
            self.assertTrue(self.game["MIN_X"] <= x <= self.game["MAX_X"]
                            and self.game["MIN_Y"] <= y <= self.game["MAX_Y"],
                            f"slot {slot} at ({x}, {y}) is outside the firmware's travel")


class TestTheWalkerCannotEnergiseTheCoil(unittest.TestCase):
    """The safety property the walker's whole design rests on.

    It is meant to be runnable over a board with pieces on it. With the coil
    dead it cannot drag, drop or fling anything, no matter how wrong its
    geometry is. One stray analogWrite would quietly take that away, and the
    first sign would be a piece skidding across the board.
    """

    @classmethod
    def setUpClass(cls):
        cls.text = WALKER_SKETCH.read_text()
        cls.code = strip_comments(cls.text)

    def test_it_never_calls_analogWrite(self):
        # PWM is the only way to drive the coil at all, so its absence is the
        # strongest single statement this file can make.
        self.assertFalse("analogWrite" in self.code,
                         "the walker must never call analogWrite -- that is the coil")

    def test_it_has_none_of_the_magnet_verbs(self):
        for verb in ("magAttract", "magRepel", "magHold", "magRelease", "magPulse"):
            self.assertFalse(verb in self.code,
                             f"the walker must not define or call {verb}")

    def test_the_coil_pins_are_only_ever_driven_low(self):
        for pin in ("AIN1", "AIN2"):
            writes = re.findall(rf"digitalWrite\(\s*{pin}\s*,\s*(\w+)\s*\)", self.code)
            self.assertTrue(writes, f"{pin} is never explicitly driven low")
            self.assertEqual(set(writes), {"LOW"},
                             f"{pin} is driven {set(writes)}, not just LOW")

    def test_it_says_it_is_not_the_game_firmware(self):
        """A board left with this flashed ignores the Pi, and the symptom looks
        like a dead serial link. The banner is what shortcuts that."""
        self.assertIn("NOT the game firmware", self.text)
        self.assertIn("chessbot_v1.ino", self.text)

    def test_the_game_firmware_by_contrast_does_drive_the_coil(self):
        """Proves the checks above are not vacuous -- they are looking for
        something that genuinely exists in a sketch that has it."""
        game = strip_comments(GAME_SKETCH.read_text())
        self.assertIn("analogWrite", game)
        self.assertIn("magHold", game)


if __name__ == "__main__":
    unittest.main()
