"""Tests for the firmware-revision handshake and the saved rig settings.

The handshake exists because of one silent failure: a board flashed with an
older sketch accepts the same commands and does something different with
them. Up to r6 the sketch held white pieces by REPEL; since every piece was
re-magnetised the same way up (r7) that shoves a white piece off its square.

So the point of these is not that the numbers parse. It is that a stale board
refuses to move, that homing cannot talk it out of that, and that the saved
settings survive a restart and reach the board on connect.
"""

import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import rig  # noqa: E402
import rig_config  # noqa: E402
import robot_moves_legacy  # noqa: E402
from robot import GantryError, MockGantry, Robot  # noqa: E402

R1_BANNER = "READY ChessBot-V1"
CURRENT_BANNER = f"READY ChessBot-V1 r{rig.FIRMWARE_REV}"


def stale_robot():
    return Robot(MockGantry(banner=R1_BANNER), planner=robot_moves_legacy)


def current_robot():
    return Robot(MockGantry(), planner=robot_moves_legacy)


class TestBannerParsing(unittest.TestCase):
    def test_a_banner_with_no_revision_is_r1(self):
        """Exactly what the original sketch prints -- so 'no revision' has to
        mean the old one, not 'unknown'."""
        self.assertEqual(rig.banner_rev(R1_BANNER), 1)
        self.assertEqual(rig.banner_rev("READY"), 1)

    def test_a_missing_banner_is_r1(self):
        self.assertEqual(rig.banner_rev(None), 1)
        self.assertEqual(rig.banner_rev(""), 1)

    def test_the_revision_is_read_out(self):
        self.assertEqual(rig.banner_rev("READY ChessBot-V1 r2"), 2)
        self.assertEqual(rig.banner_rev("READY ChessBot-V1 r17"), 17)

    def test_this_code_expects_the_one_polarity_revision(self):
        """r7 is where every piece is held by attract. An r6 board would still
        repel white, so anything older has to be refused."""
        self.assertGreaterEqual(rig.FIRMWARE_REV, 7)

    def test_the_sketch_and_rig_py_agree_on_the_revision(self):
        """The one that actually matters, and the one nothing checked before.

        rig.FIRMWARE_REV is what the host demands; the #define is what the
        board announces. Bumping one and not the other is silent in opposite
        directions -- too low and a board missing the verb is accepted, too
        high and a correctly flashed board is refused at connect -- and
        neither shows up until there is hardware on the desk.
        """
        sketch = (Path(__file__).resolve().parent.parent
                  / "firmware" / "chessbot_v1" / "chessbot_v1.ino").read_text()
        match = re.search(r"^#define\s+FIRMWARE_REV\s+(\d+)", sketch, re.M)
        self.assertIsNotNone(match, "no FIRMWARE_REV #define in the sketch")
        self.assertEqual(int(match.group(1)), rig.FIRMWARE_REV)

    def test_the_sketch_implements_what_the_revision_promises(self):
        """r4 means BURY exists. A rev bump with no verb behind it would pass
        the check above and still fail on the first capture."""
        sketch = (Path(__file__).resolve().parent.parent
                  / "firmware" / "chessbot_v1" / "chessbot_v1.ino").read_text()
        self.assertIn('cmd == "BURY"', sketch)
        self.assertIn("bool doBury(", sketch)


class TestStaleBoardRefuses(unittest.TestCase):
    def test_a_current_board_is_not_stale(self):
        robot = current_robot()
        self.assertEqual(robot.firmware_rev, rig.FIRMWARE_REV)
        self.assertFalse(robot.stale_firmware)

    def test_an_r1_board_is_stale(self):
        robot = stale_robot()
        self.assertEqual(robot.firmware_rev, 1)
        self.assertTrue(robot.stale_firmware)

    def test_an_r7_board_is_stale(self):
        """r7 has no GRID verb, which the host sends on connect."""
        robot = Robot(MockGantry(banner="READY ChessBot-V1 r7"),
                      planner=robot_moves_legacy)
        self.assertTrue(robot.stale_firmware)

    def test_an_r6_board_is_stale(self):
        """The last sketch that held white by repel."""
        robot = Robot(MockGantry(banner="READY ChessBot-V1 r6"),
                      planner=robot_moves_legacy)
        self.assertTrue(robot.stale_firmware)

    def test_the_message_says_what_to_do(self):
        message = stale_robot().message
        self.assertIn("reflash", message)
        self.assertIn("chessbot_v1", message)

    def test_a_stale_board_is_never_ready(self):
        robot = stale_robot()
        robot.homed = True          # even if it somehow were
        self.assertFalse(robot.ready)

    def test_homing_is_refused_rather_than_clearing_it(self):
        """home() clears `halted` on purpose -- it is the recovery path. It
        must not become a way to dismiss stale firmware, because re-homing
        plainly does not reflash an Arduino."""
        robot = stale_robot()
        with self.assertRaises(GantryError):
            robot.home()
        self.assertTrue(robot.stale_firmware)
        self.assertFalse(robot.ready)
        self.assertFalse(robot.homed)

    def test_playing_is_refused(self):
        import chess

        robot = stale_robot()
        robot.homed = True
        with self.assertRaises(GantryError):
            robot.play(chess.Board(), chess.Move.from_uci("e2e4"))

    def test_a_stale_board_is_sent_no_commands(self):
        robot = stale_robot()
        try:
            robot.home()
        except GantryError:
            pass
        self.assertEqual(robot._link.commands, [])


class TestOldSettingsAreIgnored(unittest.TestCase):
    """config/rig.json files written before r7 still carry white_polarity and
    release_ms. They must load cleanly, and the next save must drop them."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "rig.json"
        self.tmp.write_text(json.dumps(
            {"white_polarity": "repel", "release_ms": 200, "kick_duty": 110,
             "kick_ms": 20, "settle_ms": 400}))

    def test_an_old_file_still_loads(self):
        settings = rig_config.load(self.tmp)
        self.assertEqual(settings["settle_ms"], 400)
        for gone in ("white_polarity", "release_ms", "kick_duty", "kick_ms"):
            self.assertNotIn(gone, settings)

    def test_the_next_save_drops_the_old_keys(self):
        rig_config.save(grip_ms=200, path=self.tmp)
        stored = json.loads(self.tmp.read_text())
        self.assertEqual(stored["grip_ms"], 200)
        self.assertEqual(stored["settle_ms"], 400)
        for gone in ("white_polarity", "release_ms", "kick_duty", "kick_ms"):
            self.assertNotIn(gone, stored)

    def test_the_removed_settings_cannot_be_saved(self):
        for gone in ("white_polarity", "release_ms", "kick_duty", "kick_ms"):
            with self.assertRaises((TypeError, ValueError), msg=gone):
                rig_config.save(path=self.tmp, **{gone: 1})


class TestSettingsReachTheBoard(unittest.TestCase):
    def test_open_gantry_pushes_every_setting_on_connect(self):
        """Opening the port reboots the Uno, so whatever it was told last
        time is gone. If this stops happening, the board silently reverts to
        its compiled defaults."""
        from robot import open_gantry

        robot = open_gantry("mock")
        commands = robot._link.commands
        self.assertEqual([c.split()[0] for c in commands], ["POL", "DWELL", "GRID", "SPEED"])
        self.assertEqual(commands[-1], "SPEED 40")
        self.assertEqual(set(robot.tuning), set(rig.MOTION_TUNING))

    def test_no_release_or_kick_verbs_are_sent(self):
        """r7 has no RELEASE or KICK -- sending one would come back ERR."""
        from robot import open_gantry

        verbs = {c.split()[0] for c in open_gantry("mock")._link.commands}
        self.assertFalse({"RELEASE", "KICK"} & verbs)
        self.assertFalse({"MOVE", "KNIGHT", "BURY", "GOTO", "HOME"} & verbs)

    def test_a_stale_board_is_sent_nothing(self):
        robot = stale_robot()
        self.assertEqual(robot._link.commands, [])
        self.assertIsNone(robot.polarity)

    def test_the_polarity_goes_out_first(self):
        """Before anything can move, so no drag uses the compiled default."""
        from robot import open_gantry

        robot = open_gantry("mock", polarity=rig_config.REPEL)
        self.assertEqual(robot._link.commands[0], "POL 1")
        self.assertEqual(robot.polarity, rig_config.REPEL)


class TestOnePolarityToggle(unittest.TestCase):
    """One polarity for every piece, switchable in case the magnets are
    fitted the other way up."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "rig.json"

    def test_it_defaults_to_attract(self):
        self.assertFalse(rig.HOLD_BY_REPEL)
        self.assertEqual(rig_config.load(self.tmp)["polarity"], rig_config.ATTRACT)

    def test_it_round_trips(self):
        rig_config.save(polarity=rig_config.REPEL, path=self.tmp)
        self.assertEqual(rig_config.load(self.tmp)["polarity"], rig_config.REPEL)
        rig_config.save(polarity=rig_config.ATTRACT, path=self.tmp)
        self.assertEqual(rig_config.load(self.tmp)["polarity"], rig_config.ATTRACT)

    def test_saving_it_keeps_the_pauses(self):
        rig_config.save(settle_ms=700, path=self.tmp)
        rig_config.save(polarity=rig_config.REPEL, path=self.tmp)
        self.assertEqual(rig_config.load(self.tmp)["settle_ms"], 700)

    def test_nonsense_is_ignored_on_load_and_refused_on_save(self):
        self.tmp.write_text(json.dumps({"polarity": "sideways"}))
        self.assertEqual(rig_config.load(self.tmp)["polarity"], rig_config.ATTRACT)
        with self.assertRaises(ValueError):
            rig_config.save(polarity="sideways", path=self.tmp)

    def test_the_old_white_only_setting_is_not_carried_over(self):
        """white_polarity=repel meant 'white is the opposite of black'.
        Reading it as 'repel every piece' would drop every black piece."""
        self.tmp.write_text(json.dumps({"white_polarity": "repel"}))
        self.assertEqual(rig_config.load(self.tmp)["polarity"], rig_config.ATTRACT)

    def test_the_pol_command(self):
        self.assertEqual(rig_config.pol_command(rig_config.ATTRACT), "POL 0")
        self.assertEqual(rig_config.pol_command(rig_config.REPEL), "POL 1")

    def test_setting_it_sends_pol(self):
        robot = current_robot()
        robot.set_polarity(rig_config.REPEL)
        self.assertEqual(robot._link.commands, ["POL 1"])
        self.assertEqual(robot.polarity, rig_config.REPEL)

    def test_an_unknown_polarity_is_refused_before_anything_is_sent(self):
        robot = current_robot()
        with self.assertRaises(ValueError):
            robot.set_polarity("sideways")
        self.assertEqual(robot._link.commands, [])
        self.assertIsNone(robot.polarity)


if __name__ == "__main__":
    unittest.main()


class TestTheSketchMatchesTheMotionSettings(unittest.TestCase):
    """DWELL exists, the sketch's defaults are rig.py's, and it starts at the
    same 40mm/s the host sends."""

    @classmethod
    def setUpClass(cls):
        cls.sketch = (Path(__file__).resolve().parent.parent
                      / "firmware" / "chessbot_v1" / "chessbot_v1.ino").read_text()

    def _const(self, name):
        match = re.search(rf"const\s+int\s+{name}\s*=\s*(\d+)", self.sketch)
        self.assertIsNotNone(match, name)
        return int(match.group(1))

    def test_the_verbs_exist(self):
        self.assertIn('cmd == "DWELL"', self.sketch)

    def test_the_pol_verb_exists(self):
        self.assertIn('cmd == "POL"', self.sketch)

    def test_the_sketch_default_matches_rig(self):
        match = re.search(r"const\s+bool\s+HOLD_BY_REPEL\s*=\s*(true|false)", self.sketch)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1) == "true", rig.HOLD_BY_REPEL)

    def test_the_removed_verbs_are_gone(self):
        for verb in ("POLTEST", "RELEASE", "KICK"):
            self.assertNotIn(f'cmd == "{verb}"', self.sketch, verb)

    def test_the_defaults_match_rig(self):
        self.assertEqual(self._const("GRIP_MS"), rig.MOTION_TUNING["grip_ms"][0])
        self.assertEqual(self._const("SETTLE_MS"), rig.MOTION_TUNING["settle_ms"][0])

    def test_the_gridline_power_defaults_to_full_and_matches_rig(self):
        """60% and then 80% both sometimes lost a knight partway along the L."""
        self.assertEqual(self._const("GRID_PCT"), rig.MOTION_TUNING["grid_pct"][0])
        self.assertEqual(rig.MOTION_TUNING["grid_pct"], (100, 0, 100))
        self.assertEqual(self._const("MAG_FULL"), rig.MAG_FULL)
        self.assertIn('cmd == "GRID"', self.sketch)

    def test_every_gridline_leg_uses_the_tunable_power(self):
        self.assertNotIn("MAG_DIAG", self.sketch)
        self.assertEqual(self.sketch.count("magHold(gridDuty())"), 3)

    def test_the_settle_wait_is_1200_ms(self):
        """300 and then 1000 ms sometimes left the core magnetised enough to
        tow a piece."""
        self.assertEqual(rig.MOTION_TUNING["settle_ms"][0], 1200)

    def test_the_feed_rate_matches_rig(self):
        match = re.search(r"float\s+feedRateMMS\s*=\s*([\d.]+)", self.sketch)
        self.assertEqual(float(match.group(1)), rig.FEED_MMS)
        self.assertEqual(rig.FEED_MMS, 40.0)

    def test_the_speed_ramp_matches_rig(self):
        for name, value in (("START_MMS", rig.START_MMS), ("ACCEL_MMS2", rig.ACCEL_MMS2)):
            match = re.search(rf"const\s+float\s+{name}\s*=\s*([\d.]+)", self.sketch)
            self.assertIsNotNone(match, name)
            self.assertEqual(float(match.group(1)), value, name)
        self.assertLess(rig.START_MMS, rig.FEED_MMS)

    def test_every_step_is_timed_by_the_ramp(self):
        """No leg may start or stop at full feed any more."""
        self.assertNotIn("stepDelayUS", self.sketch)
        self.assertEqual(self.sketch.count("delayMicroseconds(halfUS);"), 2)

    def test_the_ramp_fits_a_half_square_step(self):
        """A weave's shortest leg is 25 mm; the ramp up and back down to
        full feed has to be short enough that it still reaches cruise."""
        ramp_mm = (rig.FEED_MMS ** 2 - rig.START_MMS ** 2) / (2 * rig.ACCEL_MMS2)
        self.assertLess(2 * ramp_mm, rig.SQUARE_MM / 2)


class TestTheSketchCarriesEveryPieceTheSameWay(unittest.TestCase):
    """r7: coil off -> drive to the source -> hold + grip -> carry ->
    coil off + settle. Checked in the source, since there is no Uno here."""

    @classmethod
    def setUpClass(cls):
        cls.sketch = (Path(__file__).resolve().parent.parent
                      / "firmware" / "chessbot_v1" / "chessbot_v1.ino").read_text()

    def _body(self, signature):
        # The definition, not a forward declaration: a "(...) {" line.
        start = re.search(re.escape(signature) + r"[^;{]*\{", self.sketch).start()
        end = self.sketch.index("\n}\n", start)
        return self.sketch[start:end]

    def test_every_carry_forces_the_coil_off_before_driving_to_the_piece(self):
        for fn in ("bool doMove(", "bool doKnight(", "bool doBury("):
            body = self._body(fn)
            self.assertIn("magOff();", body, fn)
            self.assertLess(body.index("magOff();"),
                            body.index("if (!gotoSquare(f0, r0))"), fn)

    def test_every_carry_holds_through_magHold(self):
        """So the one POL setting decides the polarity of every carry --
        none of them may drive the coil a fixed way on its own."""
        for fn in ("bool doMove(", "bool doKnight(", "bool doBury(",
                   "void magRelease("):
            body = self._body(fn)
            self.assertNotIn("magRepel", body, fn)
            self.assertNotIn("magAttract", body, fn)

    def test_the_hold_follows_the_one_setting(self):
        body = self._body("void magHold(")
        self.assertIn("if (holdByRepel) magRepel(duty);", body)
        self.assertIn("magAttract(duty);", body)

    def test_the_release_is_a_plain_off_then_the_settle_pause(self):
        body = self._body("void magRelease(")
        self.assertIn("magOff();", body)
        self.assertIn("delay(settleMS);", body)
        self.assertLess(body.index("magOff();"), body.index("delay(settleMS);"))
        self.assertNotIn("magHold", body)

    def test_there_is_no_colour_left_in_the_protocol(self):
        for gone in ("whiteReversed", "WHITE_IS_REVERSED", "parseColour", "reversed"):
            self.assertNotIn(gone, self.sketch, gone)
