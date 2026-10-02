"""Player Koi — animated explainer (Manim CE + manim-voiceover + Edge TTS).

Render one scene:  manim -pql scenes.py Vision
Render all:        ./render.sh
Narration source:  script.md
"""

import chess
from manim import *

from common import *


# ── 1. Hook ──────────────────────────────────────────────────────────────
class Hook(KoiScene):
    def construct(self):
        phone = RoundedRectangle(width=2.4, height=4.4, corner_radius=0.3, stroke_color=MUTED, stroke_width=4,
                                 fill_color="#171a23", fill_opacity=1)
        notch = RoundedRectangle(width=0.7, height=0.12, corner_radius=0.06, stroke_width=0, fill_color=MUTED,
                                 fill_opacity=1).move_to(phone.get_top() + DOWN * 0.25)
        mini = ChessBoard(sq=0.25, coords=False).move_to(phone)
        screen = VGroup(phone, notch, mini)

        with self.voiceover(
            "Chess is more popular than ever, but almost all of it happens on a screen. "
            "<bookmark mark='box'/>A real board needs a second player, so when you're alone, it stays in the box. "
            "<bookmark mark='idea'/>So we asked: what if the board itself could play back? "
            "<bookmark mark='meet'/>Meet Player Koi."
        ):
            self.play(FadeIn(screen, shift=UP * 0.5), run_time=1)
            for san in ("e4", "e5", "Nf3", "Nc6"):
                a, b, piece, *_ = mini.push(san)
                self.play(piece.animate.move_to(mini.at(b)), run_time=0.45)
            taps = VGroup(*[T(s, 26, MUTED) for s in ("tap", "swipe", "scroll")]).arrange(DOWN, buff=0.5)
            taps.next_to(phone, RIGHT, buff=0.8)
            self.play(LaggedStart(*[FadeIn(t, shift=LEFT * 0.2) for t in taps], lag_ratio=0.3), run_time=1)

            self.wait_until_bookmark("box")
            self.play(FadeOut(taps), FadeOut(screen, shift=LEFT), run_time=0.8)
            board = ChessBoard(sq=0.42, coords=False).shift(RIGHT * 0.5)
            seat = VGroup(Circle(radius=0.35, stroke_color=MUTED, stroke_width=3), T("?", 34, MUTED))
            seat.next_to(board, UP, buff=0.3)
            you = VGroup(Circle(radius=0.35, stroke_color=WHITE, stroke_width=3, fill_color=SKIN, fill_opacity=0.6),
                         T("you", 20)).arrange(DOWN, buff=0.1).next_to(board, DOWN, buff=0.2)
            self.play(FadeIn(board), FadeIn(you, shift=UP * 0.2), run_time=1)
            self.play(Create(seat[0]), Write(seat[1]), run_time=0.8)
            box = Rectangle(width=board.width + 0.4, height=board.height + 0.4, stroke_color="#7a5c3e",
                            stroke_width=6).move_to(board)
            lid = Rectangle(width=box.width, height=box.height, stroke_color="#7a5c3e", stroke_width=6,
                            fill_color="#3a2c20", fill_opacity=1).move_to(box).shift(RIGHT * 8).set_z_index(8)
            lid_label = T("still in the box", 30, "#d8c3a5").move_to(box).set_z_index(9).shift(RIGHT * 8)
            self.play(Create(box), FadeOut(seat), FadeOut(you), run_time=0.8)
            self.play(lid.animate.shift(LEFT * 8), lid_label.animate.shift(LEFT * 8), run_time=1.2)

            self.wait_until_bookmark("idea")
            self.play(lid.animate.shift(UP * 8), lid_label.animate.shift(UP * 8), FadeOut(box), run_time=1)
            q = T("What if the board could play back?", 40, ACCENT).to_edge(UP, buff=0.5)
            self.play(Write(q), run_time=1.2)
            self.play(Indicate(board, color=ACCENT, scale_factor=1.04), run_time=1)

            self.wait_until_bookmark("meet")
            title = VGroup(T("Player", 110), T("Koi", 110, HAND)).arrange(RIGHT, buff=0.35)
            self.play(FadeOut(VGroup(q, board)), run_time=0.6)
            self.play(Write(title), run_time=1.2)
        sub = T("a robotic chessboard that sees, thinks and moves", 32, MUTED).next_to(title, DOWN, buff=0.4)
        self.play(FadeIn(sub, shift=UP * 0.2), run_time=0.8)
        self.wait(0.8)
        self.play(FadeOut(VGroup(title, sub)), run_time=0.6)


# ── 2. Core idea ─────────────────────────────────────────────────────────
class CoreIdea(KoiScene):
    def construct(self):
        board = ChessBoard(sq=0.5, coords=False).shift(DOWN * 0.25)
        eyes_icon = camera_icon(s=1.1)
        eyes = VGroup(eyes_icon, T("Eyes", 36, EYE), T("overhead camera", 22, MUTED)).arrange(DOWN, buff=0.15)
        eyes.move_to([-5.0, 1.0, 0])
        brain = VGroup(chip_icon(s=1.1), T("Brain", 36, BRAIN), T("Raspberry Pi 5\n+ Stockfish", 22, MUTED,
                       line_spacing=0.8)).arrange(DOWN, buff=0.15).move_to([5.0, 1.5, 0])
        handg = VGroup(magnet_icon(s=1.0), T("Hand", 36, HAND), T("magnet under the board", 22, MUTED)).arrange(
            DOWN, buff=0.15).move_to([5.0, -2.2, 0])
        magnet = make_magnet_glow(board)
        hand = make_hand(board.sq)

        with self.voiceover(
            "Player Koi is a real chessboard with three new abilities. "
            "<bookmark mark='eyes'/>Eyes: a camera above the board watches every square. "
            "<bookmark mark='brain'/>A brain: a Raspberry Pi 5 that knows the rules and runs the Stockfish chess engine. "
            "<bookmark mark='hand'/>And a hand: a magnet hidden under the board that slides the computer's pieces. "
            "<bookmark mark='loop'/>You move a piece by hand. <bookmark mark='sees'/>It sees your move, "
            "<bookmark mark='thinks'/>thinks, <bookmark mark='plays'/>and plays its reply."
        ):
            self.play(FadeIn(board, scale=0.95), run_time=1)

            self.wait_until_bookmark("eyes")
            self.play(FadeIn(eyes, shift=RIGHT * 0.3), run_time=0.8)
            cone = Polygon(eyes_icon.get_right() + RIGHT * 0.05, board.grid.get_corner(UL), board.grid.get_corner(DL),
                           stroke_width=0, fill_color=EYE, fill_opacity=0.12)
            self.play(FadeIn(cone), run_time=0.6)
            scan_sweep(self, board, run_time=1.2)

            self.wait_until_bookmark("brain")
            self.play(FadeIn(brain, shift=LEFT * 0.3), run_time=0.8)

            self.wait_until_bookmark("hand")
            self.play(FadeIn(handg, shift=LEFT * 0.3), FadeIn(magnet), run_time=0.8)
            self.play(magnet.animate.move_to(board.at("d5")), run_time=1.2)
            self.play(magnet.animate.move_to(board.point(8, -1)), run_time=1.0)

            self.wait_until_bookmark("loop")
            hand_move(self, board, hand, "e4", speed=0.9)

            self.wait_until_bookmark("sees")
            a1 = CurvedArrow(eyes[0].get_top() + UP * 0.1, brain[0].get_left() + LEFT * 0.1, angle=-PI / 3,
                             color=EYE, stroke_width=5)
            self.play(Create(a1), Indicate(eyes[0], color=EYE), run_time=0.7)
            self.wait_until_bookmark("thinks")
            a2 = Arrow(brain.get_bottom(), handg.get_top(), color=BRAIN, stroke_width=5, buff=0.1)
            self.play(GrowArrow(a2), Indicate(brain[0], color=BRAIN), run_time=0.7)
            self.wait_until_bookmark("plays")
            a3 = Arrow(handg[0].get_left(), board.grid.get_right() + DOWN * 1.2, color=HAND, stroke_width=5, buff=0.15)
            self.play(GrowArrow(a3), run_time=0.5)
            robot_move(self, board, magnet, "e5", speed=0.7)
        self.wait(0.6)
        self.play(FadeOut(Group(*self.mobjects)), run_time=0.7)


# ── 3. How it sees ───────────────────────────────────────────────────────
class Vision(KoiScene):
    def construct(self):
        board = ChessBoard(sq=0.62).move_to([-3.4, -0.1, 0])
        panel_x = 3.4
        hand = make_hand(board.sq)

        def step_title(n, text, color=EYE):
            return VGroup(T(str(n), 30, BG, weight=BOLD).add_background_rectangle(color=color, opacity=1, buff=0.12),
                          T(text, 34, color)).arrange(RIGHT, buff=0.3).move_to([panel_x, 3.0, 0])

        with self.voiceover(
            "How does it see your move? "
            "<bookmark mark='gate'/>First, a simple motion detector waits for your hand to enter the board, "
            "<bookmark mark='leave'/>and then leave. "
            "<bookmark mark='scan'/>Then a small AI model looks at all sixty-four squares, and answers just one question "
            "for each: <bookmark mark='e'/>empty, <bookmark mark='w'/>white, <bookmark mark='b'/>or black."
        ):
            head = T("How it sees", 46).to_edge(UP, buff=0.35)
            self.play(FadeIn(board), Write(head), run_time=1)
            self.play(head.animate.scale(0.6).to_corner(UL, buff=0.35), run_time=0.6)

            self.wait_until_bookmark("gate")
            t1 = step_title(1, "Motion gate")
            dot = Dot(radius=0.16, color=MUTED)
            state = T("waiting…", 30, MUTED)
            meter = VGroup(dot, state).arrange(RIGHT, buff=0.3).move_to([panel_x, 1.6, 0])
            self.play(FadeIn(t1, shift=DOWN * 0.2), FadeIn(meter), run_time=0.6)
            # hand enters → motion
            a, b, piece, *_ = board.push("e4")
            grip = UP * board.sq * 0.95
            hand.move_to(board.at(a) + DOWN * 6)
            self.add(hand)
            moving = T("motion", 30, HAND).move_to(state, aligned_edge=LEFT)
            self.play(hand.animate.move_to(board.at(a) - grip), dot.animate.set_color(HAND),
                      Transform(state, moving), run_time=0.8)
            self.play(hand.animate.move_to(board.at(b) - grip), piece.animate.move_to(board.at(b)), run_time=0.9)
            self.wait_until_bookmark("leave")
            quiet = T("quiet: board settled", 30, GOOD).move_to(state, aligned_edge=LEFT)
            self.play(hand.animate.shift(DOWN * 6), run_time=0.7)
            self.remove(hand)
            self.play(dot.animate.set_color(GOOD), Transform(state, quiet), run_time=0.5)

            self.wait_until_bookmark("scan")
            t2 = step_title(2, "Read all 64 squares")
            self.play(FadeOut(meter), ReplacementTransform(t1, t2), run_time=0.6)
            scan_sweep(self, board, run_time=1.6)
            glyphs = VGroup(*[p.glyph for p in board.pieces.values()])
            empties = VGroup(*[Dot(radius=board.sq * 0.08, color=MUTED).move_to(board.at(n))
                               for n in board.squares if n not in board.pieces]).set_z_index(3)
            self.play(glyphs.animate.set_opacity(0), run_time=0.6)

            def legend_row(mob, text):
                return VGroup(mob, T(text, 30)).arrange(RIGHT, buff=0.35)

            leg_e = legend_row(Dot(radius=0.12, color=MUTED), "empty")
            leg_w = legend_row(Circle(radius=0.22, fill_color=WHITE_PC, fill_opacity=1, stroke_width=0), "white")
            leg_b = legend_row(Circle(radius=0.22, fill_color=BLACK_PC, fill_opacity=1, stroke_color="#9aa1b3",
                                      stroke_width=2), "black")
            legend = VGroup(leg_e, leg_w, leg_b).arrange(DOWN, buff=0.35, aligned_edge=LEFT).move_to([panel_x, 1.0, 0])
            self.wait_until_bookmark("e")
            self.play(FadeIn(leg_e), LaggedStart(*[FadeIn(d, scale=0.3) for d in empties], lag_ratio=0.02),
                      run_time=0.7)
            self.wait_until_bookmark("w")
            self.play(FadeIn(leg_w), Indicate(VGroup(*[p for p in board.pieces.values() if p.is_white]),
                                              color=WHITE, scale_factor=1.1), run_time=0.6)
            self.wait_until_bookmark("b")
            self.play(FadeIn(leg_b), Indicate(VGroup(*[p for p in board.pieces.values() if not p.is_white]),
                                              color=MUTED, scale_factor=1.1), run_time=0.6)

        with self.voiceover(
            "It never needs to know which piece is which. "
            "<bookmark mark='start'/>The game starts from the standard position, so the software already knows."
        ):
            note = T("Which piece? Not needed.", 32, ACCENT).move_to([panel_x, -1.2, 0])
            self.play(FadeIn(note, shift=UP * 0.2), run_time=0.6)
            self.wait_until_bookmark("start")
            note2 = T("Identity is tracked in software,\nmove by move, from the start.", 26, MUTED,
                      line_spacing=0.9).next_to(note, DOWN, buff=0.3)
            self.play(FadeIn(note2), glyphs.animate.set_opacity(1), run_time=1.0)

        with self.voiceover(
            "Comparing before and after, two squares changed: <bookmark mark='e2'/>e2 became empty, "
            "<bookmark mark='e4'/>and e4 turned white. "
            "<bookmark mark='legal'/>That change is checked against every legal move, "
            "<bookmark mark='one'/>and exactly one fits: pawn to e4."
        ):
            t3 = step_title(3, "Match a legal move", GOOD)
            self.play(FadeOut(VGroup(legend, note, note2, empties)), ReplacementTransform(t2, t3), run_time=0.6)
            self.wait_until_bookmark("e2")
            h2 = board.highlight("e2", BAD)
            d1 = T("e2:  white → empty", 32).move_to([panel_x, 1.8, 0])
            self.play(Create(h2), FadeIn(d1), run_time=0.6)
            self.wait_until_bookmark("e4")
            h4 = board.highlight("e4", GOOD)
            d2 = T("e4:  empty → white", 32).next_to(d1, DOWN, buff=0.25, aligned_edge=LEFT)
            self.play(Create(h4), FadeIn(d2), run_time=0.6)

            self.wait_until_bookmark("legal")
            start = chess.Board()
            sans = sorted(start.san(m) for m in start.legal_moves)  # all 20 opening moves
            cells = VGroup(*[T(s, 26) for s in sans]).arrange_in_grid(rows=4, cols=5, buff=(0.45, 0.28))
            cells.move_to([panel_x, -1.3, 0])
            self.play(LaggedStart(*[FadeIn(c, scale=0.8) for c in cells], lag_ratio=0.05), run_time=1.2)
            hit = cells[sans.index("e4")]
            others = VGroup(*[c for c in cells if c is not hit])
            self.play(others.animate.set_opacity(0.18), run_time=1.0)
            self.wait_until_bookmark("one")
            box = SurroundingRectangle(hit, color=GOOD, buff=0.1, stroke_width=4)
            self.play(Create(box), hit.animate.set_color(GOOD), run_time=0.6)
            big = VGroup(T("e4", 72, GOOD, weight=BOLD), T("✓", 72, GOOD)).arrange(RIGHT, buff=0.3)
            big.move_to([panel_x, -1.3, 0])
            self.play(FadeOut(others), ReplacementTransform(VGroup(hit, box), big), run_time=0.8)

        with self.voiceover(
            "And if the picture is unclear, or more than one move fits, <bookmark mark='flag'/>it doesn't guess. "
            "It flags the squares and asks you."
        ):
            self.play(FadeOut(VGroup(big, d1, d2, h2, h4)), run_time=0.5)
            # an unclear read: a hand shadow over two squares
            fog = VGroup(*[Square(board.sq, stroke_width=0, fill_color=MUTED, fill_opacity=0.55)
                           .move_to(board.at(n)) for n in ("f6", "g6")]).set_z_index(4)
            qs = VGroup(*[T("?", 34, WHITE).move_to(board.at(n)).set_z_index(5) for n in ("f6", "g6")])
            self.play(FadeIn(fog), FadeIn(qs), run_time=0.7)
            self.wait_until_bookmark("flag")
            reds = VGroup(*[board.highlight(n, BAD) for n in ("f6", "g6")])
            flag = badge("FLAGGED: check f6, g6", BAD, 30).move_to([panel_x, 0.8, 0])
            never = T("no guessing — you confirm", 28, MUTED).next_to(flag, DOWN, buff=0.3)
            self.play(Create(reds), FadeIn(flag, scale=1.2), run_time=0.7)
            self.play(FadeIn(never), run_time=0.5)
        self.wait(0.5)
        self.play(FadeOut(Group(*self.mobjects)), run_time=0.7)


# ── 4. How it moves ──────────────────────────────────────────────────────
class RobotArm(KoiScene):
    def construct(self):
        head = T("How it moves", 46).to_edge(UP, buff=0.35)

        sf = block("Stockfish", "chess engine", BRAIN)
        pi = block("Raspberry Pi 5", "the brain", BRAIN)
        uno = block("Arduino Uno", "motion control", HAND)
        tmc = block("2× TMC2208", "stepper drivers", HAND)
        nema = block("2× NEMA 17", "CoreXY gantry", HAND)
        drv = block("DRV8872", "H-bridge", HAND)
        mag = block("Electromagnet", "under the board", HAND)
        psu = block("7.5 V supply", "motors + magnet", ACCENT, w=2.4, h=0.9)
        row1 = VGroup(sf, pi, uno).arrange(RIGHT, buff=1.8).move_to([0, 1.9, 0])
        tmc.move_to([uno.get_x() - 1.7, 0.0, 0])
        drv.move_to([uno.get_x() + 1.7, 0.0, 0])
        nema.next_to(tmc, DOWN, buff=0.8)
        mag.next_to(drv, DOWN, buff=0.8)
        psu.move_to([-2.6, -1.5, 0])
        arrows = VGroup(
            Arrow(sf.get_right(), pi.get_left(), buff=0.08, color=BRAIN),
            Arrow(pi.get_right(), uno.get_left(), buff=0.08, color=EYE),
            Arrow(uno.get_bottom(), tmc.get_top(), buff=0.08, color=HAND),
            Arrow(uno.get_bottom(), drv.get_top(), buff=0.08, color=HAND),
            Arrow(tmc.get_bottom(), nema.get_top(), buff=0.08, color=HAND),
            Arrow(drv.get_bottom(), mag.get_top(), buff=0.08, color=HAND),
        )
        usb = T("USB serial", 22, EYE).next_to(arrows[1], UP, buff=0.15)
        power = VGroup(
            DashedLine(psu.get_right(), tmc.get_left(), color=ACCENT, stroke_width=2),
            DashedLine(psu.get_right(), mag.get_left() + LEFT * 0.05, color=ACCENT, stroke_width=2),
        )

        with self.voiceover(
            "Now it's the computer's turn. <bookmark mark='sf'/>Stockfish picks a reply. "
            "<bookmark mark='usb'/>The Pi sends the move over USB to an Arduino Uno, "
            "<bookmark mark='steppers'/>which drives two stepper motors on a CoreXY frame under the board, "
            "<bookmark mark='magnet'/>and switches an electromagnet on."
        ):
            self.play(Write(head), run_time=0.8)
            self.wait_until_bookmark("sf")
            self.play(FadeIn(sf, shift=UP * 0.2), run_time=0.6)
            self.play(GrowArrow(arrows[0]), FadeIn(pi, shift=UP * 0.2), run_time=0.7)
            self.wait_until_bookmark("usb")
            self.play(GrowArrow(arrows[1]), FadeIn(usb), FadeIn(uno, shift=UP * 0.2), run_time=0.8)
            pkt = badge("d7d5", EYE, 22).move_to(arrows[1].get_start())
            self.play(pkt.animate.move_to(arrows[1].get_end()), run_time=0.9)
            self.play(FadeOut(pkt), run_time=0.2)
            self.wait_until_bookmark("steppers")
            self.play(GrowArrow(arrows[2]), FadeIn(tmc), run_time=0.6)
            self.play(GrowArrow(arrows[4]), FadeIn(nema), FadeIn(psu), Create(power[0]), run_time=0.8)
            self.wait_until_bookmark("magnet")
            self.play(GrowArrow(arrows[3]), FadeIn(drv), run_time=0.6)
            self.play(GrowArrow(arrows[5]), FadeIn(mag), Create(power[1]), run_time=0.6)
            self.play(Indicate(mag, color=HAND), run_time=0.7)

        diagram = VGroup(row1, tmc, drv, nema, mag, psu, arrows, usb, power)
        board = ChessBoard(sq=0.56, ring=True, fen=chess.Board().fen()).move_to([-2.3, -0.35, 0])
        hand = make_hand(board.sq)
        magnet = make_magnet_glow(board)
        cap_x = 4.2

        def caption(text, color=HAND):
            return T(text, 32, color).move_to([cap_x, 2.0, 0])

        with self.voiceover(
            "The magnet grabs the piece through the board, and slides it to its square."
        ):
            self.play(FadeOut(diagram), head.animate.scale(0.6).to_corner(UL, buff=0.35), run_time=0.7)
            self.play(FadeIn(board), run_time=0.8)
            hand_move(self, board, hand, "e4", speed=0.6)
            c = caption("straight slide")
            self.play(FadeIn(magnet), FadeIn(c), run_time=0.5)
            robot_move(self, board, magnet, "d5", speed=0.8)

        with self.voiceover(
            "Knights are trickier, because they can't jump. <bookmark mark='gaps'/>So the magnet steers them along "
            "the gaps between the squares, past the other pieces."
        ):
            hand_move(self, board, hand, "Nc3", speed=0.6)
            self.wait_until_bookmark("gaps")
            c2 = VGroup(T("knight", 32, HAND), T("along the gaps,\nat lower magnet power", 24, MUTED,
                                                line_spacing=0.9)).arrange(DOWN, buff=0.15).move_to([cap_x, 1.8, 0])
            self.play(ReplacementTransform(c, c2), run_time=0.5)
            tr = robot_move(self, board, magnet, "Nf6", speed=1.1, show_trace=True)
            self.play(FadeOut(tr), run_time=0.4)

        with self.voiceover(
            "For a capture, the arm first carries your piece off to a parking ring around the board, "
            "<bookmark mark='then'/>then moves its own piece in."
        ):
            hand_move(self, board, hand, "exd5", speed=0.6)
            c3 = VGroup(T("capture", 32, HAND), T("victim parked on\nthe 32-slot ring first", 24, MUTED,
                                                 line_spacing=0.9)).arrange(DOWN, buff=0.15).move_to([cap_x, 1.8, 0])
            self.play(ReplacementTransform(c2, c3), Indicate(board.ring, color=HAND, scale_factor=1.0), run_time=0.8)
            robot_move(self, board, magnet, "Qxd5", speed=0.9)
        self.wait(0.5)
        self.play(FadeOut(Group(*self.mobjects)), run_time=0.7)


# ── 5. Coach & safety ────────────────────────────────────────────────────
class CoachSafety(KoiScene):
    def construct(self):
        fen = chess.Board()
        fen.push_san("e4"); fen.push_san("e5")
        board = ChessBoard(sq=0.6, fen=fen.fen()).move_to([-3.0, -0.2, 0])
        hand = make_hand(board.sq)
        magnet = make_magnet_glow(board)
        px = 3.6

        # eval bar beside the board (white share of the bar)
        ev = ValueTracker(0.52)
        bar_h = board.grid.height
        bar_frame = Rectangle(width=0.32, height=bar_h, stroke_color=MUTED, stroke_width=2,
                              fill_color=BLACK_PC, fill_opacity=1).next_to(board.coords, LEFT, buff=0.25)
        bar_fill = always_redraw(lambda: Rectangle(
            width=0.32, height=max(0.01, bar_h * ev.get_value()), stroke_width=0, fill_color=WHITE_PC, fill_opacity=1
        ).align_to(bar_frame, DOWN).align_to(bar_frame, LEFT))

        with self.voiceover(
            "Player Koi can also teach. <bookmark mark='arrow'/>In guided mode, a coach draws the best move as a green "
            "arrow, <bookmark mark='why'/>and explains why."
        ):
            head = T("Coach & safety", 46).to_edge(UP, buff=0.35)
            self.play(Write(head), FadeIn(board), run_time=1)
            self.play(head.animate.scale(0.6).to_corner(UL, buff=0.35), FadeIn(bar_frame), FadeIn(bar_fill),
                      run_time=0.6)
            self.wait_until_bookmark("arrow")
            arrow = Arrow(board.at("g1"), board.at("f3"), buff=0, color=GOOD, stroke_width=10,
                          max_tip_length_to_length_ratio=0.35).set_z_index(5)
            self.play(GrowArrow(arrow), run_time=0.8)
            self.wait_until_bookmark("why")
            tip = VGroup(T("Coach:  Nf3", 34, GOOD),
                         T("develops the knight;\nattacks the pawn on e5", 26, MUTED, line_spacing=0.9)
                         ).arrange(DOWN, buff=0.2, aligned_edge=LEFT).move_to([px, 1.8, 0])
            self.play(FadeIn(tip, shift=LEFT * 0.2), run_time=0.7)

        grades = [("Best", GOOD), ("Good", "#9bd67a"), ("Inaccuracy", ACCENT), ("Mistake", HAND), ("Blunder", BAD)]
        with self.voiceover(
            "After you move, it grades your move, <bookmark mark='scale'/>from best all the way down to blunder."
        ):
            self.play(FadeOut(arrow), run_time=0.3)
            hand_move(self, board, hand, "Nf3", speed=0.7)
            g = badge("Best", GOOD, 34).move_to([px, 0.2, 0])
            self.play(FadeIn(g, scale=1.3), ev.animate.set_value(0.56), run_time=0.6)
            self.wait_until_bookmark("scale")
            for name, col in grades[1:]:
                ng = badge(name, col, 34).move_to(g)
                self.play(Transform(g, ng), ev.animate.set_value({"Good": 0.53, "Inaccuracy": 0.45,
                          "Mistake": 0.33, "Blunder": 0.12}[name]), run_time=0.45)
            self.play(Transform(g, badge("Best", GOOD, 34).move_to(g)), ev.animate.set_value(0.56), run_time=0.5)

        with self.voiceover("There are thousands of puzzles too, picked near your rating."):
            card = RoundedRectangle(width=4.4, height=1.7, corner_radius=0.2, stroke_color=ACCENT, stroke_width=3,
                                    fill_color=ACCENT, fill_opacity=0.08)
            card_txt = VGroup(T("Puzzle", 32, ACCENT),
                              T("mate in 2  ·  near your rating", 24, MUTED),
                              T("from the free Lichess database", 20, MUTED)).arrange(DOWN, buff=0.12)
            puzzle = VGroup(card, card_txt.move_to(card)).move_to([px, -1.8, 0])
            self.play(FadeIn(puzzle, shift=UP * 0.2), run_time=0.7)

        with self.voiceover(
            "And it checks its own work: after every robot move, <bookmark mark='scan'/>the camera reads all "
            "sixty-four squares again."
        ):
            self.play(FadeOut(VGroup(tip, g, puzzle)), run_time=0.5)
            self.play(FadeIn(magnet), run_time=0.3)
            robot_move(self, board, magnet, "Nc6", speed=0.7)
            self.wait_until_bookmark("scan")
            scan_sweep(self, board, run_time=1.3)
            ok = VGroup(T("✓", 44, GOOD), T("64 / 64 squares match", 30, GOOD)).arrange(RIGHT, buff=0.25)
            ok.move_to([px, 1.4, 0])
            self.play(FadeIn(ok, shift=UP * 0.2), run_time=0.5)

        with self.voiceover(
            "If a piece slipped, or a hand got in the way, <bookmark mark='halt'/>the arm stops, "
            "<bookmark mark='fix'/>and you fix it with undo or edit board."
        ):
            # the next robot move slips: the bishop stops short of its square
            board.game.push_san("Bc4")
            board.pieces["c4"] = board.pieces.pop("f1")
            self.play(board.pieces["c4"].animate.move_to(board.at("c4")), FadeOut(ok), run_time=0.8)
            a, b, piece, *_ = board.push("Bc5")
            self.play(magnet.animate.move_to(board.at(a)), run_time=0.5)
            stop = (board.at(a) + board.at(b)) / 2 + board.sq * 0.18 * RIGHT
            self.play(magnet.animate.move_to(board.at(b)), piece.animate.move_to(stop), run_time=1.0)
            scan_sweep(self, board, BAD, run_time=1.0)
            bad = VGroup(board.highlight("c5", BAD), board.highlight("f8", BAD))
            self.wait_until_bookmark("halt")
            halt = badge("HALT", BAD, 54).move_to([px, 1.5, 0])
            why = T("camera ≠ intended move  Bc5", 26, MUTED).next_to(halt, DOWN, buff=0.3)
            self.play(Create(bad), FadeIn(halt, scale=1.4), FadeIn(why), run_time=0.7)
            self.wait_until_bookmark("fix")
            btns = VGroup(*[
                VGroup(RoundedRectangle(width=2.2, height=0.75, corner_radius=0.12, stroke_color=EYE, stroke_width=3),
                       T(label, 28, EYE)) for label in ("Undo", "Edit board")
            ]).arrange(RIGHT, buff=0.4).move_to([px, -0.6, 0])
            for bt in btns:
                bt[1].move_to(bt[0])
            self.play(LaggedStart(*[FadeIn(bt, shift=UP * 0.2) for bt in btns], lag_ratio=0.3), run_time=0.8)
            self.play(Indicate(btns[0], color=EYE), run_time=0.6)
            self.play(piece.animate.move_to(board.at("f8")), magnet.animate.move_to(board.at("f8")),
                      FadeOut(bad), FadeOut(halt), FadeOut(why), run_time=1.0)
        self.wait(0.5)
        self.play(FadeOut(Group(*self.mobjects)), run_time=0.7)


# ── 6. Outro ─────────────────────────────────────────────────────────────
class Outro(KoiScene):
    def construct(self):
        title = VGroup(T("Player", 110), T("Koi", 110, HAND)).arrange(RIGHT, buff=0.35).shift(UP * 1.0)
        icons = VGroup(
            VGroup(camera_icon(s=0.8), T("sees", 34, EYE)).arrange(DOWN, buff=0.25),
            VGroup(chip_icon(s=0.8), T("thinks", 34, BRAIN)).arrange(DOWN, buff=0.25),
            VGroup(magnet_icon(s=0.8), T("moves", 34, HAND)).arrange(DOWN, buff=0.25),
        ).arrange(RIGHT, buff=1.6).next_to(title, DOWN, buff=0.8)
        foot = T("Raspberry Pi 5  ·  IMX219 camera  ·  Arduino Uno  ·  CoreXY gantry  ·  Stockfish", 22, MUTED)
        foot.to_edge(DOWN, buff=0.5)

        with self.voiceover(
            "Player Koi. <bookmark mark='real'/>A real chessboard that <bookmark mark='sees'/>sees, "
            "<bookmark mark='thinks'/>thinks, <bookmark mark='moves'/>and moves."
        ):
            self.play(Write(title), run_time=1.0)
            self.wait_until_bookmark("sees")
            self.play(FadeIn(icons[0], shift=UP * 0.2), run_time=0.4)
            self.wait_until_bookmark("thinks")
            self.play(FadeIn(icons[1], shift=UP * 0.2), run_time=0.4)
            self.wait_until_bookmark("moves")
            self.play(FadeIn(icons[2], shift=UP * 0.2), run_time=0.4)
        self.play(FadeIn(foot), run_time=0.8)
        self.wait(2.0)
        self.play(FadeOut(Group(*self.mobjects)), run_time=1.0)
