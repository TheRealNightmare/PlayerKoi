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
- **The V2 board**: a 400 × 400mm playing area on 50mm squares, printed as a
  sticker on a 570 × 570mm laser-cut panel, with the CoreXY gantry reaching
  500 × 500mm -- a full square past every edge, which is where captured
  pieces are parked. V1 was 230 × 230mm on 28.75mm squares; git history has
  those numbers. Geometry, bill of materials and the panel drawings are in
  [`docs/HARDWARE.md`](docs/HARDWARE.md).

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

If you use more than one setup -- different room, lighting, camera height,
board or piece set -- give each one a tag: `python3 src/calibrate.py --env 2`
writes `config/calibration-env2.json` and leaves the others alone.

### 3. Get a classifier

Training doesn't happen on the Pi, and nothing is trained from scratch:
the pipeline fine-tunes ImageNet-pretrained `yolov8n-cls` on crops from
your own rig, which is why ~12 collection rounds per setup is enough. It
only ever learns three classes -- empty / white / black -- never piece
type. [`WORKFLOW.md`](WORKFLOW.md) walks the whole cycle end to end;
[`training/NOTES.md`](training/NOTES.md) explains why. The pipeline:
`src/collect_square_crops.py --env <tag>` (on the Pi, collects real
training photos from your own board -- no public dataset, no legal chess
position needed) -> `training/train_classifier.py` (on a GPU machine) ->
`training/eval_by_env.py` (per-environment accuracy, tells you which
setup needs more data) -> `training/export_ncnn.py` ->
`training/deploy.py` back to the Pi's `models/square_classifier_ncnn_model`.

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
python3 src/web_ui.py --robot auto            # the real gantry, port detected
python3 src/web_ui.py --robot mock            # dry run: logs commands, moves nothing
```

**Park the carriage on the a8 corner before homing.** This build has no limit
switches, so `HOME` drives to where it *assumes* the origin is rather than
finding it. If the carriage starts anywhere else, every move afterwards is
silently wrong and nothing will tell you.

The firmware calls that corner `h1` — it assumes the board is seated the other
way round. On this rig it isn't, so `rig.ORIGIN_SQUARE = "a8"` rotates every
square 180 degrees on the way to the Arduino (`--board-origin` overrides it).
Bench commands are deliberately *not* rotated, so `GOTO a1` still means the
firmware's a1. Wiring, flashing and the full bring-up sequence are
in **[docs/CONNECTION.md](docs/CONNECTION.md)**.

The arm homes on startup, then plays automatically whenever the engine
decides on a move. It is off unless `--robot` is given, so nothing here
changes the hand-played setup above.

### Two firmwares

There are two sketches, and only one of them runs the machine:

| | `firmware/chessbot_v1/` | `firmware/chess_gantry/` |
|---|---|---|
| Status | **flashed, proven** | unbuilt, dormant |
| Protocol | `MOVE e2e4`, `KNIGHT b1c3` | `GOTO 3.5 4`, `MAG`, `TOPPLE` |
| Plans paths | on the Uno | on the Pi (`robot_moves.py`) |
| Homing | manual park, dead reckoning | limit switches |
| Flag | `--robot-protocol legacy` (default) | `--robot-protocol native` |

The live rig takes *whole moves* and does its own routing, with the magnet
duties and edge handling measured into the firmware. So on this machine the
Pi's job is chess rules only — [`src/robot_moves_legacy.py`](src/robot_moves_legacy.py)
decides *which* squares and what a human must clear, and nothing more.

`chess_gantry` is kept for a future rebuild with limit switches, where path
planning moves back onto the Pi and [`src/robot_moves.py`](src/robot_moves.py)'s
lattice routing comes into play. Both planners are unit-tested and either can
be dropped into the same execution layer.

Measured constants live in one place, [`src/rig.py`](src/rig.py), mirrored from
the firmware that owns them.

What it does physically:

- **Ordinary moves** slide centre to centre. Chess legality already
  guarantees a sliding piece has a clear path.
- **Knights**, and the **castling rook** (which has to get past the king
  that just jumped over it), ride the lattice lines *between* squares
  instead, at reduced magnet power -- and not down the middle of the gap:
  the route shifts toward whichever flank it can prove is empty. On 50mm
  squares the midline is already 25mm from the pieces either side, so this is
  margin on top of margin; on the old 28.75mm board it was the difference
  between clearing a piece and dragging it.
- **Captures** are driven off the board by the arm itself. The victim is
  lifted first -- dragging onto an occupied square would just shove two
  pieces around -- and parked on the nearest free slot of a 32-slot ring in
  the 50mm margin around the board. No human, no waiting. En passant lifts
  the pawn *beside* the destination, not on it.
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
re-enable** in the UI.

There's also a **HALT** button, with one caveat worth knowing before you need
it: on the legacy firmware a halt only takes effect once the command in flight
finishes. That firmware has no soft e-stop and its motion is blocking, so the
arm will refuse the *next* move but cannot abandon the current one. To stop a
move in progress, pull the 7.5 V jack — that is instant.

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
- **The camera has not been re-mounted for the bigger board.** The IMX219 is
  positioned for the old 230mm board; at 400mm it has to be raised and the
  rig recalibrated, and the classifier's crops rescaled with it. The vision
  *code* needs no change -- it works in normalised 0..8 board space off the
  homography and never sees millimetres -- but nothing here plays a game
  until that is done.
- **There are no limit switches.** `HOME` drives to the assumed origin instead
  of seeking it, so position is dead reckoning from a manual park on h1. A
  missed step or a power-on with the carriage elsewhere corrupts every
  coordinate afterwards, and nothing detects it -- the camera confirming each
  move is what eventually catches it, by flagging and halting.
- **Routing clearance used to be the arm's real limitation, and is not any
  more.** On the old 28.75mm board the midline between two pieces was 14.4mm
  from each -- *inside* the 13-15mm piece bases -- and the 25mm magnet's edge
  came within about 2mm of a flanking piece's centre. At 50mm the midline is
  25mm and the pole face stops 12.5mm short. What replaces it as the thing to
  watch is the opposite problem: a piece can now sit much further from the
  coil, and the 50N magnet's grab at that offset is **untested**. See
  [`docs/HARDWARE.md`](docs/HARDWARE.md).
- **Ø8mm rod over a ~500mm span will sag** under the carriage in a way it did
  not over 250mm. Accepted for cost; if the carriage binds or the grip varies
  between the middle of the board and the edges, the fix is Ø10/Ø12mm rod or
  supported rail -- which changes the bearing blocks, so it is a re-cut.
- Not yet built: puzzle mode, AI coach, past-match analysis, remote play.

## Repo layout

```
config/       generated calibration data, plus rig.json -- the settings
              that must survive a restart (magnet polarity, release fade,
              and which graveyard slots hold a captured piece)
design/       generated board artwork: the printed sticker, and the panel
              as DXF / AI / SVG / STEP. Rebuild with tools/, don't hand-edit
docs/         DESIGN.md -- the full system document (architecture, geometry,
              protocols, decisions, risks)
              HARDWARE.md -- robot arm circuit, wiring, bring-up
              CONNECTION.md -- what to flash, how to wire it, bring-up
              order, the full command reference, troubleshooting
firmware/     chessbot_v1/ -- THE SKETCH TO FLASH. Takes whole moves
              (MOVE e2e4, KNIGHT b1c3, BURY e4 -350 -50 w) and routes them
              itself
              chess_gantry/ -- dormant; the unbuilt limit-switch rebuild,
              which takes GOTO/MAG/PULSE/TOPPLE instead. Do not flash
models/       exported NCNN classifier (git-ignored, copied from training
              machine): square_classifier_ncnn_model/
src/          capture, calibration, square classification, board-state
              helpers, event-gated tracking loop, legal-move resolution,
              web UI, and the robot arm (rig.py holds the machine's measured
              constants; graveyard.py picks which slot a captured piece goes
              to; robot_moves_legacy.py turns a move into firmware commands,
              robot_moves.py plans a waypoint path for the dormant native
              rig, robot.py drives and verifies either)
tests/        unit tests for move resolution, the classifier's consensus
              wrapper, the tracking loop's delta computation, the robot's
              path planning + halt-on-mismatch behaviour, and the board
              geometry (no camera, gantry or trained model required --
              fakes throughout)
tools/        generators for everything in design/, plus read_board_ai.py,
              which reads the hole pattern out of the original Illustrator
              file. No dependencies beyond the standard library
training/     dataset collection + training + export instructions (see
              training/NOTES.md) -- most steps run off-Pi
```
