"""Puzzle mode's logic: the puzzle bank, your rating, and one puzzle's run.

No camera, no gantry, no threads -- web_ui.PuzzleController drives this and
owns all of that. What lives here is the part worth unit-testing
(tests/test_puzzles.py): which puzzle comes next, whether a move solves it,
what to move to set the next one up, and what the attempt was worth.

The puzzles are a slice of the Lichess puzzle database (CC0), cut down by
tools/make_puzzles.py to data/puzzles.csv. Lichess stores a puzzle as the
position *before* the opponent's move that sets up the tactic, plus the whole
line from there:

    fen     Black to move (the slice is filtered to that)
    moves   black_setup  white_1  black_1  white_2 ...

So the board the human sets up is `fen`, the arm plays moves[0] for Black, and
White -- the human, sitting on their usual side of the rig -- solves the rest.
Every even index after the first is the arm's reply, every odd index the
human's move to find.

A move that isn't the stored one still solves the puzzle if it mates, which is
the rule Lichess itself applies: a mate-in-2 often has several mating moves on
the last ply and it would be absurd to fail someone for picking another.
"""

import csv
import json
import os
import random
import time
from dataclasses import dataclass, field
from pathlib import Path

import chess

from board_state import FILES
from move_resolver import matrix_from_board

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BANK = REPO_ROOT / "data" / "puzzles.csv"
DEFAULT_PROGRESS = REPO_ROOT / "config" / "puzzles.json"

START_RATING = 1200
ELO_K = 32
# Adaptive picking looks this far either side of your rating first, then
# widens by the same step until something matches the other filters.
ADAPTIVE_WINDOW = 100
ADAPTIVE_MAX_WINDOW = 1500
HISTORY_LIMIT = 500
# Puzzles seen this recently are not offered again.
RECENT_EXCLUDE = 200

SOLVED = "solved"
HINTED = "hinted"
FAILED = "failed"
SKIPPED = "skipped"

# One puzzle's phases, in the order they normally happen.
SETUP = "setup"          # showing the layout; the human is placing pieces
OPPONENT = "opponent"    # Black's move from the line is being placed
SOLVING = "solving"      # White to move -- the human's turn
SOLVED_PHASE = "solved"
REVEALED = "revealed"    # gave up and was shown the answer

# The themes worth offering on the menu -- Lichess has ~60, most too niche to
# filter by on purpose.
MENU_THEMES = (
    "mateIn1", "mateIn2", "mateIn3", "fork", "pin", "skewer", "hangingPiece",
    "discoveredAttack", "sacrifice", "endgame", "short",
)

_PIECE_WORDS = ("pawn", "knight", "bishop", "rook", "queen", "king")


@dataclass(frozen=True)
class Puzzle:
    id: str
    fen: str
    moves: tuple
    rating: int
    themes: tuple = ()
    pieces: int = 32

    def board(self):
        return chess.Board(self.fen)

    def solution(self):
        return [chess.Move.from_uci(uci) for uci in self.moves]

    def target_matrix(self):
        return matrix_from_board(self.board())

    def info(self):
        return {"id": self.id, "rating": self.rating, "themes": list(self.themes),
                "pieces": self.pieces}


def _parse_row(row):
    moves = tuple(row["moves"].split())
    themes = tuple(row.get("themes", "").split())
    pieces = row.get("pieces")
    fen = row["fen"]
    if not pieces:
        pieces = len(chess.Board(fen).piece_map())
    return Puzzle(id=row["id"], fen=fen, moves=moves, rating=int(row["rating"]),
                  themes=themes, pieces=int(pieces))


class PuzzleBank:
    """All the puzzles, and the filters the menu offers over them."""

    def __init__(self, puzzles):
        self.puzzles = list(puzzles)

    @classmethod
    def load(cls, path=DEFAULT_BANK):
        with open(path, newline="") as handle:
            return cls(_parse_row(row) for row in csv.DictReader(handle))

    def __len__(self):
        return len(self.puzzles)

    def matching(self, themes=(), max_pieces=None, exclude=()):
        exclude = set(exclude)
        wanted = set(themes or ())
        return [
            p for p in self.puzzles
            if p.id not in exclude
            and (not wanted or wanted & set(p.themes))
            and (not max_pieces or p.pieces <= max_pieces)
        ]

    def pick(self, rating_range=None, target=None, themes=(), max_pieces=None,
             exclude=(), rng=random):
        """One puzzle, or None if nothing matches the filters at all.

        `rating_range` (lo, hi) is a hard filter. `target` is adaptive mode:
        the closest band around it that has anything in it. With neither,
        any rating goes. A recently-seen puzzle is only offered again when
        the filters leave nothing else.
        """
        pool = self.matching(themes, max_pieces, exclude) or self.matching(themes, max_pieces)
        if not pool:
            return None
        if rating_range is not None:
            lo, hi = rating_range
            ranged = [p for p in pool if lo <= p.rating <= hi]
            return rng.choice(ranged) if ranged else None
        if target is not None:
            window = ADAPTIVE_WINDOW
            while window <= ADAPTIVE_MAX_WINDOW:
                near = [p for p in pool if abs(p.rating - target) <= window]
                if near:
                    return rng.choice(near)
                window += ADAPTIVE_WINDOW
        return rng.choice(pool)


def elo_expected(rating, opponent):
    return 1.0 / (1.0 + 10 ** ((opponent - rating) / 400.0))


@dataclass
class PuzzleProgress:
    """Your puzzle rating and history, kept in config/puzzles.json so a
    restart doesn't cost you your streak."""

    path: Path = DEFAULT_PROGRESS
    rating: int = START_RATING
    streak: int = 0
    best_streak: int = 0
    history: list = field(default_factory=list)

    @classmethod
    def load(cls, path=DEFAULT_PROGRESS):
        path = Path(path)
        progress = cls(path=path)
        try:
            stored = json.loads(path.read_text())
        except (OSError, ValueError):
            return progress  # first run, or a file mangled by hand: start fresh
        progress.rating = int(stored.get("rating", START_RATING))
        progress.streak = int(stored.get("streak", 0))
        progress.best_streak = int(stored.get("best_streak", 0))
        progress.history = list(stored.get("history", []))[-HISTORY_LIMIT:]
        return progress

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({
            "rating": self.rating,
            "streak": self.streak,
            "best_streak": self.best_streak,
            "history": self.history[-HISTORY_LIMIT:],
        }, indent=2) + "\n")
        # Atomic, so a power cut mid-write loses one result rather than the
        # whole file.
        os.replace(tmp, self.path)

    def recent_ids(self, count=RECENT_EXCLUDE):
        return [entry["id"] for entry in self.history[-count:]]

    def record(self, puzzle, result):
        """Scores one attempt and saves. Returns the rating change.

        solved: full win. hinted: a win at half weight -- you got there, with
        help. failed: a loss. skipped: no rating change at all, since you
        never tried it.
        """
        before = self.rating
        if result == SKIPPED:
            delta = 0
        else:
            score = 0.0 if result == FAILED else 1.0
            k = ELO_K / 2 if result == HINTED else ELO_K
            delta = round(k * (score - elo_expected(self.rating, puzzle.rating)))
            if delta == 0:
                # A far-too-easy solve (or far-too-hard miss) rounds to nothing
                # in pure Elo, which reads as the result being ignored.
                delta = 1 if score else -1
            if result == FAILED:
                self.streak = 0
            else:
                self.streak += 1
                self.best_streak = max(self.best_streak, self.streak)
        self.rating = max(100, self.rating + delta)
        self.history.append({"id": puzzle.id, "result": result, "puzzle_rating": puzzle.rating,
                             "rating_before": before, "ts": int(time.time())})
        self.history = self.history[-HISTORY_LIMIT:]
        self.save()
        return self.rating - before

    def summary(self):
        solved = sum(1 for e in self.history if e["result"] in (SOLVED, HINTED))
        tried = sum(1 for e in self.history if e["result"] != SKIPPED)
        return {"rating": self.rating, "streak": self.streak,
                "best_streak": self.best_streak, "solved": solved, "attempted": tried}


def is_correct(board, played, expected):
    """Whether `played` solves this step: the stored move, or any mate."""
    if played == expected:
        return True
    if played not in board.legal_moves:
        return False
    scratch = board.copy(stack=False)
    scratch.push(played)
    return scratch.is_checkmate()


def _label_words(label):
    color, piece = label.split("-")
    return f"{color} {piece}"


def setup_diff(current, target):
    """What to change to turn `current` into `target` (both board matrices).

    Returns {"steps": [str], "remove": [sq], "add": [sq], "swap": [sq]} --
    removals first so there are free hands and free squares, then swaps, then
    placements. Squares are names like "e4", for the UI to highlight.
    """
    remove, swap, add = [], [], []
    for rank in range(8):
        for file in range(8):
            have, want = current[rank][file], target[rank][file]
            if have == want:
                continue
            name = f"{FILES[file]}{rank + 1}"
            if want is None:
                remove.append((name, f"remove the {_label_words(have)} from {name}"))
            elif have is None:
                add.append((name, f"put a {_label_words(want)} on {name}"))
            else:
                swap.append((name, f"{name}: replace the {_label_words(have)} "
                                   f"with a {_label_words(want)}"))
    return {
        "steps": [text for _sq, text in remove + swap + add],
        "remove": [sq for sq, _t in remove],
        "swap": [sq for sq, _t in swap],
        "add": [sq for sq, _t in add],
    }


def color_mismatches(read, target):
    """Squares where the camera's read disagrees with the target layout.

    `read` is TrackingLoop.read_board()'s {(file, rank): "empty"|"white"|
    "black"|UNRESOLVED}. An unresolved square counts as a mismatch -- the
    whole point of the check is not starting a puzzle on a position nobody
    has confirmed. Returns [{square, expected, seen}].
    """
    bad = []
    for rank in range(8):
        for file in range(8):
            label = target[rank][file]
            expected = "empty" if label is None else label.split("-")[0]
            seen = read.get((file, rank))
            if seen != expected:
                bad.append({"square": f"{FILES[file]}{rank + 1}", "expected": expected,
                            "seen": seen if isinstance(seen, str) else "unclear"})
    return bad


class PuzzleRun:
    """One attempt at one puzzle. Tracks where in the line it is and what
    the attempt will be scored as; knows nothing about how moves get made."""

    def __init__(self, puzzle):
        self.puzzle = puzzle
        self.solution = puzzle.solution()
        self.index = 0          # the next move in the line
        self.phase = SETUP
        self.failed_once = False
        self.hinted = False
        self.recorded = False
        self.wrong_note = None  # set after a wrong move until the next one

    @property
    def next_move(self):
        return self.solution[self.index] if self.index < len(self.solution) else None

    @property
    def players_turn(self):
        # Odd indices are White's -- the human's.
        return self.index % 2 == 1

    def begin(self):
        """Setup confirmed; the arm (or the human, for it) plays Black's
        opening move of the line."""
        self.phase = OPPONENT

    def opponent_done(self):
        self.index += 1
        self.phase = SOLVING if self.index < len(self.solution) else SOLVED_PHASE

    def player_moved(self, board, played):
        """Judge the human's move in `board` (the position before it).
        Returns True if it was right."""
        expected = self.next_move
        if expected is None or not is_correct(board, played, expected):
            self.failed_once = True
            return False
        self.wrong_note = None
        scratch = board.copy(stack=False)
        scratch.push(played)
        self.index += 1
        if scratch.is_checkmate() or self.index >= len(self.solution):
            self.index = len(self.solution)
            self.phase = SOLVED_PHASE
        else:
            self.phase = OPPONENT
        return True

    def hint(self):
        """The square of the piece to move, and marks the attempt hinted."""
        move = self.next_move
        if move is None or not self.players_turn:
            return None
        self.hinted = True
        return chess.square_name(move.from_square)

    def reveal(self):
        """Gives up: the rest of the line, as UCI strings."""
        self.phase = REVEALED
        return [m.uci() for m in self.solution[self.index:]]

    @property
    def finished(self):
        return self.phase in (SOLVED_PHASE, REVEALED)

    def result(self):
        if self.phase == REVEALED or self.failed_once:
            return FAILED
        if self.hinted:
            return HINTED
        return SOLVED
