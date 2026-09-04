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

## E. Gotchas that have actually bitten

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

## F. Tuning knobs

| Problem | Knob |
|---|---|
| Moves not detected at all | `web_ui.py --motion-thresh 2.0` (default 3.0). Diagnose with `debug_classifier.py --watch` |
| Too many "low-confidence" flags | `web_ui.py --min-conf 0.4` (default 0.5) |
| Wrong moves accepted | Raise `--min-conf`, or collect more data for the squares that misread |
| Engine too strong | Skill slider in the UI (0–20; 0 is genuinely beatable) |
| Board diagram wrong | **Undo last move** for one bad move; **Edit board** for a full resync |

Diagnostics:

```bash
python3 src/debug_classifier.py            # per-square class + confidence
python3 src/debug_classifier.py --watch    # live motion-gate scores
```
