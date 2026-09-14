# Runbook

Copy-pasteable command sequences for everything you actually do. Every command
here is executable as written — no placeholders to substitute.

- Whole cycle explained start to finish → [WORKFLOW.md](WORKFLOW.md)
- *Why* it works this way → [README.md](README.md) and [training/NOTES.md](training/NOTES.md)

## What do you want to do?

| | Section |
|---|---|
| Move data or models between the Pi and the PC | [A](#a-moving-files-between-the-machines) |
| Train a model — fresh, or improve the existing one | [B](#b-training) |
| Collect data for a new room / board / lighting | [C](#c-collect-data-for-an-environment) |
| Use crops collected automatically while playing | [D](#d-harvest-from-play) |
| Put a trained model on the Pi | [E](#e-export-and-deploy) |
| Play against the engine | [F](#f-playing-against-the-engine) |
| Play against the robot arm | [G](#g-playing-with-the-robot-arm) |
| Watch the machine play itself | [G2](#g2-ai-vs-ai-no-camera) |
| Something broke | [H](#h-gotchas-that-have-actually-bitten) · [I](#i-tuning-knobs) |

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

The camera is on the Pi, so **all data is collected there**. The GPU is on the
PC, so **all training happens there**. Section A is how things cross.

## How the scripts find your model

`export_ncnn.py`, `eval_by_env.py` and `deploy.py` all resolve weights the
same way, so you normally pass **no path at all**:

```bash
ls runs/classify/*/weights/best.pt     # what you actually have
```

- **No flag** → the newest run under `runs/classify/`, printed for you to
  confirm. Add `--yes` to skip the prompt (needed in scripts — without a
  terminal it refuses rather than hanging).
- **`--run NAME`** → a specific run, e.g. `--run train-2`. Use this to go back
  to an older model.
- **A full path** (`--weights ...` / `--model-dir ...`) → always wins.

Ultralytics auto-increments run names (`train` → `train-2` → `train-3`), which
is why nothing below hardcodes one. Pass a path that doesn't exist and the
error lists the runs you really have.

> ⚠️ `runs/`, `training/datasets/` and `*.pt` are **git-ignored**. A trained
> `best.pt` exists in exactly one place — this machine's disk. Clearing
> `runs/` destroys it permanently; NCNN on the Pi cannot be converted back.
> The dataset is what's truly irreplaceable, so keep it on both boxes.

---

## A. Moving files between the machines

Everything moves by `rsync` over ssh. **Trailing slashes on both sides are
required** — they mean "the contents of", and without them you get a nested
directory. `rsync` **merges** into the destination; it never wipes what's
already there.

| What | Direction | When |
|---|---|---|
| `training/datasets/squares/` | Pi → PC | after collecting ([C](#c-collect-data-for-an-environment)) |
| `training/datasets/harvested/` | Pi → PC | after playing with `--harvest` ([D](#d-harvest-from-play)) |
| NCNN model directory | PC → Pi | after training ([E](#e-export-and-deploy)) |
| `config/calibration-env*.json` | **stays on the Pi** | never moves — it describes that Pi's camera |

### Pull the dataset (Pi → PC)

```bash
# on the training PC
rsync -av nightmare@192.168.0.106:~/C/PlayerKoi/training/datasets/squares/ \
          training/datasets/squares/
```

Verify it arrived before training on it:

```bash
find training/datasets/squares -name "*.jpg" \
  | sed 's#.*/\([^/]*\)/[^/]*$#\1#' | sort | uniq -c

tail -1 training/datasets/squares/manifest.jsonl
```

### Pull harvested crops (Pi → PC)

```bash
rsync -av nightmare@192.168.0.106:~/C/PlayerKoi/training/datasets/harvested/ \
          training/datasets/harvested/
```

Keep this **separate** from `squares/` until you've inspected it — see
[section D](#d-harvest-from-play).

### Push the model (PC → Pi)

Covered in full by [section E](#e-export-and-deploy) — `deploy.py` wraps the
`scp`, and the old model must be deleted first.

### Move everything at once

After a collecting session, before a training session:

```bash
# PC: pull both datasets
rsync -av nightmare@192.168.0.106:~/C/PlayerKoi/training/datasets/squares/ \
          training/datasets/squares/
rsync -av nightmare@192.168.0.106:~/C/PlayerKoi/training/datasets/harvested/ \
          training/datasets/harvested/
```

Going the other way — a fresh Pi, or one whose dataset you cleared — push the
dataset back so both machines hold a copy:

```bash
rsync -av training/datasets/squares/ \
          nightmare@192.168.0.106:~/C/PlayerKoi/training/datasets/squares/
```

Add `--dry-run` to any of these to see what would transfer without doing it.

---

## B. Training

**Nothing here ever trains from scratch.** Both modes below start from a
pretrained model — the only question is *which* pretrained model:

| Mode | Starts from | Use when |
|---|---|---|
| **Fresh** | `yolov8n-cls.pt` (ImageNet) | no previous run on this machine; or the current model went bad and you want a clean slate |
| **Continue** | your own `best.pt` | you added data and want to keep what the model already learned |

### Fresh — start from the pretrained YOLOv8n-cls

```bash
python training/train_classifier.py --data training/datasets/squares
```

That's the whole command. `--model` defaults to `yolov8n-cls.pt`, which
ultralytics downloads automatically if it isn't in the repo root.

Use this when `ls runs/classify/*/weights/best.pt` comes back empty, or when
you want to rule out a bad previous model. It costs ~30 s on the GPU for a
12-round dataset, so a clean rerun is cheap.

### Continue — fine-tune your current best

```bash
python training/train_classifier.py --data training/datasets/squares \
    --model runs/classify/train/weights/best.pt
```

This is the normal path after adding an environment or merging harvested
crops: it keeps everything the model learned about your *other* environments
instead of relearning them. Point `--model` at a path that exists — unlike the
other scripts, this flag is not auto-resolved, and a wrong path is an error
rather than a fallback.

### Resume an interrupted run

```bash
python training/train_classifier.py --resume --name train
```

Picks up from `runs/classify/<name>/weights/last.pt`. Only works once the run
has saved at least one epoch; data/epochs/imgsz come from the checkpoint, so
don't re-pass them.

### Flags worth knowing

| Flag | Default | Why you'd change it |
|---|---|---|
| `--epochs` | 50 | More rarely helps — collect data instead |
| `--imgsz` | 64 | **Keep in sync with `src/square_classifier.py`** |
| `--batch` | -1 (auto) | Auto-sizes to VRAM |
| `--device` | 0 | `cpu` to force CPU |
| `--name` | train | Label a run: `--name env3-test` |
| `--workers` | 8 | Lower to 2 or 0 on WSL2 |
| `--allow-cpu` | off | Otherwise it aborts rather than crawling on CPU |

### Reading the result

```
                   all      0.984          1     <- val top-1, per epoch
Test top-1: 0.9648   top-5: 1.0000              <- the honest number
Done. Best weights: .../runs/classify/train/weights/best.pt
```

**Trust the test number.** `best.pt` is chosen by whichever epoch scored best
on `val/`, so val has been peeked at. Then break it down per environment:

```bash
python training/eval_by_env.py --data training/datasets/squares
```

A weak row means collect more rounds *in that environment*. An old row that
dropped means the model is trading environments against each other — that one
needs more rounds too.

Then [export and deploy](#e-export-and-deploy).

---

## C. Collect data for an environment

An **environment** is one combination of room, lighting, camera height, board
and piece set. The classifier sees the board surface as background, so a
different board is a different problem — and so is a big lighting or
camera-position change. Run this section once per environment, with a new
`--env` tag each time.

**0. Probe first — does it even need training?**

Don't spend 25 rounds on a hunch. Calibrate, then point the *current* model at
the new setup and look at the grid:

```bash
# on the Pi
python3 src/calibrate.py --env 3
python3 src/debug_classifier.py --calibration config/calibration-env3.json
```

Any square showing the **wrong colour**, or more than ~15 of 64 below
min-conf, means train for it. If it looks clean, get a real number from 4
throwaway rounds:

```bash
# Pi
python3 src/collect_square_crops.py --env 3 --rounds 4 \
    --out training/datasets/probe-env3 --notes "PROBE"

# training PC
rsync -av nightmare@192.168.0.106:~/C/PlayerKoi/training/datasets/probe-env3/ \
          training/datasets/probe-env3/
python training/eval_by_env.py --data training/datasets/probe-env3 --split train
```

≥99% → no training needed, just play. 97–99% → 10–12 rounds. <97% → the full
25–30. Full procedure and how to read the confusions:
[WORKFLOW.md § 9](WORKFLOW.md#9-testing-a-new-environment-before-training).

If you do train, fold the probe in rather than recollecting it — it's already
labelled and already tagged `env3-`:

```bash
rsync -av training/datasets/probe-env3/ training/datasets/squares/
rm -rf training/datasets/probe-env3
```

**1. Collect** (on the Pi) — set the board up physically first

```bash
python3 src/collect_square_crops.py --env 3 --rounds 20 \
    --notes "spare room, ceiling light, glass set"
```

The env prefix keeps this run from overwriting earlier ones, so it **adds** to
the dataset. It prints existing counts on startup — confirm it's growing — and
appends a line to `training/datasets/squares/manifest.jsonl`.

Rounds are split whole into `train/` / `val/` / `test/`; never re-split these
by file, or burst frames leak across splits and the accuracy becomes a lie.

Collection doesn't use the model at all, only calibration, so it's fine that
the current model is useless in the new environment.

How much: **12 rounds** is the minimum (~97–98%, frequent corrections),
**25–30** is the target (~99%+). Split across sittings — 15 in daylight plus
15 under lamplight beats 30 in one go. The maths:
[WORKFLOW.md § 3c](WORKFLOW.md#3c-how-much-data-do-i-need).

**2. Move it to the PC** — [section A](#a-moving-files-between-the-machines).

**3. Train** — [section B](#b-training), *continue* mode if you already have a
model, *fresh* if you don't.

**4. Deploy** — [section E](#e-export-and-deploy).

> More rounds mainly buys **per-square coverage** (~10 of 64 squares get a
> piece per round). But 30 rounds in one sitting is one lighting condition —
> 15 now and 15 this evening is worth more.

---

## D. Harvest from play

`web_ui.py --harvest` saves labelled crops for all 64 squares every time a
move resolves — the tracker knows the true board state at that moment, so the
labels come free. It only writes crops; **nothing improves until you merge and
retrain.**

**1. Pull the crops** — [section A](#a-moving-files-between-the-machines)

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
square, merging teaches it its own mistake. Harvest is also excluded on undo,
deliberately — an undo reports the reverted position while the physical board
still shows the post-move one.

**3. Back up, then merge**

```bash
cp -r training/datasets/squares training/datasets/squares.bak
rsync -av training/datasets/harvested/ training/datasets/squares/
```

**4. Fine-tune** — *continue* mode, [section B](#b-training)

```bash
python training/train_classifier.py --data training/datasets/squares \
    --model runs/classify/train/weights/best.pt
```

**5. Check it didn't get worse**

```bash
python training/eval_by_env.py --data training/datasets/squares
```

**6. Export and deploy** — [section E](#e-export-and-deploy).

**7. If accuracy dropped**, the harvested labels were noisy:

```bash
rm -rf training/datasets/squares
mv training/datasets/squares.bak training/datasets/squares
# then redeploy the previous model with --run <older run>
```

Once a batch is merged and working, clear the Pi's copy so the next pull only
brings new material:

```bash
ssh nightmare@192.168.0.106 'rm -rf ~/C/PlayerKoi/training/datasets/harvested'
```

---

## E. Export and deploy

```bash
python training/export_ncnn.py --imgsz 64

mv runs/classify/train/weights/best_ncnn_model \
   runs/classify/train/weights/square_classifier_ncnn_model

# delete the old model FIRST — see gotchas
ssh nightmare@192.168.0.106 'rm -rf ~/C/PlayerKoi/models/square_classifier_ncnn_model'

python training/deploy.py nightmare@192.168.0.106 --dest ~/C/PlayerKoi/models/
```

Both scripts default to the newest run; add `--run train-2` to ship an older
one (that's also how you roll back). The `mv` gives the directory the name
`src/main.py`, `web_ui.py` and `debug_classifier.py` all expect.

Then on the Pi:

```bash
python3 src/debug_classifier.py      # verify before trusting it
python3 src/web_ui.py --harvest
```

Open `http://192.168.0.106:8000/`.

---

## F. Playing against the engine

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

## G. Playing with the robot arm

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

## G2. AI vs AI (no camera)

Stockfish plays both sides and the arm places every move. **Nothing in this
mode uses the camera** — no calibration, no classifier, no model. The position
is tracked purely in software (`src/headless_loop.py`), which is sound only
because the arm is the sole thing touching the board.

That is the trade: there is no verification. A slipped belt, a dragged
neighbour, a piece knocked over — none of it is detected, and every move after
it is played into a position that no longer exists.

**1. Set the board up completely.** All 32 pieces, standard position, White at
the a1 end. This is assumed, never checked.

**2. Park the carriage on h1** by hand. No limit switches — `HOME` drives to
the assumed origin rather than finding it.

**3. Dry run first** if anything changed:

```bash
python3 src/web_ui.py --ai-vs-ai --robot mock
```

**4. Then for real.** It homes on startup — keep hands clear.

```bash
python3 src/web_ui.py --ai-vs-ai --robot /dev/ttyACM0 \
    --engine-skill 3 --move-delay 2
```

Open the UI and press **play**. `--move-delay` is the pause between moves;
keep it generous the first few games so you have time to reach **HALT**.

**Captures still stop and wait for you** — same blocking prompt as section G,
with no time limit. Press **Done — piece removed**.

**Promotions are advisory**: swap a queen in when asked. The software already
records it as a queen either way, so the arm will keep dragging the pawn
around as one if you don't.

**Watch the first few moves.** Once the physical board and the software
diverge, nothing will tell you — that's what the camera was for.

---

## H. Gotchas that have actually bitten

**Run directory is `train-2`, not `train2`.** Ultralytics auto-increments
with a hyphen. You rarely need to type it — the scripts default to the newest
run — but `--run` and `train_classifier.py --model` both want the real name.

**`scp -r` nests into an existing directory.** If
`models/square_classifier_ncnn_model` already exists on the Pi, deploying
creates `square_classifier_ncnn_model/square_classifier_ncnn_model` and
the model fails to load. Always `rm -rf` the old one first.

**Collect with `--env`.** Filenames are prefixed with the env tag, which is
what makes a batch traceable, removable, and scoreable by
`eval_by_env.py`. Without it the crops still land safely (the session id
defaults to a timestamp, so nothing collides) but they show up forever as
`untagged` and can't be attributed to a setup.

**`--harvest` never fixes anything by itself.** It only writes crops. You
have to merge and retrain.

**`runs/` is git-ignored, and clearing it is irreversible.** A `best.pt`
lives only on the training PC's disk; the Pi has an NCNN export, which
cannot be converted back to `.pt`. This has already cost one trained model.
The dataset matters more than the weights — weights can be retrained in
seconds, 30 rounds of collection cannot.

**Fine-tuning needs weights that exist.** `--model runs/classify/.../best.pt`
fails on a machine where `runs/` is empty. Omit `--model` and it correctly
starts from the pretrained `yolov8n-cls.pt`.

**Trust test top-1, not val.** `best.pt` is chosen by whichever epoch scored
best on `val/`, so val has effectively been peeked at. A large val-test gap
means the model memorised the training rounds — collect more, don't train
longer.

---

## I. Tuning knobs

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
