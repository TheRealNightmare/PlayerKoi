# Plyer Koi

Real-time chess piece detection on a Raspberry Pi 5: a fixed overhead
IMX219 camera watches a physical chess board, tracks moves as real
algebraic notation via python-chess, and serves a live board diagram +
camera feed from a small built-in web UI.

The board starts at the standard position (assumed, not detected -- see
"How detection works" below), and the tracker maintains full piece
identity in software from there by applying resolved legal moves -- so
vision never needs to recognize piece *type* at all, only occupancy and
color (empty/white/black) per square. That's a classical-CV read (plain
pixel-color comparisons against a calibration-time baseline, no model
inference), gated by a cheap ML-free motion detector and cross-checked
against every legal chess move. There is no automatic ML fallback: when a
settle can't be resolved with confidence, the web UI flags it and offers a
manual correction. See `src/tracking_loop.py` and `src/occupancy_color.py`
for the full flow.

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

Calibration then walks through two more steps: clear the board completely
(for an empty-square color reference), then set up the standard starting
position (for a white/black piece-color reference). It warns immediately
if white/black contrast looks too low to read reliably -- fix lighting or
piece contrast and re-run if so.

Re-run this any time the camera, board, lighting, or piece set changes.
There's no model to train or deploy -- see
[`training/NOTES.md`](training/NOTES.md) for why.

### 3. Run

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

## How detection works

The board is assumed to start at the standard chess position. From there,
the tracker maintains full piece identity purely in software -- it applies
every resolved legal move to its own internal `chess.Board()`, so it
always knows piece *type*, never needing to re-derive it from vision.
`src/tracking_loop.py` runs:

1. `src/roi_diff.py`'s `BoardMotionGate` watches a cheap grayscale diff of
   the board ROI, with no model involved, and fires once a hand has
   entered and left the board (motion, then quiet).
2. `src/occupancy_color.py` reads **all 64 squares** -- not a shortlist --
   for occupancy and color only (empty / white / black), via plain
   pixel-color comparisons against the baseline captured in `calibrate.py`.
   Several frames are sampled and required to agree (consensus) before a
   square's read is trusted; a boundary-line or inconsistent read returns
   `UNRESOLVED` rather than a guess.
3. The observed delta (every square whose confirmed color differs from the
   tracked state) is matched against every legal move's expected delta in
   `src/move_resolver.py`'s `resolve_from_deltas` -- computed by diffing
   `python-chess`'s own board before/after each candidate move, so
   captures/castling/en passant are handled correctly for free. A unique
   match is accepted and applied (this also supplies real algebraic
   notation, e.g. `Nf3`); pawn promotion always resolves to queen, since
   color alone can't reveal the promoted piece type.
4. If any square's read is unresolved, the delta doesn't match any legal
   move, or it matches more than one, the board is **not** guessed at --
   the loop leaves state untouched and flags it. The web UI's "Fix board"
   control is the only recovery path: it lets you set each square's true
   piece and side-to-move, which the tracker adopts (inferring which
   castling rights still make sense from where the kings/rooks ended up).

## Known limitations / next steps

- **No vision-based Setup Verification yet.** The tracker currently
  *assumes* the physical board starts at the standard position rather
  than confirming it via the camera. There's also no "New Game" control
  wired up yet, though `TrackingLoop.reset()` does everything needed for
  one.
- **Occupancy/color threshold tuning is untested on real hardware.** The
  classical-CV logic and consensus/delta-matching are unit-tested off-Pi
  (see `tests/`), but real lighting, piece contrast, and camera exposure
  drift need validating -- and re-calibrating against -- on the actual rig.
- Not yet built: puzzle mode, AI coach, past-match analysis, remote play.

## Repo layout

```
config/       generated calibration data (git-ignored), including the
              occupancy/color baseline captured by calibrate.py
src/          capture, calibration, occupancy/color reading, board-state
              helpers, event-gated tracking loop, legal-move resolution, web UI
tests/        unit tests for move resolution, occupancy/color reading, and
              the tracking loop's delta computation (no camera required)
training/     orphaned full-frame detector training pipeline, kept for
              reference -- see training/NOTES.md for why it's unused now
```
