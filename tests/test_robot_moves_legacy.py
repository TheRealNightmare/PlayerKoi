"""Tests for robot_moves_legacy.plan -- the ChessBot-V1 command sequence.

Much less arithmetic than test_robot_moves, because this firmware plans its
own paths. What is left to get wrong is chess rules, and those are exactly
the cases with teeth: a capture has to clear the destination BEFORE the
attacker is dragged onto it, en passant's victim isn't on the destination at
all, and castling's rook doesn't appear in the move.

Pure python-chess -- no serial port, no Arduino, no camera.
"""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import chess  # noqa: E402

import robot_moves  # noqa: E402
import robot_moves_legacy as legacy  # noqa: E402


def commands(steps):
    return [step.command for step in steps if step.kind == "command"]


def prompts(steps):
    return [step for step in steps if step.kind == "prompt"]


def plan_uci(board, uci, origin_square="h1"):
    """Plans with the identity orientation by default, so these tests keep
    asserting chess rules rather than how the board happens to be seated
    under the gantry. The rotation itself is tested in TestOrientation and
    in test_rig.py."""
    return legacy.plan(board, chess.Move.from_uci(uci), origin_square=origin_square)


class TestQuietMoves(unittest.TestCase):
    def test_a_pawn_is_one_straight_drag(self):
        self.assertEqual(commands(plan_uci(chess.Board(), "e2e4")), ["MOVE e2e4 w"])

    def test_a_knight_weaves_instead(self):
        # The whole reason the verb differs: a knight's diagonal between
        # centres cuts the corner of an occupied square.
        self.assertEqual(commands(plan_uci(chess.Board(), "b1c3")), ["KNIGHT b1c3 w"])

    def test_a_quiet_move_asks_the_human_for_nothing(self):
        self.assertEqual(prompts(plan_uci(chess.Board(), "e2e4")), [])

    def test_the_note_names_the_piece_and_squares(self):
        step = plan_uci(chess.Board(), "e2e4")[0]
        self.assertEqual(step.note, "pawn e2 to e4")

    def test_a_move_for_the_wrong_side_is_refused(self):
        # Not pedantry: is_capture() answers for the side to move, so a
        # move for the other colour would plan a capture prompt for the
        # wrong square rather than failing.
        with self.assertRaises(ValueError):
            plan_uci(chess.Board(), "e7e5")

    def test_an_illegal_move_is_refused(self):
        with self.assertRaises(ValueError):
            plan_uci(chess.Board(), "e2e5")


class TestCaptures(unittest.TestCase):
    """The captured piece is now driven to a graveyard slot rather than
    waited on. The ordering property is unchanged and still the point: the
    destination has to be empty before the attacker is dragged onto it."""

    def setUp(self):
        self.board = chess.Board()
        for san in ["e4", "d5"]:
            self.board.push_san(san)

    def test_the_victim_leaves_before_the_attacker_arrives(self):
        # Order is the entire point. Dragging first would shove two pieces.
        issued = commands(plan_uci(self.board, "e4d5"))
        self.assertEqual(len(issued), 2)
        self.assertTrue(issued[0].startswith("BURY d5 "), issued[0])
        self.assertEqual(issued[1], "MOVE e4d5 w")

    def test_a_capture_no_longer_stops_for_a_human(self):
        """The whole point of the bigger frame: no blocking prompt in an
        ordinary capture, because the arm can put the piece somewhere."""
        self.assertEqual(prompts(plan_uci(self.board, "e4d5")), [])

    def test_the_bury_is_tagged_with_the_VICTIM_colour(self):
        """Not the capturer's. The coil is carrying the black pawn here, and
        white magnets are reversed on this set -- the wrong token shoves the
        piece off the carriage instead of holding it."""
        bury = commands(plan_uci(self.board, "e4d5"))[0]
        self.assertTrue(bury.endswith(" b"), bury)

    def test_the_bury_drives_to_a_real_slot(self):
        bury = commands(plan_uci(self.board, "e4d5"))[0]
        _, square, x, y, _ = bury.split()
        self.assertEqual(square, "d5")
        self.assertIn((float(x), float(y)),
                      [legacy.rig.graveyard_slot_to_mm(s)
                       for s in range(legacy.rig.GRAVEYARD_SLOTS)])

    def test_the_note_says_where_the_piece_went(self):
        step = plan_uci(self.board, "e4d5")[0]
        self.assertIn("d5", step.note)
        self.assertIn("black", step.note)
        self.assertIn("slot", step.note)

    def test_an_occupied_slot_is_not_reused(self):
        first = commands(plan_uci(self.board, "e4d5"))[0]
        _, used = legacy.plan_with_slots(
            self.board, chess.Move.from_uci("e4d5"), origin_square="h1")
        again = commands(legacy.plan(
            self.board, chess.Move.from_uci("e4d5"),
            origin_square="h1", occupied=set(used)))[0]
        self.assertNotEqual(first, again)

    def test_a_full_ring_falls_back_to_asking(self):
        """Unreachable in a legal game -- 30 pieces, 32 slots -- but the UI
        can hand plan() a made-up position with a stale pile."""
        steps = legacy.plan(self.board, chess.Move.from_uci("e4d5"),
                            origin_square="h1",
                            occupied=set(range(legacy.rig.GRAVEYARD_SLOTS)))
        self.assertEqual(steps[0].kind, "prompt")
        self.assertTrue(steps[0].blocking)
        self.assertIn("d5", steps[0].prompt)


class TestEnPassant(unittest.TestCase):
    """The case hand-written rules get wrong: the pawn to lift is beside the
    destination square, not on it. RunChess's Flask app has this bug -- it
    names move.to_square in the confirm message."""

    def setUp(self):
        self.board = chess.Board()
        for san in ["e4", "a6", "e5", "d5"]:
            self.board.push_san(san)

    def test_the_bury_lifts_the_pawn_not_the_destination(self):
        bury = commands(plan_uci(self.board, "e5d6"))[0]
        self.assertTrue(bury.startswith("BURY d5 "), bury)   # where the pawn is
        self.assertFalse(bury.startswith("BURY d6 "), bury)  # where white lands

    def test_the_note_names_the_pawn_not_the_destination(self):
        note = plan_uci(self.board, "e5d6")[0].note
        self.assertIn("d5", note)
        self.assertNotIn("d6", note)

    def test_the_drag_still_goes_to_the_destination(self):
        self.assertEqual(commands(plan_uci(self.board, "e5d6"))[1], "MOVE e5d6 w")


class TestCastling(unittest.TestCase):
    """The rook's squares aren't in the move, and it has to pass under the
    king that just landed between them -- so it always weaves."""

    def kingside(self):
        board = chess.Board()
        for san in ["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5"]:
            board.push_san(san)
        return board

    def queenside(self):
        board = chess.Board()
        for san in ["d4", "d5", "Nc3", "Nc6", "Bf4", "Bf5", "Qd2", "Qd7"]:
            board.push_san(san)
        return board

    def test_kingside_moves_the_king_then_weaves_the_rook(self):
        self.assertEqual(
            commands(plan_uci(self.kingside(), "e1g1")),
            ["MOVE e1g1 w", "KNIGHT h1f1 w"],
        )

    def test_queenside_moves_the_king_then_weaves_the_rook(self):
        self.assertEqual(
            commands(plan_uci(self.queenside(), "e1c1")),
            ["MOVE e1c1 w", "KNIGHT a1d1 w"],
        )

    def test_black_castles_on_its_own_rank(self):
        board = self.kingside()
        for san in ["O-O", "Nf6", "d3"]:   # clear g8 and hand the move back
            board.push_san(san)
        self.assertEqual(
            commands(plan_uci(board, "e8g8")),
            ["MOVE e8g8 b", "KNIGHT h8f8 b"],
        )

    def test_the_rook_never_drags_straight_through_the_king(self):
        for board, uci in ((self.kingside(), "e1g1"), (self.queenside(), "e1c1")):
            rook = commands(plan_uci(board, uci))[1]
            self.assertTrue(rook.startswith("KNIGHT"), rook)


class TestPromotion(unittest.TestCase):
    def setUp(self):
        # White pawn on b7, black rook on a8 -- so both a quiet promotion
        # and a capturing one are available.
        self.board = chess.Board("r3k3/1P6/8/8/8/8/8/4K3 w q - 0 1")

    def test_the_human_is_asked_to_swap_the_piece_in(self):
        prompt = prompts(plan_uci(self.board, "b7b8q"))[-1].prompt
        self.assertIn("b8", prompt)
        self.assertIn("QUEEN", prompt)

    def test_the_promotion_prompt_does_not_block(self):
        # The move is already finished; nothing is waiting on the swap.
        self.assertFalse(prompts(plan_uci(self.board, "b7b8q"))[-1].blocking)

    def test_it_comes_last(self):
        self.assertEqual(plan_uci(self.board, "b7b8q")[-1].kind, "prompt")

    def test_underpromotion_names_the_right_piece(self):
        self.assertIn("KNIGHT", prompts(plan_uci(self.board, "b7b8n"))[-1].prompt)

    def test_a_capturing_promotion_buries_first_then_advises(self):
        steps = plan_uci(self.board, "b7a8q")
        self.assertTrue(steps[0].command.startswith("BURY a8 "))  # clear the rook
        self.assertEqual(steps[1].command, "MOVE b7a8 w")
        self.assertFalse(steps[-1].blocking)        # then swap the queen in

    def test_the_promotion_suffix_is_not_sent_to_the_firmware(self):
        # The protocol takes four characters; a stray 'q' is ERR bad square.
        # (This move is a quiet promotion, so every command is a MOVE.)
        for command in commands(plan_uci(self.board, "b7b8q")):
            self.assertEqual(len(command.split()[1]), 4, command)


class TestEveryLegalMovePlans(unittest.TestCase):
    """A sweep: whatever comes up in a real game must produce a plan the
    firmware would accept, rather than raising or emitting a malformed
    command."""

    def test_random_games_plan_cleanly(self):
        random.seed(7)
        checked = 0
        for _ in range(20):
            board = chess.Board()
            while not board.is_game_over() and board.fullmove_number < 90:
                move = random.choice(list(board.legal_moves))
                for command in commands(legacy.plan(board, move)):
                    parts = command.split()
                    if parts[0] == "BURY":
                        # BURY <square> <x> <y> <w|b>
                        self.assertEqual(len(parts), 5, command)
                        self.assertIsNotNone(chess.parse_square(parts[1]))
                        float(parts[2]), float(parts[3])   # or ERR usage
                        self.assertIn(parts[4], ("w", "b"), command)
                        continue
                    verb, squares, polarity = parts
                    self.assertIn(verb, ("MOVE", "KNIGHT"))
                    self.assertEqual(len(squares), 4, command)
                    # Both halves must be real squares, or the firmware
                    # answers ERR bad square.
                    self.assertIsNotNone(chess.parse_square(squares[:2]))
                    self.assertIsNotNone(chess.parse_square(squares[2:]))
                checked += 1
                board.push(move)
        self.assertGreater(checked, 1000, "sanity: the sweep should cover many moves")

    def test_every_capture_buries_the_victim_first(self):
        """The ordering property, swept over real games: the destination is
        always cleared before the attacker is dragged onto it."""
        random.seed(3)
        captures = 0
        for _ in range(20):
            board = chess.Board()
            while not board.is_game_over() and board.fullmove_number < 90:
                move = random.choice(list(board.legal_moves))
                if board.is_capture(move):
                    issued = commands(legacy.plan(board, move))
                    self.assertTrue(issued[0].startswith("BURY "),
                                    f"{move.uci()}: {issued}")
                    self.assertFalse(issued[1].startswith("BURY "),
                                     f"{move.uci()}: {issued}")
                    captures += 1
                board.push(move)
        self.assertGreater(captures, 100, "sanity: the sweep should cover many captures")

    def test_no_capture_ever_stops_for_a_human(self):
        random.seed(5)
        for _ in range(10):
            board = chess.Board()
            while not board.is_game_over() and board.fullmove_number < 90:
                move = random.choice(list(board.legal_moves))
                for step in legacy.plan(board, move):
                    # Promotion still prompts, but advisory and never blocking.
                    self.assertFalse(step.kind == "prompt" and step.blocking,
                                     f"{move.uci()} still blocks")
                board.push(move)


class TestOrientation(unittest.TestCase):
    """Only Step.command is rotated into machine orientation.

    Step.note and Step.prompt are read by a human looking at the real board,
    where the pieces are laid out normally -- it is the gantry that is
    rotated, not the game. Telling the human "take the pawn off d5" when the
    pawn is on e4 would be worse than the original bug.
    """

    def test_the_command_is_rotated(self):
        self.assertEqual(
            commands(plan_uci(chess.Board(), "e2e4", origin_square="a8")),
            ["MOVE d7d5 w"],
        )

    def test_a_knight_command_is_rotated_too(self):
        self.assertEqual(
            commands(plan_uci(chess.Board(), "b1c3", origin_square="a8")),
            ["KNIGHT g8f6 w"],
        )

    def test_the_note_stays_in_real_notation(self):
        steps = plan_uci(chess.Board(), "e2e4", origin_square="a8")
        self.assertIn("e2", steps[0].note)
        self.assertIn("e4", steps[0].note)
        self.assertNotIn("d7", steps[0].note)

    def test_the_capture_note_stays_in_real_notation(self):
        board = chess.Board("rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2")
        steps = plan_uci(board, "e4d5", origin_square="a8")
        self.assertIn("d5", steps[0].note)
        self.assertNotIn("e4", steps[0].note)

    def test_the_bury_square_is_rotated_but_the_coordinate_is_mirrored(self):
        """The two halves of a BURY rotate by different means: the square by
        orient(), the millimetres by orient_mm(). Rotating one and not the
        other sends the arm to the right slot from the wrong square."""
        import rig

        board = chess.Board("rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2")
        _, square, x, y, _ = commands(
            plan_uci(board, "e4d5", origin_square="a8"))[0].split()
        self.assertEqual(square, rig.orient_square("d5", "a8"))
        upright = commands(plan_uci(board, "e4d5", origin_square="h1"))[0].split()
        self.assertEqual(
            (float(x), float(y)),
            rig.orient_mm(float(upright[2]), float(upright[3]), origin="a8"))

    def test_castling_rotates_both_commands(self):
        board = chess.Board("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
        self.assertEqual(
            commands(plan_uci(board, "e1g1", origin_square="a8")),
            ["MOVE d8b8 w", "KNIGHT a8c8 w"],
        )

    def test_the_default_origin_is_the_rig_constant(self):
        """plan() with no origin must use the machine's measured seating,
        not the firmware's assumption -- otherwise the fix never reaches the
        arm."""
        import rig

        self.assertEqual(
            legacy.plan(chess.Board(), chess.Move.from_uci("e2e4"))[0].command,
            f"MOVE {rig.orient_uci('e2e4')} w",
        )

    def test_every_legal_move_still_names_real_squares(self):
        """Sweep: a rotated command must always be a valid square pair, and
        never accidentally leak into the human-facing text."""
        board = chess.Board()
        random.seed(11)
        for _ in range(200):
            if board.is_game_over():
                board = chess.Board()
            move = random.choice(list(board.legal_moves))
            for step in legacy.plan(board, move, origin_square="a8"):
                if step.kind == "command":
                    squares = step.command.split()[1]
                    # BURY names one square, MOVE/KNIGHT name two.
                    self.assertIn(len(squares), (2, 4), step.command)
                    self.assertTrue(all(c in "abcdefgh" for c in squares[::2]))
                    self.assertTrue(all(c in "12345678" for c in squares[1::2]))
            board.push(move)


class TestMagnetPolarity(unittest.TestCase):
    """The white pieces on this set have their magnets the other way up, so
    the coil polarity has to follow the colour being carried. Holding a white
    piece with the black polarity pushes it off the square instead of
    gripping it, and the release pulse grabs it instead of letting go -- so
    the carriage drags it onward into the next move.

    The firmware decides what the coil does; this side only has to say which
    colour it is. That is the w|b suffix on MOVE and KNIGHT.
    """

    @staticmethod
    def _tokens(steps):
        return [c.split()[-1] for c in commands(steps)]

    def test_a_white_move_is_tagged_white(self):
        self.assertEqual(self._tokens(plan_uci(chess.Board(), "e2e4")), ["w"])

    def test_a_black_move_is_tagged_black(self):
        board = chess.Board()
        board.push_san("e4")
        self.assertEqual(self._tokens(plan_uci(board, "e7e5")), ["b"])

    def test_a_knight_carries_it_too(self):
        self.assertEqual(self._tokens(plan_uci(chess.Board(), "b1c3")), ["w"])

    def test_castling_tags_the_rook_like_the_king(self):
        """Both commands move the same player's pieces, so a mismatch would
        drop the rook halfway across the back rank."""
        board = chess.Board("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
        self.assertEqual(self._tokens(plan_uci(board, "e1g1")), ["w", "w"])
        board = chess.Board("r3k2r/8/8/8/8/8/8/R3K2R b KQkq - 0 1")
        self.assertEqual(self._tokens(plan_uci(board, "e8c8")), ["b", "b"])

    def test_a_capture_tags_each_command_with_what_it_carries(self):
        """Since the arm buries the victim, the coil now carries BOTH colours
        in one move -- the black pawn out to its slot, then the white pawn in.
        One token per command, each naming what that command lifts."""
        board = chess.Board()
        for san in ("e4", "d5"):
            board.push_san(san)
        self.assertEqual(self._tokens(plan_uci(board, "e4d5")), ["b", "w"])

    def test_every_command_of_a_whole_game_is_tagged(self):
        board = chess.Board()
        random.seed(17)
        for _ in range(200):
            if board.is_game_over():
                board = chess.Board()
            move = random.choice(list(board.legal_moves))
            mover = "w" if board.turn == chess.WHITE else "b"
            # A BURY carries the captured piece, so it is tagged the opposite
            # colour: the victim is always the side not to move, en passant
            # included.
            victim = "b" if mover == "w" else "w"
            for command in commands(legacy.plan(board, move)):
                parts = command.split()
                self.assertIn(len(parts), (3, 5), command)
                self.assertEqual(parts[-1], victim if parts[0] == "BURY" else mover,
                                 command)
            board.push(move)


class TestInterchangeableWithTheNativePlanner(unittest.TestCase):
    """Robot takes either module as its planner, so the shapes must match."""

    def test_both_accept_the_same_call(self):
        board = chess.Board()
        move = chess.Move.from_uci("e2e4")
        for planner in (robot_moves, legacy):
            steps = planner.plan(board, move, topple_delay_s=1.0)
            self.assertTrue(steps)
            self.assertTrue(all(isinstance(s, robot_moves.Step) for s in steps))

    def test_both_use_the_same_step_kinds(self):
        board = chess.Board()
        for planner in (robot_moves, legacy):
            for step in planner.plan(board, chess.Move.from_uci("b1c3")):
                self.assertIn(step.kind, ("command", "wait", "prompt"))


if __name__ == "__main__":
    unittest.main()
