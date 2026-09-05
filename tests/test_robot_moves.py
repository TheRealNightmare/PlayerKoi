"""Tests for robot_moves.plan -- the gantry command sequence for a move.

These are the tests that matter most for the arm, because a wrong plan is a
piece knocked across the room rather than a wrong pixel. The cases with real
teeth are the ones where "go from A to B" isn't enough: a knight has to
travel through the gaps between squares, the castling rook has to get past
the king that just jumped over it, and en passant has to topple a pawn that
isn't on the destination square at all.

Pure python-chess -- no serial port, no Arduino, no camera.
"""

import math
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import chess  # noqa: E402

import robot_moves  # noqa: E402


def commands(steps):
    return [step.command for step in steps if step.kind == "command"]


def gotos(steps):
    """Just the waypoints, as (x, y) floats -- the actual path travelled."""
    points = []
    for step in steps:
        if step.kind == "command" and step.command.startswith("GOTO "):
            _, x, y = step.command.split()
            points.append((float(x), float(y)))
    return points


class TestQuietMoves(unittest.TestCase):
    def test_pawn_push_is_a_direct_slide(self):
        board = chess.Board()
        steps = robot_moves.plan(board, chess.Move.from_uci("e2e4"))

        # e2 is file 4, rank 1; e4 is file 4, rank 3.
        self.assertEqual(gotos(steps)[:2], [(4.0, 1.0), (4.0, 3.0)])
        self.assertEqual(
            commands(steps),
            [
                "GOTO 4.00 1.00",
                f"MAG {robot_moves.MAG_HOLD}",
                "GOTO 4.00 3.00",
                "PULSE",
                "MAG 0",
                "GOTO 0.00 0.00",
            ],
        )

    def test_magnet_is_off_before_the_first_move_and_after_the_last(self):
        board = chess.Board()
        steps = robot_moves.plan(board, chess.Move.from_uci("e2e4"))
        issued = commands(steps)

        # Nothing energises the coil before the carriage has arrived...
        self.assertTrue(issued[0].startswith("GOTO"))
        # ...and the plan always ends released and parked, so a resting
        # magnet never tugs at the piece above it.
        self.assertEqual(issued[-2:], ["MAG 0", "GOTO 0.00 0.00"])

    def test_bishop_slides_diagonally_through_empty_squares(self):
        board = chess.Board("8/8/8/8/8/8/8/2B5 w - - 0 1")
        steps = robot_moves.plan(board, chess.Move.from_uci("c1h6"))
        self.assertEqual(gotos(steps)[:2], [(2.0, 0.0), (7.0, 5.0)])


class TestKnightRouting(unittest.TestCase):
    """A knight's path crosses occupied squares, so it rides the lattice
    lines between them -- the reference implementation's special case,
    generalised."""

    def test_knight_rides_the_gap_between_files(self):
        board = chess.Board()
        board.push_san("e4")  # so b8c6 is Black's move to make
        steps = robot_moves.plan(board, chess.Move.from_uci("b8c6"))

        # b8 (1,7) -> corner (1.5,6.5) -> along the b/c gap to (1.5,5.5)
        # -> down into c6 (2,5). Both b7 and c7 hold pawns here, so the
        # traversal has to split the gap: 15mm from each, which is the most
        # that geometry allows. See TestLatticeClearance for the cases where
        # it can do better.
        self.assertEqual(
            gotos(steps)[:4],
            [(1.0, 7.0), (1.5, 6.5), (1.5, 5.5), (2.0, 5.0)],
        )

    def test_knight_drops_magnet_power_while_off_centre(self):
        board = chess.Board()
        board.push_san("e4")
        steps = robot_moves.plan(board, chess.Move.from_uci("b8c6"))
        issued = commands(steps)

        grip = issued.index(f"MAG {robot_moves.MAG_HOLD}")
        edge = issued.index(f"MAG {robot_moves.MAG_EDGE}")
        self.assertLess(grip, edge, "must grip at full strength before easing off centre")
        self.assertIn(f"MAG {robot_moves.MAG_HOLD}", issued[edge + 1:], "must regrip before setting down")

    def test_every_waypoint_stays_on_the_board(self):
        # A knight in the corner is the case that would route off the frame
        # if the offsets were applied in the wrong direction.
        board = chess.Board("8/8/8/8/8/8/8/N7 w - - 0 1")
        steps = robot_moves.plan(board, chess.Move.from_uci("a1b3"))
        for x, y in gotos(steps):
            self.assertGreaterEqual(x, -0.5)
            self.assertGreaterEqual(y, -0.5)
            self.assertLessEqual(x, 7.5)
            self.assertLessEqual(y, 7.5)


class TestLatticeClearance(unittest.TestCase):
    """On 30mm squares the midline of a gap is only 15mm from the pieces on
    either side -- against 13-15mm bases, and a 25mm magnet, that's nothing.
    So a traversal shifts toward a flank it can prove is empty.

    These are the tests that decide whether the arm knocks pieces over, so
    they assert exact coordinates rather than 'roughly avoids things'.
    """

    def test_shifts_toward_an_empty_file(self):
        # 1.e4 c5 2.Nf3 -- c7 is empty, b7 still has its pawn, so the b8-c6
        # traversal should move off the midline toward the c-file.
        board = chess.Board("rnbqkbnr/pp1ppppp/8/2p5/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2")
        points = gotos(robot_moves.plan(board, chess.Move.from_uci("b8c6")))
        self.assertEqual(points[1:3], [(1.7, 6.5), (1.7, 5.5)])

    def test_shifts_the_other_way_for_an_empty_b_file(self):
        # b7 empty instead: the shift reverses, away from the c7 pawn.
        board = chess.Board("rnbqkbnr/p1pppppp/1p6/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 2")
        points = gotos(robot_moves.plan(board, chess.Move.from_uci("b8c6")))
        self.assertEqual(points[1:3], [(1.3, 6.5), (1.3, 5.5)])

    def test_stays_on_the_midline_when_both_flanks_are_occupied(self):
        # The opening pawn wall. Nothing to be done -- 15mm each side is the
        # geometric maximum, and pretending otherwise would just move the
        # knight closer to one of them.
        board = chess.Board()
        board.push_san("e4")
        points = gotos(robot_moves.plan(board, chess.Move.from_uci("b8c6")))
        self.assertEqual(points[1:3], [(1.5, 6.5), (1.5, 5.5)])

    def test_stays_on_the_midline_when_both_flanks_are_empty(self):
        # Nothing to dodge, so don't wander -- the midline is the simplest
        # path and every waypoint stays predictable.
        board = chess.Board("8/8/8/8/8/8/8/N7 w - - 0 1")
        points = gotos(robot_moves.plan(board, chess.Move.from_uci("a1b3")))
        self.assertEqual(points[1:3], [(0.5, 0.5), (0.5, 1.5)])

    def test_castling_rook_drops_further_from_the_king_when_g7_is_free(self):
        # Fianchetto: g7 is empty, so the rook's traverse under the king on
        # g8 shifts down to rank 6.3 rather than splitting 6.5.
        board = chess.Board("r1bqk2r/pppp1p1p/2n2np1/2b1p3/2B1P3/2N2N2/PPPP1PPP/R1BQK2R b KQkq - 0 6")
        points = gotos(robot_moves.plan(board, chess.Move.from_uci("e8g8")))
        self.assertEqual(points[3:5], [(6.5, 6.3), (5.5, 6.3)])

    def test_castling_rook_splits_the_gap_when_g7_is_occupied(self):
        board = chess.Board("r1bqk2r/pppp1ppp/2n2n2/2b1p3/2B1P3/2N2N2/PPPP1PPP/R1BQK2R b KQkq - 5 5")
        points = gotos(robot_moves.plan(board, chess.Move.from_uci("e8g8")))
        self.assertEqual(points[3:5], [(6.5, 6.5), (5.5, 6.5)])

    def test_an_off_board_flank_is_never_shifted_toward(self):
        # An off-board side is "empty" in a useless sense -- the carriage
        # can't go there, and shifting that way would put the magnet over
        # the edge of the PCB. _flanking reports it as no squares at all,
        # and _bias must not read that as a clear run.
        #
        # No legal move actually produces a gap outside 0.5..6.5 (the
        # perpendicular offset always points inward, and a knight's gap is
        # always between two real files), so this exercises the guard
        # directly rather than through plan().
        off_low, off_high = robot_moves._flanking(-0.5, 0.5, 1.5, vertical=True)
        self.assertEqual(off_low, [], "file -1 does not exist")

        # a2 occupied: the only real side is blocked, so stay put -- and in
        # particular do NOT shift toward the void.
        blocked = chess.Board("8/8/8/8/8/8/p7/1N6 w - - 0 1")
        self.assertEqual(robot_moves._bias(off_low, off_high, blocked, set()), 0.0)

        # a2 free: shifting is fine, but only inward, onto the board.
        free = chess.Board("8/8/8/8/8/8/8/1N6 w - - 0 1")
        self.assertGreater(robot_moves._bias(off_low, off_high, free, set()), 0.0)

    def test_the_departed_square_does_not_count_as_an_obstacle(self):
        # The piece being carried is on the magnet, not on its origin square,
        # so its own square must not block a shift toward that side.
        board = chess.Board("8/8/8/8/8/8/1p6/1N6 w - - 0 1")
        low, high = robot_moves._flanking(1.5, 0.5, 2.5, vertical=True)
        b1 = chess.B1
        self.assertIn(chess.B2, low)
        without = robot_moves._bias(low, high, board, set())
        with_ignore = robot_moves._bias(low, high, board, {chess.B2, b1})
        self.assertEqual(without, robot_moves.LATTICE_BIAS, "b2 pawn blocks the b-file side")
        self.assertEqual(with_ignore, 0.0, "ignoring it leaves both sides clear")


class TestIllegalMovesAreRefused(unittest.TestCase):
    def test_a_move_for_the_other_side_is_rejected(self):
        # python-chess answers is_capture() for the side to move, so a move
        # belonging to the other side would plan a topple on the mover's own
        # departure square. Refusing is the only safe answer.
        board = chess.Board()
        with self.assertRaises(ValueError):
            robot_moves.plan(board, chess.Move.from_uci("b8c6"))

    def test_an_impossible_move_is_rejected(self):
        board = chess.Board()
        with self.assertRaises(ValueError):
            robot_moves.plan(board, chess.Move.from_uci("e2e5"))


class TestCaptures(unittest.TestCase):
    def test_victim_is_toppled_before_the_capturing_piece_moves(self):
        board = chess.Board()
        board.push_san("e4")
        board.push_san("d5")
        steps = robot_moves.plan(board, chess.Move.from_uci("e4d5"))
        issued = commands(steps)

        topple = issued.index("TOPPLE")
        grip = issued.index(f"MAG {robot_moves.MAG_HOLD}")
        self.assertLess(topple, grip, "the square must be cleared before sliding a piece onto it")
        # It topples the piece standing on d5 (3,4), not somewhere else.
        self.assertEqual(gotos(steps)[0], (3.0, 4.0))

    def test_capture_waits_for_the_human_to_lift_the_piece(self):
        board = chess.Board()
        board.push_san("e4")
        board.push_san("d5")
        steps = robot_moves.plan(board, chess.Move.from_uci("e4d5"), topple_delay_s=7.0)

        waits = [step for step in steps if step.kind == "wait"]
        self.assertEqual(len(waits), 1)
        self.assertEqual(waits[0].seconds, 7.0)
        self.assertIn("d5", waits[0].prompt)
        # And it stands clear of the board while waiting, rather than
        # hovering under the piece the human is reaching for.
        wait_at = steps.index(waits[0])
        self.assertEqual(gotos(steps[:wait_at])[-1], robot_moves.PARK)

    def test_en_passant_topples_the_pawn_beside_the_destination(self):
        # Black pawn on d4, white plays e2e4; d4xe3 e.p. removes the pawn on
        # e4 -- a square that is neither the mover's from nor its to.
        board = chess.Board("4k3/8/8/8/3p4/8/4PP2/4K3 w - - 0 1")
        board.push_san("e4")
        move = chess.Move.from_uci("d4e3")
        self.assertTrue(board.is_en_passant(move))

        steps = robot_moves.plan(board, move)
        # e4 is (4,3) -- the captured pawn's square, not e3 (4,2).
        self.assertEqual(gotos(steps)[0], (4.0, 3.0))
        self.assertEqual(commands(steps)[1], "TOPPLE")
        self.assertIn("e4", [step.prompt for step in steps if step.kind == "wait"][0])

    def test_quiet_move_never_topples(self):
        board = chess.Board()
        steps = robot_moves.plan(board, chess.Move.from_uci("e2e4"))
        self.assertNotIn("TOPPLE", commands(steps))
        self.assertEqual([step for step in steps if step.kind == "wait"], [])


class TestCastling(unittest.TestCase):
    def test_kingside_moves_the_king_then_routes_the_rook_past_it(self):
        board = chess.Board("r1bqk2r/pppp1ppp/2n2n2/2b1p3/2B1P3/2N2N2/PPPP1PPP/R1BQK2R b KQkq - 5 5")
        move = chess.Move.from_uci("e8g8")
        self.assertTrue(board.is_castling(move))

        points = gotos(robot_moves.plan(board, move))
        # King e8 (4,7) -> g8 (6,7) direct, f8 being empty.
        self.assertEqual(points[:2], [(4.0, 7.0), (6.0, 7.0)])
        # Rook h8 (7,7) must NOT drive down the middle of rank 8 -- the king
        # is now on g8. It drops to the rank 7/8 gap and crosses there.
        self.assertEqual(points[2:6], [(7.0, 7.0), (6.5, 6.5), (5.5, 6.5), (5.0, 7.0)])

    def test_queenside_routes_the_rook_around_the_king(self):
        board = chess.Board("r3kbnr/pppqpppp/2np4/8/8/2NP4/PPPQPPPP/R3KBNR w KQkq - 4 5")
        move = chess.Move.from_uci("e1c1")
        self.assertTrue(board.is_castling(move))

        points = gotos(robot_moves.plan(board, move))
        self.assertEqual(points[:2], [(4.0, 0.0), (2.0, 0.0)])
        # Rook a1 (0,0) -> d1 (3,0), crossing under the king now on c1, so
        # it rides the gap above rank 1.
        self.assertEqual(points[2:6], [(0.0, 0.0), (0.5, 0.5), (2.5, 0.5), (3.0, 0.0)])

    def test_castling_carries_exactly_two_pieces(self):
        board = chess.Board("r1bqk2r/pppp1ppp/2n2n2/2b1p3/2B1P3/2N2N2/PPPP1PPP/R1BQK2R b KQkq - 5 5")
        steps = robot_moves.plan(board, chess.Move.from_uci("e8g8"))
        # One centring pulse per piece set down.
        self.assertEqual(commands(steps).count("PULSE"), 2)


class TestPromotion(unittest.TestCase):
    def test_promotion_moves_the_pawn_then_asks_for_the_swap(self):
        board = chess.Board("4k3/8/8/8/8/8/3p4/4K3 b - - 0 1")
        steps = robot_moves.plan(board, chess.Move.from_uci("d2d1q"))

        prompts = [step for step in steps if step.kind == "prompt"]
        self.assertEqual(len(prompts), 1)
        self.assertIn("d1", prompts[0].prompt)
        self.assertIn("QUEEN", prompts[0].prompt)
        # The prompt comes last: the pawn is already physically on d1.
        self.assertIs(steps[-1], prompts[0])

    def test_underpromotion_names_the_right_piece(self):
        board = chess.Board("4k3/8/8/8/8/8/3p4/4K3 b - - 0 1")
        steps = robot_moves.plan(board, chess.Move.from_uci("d2d1n"))
        self.assertIn("KNIGHT", [step for step in steps if step.kind == "prompt"][0].prompt)

    def test_no_prompt_on_an_ordinary_move(self):
        board = chess.Board()
        steps = robot_moves.plan(board, chess.Move.from_uci("e2e4"))
        self.assertEqual([step for step in steps if step.kind == "prompt"], [])


def _point_to_segment(point, start, end):
    """Distance from a square centre to a straight run of the carriage."""
    (px, py), (ax, ay), (bx, by) = point, start, end
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def closest_approach(board, move):
    """The nearest the carried piece ever gets to a piece it isn't moving,
    in squares, over every leg of the plan where the magnet is energised.

    Legs with the coil off don't count -- the carriage passing under a piece
    with no field can't disturb it.
    """
    toppled = set()
    if board.is_en_passant(move):
        toppled.add(chess.square(chess.square_file(move.to_square), chess.square_rank(move.from_square)))
    elif board.is_capture(move):
        toppled.add(move.to_square)

    occupied = [
        (chess.square_file(sq), chess.square_rank(sq))
        for sq in board.piece_map()
        if sq not in toppled
    ]

    duty, previous, worst = 0, None, float("inf")
    for step in robot_moves.plan(board, move):
        if step.kind != "command":
            continue
        if step.command.startswith("MAG "):
            duty = int(step.command.split()[1])
            continue
        if step.command == "PULSE":
            duty = 0  # pulse ends with the coil off
            continue
        if not step.command.startswith("GOTO"):
            continue

        _, sx, sy = step.command.split()
        here = (float(sx), float(sy))
        if duty > 0 and previous is not None:
            for centre in occupied:
                # A square that *is* an endpoint of this leg is the piece
                # being picked up or set down -- not an obstacle.
                if (
                    math.hypot(centre[0] - here[0], centre[1] - here[1]) < 0.01
                    or math.hypot(centre[0] - previous[0], centre[1] - previous[1]) < 0.01
                ):
                    continue
                worst = min(worst, _point_to_segment(centre, previous, here))
        previous = here
    return worst


class TestClearanceInvariant(unittest.TestCase):
    """The load-bearing property: while carrying a piece, the arm never comes
    closer than half a square (15mm on this board) to any other piece.

    15mm is the geometric floor -- the midline of a gap -- and against 13-15mm
    bases it's already tight, so anything closer means pieces get knocked
    over. Asserted over whole random games rather than hand-picked positions,
    because the cases that would violate it are crowded middlegames nobody
    thinks to write down.
    """

    FLOOR = 0.5  # squares

    def test_never_closer_than_half_a_square_over_full_games(self):
        random.seed(11)
        checked = 0
        for _ in range(25):
            board = chess.Board()
            while not board.is_game_over() and board.fullmove_number < 90:
                move = random.choice(list(board.legal_moves))
                gap = closest_approach(board, move)
                self.assertGreaterEqual(
                    gap,
                    self.FLOOR - 1e-9,
                    f"{move.uci()} passes {gap * robot_moves.SQUARE_MM:.1f}mm from another "
                    f"piece in {board.fen()}",
                )
                checked += 1
                board.push(move)
        self.assertGreater(checked, 2000, "sanity: the sweep should cover thousands of moves")

    def test_the_opening_knight_is_the_tight_case(self):
        # Documents *why* the floor is 0.5 and not something roomier: the
        # rank-7 pawn wall is solid, so this move has no better option. If
        # pieces catch on the rig, this is the move that will show it.
        board = chess.Board()
        board.push_san("e4")
        self.assertAlmostEqual(closest_approach(board, chess.Move.from_uci("b8c6")), 0.5)

    def test_the_same_knight_gets_more_room_once_a_flank_clears(self):
        # With c7 empty the long traverse leg moves out to 0.7 squares
        # (21mm), and what's left binding is the short diagonal onto the
        # corner, which passes 0.583 squares (17.4mm) from the bishop on c8.
        # So the move's worst point improves from 15.0mm to 17.4mm -- less
        # than the traverse alone would suggest, but real.
        board = chess.Board("rnbqkbnr/pp1ppppp/8/2p5/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2")
        gap = closest_approach(board, chess.Move.from_uci("b8c6"))
        self.assertGreater(gap, self.FLOOR, "biasing must beat the midline")
        self.assertAlmostEqual(gap, 0.581, places=2)


class TestEveryLegalMoveIsPlannable(unittest.TestCase):
    """A plan that raises mid-game leaves the arm holding a piece, so the
    planner has to survive every legal move it could be handed."""

    def test_opening_position(self):
        self._walk(chess.Board())

    def test_tactical_middlegame(self):
        self._walk(chess.Board("r1bq1rk1/pppp1ppp/2n2n2/2b1p3/2B1P3/2NP1N2/PPP2PPP/R1BQ1RK1 w - - 0 7"))

    def test_endgame_with_promotions_available(self):
        self._walk(chess.Board("8/1P4k1/8/8/8/8/6p1/6K1 w - - 0 1"))

    def _walk(self, board):
        for move in board.legal_moves:
            steps = robot_moves.plan(board, move)
            self.assertTrue(steps, f"empty plan for {move.uci()}")
            for step in steps:
                if step.kind != "command":
                    continue
                if step.command.startswith("GOTO "):
                    _, x, y = step.command.split()
                    self.assertTrue(-0.5 <= float(x) <= 7.5, f"{move.uci()} routes off the board in x")
                    self.assertTrue(-0.5 <= float(y) <= 7.5, f"{move.uci()} routes off the board in y")
                else:
                    self.assertRegex(step.command, r"^(MAG \d+|PULSE|TOPPLE)$")


if __name__ == "__main__":
    unittest.main()
