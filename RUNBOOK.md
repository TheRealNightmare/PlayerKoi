# Runbook

Copy-pasteable command sequences for the things you actually do. For *why*
any of this works the way it does, see [README.md](README.md) and
[training/NOTES.md](training/NOTES.md).

## Machines

| | Path | venv |
|---|---|---|
| **Pi** (`nightmare@192.168.0.106`) | `~/C/PlayerKoi` | `.venv` |
| **Training PC** | `~/MicroChess` | `.venv-train` |

```bash
# Pi
ssh nightmare@192.168.0.106
cd ~/C/PlayerKoi && source .venv/bin/activate

# Training PC
cd ~/MicroChess && source .venv-train/bin/activate
```

---

## A. Retrain from harvested play data

You've been running `web_ui.py --harvest`. Harvesting only *collects* —
nothing improves until you merge and retrain.

**1. Pull the crops** (training PC)

```bash
rsync -av nightmare@192.168.0.106:~/C/PlayerKoi/training/datasets/harvested/ \
          training/datasets/harvested/
```

**2. Inspect before trusting it**

```bash
find training/datasets/harvested -name "*.jpg" \
  | sed 's#.*/\([^/]*\)/[^/]*$#\1#' | sort | uniq -c

ls training/datasets/harvested/train/black | head
```

These labels came from the tracker, not from you. If it was misreading a
square, merging teaches it its own mistake.

**3. Back up, then merge**

```bash
cp -r training/datasets/squares training/datasets/squares.bak
rsync -av training/datasets/harvested/ training/datasets/squares/
```

**4. Fine-tune** — always from your current best weights, not scratch

```bash
python training/train_classifier.py --data training/datasets/squares \
    --model runs/classify/train-2/weights/best.pt
```

Note the path it prints at the end — ultralytics auto-increments
(`train-2` → `train-3`). Use that below.

**5. Export and deploy** — see [section C](#c-export-and-deploy).

**6. If accuracy dropped**, the harvested labels were noisy:

```bash
rm -rf training/datasets/squares
mv training/datasets/squares.bak training/datasets/squares
# then redeploy the previous model
```

Once a batch is merged and working, clear the Pi's copy so the next pull
only brings new material:

```bash
ssh nightmare@192.168.0.106 'rm -rf ~/C/PlayerKoi/training/datasets/harvested'
```

---

## B. New board, or a big lighting change

The classifier sees the board surface as background, so a different board
is a different problem. Same for a major lighting change.

**1. On the Pi** — set the board up physically first

```bash
python3 src/calibrate.py                     # corners moved
python3 src/collect_square_crops.py --session <name> --rounds 20
```

`--session` keeps this run from overwriting earlier ones, so it **adds**
to the dataset. It prints existing counts on startup — confirm it's
growing.

Collection doesn't use the model at all, only calibration, so it's fine
that the current model is useless on the new board.

**2. On the training PC**

```bash
rsync -av nightmare@192.168.0.106:~/C/PlayerKoi/training/datasets/squares/ \
          training/datasets/squares/

python training/train_classifier.py --data training/datasets/squares \
    --model runs/classify/train-2/weights/best.pt
```

Keeping both boards' crops in one dataset gives one model that handles
both.

**3.** Export and deploy — [section C](#c-export-and-deploy).

> More rounds mainly buys **per-square coverage** (~10 of 64 squares get a
> piece per round). But 30 rounds in one sitting is one lighting
> condition — 15 now and 15 this evening is worth more.

---

## C. Export and deploy

Substitute the real run directory (`train-3`, etc.) that training printed.

```bash
python training/export_ncnn.py --weights runs/classify/train-3/weights/best.pt --imgsz 64

mv runs/classify/train-3/weights/best_ncnn_model \
   runs/classify/train-3/weights/square_classifier_ncnn_model

# delete the old model FIRST — see gotchas
ssh nightmare@192.168.0.106 'rm -rf ~/C/PlayerKoi/models/square_classifier_ncnn_model'

python training/deploy.py nightmare@192.168.0.106 \
    --model-dir runs/classify/train-3/weights/square_classifier_ncnn_model \
    --dest ~/C/PlayerKoi/models/
```

Then on the Pi:

```bash
python3 src/debug_classifier.py      # verify before trusting it
python3 src/web_ui.py --harvest
```

Open `http://192.168.0.106:8000/`.

---

## D. Playing against the engine

```bash
sudo apt install stockfish           # one-time, on the Pi
python3 src/web_ui.py --harvest
```

Toggle the engine on in the box at the bottom, set the skill slider low to
start. Play your White move physically; the box shows Black's reply
(`d7 → d5`) with both squares highlighted, spelling out the extra action
for castling / en passant / promotion.

While a move is pending it's the **only** move the tracker accepts. Undo
and Edit board override that if you want to deviate.

---

## E. Playing with the robot arm

Full circuit and first-time bring-up: **[docs/HARDWARE.md](docs/HARDWARE.md)**.
This section is the day-to-day sequence once it's built and calibrated.

**1. Power up in this order** — USB first, barrel jack second.

```bash
ls /dev/ttyACM*                      # confirm the Uno enumerated
```

**2. First time on a rebuilt/re-flashed rig,** verify the geometry and the
clearance before anything touches a real game — `GOTO 7 0` must travel exactly
210 mm, and a knight must leave the back rank without catching the pawns
either side. Both procedures are in
[docs/HARDWARE.md](docs/HARDWARE.md#bring-up-and-calibration), steps 4 and 7.

**3. Dry run if anything changed** (firmware, wiring, the code):

```bash
python3 src/web_ui.py --robot mock   # logs every gantry command, moves nothing
```

**4. Then for real.** It homes on startup — keep hands clear.

```bash
python3 src/web_ui.py --robot /dev/ttyACM0 --harvest
```

Toggle the engine on as usual. From then on it plays its own moves: you move
White, the arm answers.

**When it captures**, it topples the piece, retreats to the corner, and waits
5 seconds (`--topple-delay`) for you to lift it off. Take it off promptly —
if it's still lying there when the arm comes back, the incoming piece shoves
it and the settle flags.

**When it promotes**, the UI asks you to swap a queen in. Do it; the software
already thinks it's a queen.

**If it halts** (red box), the camera didn't agree with what the arm did.
Fix the physical board with **Edit board** or **Undo last move**, then press
**Home / re-enable**. It will not move again until you do.

**Bench console** for poking the gantry directly, without any chess:

```bash
python3 src/robot.py --port /dev/ttyACM0 --console
gantry> HOME
gantry> GOTO 3.5 4
gantry> MAG 170
gantry> OFF
```

---

## F. Gotchas that have actually bitten

**Run directory is `train-2`, not `train2`.** Ultralytics auto-increments
with a hyphen. Follow the path the training script prints — don't guess.

**`scp -r` nests into an existing directory.** If
`models/square_classifier_ncnn_model` already exists on the Pi, deploying
creates `square_classifier_ncnn_model/square_classifier_ncnn_model` and
the model fails to load. Always `rm -rf` the old one first.

**Collection filenames need `--session`.** Without it they'd collide
across runs. It defaults to a timestamp, so it's safe either way, but a
label (`--session greenboard`) makes batches traceable and removable.

**`--harvest` never fixes anything by itself.** It only writes crops. You
have to merge and retrain.

---

## G. Tuning knobs

| Problem | Knob |
|---|---|
| Moves not detected at all | `web_ui.py --motion-thresh 2.0` (default 3.0). Diagnose with `debug_classifier.py --watch` |
| Too many "low-confidence" flags | `web_ui.py --min-conf 0.4` (default 0.5) |
| Wrong moves accepted | Raise `--min-conf`, or collect more data for the squares that misread |
| Engine too strong | Skill slider in the UI (0–20; 0 is genuinely beatable) |
| Board diagram wrong | **Undo last move** for one bad move; **Edit board** for a full resync |
| Arm drops pieces mid-drag | Raise `MAG_HOLD`/`MAG_EDGE` in `src/robot_moves.py` (the firmware clamps at `MAG_MAX_PWM`) |
| **Neighbouring pieces dragged along as the arm passes** | Lower `MAG_EDGE`. On 30 mm squares the magnet's edge comes within 2.5 mm of a flanking piece's centre — see Clearances in [docs/HARDWARE.md](docs/HARDWARE.md) |
| **Knight catches pieces leaving the back rank** | The opening pawn wall is the one case routing can't improve on (15 mm each side). Narrower bases or a smaller coil; no software fix |
| Magnet coil getting hot | Lower `MAG_MAX_PWM` in the sketch, or feed the DRV8872 from a 5 V buck |
| Pieces land off-centre | Tune `PULSE_REVERSE_MS`/`PULSE_HOLD_MS` in the sketch |
| Piece knocked off the board when toppled | Lower `TOPPLE_MS` in the sketch |
| Arm drifts a bit further off every move | `STEPS_PER_SQUARE` should be exactly 600 — verify `GOTO 7 0` travels 210 mm. If it does, the motors are stalling: lower `MAX_SPEED` |
| Arm too slow | `MAX_SPEED` 1500 → 2500 steps/s (75 → 125 mm/s), once the belts are tensioned |
| Motors buzz but don't turn | `MAX_SPEED` too high to start from rest (no acceleration in `MultiStepper`), or Vref too low |
| An axis homes away from its switch | Swap one coil pair on that motor — no code change |
| Not enough time to clear a captured piece | `web_ui.py --topple-delay 10` |
| Arm halts constantly | The camera is disagreeing with it — check `debug_classifier.py` before blaming the gantry |

Diagnostics:

```bash
python3 src/debug_classifier.py            # per-square class + confidence
python3 src/debug_classifier.py --watch    # live motion-gate scores
```
