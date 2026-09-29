"""Tests for /admin: the command builder, and AdminConsole's safety rules
over a MockGantry.

The failure worth pinning: a click on e4 that reaches the firmware
unrotated, or a hand-driven move in a running game that leaves the tracked
board disagreeing with the real one.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import admin_moves  # noqa: E402
import rig  # noqa: E402
import robot as robot_mod  # noqa: E402
import robot_moves_legacy  # noqa: E402
import web_ui  # noqa: E402
from headless_loop import HeadlessLoop  # noqa: E402


class _OriginMixin:
    origin = "h1"

    def setUp(self):
        self._saved = rig.ORIGIN_SQUARE
        rig.ORIGIN_SQUARE = self.origin

    def tearDown(self):
        rig.ORIGIN_SQUARE = self._saved


class TestCommands(_OriginMixin, unittest.TestCase):
    def test_goto(self):
        self.assertEqual(admin_moves.goto_command("e4"), "GOTO e4")

    def test_straight_and_weave(self):
        self.assertEqual(admin_moves.move_command("e2", "e4", "white", False), "MOVE e2e4 w")
        self.assertEqual(admin_moves.move_command("g8", "f6", "black", True), "KNIGHT g8f6 b")

    def test_bad_input_is_refused(self):
        for args in (("e2", "e2", "white", False), ("e9", "e4", "white", False),
                     ("e2", "e4", "red", False), (None, "e4", "white", False)):
            with self.assertRaises(ValueError, msg=args):
                admin_moves.move_command(*args)
        with self.assertRaises(ValueError):
            admin_moves.goto_command("z1")

    def test_mag(self):
        self.assertEqual(admin_moves.mag_command("off"), "MAG 0")
        self.assertEqual(admin_moves.mag_command("attract"), "MAG 1")
        self.assertEqual(admin_moves.mag_command("repel"), "MAG 2")
        with self.assertRaises(ValueError):
            admin_moves.mag_command("on")

    def test_parse_pos(self):
        self.assertEqual(admin_moves.parse_pos("POS -30.0 60.5"), (-30.0, 60.5))
        self.assertIsNone(admin_moves.parse_pos(""))


class TestCommandsRotated(_OriginMixin, unittest.TestCase):
    origin = "a8"

    def test_squares_are_oriented_like_the_game_path(self):
        self.assertEqual(admin_moves.goto_command("e4"), f"GOTO {rig.orient_square('e4')}")
        self.assertEqual(admin_moves.move_command("e2", "e4", "white", False),
                         f"MOVE {rig.orient_uci('e2e4')} w")
        self.assertNotEqual(admin_moves.goto_command("e4"), "GOTO e4")


class TestMatrix(unittest.TestCase):
    def setUp(self):
        self.matrix = HeadlessLoop().current_matrix

    def test_moves_the_label(self):
        moved, error = admin_moves.apply_to_matrix(self.matrix, "e2", "e4")
        self.assertIsNone(error)
        self.assertIsNone(moved[1][4])            # e2
        self.assertEqual(moved[3][4], "white-pawn")  # e4
        self.assertEqual(self.matrix[1][4], "white-pawn")  # input untouched

    def test_refuses_empty_from_and_occupied_to(self):
        self.assertIsNone(admin_moves.apply_to_matrix(self.matrix, "e4", "e5")[0])
        self.assertIsNone(admin_moves.apply_to_matrix(self.matrix, "e1", "e2")[0])


class _FakeSession:
    def __init__(self, robot, loop=None, controller=None):
        self.robot = robot
        self.loop = loop
        self.engine_controller = controller
        self.robot_controller = None
        self.parked = 0

    @property
    def mode(self):
        return "menu" if self.loop is None else "ai_vs_ai"

    @property
    def running(self):
        return self.loop is not None

    def park(self):
        self.parked += 1
        self.robot.home()
        return True, None


class _FakeEngine:
    def __init__(self):
        self.enabled = True

    def configure(self, enabled=None, skill=None):
        if enabled is not None:
            self.enabled = enabled


class TestConsole(_OriginMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.gantry = robot_mod.MockGantry()
        self.robot = robot_mod.Robot(self.gantry, planner=robot_moves_legacy,
                                     config_path=os.path.join(self.tmp.name, "rig.json"))
        self.robot.home()
        self.gantry.commands.clear()

    def tearDown(self):
        self.tmp.cleanup()
        super().tearDown()

    def test_goto_on_the_menu(self):
        console = web_ui.AdminConsole(_FakeSession(self.robot))
        status, payload = console.action({"action": "goto", "square": "e4"})
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.gantry.commands, ["GOTO e4", "POS"])
        self.assertEqual(payload["log"][-1]["command"], "GOTO e4")

    def test_not_homed_is_refused_but_magnet_off_still_works(self):
        self.robot.halt("test")
        console = web_ui.AdminConsole(_FakeSession(self.robot))
        status, _ = console.action({"action": "goto", "square": "e4"})
        self.assertEqual(status, 400)
        self.assertNotIn("GOTO e4", self.gantry.commands)
        status, _ = console.action({"action": "mag", "mode": "off"})
        self.assertEqual(status, 200)
        self.assertIn("MAG 0", self.gantry.commands)

    def test_busy_arm_is_refused(self):
        self.robot.busy = True
        console = web_ui.AdminConsole(_FakeSession(self.robot))
        status, _ = console.action({"action": "goto", "square": "e4"})
        self.assertEqual(status, 409)
        self.assertEqual(self.gantry.commands, [])

    def test_move_in_a_game_pauses_the_engine_and_updates_the_board(self):
        loop = HeadlessLoop()
        engine = _FakeEngine()
        console = web_ui.AdminConsole(_FakeSession(self.robot, loop, engine))
        status, payload = console.action(
            {"action": "move", "from": "e2", "to": "e3", "colour": "white"})
        self.assertEqual(status, 200, payload)
        self.assertFalse(engine.enabled)
        self.assertTrue(payload["engine_paused"])
        self.assertIn("MOVE e2e3 w", self.gantry.commands)
        self.assertEqual(loop.current_matrix[2][4], "white-pawn")
        self.assertIsNone(loop.current_matrix[1][4])
        self.assertFalse(loop.is_paused)

    def test_move_onto_a_piece_in_a_game_never_reaches_the_arm(self):
        loop = HeadlessLoop()
        console = web_ui.AdminConsole(_FakeSession(self.robot, loop, _FakeEngine()))
        status, payload = console.action(
            {"action": "move", "from": "d1", "to": "d2", "colour": "white"})
        self.assertEqual(status, 400)
        self.assertIn("occupied", payload["error"])
        self.assertEqual(self.gantry.commands, [])

    def test_a_firmware_error_halts_and_drops_the_coil(self):
        class Angry(robot_mod.MockGantry):
            def send(self, command):
                if command.startswith("MOVE"):
                    self.commands.append(command)
                    raise robot_mod.GantryError("ERR blocked")
                return super().send(command)

        gantry = Angry()
        robot = robot_mod.Robot(gantry, planner=robot_moves_legacy,
                                config_path=os.path.join(self.tmp.name, "rig.json"))
        robot.home()
        console = web_ui.AdminConsole(_FakeSession(robot))
        status, _ = console.action({"action": "move", "from": "a1", "to": "a3",
                                    "colour": "white"})
        self.assertEqual(status, 400)
        self.assertTrue(robot.halted)
        self.assertEqual(gantry.commands[-1], "OFF")
        self.assertFalse(robot.busy)

    def test_halt_and_home(self):
        session = _FakeSession(self.robot)
        console = web_ui.AdminConsole(session)
        self.assertEqual(console.action({"action": "halt"})[0], 200)
        self.assertTrue(self.robot.halted)
        self.assertEqual(console.action({"action": "home"})[0], 200)
        self.assertTrue(self.robot.homed)
        self.assertEqual(session.parked, 1)

    def test_no_robot(self):
        console = web_ui.AdminConsole(_FakeSession(None))
        self.assertEqual(console.action({"action": "goto", "square": "e4"})[0], 400)


if __name__ == "__main__":
    unittest.main()


class TestDefaultOrigin(unittest.TestCase):
    """The reported bug: a1 on /admin went past a8. At the rig's default
    origin the page must send exactly what the serial monitor would."""

    def test_squares_go_out_as_typed(self):
        self.assertEqual(rig.ORIGIN_SQUARE, "h1")
        self.assertEqual(admin_moves.goto_command("a1"), "GOTO a1")
        self.assertEqual(admin_moves.goto_command("h8"), "GOTO h8")
        self.assertEqual(admin_moves.move_command("e2", "e4", "white", False), "MOVE e2e4 w")


class TestOriginIsReported(unittest.TestCase):
    """A rotated origin sends every square to its diagonal opposite. That
    has to be visible on /admin, not discovered by watching the arm."""

    def setUp(self):
        self._saved = rig.ORIGIN_SQUARE

    def tearDown(self):
        rig.ORIGIN_SQUARE = self._saved

    def test_the_default_says_no_rotation(self):
        rig.ORIGIN_SQUARE = "h1"
        state = web_ui.AdminConsole(_FakeSession(None)).state()
        self.assertEqual(state["origin"], "h1")
        self.assertFalse(state["rotated"])
        self.assertEqual(state["sample"], {"a1": "a1", "h1": "h1", "h8": "h8", "a8": "a8"})
        self.assertTrue(state["version"])

    def test_a_rotated_launch_is_flagged(self):
        rig.ORIGIN_SQUARE = "a8"
        state = web_ui.AdminConsole(_FakeSession(None)).state()
        self.assertTrue(state["rotated"])
        self.assertEqual(state["sample"]["a1"], "h8")
        self.assertEqual(state["sample"]["h1"], "a8")
