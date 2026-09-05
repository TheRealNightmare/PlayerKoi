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
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import chess  # noqa: E402
import numpy as np  # noqa: E402

import robot as robot_mod  # noqa: E402
import tracking_loop  # noqa: E402
from move_resolver import standard_starting_matrix  # noqa: E402
from robot import RobotController  # noqa: E402
from square_classifier import ALL_SQUARES, BLACK, EMPTY, WHITE  # noqa: E402


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
            ["HOME", "GOTO 4.00 1.00", "MAG 170", "GOTO 4.00 3.00", "PULSE", "MAG 0", "GOTO 0.00 0.00"],
        )

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


if __name__ == "__main__":
    unittest.main()
