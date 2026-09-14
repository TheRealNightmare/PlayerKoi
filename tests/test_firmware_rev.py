"""Tests for the firmware-revision handshake and the saved polarity.

Both exist because of one silent failure: r1 of the sketch accepts
"MOVE e2e4 w" -- parsePly reads four characters and ignores the rest -- and
then attracts for every move, which shoves every white piece off its square.
The host believed it had sent a polarity; the board had never heard of one;
nothing anywhere said so.

So the point of these is not that the numbers parse. It is that a stale board
refuses to move, that homing cannot talk it out of that, and that the setting
survives a restart.
"""

import json
import os
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

    def test_this_code_expects_at_least_the_polarity_revision(self):
        self.assertGreaterEqual(rig.FIRMWARE_REV, 2)


class TestStaleBoardRefuses(unittest.TestCase):
    def test_a_current_board_is_not_stale(self):
        robot = current_robot()
        self.assertEqual(robot.firmware_rev, rig.FIRMWARE_REV)
        self.assertFalse(robot.stale_firmware)

    def test_an_r1_board_is_stale(self):
        robot = stale_robot()
        self.assertEqual(robot.firmware_rev, 1)
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


class TestSavedPolarity(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "rig.json"

    def test_it_defaults_to_the_measured_value(self):
        """rig.WHITE_IS_REVERSED is the measurement; the file is only an
        override for it."""
        expected = rig_config.REPEL if rig.WHITE_IS_REVERSED else rig_config.ATTRACT
        self.assertEqual(rig_config.load(self.tmp)["white_polarity"], expected)

    def test_it_round_trips(self):
        rig_config.save(rig_config.ATTRACT, self.tmp)
        self.assertEqual(rig_config.load(self.tmp)["white_polarity"], rig_config.ATTRACT)
        rig_config.save(rig_config.REPEL, self.tmp)
        self.assertEqual(rig_config.load(self.tmp)["white_polarity"], rig_config.REPEL)

    def test_a_corrupt_file_falls_back_instead_of_raising(self):
        """A rig that won't start because its config has a stray comma is
        worse than one that starts with the compiled-in value."""
        self.tmp.write_text("{ not json")
        self.assertIn("white_polarity", rig_config.load(self.tmp))

    def test_an_unknown_value_in_the_file_is_ignored(self):
        self.tmp.write_text(json.dumps({"white_polarity": "sideways"}))
        self.assertIn(rig_config.load(self.tmp)["white_polarity"], rig_config.POLARITIES)

    def test_saving_nonsense_is_refused(self):
        with self.assertRaises(ValueError):
            rig_config.save("sideways", self.tmp)

    def test_the_pol_command_matches_the_polarity(self):
        """POL 1 means white is reversed, i.e. held by repel."""
        self.assertEqual(rig_config.pol_command(rig_config.REPEL), "POL 1")
        self.assertEqual(rig_config.pol_command(rig_config.ATTRACT), "POL 0")


class TestPolarityReachesTheBoard(unittest.TestCase):
    def test_setting_it_sends_pol(self):
        robot = current_robot()
        robot.set_white_polarity(rig_config.ATTRACT)
        self.assertIn("POL 0", robot._link.commands)
        self.assertEqual(robot.white_polarity, rig_config.ATTRACT)

    def test_an_unknown_polarity_is_refused_before_anything_is_sent(self):
        robot = current_robot()
        with self.assertRaises(ValueError):
            robot.set_white_polarity("sideways")
        self.assertEqual(robot._link.commands, [])

    def test_open_gantry_pushes_the_saved_value_on_connect(self):
        """Opening the port reboots the Uno, so whatever it was told last
        time is gone. If this stops happening, the board silently reverts to
        its compiled default."""
        from robot import open_gantry

        robot = open_gantry("mock", white_polarity=rig_config.ATTRACT)
        self.assertEqual(robot._link.commands, ["POL 0"])
        self.assertEqual(robot.white_polarity, rig_config.ATTRACT)

    def test_a_stale_board_is_not_sent_pol(self):
        """r1 has no POL verb and would answer ERR, which would look like a
        dead link rather than old firmware."""
        robot = stale_robot()
        self.assertEqual(robot._link.commands, [])
        self.assertIsNone(robot.white_polarity)


if __name__ == "__main__":
    unittest.main()
