"""Tests for the robot's execution layer -- Robot walking a plan over a
fake gantry, and the closed loop where the camera has to confirm what the
arm just did.

The failure this file exists to pin down: the arm moves, the board doesn't
match, and the software carries on playing into a position that no longer
exists. Every path where that could happen is asserted to halt instead.

No serial port and no camera -- MockGantry acks commands, and the tracking
loop's classifier is monkeypatched exactly as in test_tracking_loop.py.
"""

import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import chess  # noqa: E402
import numpy as np  # noqa: E402

import robot as robot_mod  # noqa: E402
import robot_moves  # noqa: E402
import robot_moves_legacy  # noqa: E402
import tracking_loop  # noqa: E402
from move_resolver import standard_starting_matrix  # noqa: E402
from robot import RobotController  # noqa: E402
import rig  # noqa: E402
import rig_config  # noqa: E402
from square_classifier import ALL_SQUARES, BLACK, EMPTY, WHITE  # noqa: E402


def setUpModule():
    """Pin the identity orientation for this whole file.

    rig.ORIGIN_SQUARE says how the board is seated under the gantry and
    rotates every square bound for the firmware (see rig.orient). That is a
    property of the machine, not of a plan, so these tests neutralise it and
    go on asserting the geometry they were written for. The rotation itself
    is tested in test_rig.py.
    """
    global _saved_origin
    _saved_origin = rig.ORIGIN_SQUARE
    rig.ORIGIN_SQUARE = "h1"


def tearDownModule():
    rig.ORIGIN_SQUARE = _saved_origin



def _matrix_state(matrix, square):
    file_idx, rank_idx = square
    label = matrix[rank_idx][file_idx]
    if label is None:
        return EMPTY
    return WHITE if label.startswith("white") else BLACK


def _consensus_for(matrix):
    return {square: _matrix_state(matrix, square) for square in ALL_SQUARES}


class _FakeCaptureStream:
    def __init__(self):
        self.frame = np.zeros((10, 10, 3), dtype=np.uint8)

    def get_latest(self):
        return self.frame, 0.0


class _AngryGantry(robot_mod.MockGantry):
    """Fails on the Nth command, the way a real one does mid-sequence."""

    def __init__(self, fail_on):
        super().__init__()
        self._fail_on = fail_on

    def send(self, command):
        super().send(command)
        if len(self.commands) == self._fail_on:
            raise robot_mod.GantryError(f"{command} -> ERR ABORT")
        return ""


def _make_robot(link=None, topple_delay_s=0.0):
    robot = robot_mod.Robot(link or robot_mod.MockGantry(), topple_delay_s=topple_delay_s)
    robot.home()
    return robot


class TestRobotExecution(unittest.TestCase):
    def test_play_sends_the_whole_plan_and_parks(self):
        robot = _make_robot()
        robot.play(chess.Board(), chess.Move.from_uci("e2e4"))

        self.assertEqual(
            robot._link.commands,
            [
                "HOME", "GOTO 4.00 1.00", "MAG 170", "GOTO 4.00 3.00", "PULSE", "MAG 0",
                "GOTO {:.2f} {:.2f}".format(*robot_moves.PARK),   # h1, the machine's origin
            ],
        )

    def test_the_legacy_planner_can_be_swapped_in(self):
        # Robot is planner-agnostic on purpose: the same execution layer
        # drives either firmware.
        robot = robot_mod.Robot(robot_mod.MockGantry(), planner=robot_moves_legacy)
        robot.home()
        robot.play(chess.Board(), chess.Move.from_uci("e2e4"))
        self.assertEqual(robot._link.commands, ["HOME", "MOVE e2e4 w"])

    def test_refuses_to_move_before_homing(self):
        robot = robot_mod.Robot(robot_mod.MockGantry())
        with self.assertRaises(robot_mod.GantryError):
            robot.play(chess.Board(), chess.Move.from_uci("e2e4"))
        self.assertEqual(robot._link.commands, [], "nothing may be sent from an unhomed state")

    def test_a_mid_sequence_failure_drops_the_coil_and_halts(self):
        # Fail on the 4th command -- GOTO destination, i.e. while gripping.
        robot = _make_robot(_AngryGantry(fail_on=4))
        with self.assertRaises(robot_mod.GantryError):
            robot.play(chess.Board(), chess.Move.from_uci("e2e4"))

        self.assertTrue(robot.halted)
        self.assertFalse(robot.homed, "an aborted move leaves the carriage nowhere known")
        self.assertEqual(robot._link.commands[-1], "OFF", "a halted arm must not keep gripping")

    def test_a_halted_robot_refuses_further_moves(self):
        robot = _make_robot(_AngryGantry(fail_on=4))
        with self.assertRaises(robot_mod.GantryError):
            robot.play(chess.Board(), chess.Move.from_uci("e2e4"))

        before = len(robot._link.commands)
        with self.assertRaises(robot_mod.GantryError):
            robot.play(chess.Board(), chess.Move.from_uci("d2d4"))
        self.assertEqual(len(robot._link.commands), before, "halted means halted")

    def test_homing_clears_a_halt(self):
        robot = _make_robot()
        robot.halt("test")
        self.assertTrue(robot.halted)

        robot.home()
        self.assertFalse(robot.halted)
        self.assertTrue(robot.homed)

    def test_halt_aborts_the_gantry(self):
        robot = _make_robot()
        robot.halt("stop now")
        self.assertIn("!", robot._link.commands)
        self.assertEqual(robot.message, "stop now")


class TestClosedLoop(unittest.TestCase):
    """RobotController + TrackingLoop: the arm moves, then the camera has to
    agree before the move counts."""

    def setUp(self):
        self.capture_stream = _FakeCaptureStream()
        self.updates = []

        def on_update(matrix, move_text, frame, flagged, reason):
            self.updates.append((move_text, flagged, reason))
            if flagged and self.controller is not None:
                self.controller.note_flag(reason)

        self.controller = None
        self.loop = tracking_loop.TrackingLoop(
            capture_stream=self.capture_stream,
            calibration_matrix=np.eye(3),
            image_size=(10, 10),
            classifier_model=None,
            on_update=on_update,
        )
        self.robot = _make_robot()
        self.controller = RobotController(self.robot, self.loop)

    def _camera_sees(self, matrix):
        return mock.patch.object(tracking_loop, "read_settled_state", return_value=_consensus_for(matrix))

    def test_a_move_the_camera_confirms_is_committed(self):
        board = self.loop.board_copy
        move = chess.Move.from_uci("e2e4")
        self.loop.set_expected_move(move)

        after = standard_starting_matrix()
        after[1][4] = None
        after[3][4] = "white-pawn"

        with self._camera_sees(after):
            ok, error = self.controller.execute(board, move)

        self.assertTrue(ok, error)
        self.assertFalse(self.robot.halted)
        self.assertEqual(self.updates[-1][0], "e4")
        self.assertEqual(self.loop.board_copy.move_stack[-1], move)

    def test_a_piece_that_lands_on_the_wrong_square_halts_the_arm(self):
        board = self.loop.board_copy
        move = chess.Move.from_uci("e2e4")
        self.loop.set_expected_move(move)

        # The belt slipped: the pawn ended on d4, not e4.
        wrong = standard_starting_matrix()
        wrong[1][4] = None
        wrong[3][3] = "white-pawn"

        with self._camera_sees(wrong):
            ok, error = self.controller.execute(board, move)

        self.assertFalse(ok)
        self.assertTrue(self.robot.halted, "a mismatch must stop the arm, not just report it")
        self.assertEqual(self.loop.board_copy.move_stack, [], "the bad position must not be committed")
        self.assertTrue(self.updates[-1][1], "the settle should have flagged")

    def test_a_move_that_did_not_happen_at_all_halts_the_arm(self):
        # Gantry reports success but nothing on the board moved -- a dropped
        # piece, or a magnet too weak to drag it.
        board = self.loop.board_copy
        move = chess.Move.from_uci("e2e4")
        self.loop.set_expected_move(move)

        with self._camera_sees(standard_starting_matrix()):
            ok, error = self.controller.execute(board, move)

        self.assertFalse(ok)
        self.assertTrue(self.robot.halted)
        self.assertEqual(self.loop.board_copy.move_stack, [])

    def test_tracking_is_paused_while_the_arm_moves_and_released_after(self):
        board = self.loop.board_copy
        move = chess.Move.from_uci("e2e4")
        self.loop.set_expected_move(move)
        seen = []

        original = self.robot.play

        def spy(*args, **kwargs):
            seen.append(self.loop.is_paused)
            return original(*args, **kwargs)

        after = standard_starting_matrix()
        after[1][4] = None
        after[3][4] = "white-pawn"

        with mock.patch.object(self.robot, "play", spy), self._camera_sees(after):
            self.controller.execute(board, move)

        self.assertEqual(seen, [True], "the tracker must be held still while the gantry moves")
        self.assertFalse(self.loop.is_paused, "and released afterwards")

    def test_a_halted_arm_will_not_start_another_move(self):
        self.robot.halt("previous failure")
        ok, error = self.controller.execute(self.loop.board_copy, chess.Move.from_uci("e2e4"))
        self.assertFalse(ok)
        self.assertIn("previous failure", error)


class TestForceSettle(unittest.TestCase):
    """force_settle exists because the motion gate eats the robot's own
    settle while tracking is paused."""

    def setUp(self):
        self.capture_stream = _FakeCaptureStream()
        self.loop = tracking_loop.TrackingLoop(
            capture_stream=self.capture_stream,
            calibration_matrix=np.eye(3),
            image_size=(10, 10),
            classifier_model=None,
            on_update=lambda *args: None,
        )

    def test_resolves_without_the_motion_gate_ever_firing(self):
        after = standard_starting_matrix()
        after[1][4] = None
        after[3][4] = "white-pawn"

        with mock.patch.object(tracking_loop, "read_settled_state", return_value=_consensus_for(after)):
            self.assertTrue(self.loop.force_settle())
        self.assertEqual(self.loop.board_copy.move_stack[-1], chess.Move.from_uci("e2e4"))

    def test_reports_false_when_nothing_changed(self):
        unchanged = standard_starting_matrix()
        with mock.patch.object(tracking_loop, "read_settled_state", return_value=_consensus_for(unchanged)):
            self.assertFalse(self.loop.force_settle())

    def test_reports_false_when_the_change_cannot_be_explained(self):
        nonsense = standard_starting_matrix()
        nonsense[3][3] = "white-pawn"  # a pawn appears from nowhere
        with mock.patch.object(tracking_loop, "read_settled_state", return_value=_consensus_for(nonsense)):
            self.assertFalse(self.loop.force_settle())
        self.assertEqual(self.loop.board_copy.move_stack, [])


class TestBlockingPrompts(unittest.TestCase):
    """The arm must never sail past an unanswered blocking prompt and drag a
    piece onto an occupied square.

    An ordinary capture no longer blocks -- the arm buries the victim on a
    graveyard slot instead of waiting for a human. What still blocks is a
    capture with nowhere to put the piece, so these drive that: a full ring.
    It cannot arise in a legal game (30 pieces, 32 slots), but plan() is also
    handed hand-built positions by the correction flow, and the refusal path
    is the one that must not rot.
    """

    def _robot(self, **kwargs):
        """A Robot whose pile is a temp file, never the repo's config.

        Without this every test here rewrites config/rig.json and the suite
        starts depending on what the previous run happened to leave behind.
        """
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.addCleanup(os.unlink, path)
        kwargs.setdefault("planner", robot_moves_legacy)
        robot = robot_mod.Robot(robot_mod.MockGantry(), config_path=path, **kwargs)
        # Every slot taken, so the capture below has nowhere to go and falls
        # back to asking a human.
        robot.graveyard = list(range(rig.GRAVEYARD_SLOTS))
        return robot

    def _captured_position(self):
        board = chess.Board()
        for san in ["e4", "d5"]:
            board.push_san(san)
        return board, chess.Move.from_uci("e4d5")

    def test_an_unanswered_prompt_stops_the_move(self):
        # The default callback refuses, which is the safe direction: a
        # blocking prompt with nobody listening must not be skipped.
        robot = self._robot()
        robot.home()
        board, move = self._captured_position()
        with self.assertRaises(robot_mod.GantryError):
            robot.play(board, move)
        self.assertEqual(robot._link.commands, ["HOME", "OFF"],
                         "the drag must not be sent when the prompt went unanswered")

    def test_confirming_lets_the_drag_through(self):
        robot = self._robot(on_prompt=lambda step: True)
        robot.home()
        board, move = self._captured_position()
        robot.play(board, move)
        self.assertEqual(robot._link.commands, ["HOME", "MOVE e4d5 w"])

    def test_the_prompt_arrives_before_the_drag(self):
        seen = []

        def answer(step):
            seen.append(list(robot._link.commands))
            return True

        robot = self._robot(on_prompt=answer)
        robot.home()
        board, move = self._captured_position()
        robot.play(board, move)
        self.assertEqual(seen, [["HOME"]], "nothing may be dragged before the board is clear")

    def test_controller_confirm_releases_the_waiting_move(self):
        robot = self._robot()
        robot.home()
        controller = robot_mod.RobotController(robot, mock.Mock())
        board, move = self._captured_position()

        done = []
        thread = threading.Thread(
            target=lambda: done.append(robot.play(board, move)), daemon=True
        )
        thread.start()

        deadline = time.monotonic() + 2.0
        while controller.state()["awaiting_confirm"] is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIsNotNone(controller.state()["awaiting_confirm"], "should be waiting")

        self.assertTrue(controller.confirm())
        thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive(), "confirming must release the move")
        self.assertEqual(robot._link.commands, ["HOME", "MOVE e4d5 w"])
        self.assertIsNone(controller.state()["awaiting_confirm"])

    def test_confirming_when_nothing_waits_is_reported_not_swallowed(self):
        robot = robot_mod.Robot(robot_mod.MockGantry(), planner=robot_moves_legacy)
        controller = robot_mod.RobotController(robot, mock.Mock())
        self.assertFalse(controller.confirm())
        self.assertFalse(controller.cancel())

    def test_halting_frees_a_blocked_prompt(self):
        # Otherwise a halt requested while the arm waits would leave the
        # robot thread parked on the Event forever.
        robot = self._robot()
        robot.home()
        controller = robot_mod.RobotController(robot, mock.Mock())
        board, move = self._captured_position()

        thread = threading.Thread(target=lambda: None, daemon=True)
        thread = threading.Thread(
            target=lambda: self.assertRaises(robot_mod.GantryError, robot.play, board, move),
            daemon=True,
        )
        thread.start()

        deadline = time.monotonic() + 2.0
        while controller.state()["awaiting_confirm"] is None and time.monotonic() < deadline:
            time.sleep(0.01)

        controller.halt("testing")
        thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive(), "halting must not leave the arm blocked")
        self.assertTrue(robot.halted)


class TestGraveyardPile(unittest.TestCase):
    """Which slots are taken, and when that is written down.

    The pile is the one bit of Robot state that outlives the process, because
    the pieces physically outlive it too -- they are still sitting on the
    board after a reboot.
    """

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.path)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def _robot(self, link=None, **kwargs):
        return robot_mod.Robot(link or robot_mod.MockGantry(),
                               planner=robot_moves_legacy,
                               config_path=self.path, **kwargs)

    def _capture(self):
        board = chess.Board()
        for san in ["e4", "d5"]:
            board.push_san(san)
        return board, chess.Move.from_uci("e4d5")

    def test_a_fresh_rig_starts_with_an_empty_ring(self):
        self.assertEqual(self._robot().graveyard, [])

    def test_a_capture_fills_a_slot(self):
        robot = self._robot()
        robot.home()
        robot.play(*self._capture())
        self.assertEqual(len(robot.graveyard), 1)

    def test_the_pile_survives_a_restart(self):
        robot = self._robot()
        robot.home()
        robot.play(*self._capture())
        self.assertEqual(self._robot().graveyard, robot.graveyard)

    def test_successive_captures_take_different_slots(self):
        robot = self._robot()
        robot.home()
        board = chess.Board()
        taken = set()
        for san in ["e4", "d5", "exd5", "Qxd5", "Nc3", "Qxa2", "Rxa2"]:
            move = board.parse_san(san)
            if board.is_capture(move):
                robot.play(board, move)
                self.assertGreater(len(robot.graveyard), len(taken),
                                   f"{san} reused a slot")
                taken = set(robot.graveyard)
            board.push(move)
        self.assertGreaterEqual(len(taken), 3)

    def test_a_failed_move_does_not_consume_a_slot(self):
        """Committing before the firmware acked would strand a real slot for
        the rest of the game, and nothing would ever free it."""
        class Refusing(robot_mod.MockGantry):
            def send(self, command):
                if command.startswith("BURY"):
                    raise robot_mod.GantryError("ERR out of range")
                return super().send(command)

        robot = self._robot(link=Refusing())
        robot.home()
        with self.assertRaises(robot_mod.GantryError):
            robot.play(*self._capture())
        self.assertEqual(robot.graveyard, [])
        self.assertEqual(rig_config.load(self.path)["graveyard"], [])

    def test_a_quiet_move_writes_nothing(self):
        robot = self._robot()
        robot.home()
        robot.play(chess.Board(), chess.Move.from_uci("e2e4"))
        self.assertFalse(os.path.exists(self.path),
                         "a move with no capture should not touch the config")

    def test_clearing_empties_the_ring_and_the_file(self):
        robot = self._robot()
        robot.home()
        robot.play(*self._capture())
        robot.clear_graveyard()
        self.assertEqual(robot.graveyard, [])
        self.assertEqual(rig_config.load(self.path)["graveyard"], [])

    def test_freeing_a_slot_says_where_the_piece_is(self):
        """A correction undoes the capture, but the piece is still sitting on
        the slot -- the caller has to be able to tell the human which one."""
        robot = self._robot()
        robot.home()
        robot.play(*self._capture())
        slot = next(iter(robot.graveyard))
        where = robot.free_graveyard_slot(slot)
        self.assertEqual(where, rig.graveyard_slot_to_mm(slot))
        self.assertNotIn(slot, robot.graveyard)
        self.assertEqual(rig_config.load(self.path)["graveyard"], [])

    def test_freeing_a_slot_that_is_not_taken_is_harmless(self):
        robot = self._robot()
        robot.free_graveyard_slot(9)
        self.assertEqual(robot.graveyard, [])

    def test_the_native_planner_needs_no_pile(self):
        """robot_moves has no plan_with_slots, and must still work -- Robot
        asks for the capability rather than branching on the module."""
        robot = robot_mod.Robot(robot_mod.MockGantry(), planner=robot_moves,
                                config_path=self.path)
        robot.home()
        robot.play(chess.Board(), chess.Move.from_uci("e2e4"))
        self.assertEqual(robot.graveyard, [])

    def test_the_pile_keeps_burial_order(self):
        robot = self._robot()
        robot.home()
        board = chess.Board()
        order = []
        for san in ["e4", "d5", "exd5", "Qxd5", "Nc3", "Qxa2", "Rxa2"]:
            move = board.parse_san(san)
            if board.is_capture(move):
                before = list(robot.graveyard)
                robot.play(board, move)
                order.append(robot.graveyard[-1])
                self.assertEqual(robot.graveyard[:len(before)], before,
                                 "an earlier burial must not be reordered")
            board.push(move)
        self.assertEqual(robot.graveyard, order)

    def test_a_correction_frees_the_most_recent_slots(self):
        """The count gives away how many captures were undone; burial order
        gives away which slots they were."""
        robot = self._robot()
        robot.home()
        board = chess.Board()
        for san in ["e4", "d5", "exd5", "Qxd5", "Nc3", "Qxa2", "Rxa2"]:
            move = board.parse_san(san)
            if board.is_capture(move):
                robot.play(board, move)
            board.push(move)
        buried = list(robot.graveyard)
        self.assertGreaterEqual(len(buried), 3)

        # Put one piece back: 32 - on_board should be one fewer than the pile.
        freed = robot.reconcile_graveyard(32 - (len(buried) - 1))
        self.assertEqual([slot for slot, _ in freed], [buried[-1]],
                         "the LAST piece buried is the one an undo undoes")
        self.assertEqual(robot.graveyard, buried[:-1])
        self.assertEqual(freed[0][1], rig.graveyard_slot_to_mm(buried[-1]))

    def test_a_correction_that_changes_nothing_frees_nothing(self):
        robot = self._robot()
        robot.home()
        robot.play(*self._capture())
        self.assertEqual(robot.reconcile_graveyard(31), [])
        self.assertEqual(len(robot.graveyard), 1)

    def test_a_correction_back_to_a_full_board_empties_the_ring(self):
        robot = self._robot()
        robot.home()
        robot.play(*self._capture())
        freed = robot.reconcile_graveyard(32)
        self.assertEqual(len(freed), 1)
        self.assertEqual(robot.graveyard, [])


if __name__ == "__main__":
    unittest.main()
