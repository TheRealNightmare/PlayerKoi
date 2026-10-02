"""Shared palette and mobjects for the Player Koi explainer.

Every move on screen goes through ChessBoard, which keeps a python-chess
board in step, so an illegal move raises instead of being animated.
"""

import chess
from manim import *
from manim_voiceover import VoiceoverScene

from edge_tts_service import EdgeTTSService

# ── palette ──────────────────────────────────────────────────────────────
BG = "#0f1117"
LIGHT_SQ = "#3b4254"
DARK_SQ = "#272c3a"
WHITE_PC = "#f3eee4"
BLACK_PC = "#1a1c24"
EYE = "#58c4dd"      # camera / vision
BRAIN = "#b98cf0"    # Pi / engine
HAND = "#ff9f43"     # magnet / gantry
GOOD = "#5cd67a"
BAD = "#ff5c5c"
ACCENT = "#ffd166"
MUTED = "#8b93a7"
SKIN = "#d9a07a"

FONT = "Ubuntu Sans"
GLYPH_FONT = "DejaVu Sans"
GLYPHS = {"k": "♚", "q": "♛", "r": "♜", "b": "♝", "n": "♞", "p": "♟"}

config.background_color = BG


def T(text, size=32, color=WHITE, **kw):
    return Text(text, font=FONT, font_size=size, color=color, **kw)


class KoiScene(VoiceoverScene):
    def setup(self):
        super().setup()
        self.set_speech_service(EdgeTTSService(voice="en-US-AndrewNeural"))


# ── pieces & board ───────────────────────────────────────────────────────
class Piece(VGroup):
    def __init__(self, symbol: str, sq: float, **kw):
        super().__init__(**kw)
        self.symbol = symbol
        self.is_white = symbol.isupper()
        r = sq * 0.38
        self.base = Circle(
            radius=r,
            fill_color=WHITE_PC if self.is_white else BLACK_PC,
            fill_opacity=1,
            stroke_color=BLACK_PC if self.is_white else "#9aa1b3",
            stroke_width=2,
        )
        self.glyph = Text(
            GLYPHS[symbol.lower()], font=GLYPH_FONT, color=BLACK_PC if self.is_white else WHITE_PC
        ).scale_to_fit_height(r * 1.25)
        self.glyph.move_to(self.base)
        self.add(self.base, self.glyph)
        self.set_z_index(3)


class ChessBoard(VGroup):
    """8×8 board with optional coordinates and the 32-slot graveyard ring."""

    def __init__(self, sq=0.6, fen=chess.STARTING_FEN, coords=True, ring=False, **kw):
        super().__init__(**kw)
        self.game = chess.Board(fen)
        self.squares = {}
        self.grid = VGroup()
        for f in range(8):
            for r in range(8):
                s = Square(sq, stroke_width=0, fill_opacity=1, fill_color=LIGHT_SQ if (f + r) % 2 else DARK_SQ)
                s.move_to(np.array([(f - 3.5) * sq, (r - 3.5) * sq, 0]))
                self.squares[chess.square_name(chess.square(f, r))] = s
                self.grid.add(s)
        self.border = SurroundingRectangle(self.grid, buff=0, stroke_color=MUTED, stroke_width=2)
        self.add(self.grid, self.border)

        self.coords = VGroup()
        if coords:
            for f in range(8):
                self.coords.add(T("abcdefgh"[f], sq * 26, MUTED).move_to(self.point(f, -0.8 if not ring else -1.6)))
            for r in range(8):
                self.coords.add(T(str(r + 1), sq * 26, MUTED).move_to(self.point(-0.8 if not ring else -1.6, r)))
            # tuck them just outside the border
            for m in self.coords[:8]:
                m.shift(DOWN * sq * 0.05)
            self.add(self.coords)

        self.ring = VGroup()
        self.ring_slots = []  # (f, r) in board units
        self.ring_used = set()
        if ring:
            for i in range(8):
                self.ring_slots += [(-1, i), (8, i), (i, -1), (i, 8)]
            for f, r in self.ring_slots:
                self.ring.add(
                    DashedVMobject(Circle(radius=sq * 0.3, stroke_color=MUTED, stroke_width=1.5), num_dashes=12)
                    .move_to(self.point(f, r))
                )
            self.ring_border = DashedVMobject(
                Square(sq * 10, stroke_color=MUTED, stroke_width=1.5).move_to(self.grid), num_dashes=60
            )
            self.add(self.ring_border, self.ring)

        self.pieces = {}
        self.piece_group = VGroup()
        for sqi, p in self.game.piece_map().items():
            pc = Piece(p.symbol(), sq).move_to(self.squares[chess.square_name(sqi)])
            self.pieces[chess.square_name(sqi)] = pc
            self.piece_group.add(pc)
        self.add(self.piece_group)

    # geometry that survives shift/scale of the whole board
    @property
    def sq(self):
        return np.linalg.norm(self.squares["b1"].get_center() - self.squares["a1"].get_center())

    def point(self, f, r):
        a1 = self.squares["a1"].get_center()
        dx = self.squares["b1"].get_center() - a1
        dy = self.squares["a2"].get_center() - a1
        return a1 + f * dx + r * dy

    def at(self, name):
        return self.squares[name].get_center()

    @staticmethod
    def fr(name):
        s = chess.parse_square(name)
        return chess.square_file(s), chess.square_rank(s)

    def highlight(self, name, color=ACCENT, width=6):
        return Square(self.sq, stroke_color=color, stroke_width=width, fill_opacity=0).move_to(self.at(name)).set_z_index(4)

    # paths
    def knight_path(self, a, b):
        """Square-cornered L along the gridlines: half a square out, the long run, half back."""
        (f0, r0), (f1, r1) = self.fr(a), self.fr(b)
        df, dr = f1 - f0, r1 - r0
        if abs(dr) == 2:
            pts = [(f0, r0), (f0 + df / 2, r0), (f0 + df / 2, r1), (f1, r1)]
        else:
            pts = [(f0, r0), (f0, r0 + dr / 2), (f1, r0 + dr / 2), (f1, r1)]
        return [self.point(*p) for p in pts]

    def nearest_slot(self, name):
        f0, r0 = self.fr(name)
        free = [s for s in self.ring_slots if s not in self.ring_used]
        slot = min(free, key=lambda s: (s[0] - f0) ** 2 + (s[1] - r0) ** 2)
        self.ring_used.add(slot)
        return slot

    def graveyard_path(self, name, slot):
        f0, r0 = self.fr(name)
        sf, sr = slot
        if sf in (-1, 8):  # side column: run along a horizontal gridline
            d = 0.5 if sr >= r0 else -0.5
            pts = [(f0, r0), (f0, r0 + d), (sf, r0 + d), (sf, sr)]
        else:              # top/bottom row: run along a vertical gridline
            d = 0.5 if sf >= f0 else -0.5
            pts = [(f0, r0), (f0 + d, r0), (f0 + d, sr), (sf, sr)]
        return [self.point(*p) for p in pts]

    # moves
    def push(self, san):
        move = self.game.parse_san(san)  # raises on an illegal move
        a, b = chess.square_name(move.from_square), chess.square_name(move.to_square)
        victim_sq = None
        if self.game.is_en_passant(move):
            victim_sq = chess.square_name(move.to_square + (-8 if self.game.turn else 8))
        elif self.game.is_capture(move):
            victim_sq = b
        is_knight = self.game.piece_type_at(move.from_square) == chess.KNIGHT
        self.game.push(move)
        piece = self.pieces.pop(a)
        victim = self.pieces.pop(victim_sq) if victim_sq else None
        self.pieces[b] = piece
        return a, b, piece, victim_sq, victim, is_knight


def follow_path(mobs, pts, total_time=2.0):
    """Animate mobjects together along a polyline, segment by segment."""
    lengths = [np.linalg.norm(pts[i + 1] - pts[i]) for i in range(len(pts) - 1)]
    tot = sum(lengths) or 1
    anims = []
    for i, L in enumerate(lengths):
        if L < 1e-6:
            continue
        anims.append(
            AnimationGroup(*[m.animate.move_to(pts[i + 1]) for m in mobs], run_time=total_time * L / tot, rate_func=linear)
        )
    return Succession(*anims, rate_func=smooth) if anims else Wait(0.01)


def trace(pts, color=HAND):
    line = VMobject(stroke_color=color, stroke_width=4).set_points_as_corners(pts)
    return DashedVMobject(line, num_dashes=max(8, int(len(pts) * 6))).set_z_index(2)


def robot_move(scene, board, magnet, san, speed=1.0, show_trace=False):
    """Engine move: the magnet travels under the piece and drags it (captures go to the ring first)."""
    a, b, piece, victim_sq, victim, is_knight = board.push(san)
    tr = None
    if victim is not None:
        slot = board.nearest_slot(victim_sq)
        gp = board.graveyard_path(victim_sq, slot)
        scene.play(magnet.animate.move_to(board.at(victim_sq)), run_time=0.8 * speed)
        scene.play(follow_path([magnet, victim], gp, 2.0 * speed))
    scene.play(magnet.animate.move_to(board.at(a)), run_time=0.8 * speed)
    pts = board.knight_path(a, b) if is_knight else [board.at(a), board.at(b)]
    if show_trace:
        tr = trace(pts)
        scene.play(Create(tr), run_time=0.8 * speed)
    scene.play(follow_path([magnet, piece], pts, (2.4 if is_knight else 1.4) * speed))
    return tr


# ── hand & icons ─────────────────────────────────────────────────────────
def make_hand(sq=0.6):
    """Top-down cartoon hand, fingertips pointing up (the player sits at White)."""
    palm = RoundedRectangle(width=sq * 1.3, height=sq * 1.25, corner_radius=sq * 0.35)
    fingers = VGroup(*[
        RoundedRectangle(width=sq * 0.26, height=sq * h, corner_radius=sq * 0.13) for h in (0.75, 0.95, 0.9, 0.7)
    ]).arrange(RIGHT, buff=sq * 0.05, aligned_edge=DOWN)
    fingers.next_to(palm, UP, buff=-sq * 0.25)
    thumb = RoundedRectangle(width=sq * 0.28, height=sq * 0.7, corner_radius=sq * 0.14).rotate(-0.7)
    thumb.next_to(palm, LEFT, buff=-sq * 0.2).shift(UP * sq * 0.1)
    hand = VGroup(palm, fingers, thumb)
    hand.set_style(fill_color=SKIN, fill_opacity=0.92, stroke_color="#b07a55", stroke_width=2)
    hand.set_z_index(6)
    return hand


def hand_move(scene, board, hand, san, speed=1.0):
    """Player move: a hand comes in from the bottom, carries the piece, leaves."""
    a, b, piece, _, victim, _ = board.push(san)
    grip = UP * board.sq * 0.95  # fingertips over the piece
    off = board.at(a) + DOWN * 6
    hand.move_to(off)
    scene.add(hand)
    scene.play(hand.animate.move_to(board.at(a) - grip), run_time=0.7 * speed)
    anims = [hand.animate.move_to(board.at(b) - grip), piece.animate.move_to(board.at(b))]
    if victim is not None:
        anims.append(victim.animate.scale(0.4).set_opacity(0))
    scene.play(*anims, run_time=0.9 * speed)
    if victim is not None:
        board.piece_group.remove(victim)
    scene.play(hand.animate.move_to(board.at(b) + DOWN * 6), run_time=0.6 * speed)
    scene.remove(hand)


def make_magnet_glow(board):
    g = VGroup(
        Circle(radius=board.sq * 0.62, stroke_width=0, fill_color=HAND, fill_opacity=0.18),
        Circle(radius=board.sq * 0.45, stroke_color=HAND, stroke_width=4, fill_color=HAND, fill_opacity=0.35),
    ).set_z_index(1)
    g.move_to(board.point(8, -1))  # parked in the corner beyond h1
    return g


def camera_icon(color=EYE, s=1.0):
    body = RoundedRectangle(width=1.3 * s, height=0.85 * s, corner_radius=0.15 * s, stroke_color=color, stroke_width=4)
    lens = Circle(radius=0.27 * s, stroke_color=color, stroke_width=4).move_to(body)
    pupil = Dot(radius=0.09 * s, color=color).move_to(lens)
    bump = RoundedRectangle(width=0.4 * s, height=0.18 * s, corner_radius=0.06 * s, stroke_color=color, stroke_width=4)
    bump.next_to(body, UP, buff=0).shift(LEFT * 0.3 * s)
    return VGroup(body, lens, pupil, bump)


def chip_icon(label="Pi 5", color=BRAIN, s=1.0):
    core = RoundedRectangle(width=1.0 * s, height=1.0 * s, corner_radius=0.1 * s, stroke_color=color, stroke_width=4,
                            fill_color=color, fill_opacity=0.15)
    pins = VGroup()
    for i in range(4):
        off = (i - 1.5) * 0.22 * s
        for d in (UP, DOWN, LEFT, RIGHT):
            p = Line(ORIGIN, d * 0.16 * s, stroke_color=color, stroke_width=3)
            p.move_to(core.get_center() + d * 0.58 * s + (RIGHT if d[1] else UP) * off)
            pins.add(p)
    txt = T(label, 22 * s, color).move_to(core)
    return VGroup(pins, core, txt)


def magnet_icon(color=HAND, s=1.0):
    arc = Arc(radius=0.42 * s, start_angle=PI, angle=PI, stroke_color=color, stroke_width=22 * s)
    legs = VGroup(*[Line(ORIGIN, UP * 0.35 * s, stroke_color=color, stroke_width=22 * s)
                    .move_to(arc.get_start() + UP * 0.175 * s), Line(ORIGIN, UP * 0.35 * s, stroke_color=color,
                    stroke_width=22 * s).move_to(arc.get_end() + UP * 0.175 * s)])
    tips = VGroup(*[Rectangle(width=0.2 * s, height=0.14 * s, stroke_width=0, fill_color="#dfe3ea", fill_opacity=1)
                    .move_to(l.get_top() + UP * 0.07 * s) for l in legs])
    return VGroup(arc, legs, tips)


def block(label, sub=None, color=BRAIN, w=2.6, h=1.0, size=26):
    box = RoundedRectangle(corner_radius=0.15, width=w, height=h, stroke_color=color, stroke_width=3,
                           fill_color=color, fill_opacity=0.12)
    content = T(label, size)
    if sub:
        content = VGroup(content, T(sub, size * 0.68, MUTED)).arrange(DOWN, buff=0.08)
    content.move_to(box)
    return VGroup(box, content)


def badge(text, color, size=30):
    t = T(text, size, BG, weight=BOLD)
    box = RoundedRectangle(corner_radius=0.12, width=t.width + 0.5, height=t.height + 0.3, stroke_width=0,
                           fill_color=color, fill_opacity=1)
    return VGroup(box, t.move_to(box))


def scan_sweep(scene, board, color=EYE, run_time=1.4):
    """A bright bar sweeping rank 8 → rank 1, the camera reading every square."""
    bar = Rectangle(width=board.sq * 8.2, height=board.sq * 0.14, stroke_width=0, fill_color=color, fill_opacity=0.85)
    bar.move_to(board.point(3.5, 7.9)).set_z_index(5)
    glow = bar.copy().stretch(5, 1).set_fill(opacity=0.15)
    g = VGroup(glow, bar)
    scene.add(g)
    scene.play(g.animate.move_to(board.point(3.5, -0.9)), run_time=run_time, rate_func=linear)
    scene.remove(g)
