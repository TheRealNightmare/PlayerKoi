# MicroChess

Real-time chess piece detection on a Raspberry Pi 5: a fixed overhead
IMX219 camera watches a physical chess board and maintains a live 8x8
matrix of piece occupancy, printed/logged whenever it changes.

Scope of this version: raw board-state detection only (piece type + color
per square). No move-legality checking, FEN, or game history yet, and no
web UI -- terminal/log output only.

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

### 3. Get a detection model

Training doesn't happen on the Pi. See [`training/NOTES.md`](training/NOTES.md)
for pulling a public dataset, training a YOLOv8n model, and exporting it
to NCNN format (the fastest CPU inference backend on ARM). Copy the
resulting `best_ncnn_model/` folder into `models/` on the Pi.

### 4. Run

```bash
python3 src/main.py
```

Prints the 8x8 board matrix to the terminal whenever it changes (default:
checks every 0.75s, tune with `--interval`). Add `--log board.log` to
also append changes to a file. Uppercase letters are white pieces,
lowercase are black, `.` is empty:

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

## Known limitations / next steps

- **Accuracy depends on how close your board/lighting is to the public
  training dataset.** Tune `--conf` in `main.py` against your real setup;
  if that's not enough, fine-tune the model on a modest set of your own
  captured images (not built yet, but `training/NOTES.md` describes the
  point where this would slot in).
- **CPU-only inference speed is untested until you actually run it** --
  if frame rate is too low even for the ~1s turn-based cadence this is
  built for, try a smaller `imgsz` in `src/detect.py`, or a longer
  `--interval` in `main.py` (fine, since the use case is turn-based, not
  continuous video).
- Not yet built: FEN/`python-chess` integration and move legality, web
  dashboard, non-overhead/angled camera support.

## Repo layout

```
config/       generated calibration data (git-ignored)
models/       exported NCNN detection models (git-ignored, copied from training machine)
src/          capture, calibration, detection, board-state tracking, main loop
training/     dataset + training + export instructions (run off-Pi)
```
