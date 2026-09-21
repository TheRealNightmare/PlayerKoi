"""Tests for session.Session -- the menu/mode state machine.

No camera, no serial port, no engine binary: Session takes its mode builders
as callables precisely so the whole lifecycle can be driven with stubs. What
is being tested is the part that is easy to get wrong and expensive to debug
on the rig -- that switching modes leaks nothing, parks the arm, and never
leaves the robot thread blocked on a prompt nobody will answer.
"""

import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import chess  # noqa: E402

import rig  # noqa: E402
import session as session_mod  # noqa: E402
from headless_loop import HeadlessLoop  # noqa: E402
from robot import MockGantry, Robot, RobotController  # noqa: E402
from session import AI_VS_AI, MENU, NORMAL, ModeError, Session  # noqa: E402
import robot_moves_legacy  # noqa: E402


# Temp config files handed to the Robots these tests build; removed at exit so
# a run leaves nothing behind.
_TEMP_CONFIGS = []


def tearDownModule():
    for path in _TEMP_CONFIGS:
        try:
            os.unlink(path)
        except OSError:
            pass
    _TEMP_CONFIGS.clear()


class StubEngine:
    available = True
    error = None
    skill = 3

    def __init__(self):
        self.closed = False

    def set_skill(self, n):
        self.skill = n

    def best_move(self, board, think_s, root_moves=None):
        return None

    def top_moves(self, board, think_s, count=5, root_moves=None):
        return []

    def close(self):
        self.closed = True


class StubEngineController:
    """Stands in for web_ui.EngineController: records the calls Session makes
    and whether it was shut down."""

    def __init__(self):
        self.enabled = None
        self.closed = False
        self.robot = "unset"

    def configure(self, enabled=None, skill=None):
        if enabled is not None:
            self.enabled = enabled

    def attach_robot(self, robot):
        self.robot = robot

    def close(self):
        self.closed = True


class StubStream:
    def __init__(self):
        self.closed = False

    def get_latest(self):
        return None, None

    def close(self):
        self.closed = True


def make_session(with_robot=True, calibration=None, classifier=None, engine=None,
                 ticking=False):
    """A Session whose AI mode is a HeadlessLoop and whose 'normal' mode is a
    HeadlessLoop plus a fake camera stream -- close enough in shape to test
    the lifecycle without importing picamera2."""
    engine = engine or StubEngine()
    robot = None
    if with_robot:
        # A temp path for the graveyard pile. Session.reset() clears it, which
        # writes to disk -- without this the suite edits the repo's real
        # config/rig.json every run.
        fd, config_path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        _TEMP_CONFIGS.append(config_path)
        robot = Robot(MockGantry(), planner=robot_moves_legacy,
                      config_path=config_path)
    built = []

    def build_ai(settings):
        controller = StubEngineController()
        built.append((HeadlessLoop(), None, controller, None))
        return built[-1]

    def build_normal(settings):
        controller = StubEngineController()
        ticks = []
        tick = (lambda: ticks.append(1)) if ticking else None
        built.append((HeadlessLoop(), StubStream(), controller, tick))
        return built[-1]

    sess = Session(
        engine, robot=robot, calibration=calibration, classifier=classifier,
        build_normal=build_normal, build_ai=build_ai, poll_interval=0.01,
    )
    sess._built = built  # for assertions
    return sess


class TestAvailability(unittest.TestCase):
    def test_ai_needs_an_arm(self):
        sess = make_session(with_robot=False)
        self.assertIn("no arm", sess.unavailable_reason(AI_VS_AI))

    def test_ai_is_available_with_engine_and_arm(self):
        self.assertIsNone(make_session().unavailable_reason(AI_VS_AI))

    def test_normal_names_the_missing_calibration_and_its_script(self):
        reason = make_session().unavailable_reason(NORMAL)
        self.assertIn("calibration", reason)
        self.assertIn("calibrate.py", reason)

    def test_normal_names_the_missing_classifier_next(self, ):
        sess = make_session(calibration=__file__)  # any file that exists
        reason = sess.unavailable_reason(NORMAL)
        self.assertIn("classifier", reason)

    def test_normal_is_available_when_both_files_exist(self):
        sess = make_session(calibration=__file__, classifier=__file__)
        self.assertIsNone(sess.unavailable_reason(NORMAL))

    def test_a_missing_engine_blocks_both(self):
        engine = StubEngine()
        engine.available = False
        engine.error = "stockfish not found"
        sess = make_session(engine=engine, calibration=__file__, classifier=__file__)
        self.assertEqual(sess.unavailable_reason(AI_VS_AI), "stockfish not found")
        self.assertEqual(sess.unavailable_reason(NORMAL), "stockfish not found")

    def test_the_menu_payload_carries_a_reason_per_mode(self):
        modes = {m["mode"]: m for m in make_session().available_modes()}
        self.assertTrue(modes[AI_VS_AI]["available"])
        self.assertFalse(modes[NORMAL]["available"])
        self.assertIsNone(modes[AI_VS_AI]["reason"])
        self.assertTrue(modes[NORMAL]["reason"])


class TestStartAndStop(unittest.TestCase):
    def test_it_starts_on_the_menu_with_nothing_built(self):
        sess = make_session()
        self.assertEqual(sess.mode, MENU)
        self.assertFalse(sess.running)
        self.assertIsNone(sess.loop)
        self.assertIsNone(sess.robot_controller)

    def test_starting_builds_the_loop_and_a_robot_controller(self):
        sess = make_session()
        sess.start(AI_VS_AI)
        self.assertEqual(sess.mode, AI_VS_AI)
        self.assertIsInstance(sess.loop, HeadlessLoop)
        self.assertIsInstance(sess.robot_controller, RobotController)

    def test_the_engine_controller_is_given_the_arm(self):
        """It is built before the RobotController exists, so the link has to
        be made by Session or the arm never moves."""
        sess = make_session()
        sess.start(AI_VS_AI)
        self.assertIs(sess.engine_controller.robot, sess.robot_controller)

    def test_starting_an_unavailable_mode_is_refused(self):
        sess = make_session()
        with self.assertRaises(ModeError):
            sess.start(NORMAL)
        self.assertEqual(sess.mode, MENU)

    def test_starting_twice_is_refused(self):
        sess = make_session()
        sess.start(AI_VS_AI)
        with self.assertRaises(ModeError):
            sess.start(AI_VS_AI)

    def test_an_unknown_mode_is_refused(self):
        with self.assertRaises(ModeError):
            make_session().start("chess960")

    def test_stopping_returns_to_the_menu_and_clears_everything(self):
        sess = make_session()
        sess.start(AI_VS_AI)
        sess.stop()
        self.assertEqual(sess.mode, MENU)
        self.assertIsNone(sess.loop)
        self.assertIsNone(sess.engine_controller)
        self.assertIsNone(sess.robot_controller)

    def test_stopping_on_the_menu_is_harmless(self):
        sess = make_session()
        self.assertEqual(sess.stop()["mode"], MENU)

    def test_stopping_shuts_the_engine_controller_down(self):
        """Not doing this leaks a thread per switch that goes on driving the
        old mode's loop."""
        sess = make_session()
        sess.start(AI_VS_AI)
        controller = sess.engine_controller
        sess.stop()
        self.assertTrue(controller.closed)
        self.assertIs(controller.enabled, False)

    def test_stopping_closes_the_camera_stream(self):
        sess = make_session(calibration=__file__, classifier=__file__)
        sess.start(NORMAL)
        stream = sess.capture_stream
        sess.stop()
        self.assertTrue(stream.closed)

    def test_the_game_is_discarded_between_runs(self):
        sess = make_session()
        sess.start(AI_VS_AI)
        sess.loop.set_expected_move(chess.Move.from_uci("e2e4"))
        sess.loop.force_settle()
        sess.stop()
        sess.start(AI_VS_AI)
        self.assertEqual(sess.loop.turn, "white")
        self.assertFalse(sess.loop.board_copy.move_stack)


class TestParking(unittest.TestCase):
    def _commands(self, sess):
        return sess.robot._link.commands if sess.robot else []

    def test_stopping_parks_the_carriage(self):
        sess = make_session()
        sess.start(AI_VS_AI)
        sess.robot.home()  # the launch-time home
        before = len(sess.robot._link.commands)
        sess.stop()
        self.assertIn("HOME", sess.robot._link.commands[before:])

    def test_stop_can_skip_parking(self):
        sess = make_session()
        sess.start(AI_VS_AI)
        sess.robot.home()
        before = len(sess.robot._link.commands)
        sess.stop(park=False)
        self.assertNotIn("HOME", sess.robot._link.commands[before:])

    def test_reset_parks_and_starts_a_new_game(self):
        sess = make_session()
        sess.start(AI_VS_AI)
        sess.robot.home()
        sess.loop.set_expected_move(chess.Move.from_uci("e2e4"))
        sess.loop.force_settle()
        before = len(sess.robot._link.commands)

        ok, error = sess.reset()
        self.assertTrue(ok, error)
        self.assertIn("HOME", sess.robot._link.commands[before:])
        self.assertEqual(sess.loop.turn, "white")
        self.assertFalse(sess.loop.board_copy.move_stack)
        self.assertEqual(sess.mode, AI_VS_AI)  # stays in the mode

    def test_reset_from_the_menu_only_parks(self):
        sess = make_session()
        sess.robot.home()
        before = len(sess.robot._link.commands)
        ok, _ = sess.reset()
        self.assertTrue(ok)
        self.assertIn("HOME", sess.robot._link.commands[before:])
        self.assertEqual(sess.mode, MENU)

    def test_reset_turns_the_engine_off_first(self):
        """Otherwise the next move starts while the arm is crossing the
        board to park."""
        sess = make_session()
        sess.start(AI_VS_AI)
        sess.robot.home()
        sess.reset()
        self.assertIs(sess.engine_controller.enabled, False)

    def test_parking_with_no_arm_is_a_no_op(self):
        sess = make_session(with_robot=False, calibration=__file__, classifier=__file__)
        sess.start(NORMAL)
        self.assertEqual(sess.park(), (True, None))


class TestNoLeaks(unittest.TestCase):
    def test_switching_modes_repeatedly_leaks_no_threads(self):
        sess = make_session(calibration=__file__, classifier=__file__, ticking=True)
        sess.robot.home()
        baseline = threading.active_count()
        for _ in range(5):
            sess.start(AI_VS_AI)
            sess.stop()
            sess.start(NORMAL)
            sess.stop()
        # The tick threads are joined on stop; allow a little slack for
        # interpreter-internal threads rather than asserting exact equality.
        self.assertLessEqual(threading.active_count(), baseline + 1)

    def test_the_tick_thread_stops_with_the_mode(self):
        sess = make_session(calibration=__file__, classifier=__file__, ticking=True)
        sess.start(NORMAL)
        self.assertIsNotNone(sess._tick_thread)
        thread = sess._tick_thread
        sess.stop()
        time.sleep(0.05)
        self.assertFalse(thread.is_alive())

    def test_ai_mode_starts_no_tick_thread(self):
        """There is nothing to poll without a camera."""
        sess = make_session()
        sess.start(AI_VS_AI)
        self.assertIsNone(sess._tick_thread)


class TestStoppingWhileBlocked(unittest.TestCase):
    """A capture prompt blocks the robot thread with no timeout, on purpose.
    Tearing the mode down has to release it, or that thread holds the gantry
    forever and the next mode can never move."""

    def test_a_blocked_prompt_is_released_and_cancelled(self):
        sess = make_session()
        sess.start(AI_VS_AI)
        sess.robot.home()
        controller = sess.robot_controller

        board = chess.Board("rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2")
        move = chess.Move.from_uci("e4d5")
        # A capture only blocks when there is nowhere to put the victim: the
        # arm otherwise buries it on a free graveyard slot and never waits.
        # Fill the ring, which is what this test actually needs -- some step
        # that stops and asks, so stop() can be shown to release it.
        sess.robot.graveyard = list(range(rig.GRAVEYARD_SLOTS))
        sess.loop.set_expected_move(move)
        done = threading.Event()
        result = {}

        def play():
            result["ok"], result["error"] = controller.execute(board, move)
            done.set()

        threading.Thread(target=play, daemon=True).start()
        for _ in range(200):  # wait for it to reach the prompt
            if controller.state()["awaiting_confirm"]:
                break
            time.sleep(0.01)
        self.assertTrue(controller.state()["awaiting_confirm"])

        sess.stop()
        self.assertTrue(done.wait(timeout=5.0), "the robot thread was left blocked")
        self.assertFalse(result["ok"])

    def test_the_next_mode_starts_afterwards(self):
        sess = make_session()
        sess.start(AI_VS_AI)
        sess.stop()
        sess.start(AI_VS_AI)
        self.assertEqual(sess.mode, AI_VS_AI)


class TestClose(unittest.TestCase):
    def test_close_stops_the_mode_and_the_engine(self):
        engine = StubEngine()
        sess = make_session(engine=engine)
        sess.start(AI_VS_AI)
        sess.close()
        self.assertEqual(sess.mode, MENU)
        self.assertTrue(engine.closed)

    def test_close_parks_then_drops_the_coil(self):
        """OFF, not MAG 0: the translation to the firmware's spelling happens
        in LegacyGantryLink, below MockGantry."""
        sess = make_session()
        sess.start(AI_VS_AI)
        sess.close()
        commands = sess.robot._link.commands
        self.assertIn("HOME", commands)
        self.assertIn("OFF", commands)
        self.assertLess(commands.index("HOME"), commands.index("OFF"))


if __name__ == "__main__":
    unittest.main()
