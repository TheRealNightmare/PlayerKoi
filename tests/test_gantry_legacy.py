"""Tests for the ChessBot-V1 serial link.

The framing is inherited from GantryLink and already covered by
test_robot.py; what's specific here is the translation of the handful of
verbs the upper layers emit, and the READY banner, which this firmware
spells differently ("READY ChessBot-V1" vs a bare "READY").

No serial port -- a fake stands in for pyserial.
"""

import os
import sys
import unittest
from threading import Lock
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import gantry_legacy  # noqa: E402
import robot as robot_mod  # noqa: E402


class _FakeSerial:
    """Replies to each written command from a scripted queue."""

    def __init__(self, replies):
        self.written = []
        self._replies = list(replies)

    def write(self, raw):
        self.written.append(raw.decode().strip())

    def flush(self):
        pass

    def reset_input_buffer(self):
        pass

    def readline(self):
        if not self._replies:
            return b""
        return (self._replies.pop(0) + "\n").encode()

    def close(self):
        pass


def _link(replies):
    """A LegacyGantryLink wrapped around a fake port, skipping __init__ so
    nothing tries to open real hardware."""
    link = gantry_legacy.LegacyGantryLink.__new__(gantry_legacy.LegacyGantryLink)
    link._serial = _FakeSerial(replies)
    link._timeout = 1.0
    link.port = "fake"
    link._lock = Lock()
    return link


class TestTranslation(unittest.TestCase):
    def test_off_becomes_mag_0(self):
        # This firmware has no bare "coil off" verb.
        link = _link(["OK MAG 0"])
        link.send("OFF")
        self.assertEqual(link._serial.written, ["MAG 0"])

    def test_status_becomes_pos(self):
        link = _link(["OK POS 0.0 0.0"])
        link.send("STATUS")
        self.assertEqual(link._serial.written, ["POS"])

    def test_moves_pass_through_untouched(self):
        link = _link(["OK MOVE e2e4", "OK KNIGHT b1c3", "OK PULSE"])
        for command in ("MOVE e2e4", "KNIGHT b1c3", "PULSE"):
            link.send(command)
        self.assertEqual(link._serial.written, ["MOVE e2e4", "KNIGHT b1c3", "PULSE"])

    def test_a_command_merely_starting_with_a_translated_word_is_untouched(self):
        link = _link(["OK SPEED 40"])
        link.send("SPEED 40")
        self.assertEqual(link._serial.written, ["SPEED 40"])


class TestReplies(unittest.TestCase):
    def test_an_err_reply_raises(self):
        link = _link(["ERR out of range"])
        with self.assertRaises(robot_mod.GantryError):
            link.send("GOTO z9")

    def test_position_parses_the_pos_reply(self):
        link = _link(["OK POS -210.0 30.0"])
        self.assertEqual(link.position(), (-210.0, 30.0))

    def test_an_unparsable_position_is_an_error_not_a_crash(self):
        link = _link(["OK POS"])
        with self.assertRaises(robot_mod.GantryError):
            link.position()

    def test_a_mid_session_reset_is_reported(self):
        # The board rebooted, so its dead-reckoned position is gone. Saying
        # so beats carrying on with coordinates that are now fiction.
        link = _link(["READY ChessBot-V1"])
        with self.assertRaises(robot_mod.GantryError) as caught:
            link.send("MOVE e2e4")
        self.assertIn("reset", str(caught.exception))


class TestReadyBanner(unittest.TestCase):
    """_await_ready was relaxed to startswith so both firmwares are
    accepted; these pin that both still are."""

    def _await(self, banner):
        link = gantry_legacy.LegacyGantryLink.__new__(gantry_legacy.LegacyGantryLink)
        link._serial = _FakeSerial([banner])
        link.port = "fake"
        link._await_ready()

    def test_accepts_the_chessbot_v1_banner(self):
        self._await("READY ChessBot-V1")

    def test_still_accepts_the_bare_banner(self):
        self._await("READY")

    def test_rejects_silence(self):
        link = gantry_legacy.LegacyGantryLink.__new__(gantry_legacy.LegacyGantryLink)
        link._serial = _FakeSerial([])
        link.port = "fake"
        # Shortened from the real 8s budget, which exists for the Uno's
        # bootloader delay and would just make the suite slow here.
        with mock.patch.object(robot_mod, "READY_TIMEOUT_S", 0.05):
            with self.assertRaises(robot_mod.GantryError):
                link._await_ready()


class TestAbort(unittest.TestCase):
    def test_abort_sends_nothing_and_does_not_raise(self):
        # Documents the real limitation: no soft e-stop on this firmware, so
        # abort cannot interrupt a move in flight. It must still be safe to
        # call, because Robot.halt() always does.
        link = _link([])
        link.abort()
        self.assertEqual(link._serial.written, [])


if __name__ == "__main__":
    unittest.main()
