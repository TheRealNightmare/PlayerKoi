# MicroChess

Real-time chess piece detection on a Raspberry Pi 5: a fixed overhead
IMX219 camera watches a physical chess board, tracks moves as real
algebraic notation via python-chess, and serves a live board diagram +
camera feed from a small built-in web UI.

The board starts at the standard position (verified, not detected -- see
"How detection works" below), so per-move recognition only has to resolve
*changes*: a cheap ML-free motion gate watches for a settled board, a
per-square pixel diff narrows down which squares changed, and a small
classifier is run on just those crops. A full-frame YOLO detector is kept
only as a fallback for when that's ambiguous. See `src/tracking_loop.py`
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

Re-run this any time the camera or board physically moves.

### 3. Get two models

Training doesn't happen on the Pi. See [`training/NOTES.md`](training/NOTES.md)
for the full pipeline. You need both:

- **Fallback detector** (`models/best_ncnn_model`): a YOLOv8n full-frame
  detector, used only when the fast path below can't resolve a move.
  `training/prepare_chessred.py` -> `training/train.py` -> `training/export_ncnn.py`.
- **Per-square classifier** (`models/square_classifier_ncnn_model`): a
  tiny classifier run on individual square crops for routine per-move
  recognition. `training/prepare_square_crops.py` -> `training/train_classifier.py`
  -> `training/export_ncnn.py`.

Copy both exported folders into `models/` on the Pi with `training/deploy.py`.

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

## How detection works

The board is assumed to start at the standard chess position (a "Setup
Verification" vision check against this assumption isn't built yet -- see
below). From there, `src/tracking_loop.py` runs:

1. `src/roi_diff.py`'s `BoardMotionGate` watches a cheap grayscale diff of
   the board ROI, with no model involved, and fires once a hand has
   entered and left the board (motion, then quiet).
2. `diff_changed_squares` diffs the newly-settled frame against the last
   known-stable frame, per square, to shortlist which squares plausibly
   changed (usually 2-4; more than 6 is treated as unreliable).
3. `src/square_classifier.py` classifies only those shortlisted crops
   (empty + 6 piece types x 2 colors).
4. `src/move_resolver.py` checks whether the resulting position matches
   exactly one legal move from `python-chess`'s legal-move list. If so,
   that move is accepted (this is also what supplies real algebraic
   notation, not just a physical before/after description).
5. If the diff was ambiguous, too large, hit a low-confidence crop, or
   didn't resolve to a legal move, `src/detect.py`'s full-frame YOLO
   detector re-scans the whole board as a fallback. If even that doesn't
   resolve to a legal move, the board state is force-synced from the raw
   scan and flagged in the web UI (castling/en-passant rights reset to
   defaults in that case -- a known, accepted limitation of syncing from
   a bare snapshot).

## Known limitations / next steps

- **No vision-based Setup Verification yet.** The tracker currently
  *assumes* the physical board starts at the standard position rather
  than confirming it via the camera; wire a startup scan against
  `move_resolver.standard_starting_matrix()` before trusting a game.
  There's also no "New Game" control wired up yet, though
  `TrackingLoop.reset()` does everything needed for one.
- **Classifier accuracy depends on how close your board/lighting is to
  the ChessReD training photos** (handheld smartphone photos, not a fixed
  overhead rig) -- expect a real domain gap on first deploy. Tune
  `--conf`/classifier `min_conf` against your real setup; the design
  supports periodically fine-tuning on real, auto-labeled crops recorded
  during play (see `training/NOTES.md`).
- **CPU-only inference speed on the fast path is untested on real
  hardware** -- the per-square classifier and diff/motion-gate logic are
  all unit-tested off-Pi, but ARM-specific timing needs validating on the
  actual Pi 5.
- Not yet built: puzzle mode, AI coach, past-match analysis, remote play.

## Repo layout

```
config/       generated calibration data (git-ignored)
models/       exported NCNN models (git-ignored, copied from training machine):
              best_ncnn_model/ (fallback detector), square_classifier_ncnn_model/ (per-square classifier)
src/          capture, calibration, detection, board-state tracking, event-gated
              tracking loop, legal-move resolution, web UI
training/     dataset + training + export instructions (run off-Pi)
```
