# Plyer Koi

Real-time chess piece detection on a Raspberry Pi 5: a fixed overhead
IMX219 camera watches a physical chess board, tracks moves as real
algebraic notation via python-chess, and serves a live board diagram +
camera feed from a small built-in web UI.

The board starts at the standard position (assumed, not detected -- see
"How detection works" below), and the tracker maintains full piece
identity in software from there by applying resolved legal moves -- so
vision never needs to recognize piece *type* at all, only occupancy and
color (empty/white/black) per square. That's a small trained classifier
(`src/square_classifier.py`, 3 classes, run over all 64 squares -- see
`training/NOTES.md` for why this went through a classical-CV pixel-math
phase first and came back to a model), gated by a cheap ML-free motion
detector and cross-checked against every legal chess move. There is no
automatic rescan fallback: when a settle can't be resolved with
confidence, the web UI flags it and offers a manual correction. See
`src/tracking_loop.py` for the full flow.

**[RUNBOOK.md](RUNBOOK.md)** has the copy-pasteable command sequences for
retraining, deploying, and swapping boards -- start there for day-to-day
operation. **[docs/DESIGN.md](docs/DESIGN.md)** is the full system
document: architecture, hardware, geometry, protocols, the reasoning behind
each decision, and the open risks. **[docs/HARDWARE.md](docs/HARDWARE.md)**
is the robot arm's circuit and bring-up procedure.

## Hardware assumed

- Raspberry Pi 5 (8GB), CPU-only inference (no Coral/Hailo accelerator)
- Third-party IMX219 CSI camera module, rigidly mounted directly overhead
  the board, looking straight down
- Standard tournament Staunton chess set

## One-time Pi setup

```bash
# picamera2 comes from apt, NOT pip -- it depends on system libcamera bindings
sudo apt update
sudo apt install -y python3-picamera2 python3-opencv

# optional: the engine opponent (web UI plays Black). Without it everything
# still works, the engine box just reports it isn't installed.
sudo apt install -y stockfish

# Create a venv that can still see the apt-installed picamera2/opencv
python3 -m venv --system-site-packages .venv
source .venv/bin/activate

pip install -r requirements.txt
```

Enable the camera interface if you haven't already (`sudo raspi-config` ->
Interface Options -> Camera), and confirm the Pi detects it:

```bash
libcamera-hello --list-cameras
```

## Workflow

### 1. Camera smoke test

```bash
python3 src/capture.py capture_test.jpg
```

Open `capture_test.jpg` and confirm the whole board is visible and in
focus.

### 2. Calibrate

Requires a display (HDMI or VNC) to click on the preview window.

```bash
python3 src/calibrate.py
```

Click the board's 4 outer corners in order: a1, h1, h8, a8 (this also
tells the system which side is which -- always click from the same
physical orientation you intend to play from). Check
`config/calibration_preview.jpg` afterwards -- it should look like a
clean, square 8x8 grid. If it looks skewed, re-run calibration and click
more precisely on the actual board corners (not the outer frame/border,
if your board has one).

Re-run this any time the camera or board physically moves.

### 3. Get a classifier

Training doesn't happen on the Pi. See
[`training/NOTES.md`](training/NOTES.md) for the full pipeline:
`src/collect_square_crops.py` (on the Pi, collects real training photos
from your own board -- no public dataset, no legal chess position needed)
-> `training/train_classifier.py` (on a GPU machine) ->
`training/export_ncnn.py` -> `training/deploy.py` back to the Pi's
`models/square_classifier_ncnn_model`.

### 4. Run

```bash
python3 src/main.py                 # terminal/log output
python3 src/web_ui.py               # + http://<this-pi>:8000/ board diagram and camera feed
```

Prints/serves the 8x8 board matrix and the resolved move (e.g. `Nf3`)
whenever the board settles into a new state. There's no fixed detection
interval anymore -- `--poll-interval` (web_ui.py) just controls how often
the cheap motion gate checks the ROI, not how often full detection runs.
Add `--log board.log` to `main.py` to also append changes to a file.
Uppercase letters are white pieces, lowercase are black, `.` is empty:

```
8  r n b q k b n r
7  p p p p p p p p
6  . . . . . . . .
5  . . . . . . . .
4  . . . . . . . .
3  . . . . . . . .
2  P P P P P P P P
1  R N B Q K B N R
   A B C D E F G H
```

## Playing against the engine

With `stockfish` installed, the box at the bottom of the web UI plays the
Black side. Toggle it on, set the skill slider (0-20; start low), and play
your White move physically. The engine replies with the move to place,
e.g. `d7 → d5`, highlighting both squares on the diagram, and spelling out
the extra physical action for castling ("ALSO move the rook h8 → f8"), en
passant ("ALSO remove the pawn on d4") and promotion.

While an engine move is pending it's the *only* move the tracker will
accept -- place something else and it says so rather than quietly applying
it. Undo and Edit board still override everything if you want to deviate
deliberately.

## The robot arm

With the gantry built (see **[docs/HARDWARE.md](docs/HARDWARE.md)** for the
full circuit and bring-up), the engine plays its own moves: a CoreXY frame
under the board drags an electromagnet along the gaps between squares, an
Arduino Uno drives it, and the Pi sends it where to go.

```bash
python3 src/web_ui.py --robot /dev/ttyACM0    # the real gantry
python3 src/web_ui.py --robot mock            # dry run: logs commands, moves nothing
```

The arm homes on startup, then plays automatically whenever the engine
decides on a move. It is off unless `--robot` is given, so nothing here
changes the hand-played setup above.

The split is deliberate: the Arduino knows no chess at all. It takes
`GOTO`/`MAG`/`PULSE`/`TOPPLE` on a serial line and nothing else, while every
decision about *which* squares to visit is made in
[`src/robot_moves.py`](src/robot_moves.py) from python-chess -- so captures,
castling, en passant and knight routing are worked out where they can be
unit-tested (`tests/test_robot_moves.py`) rather than on a microcontroller.

What it does physically:

- **Ordinary moves** slide centre to centre. Chess legality already
  guarantees a sliding piece has a clear path.
- **Knights**, and the **castling rook** (which has to get past the king
  that just jumped over it), ride the lattice lines *between* squares
  instead, at reduced magnet power -- and not down the middle of the gap:
  on 30mm squares the midline is only 15mm from the pieces either side, so
  the route shifts toward whichever flank it can prove is empty.
- **Captures** topple the piece in place, then the arm retreats to the
  corner and waits `--topple-delay` seconds (default 5) for you to lift it
  off before sliding its own piece in. En passant topples the pawn beside
  the destination, not on it.
- **Promotion** moves the pawn and then asks you to swap in a queen. The
  arm can't fetch one, and vision can't tell a queen from a pawn anyway --
  the tracked state already records the promotion, so the board just has to
  be made to match.

**Every robot move is confirmed by camera before it counts.** Tracking is
held still while the gantry moves, then a full 64-square read has to match
the move the engine intended -- the same `set_expected_move` mechanism that
polices a hand-played move. A slipped belt, a dropped piece or a hand in the
way flags and **halts the arm** rather than stacking another move on top of
a position that isn't real. Recovery is Edit board / Undo, then **Home /
re-enable** in the UI. There's also a **HALT** button, and pulling the 7.5 V
jack stops everything instantly.

## How detection works

The board is assumed to start at the standard chess position. From there,
the tracker maintains full piece identity purely in software -- it applies
every resolved legal move to its own internal `chess.Board()`, so it
always knows piece *type*, never needing to re-derive it from vision.
`src/tracking_loop.py` runs:

1. `src/roi_diff.py`'s `BoardMotionGate` watches a cheap grayscale diff of
   the board ROI, with no model involved, and fires once a hand has
   entered and left the board (motion, then quiet).
2. `src/square_classifier.py` classifies **all 64 squares** -- not a
   shortlist -- for occupancy and color only (empty / white / black), via
   a small trained model (see `training/NOTES.md`). Several frames are
   sampled and required to agree (consensus) before a square's read is
   trusted; a low-confidence or inconsistent read returns `UNRESOLVED`
   rather than a guess.
3. The observed delta (every square whose confirmed color differs from the
   tracked state) is matched against every legal move's expected delta in
   `src/move_resolver.py`'s `resolve_from_deltas` -- computed by diffing
   `python-chess`'s own board before/after each candidate move, so
   captures/castling/en passant are handled correctly for free. A unique
   match is accepted and applied (this also supplies real algebraic
   notation, e.g. `Nf3`); pawn promotion always resolves to queen, since
   color alone can't reveal the promoted piece type.
4. A few unresolved squares are tolerated -- they carry no information,
   so the tracker keeps its prior belief about them and they're skipped
   when computing the delta. Since a move touches at least two squares,
   an unresolved square that *was* part of the move yields an incomplete
   delta that matches nothing, and flags. Only a systematic failure (more
   than `MAX_UNRESOLVED_SQUARES`) flags on unresolved reads alone.
5. If the delta matches no legal move, or more than one, the board is
   **not** guessed at -- the loop leaves state untouched and flags it,
   naming the squares involved. Recovery is manual: **Undo last move**
   reverts one bad move exactly (restoring castling/en-passant rights),
   and **Edit board** sets each square's true piece and side-to-move
   (inferring which castling rights still make sense from where the
   kings/rooks ended up). Both are always available, not just when
   flagged.

## Known limitations / next steps

- **No vision-based Setup Verification yet.** The tracker currently
  *assumes* the physical board starts at the standard position rather
  than confirming it via the camera. There's also no "New Game" control
  wired up yet, though `TrackingLoop.reset()` does everything needed for
  one.
- **Changing the board or the lighting means retraining.** The classifier
  sees the board surface as background, so a different board is a
  different problem. Recalibrate, collect a new session
  (`--session <name>`, which adds to the dataset rather than replacing
  it), and fine-tune from your existing weights -- see
  [`training/NOTES.md`](training/NOTES.md). Running
  `web_ui.py --harvest` grows the dataset automatically as you play.
- **The classifier needs real training data from your own rig before any
  of this works.** `src/collect_square_crops.py` hasn't shipped a
  pretrained model -- see `training/NOTES.md` for the collection/training
  pipeline. Consensus/delta-matching logic is unit-tested off-Pi with a
  fake model (see `tests/`), but classifier accuracy itself can only be
  judged after training on real photos.
- **The arm's captures need a human.** A toppled piece is left lying on its
  own square and cleared by hand within `--topple-delay`; there's no
  graveyard area, because the gantry's travel is exactly the 8x8 board. The
  delay is fixed rather than vision-gated -- if it elapses and the piece is
  still there, the incoming move disturbs it and the settle flags.
- **30mm squares leave very little routing clearance, and that's the
  arm's real limitation.** The midline between two pieces is 15mm from each,
  against 13-15mm piece bases and a 25mm magnet whose edge comes within
  2.5mm of them. Routing shifts toward an empty flank wherever it can (about
  two moves in three, taking the worst point to 17.4mm), but when both
  flanks are occupied -- a knight leaving the back rank past the opening
  pawn wall, most of all -- 15mm is the geometric maximum and no routing
  fixes it. Test that specific move before playing a real game; see
  [`docs/HARDWARE.md`](docs/HARDWARE.md). If it catches, the remedies are a
  weaker `MAG_EDGE`, narrower piece bases, or a smaller coil.
- Not yet built: puzzle mode, AI coach, past-match analysis, remote play.

## Repo layout

```
config/       generated calibration data (git-ignored)
docs/         DESIGN.md -- the full system document (architecture, geometry,
              protocols, decisions, risks)
              HARDWARE.md -- robot arm circuit, wiring, bring-up
firmware/     chess_gantry/ -- the Arduino Uno sketch (CoreXY + magnet).
              Knows no chess; takes GOTO/MAG/PULSE/TOPPLE over serial
models/       exported NCNN classifier (git-ignored, copied from training
              machine): square_classifier_ncnn_model/
src/          capture, calibration, square classification, board-state
              helpers, event-gated tracking loop, legal-move resolution,
              web UI, and the robot arm (robot_moves.py plans a move's
              gantry path, robot.py drives and verifies it)
tests/        unit tests for move resolution, the classifier's consensus
              wrapper, the tracking loop's delta computation, and the
              robot's path planning + halt-on-mismatch behaviour (no
              camera, gantry or trained model required -- fakes throughout)
training/     dataset collection + training + export instructions (see
              training/NOTES.md) -- most steps run off-Pi
```
