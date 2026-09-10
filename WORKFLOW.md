# Workflow: collect data → train → deploy

One complete cycle, start to finish, with every command spelled out. Follow
this top to bottom the first time and any time you've been away from the
project long enough to have forgotten it.

- For *why* any of this is designed the way it is → [training/NOTES.md](training/NOTES.md)
- For the terse "just the commands for task X" version → [RUNBOOK.md](RUNBOOK.md)
- For runtime tuning knobs (thresholds, robot arm) → [RUNBOOK.md § G](RUNBOOK.md#g-tuning-knobs)

**The two machines**

| | Address | Project path | venv |
|---|---|---|---|
| **Pi** | `nightmare@192.168.0.106` | `~/C/PlayerKoi` | `.venv` |
| **Training PC** | this machine | `~/MicroChess` | `.venv-train` |

```bash
# Pi
ssh nightmare@192.168.0.106
cd ~/C/PlayerKoi && source .venv/bin/activate

# Training PC
cd ~/MicroChess && source .venv-train/bin/activate
```

Data is collected on the **Pi** (that's where the camera is), training happens
on the **PC** (that's where the GPU is), and the finished model goes back to
the **Pi**. Steps 4 and 7 are the two crossings.

---

## 0. What the vision system actually does

**It reads three things per square: `empty`, `white`, `black`. It never
identifies piece type.** No amount of training data will make it say "knight",
because it is not being asked to.

Piece identity is tracked *in software*: the board starts in the standard
position, and [`src/move_resolver.py`](src/move_resolver.py) applies resolved
legal moves to a `python-chess` board. Vision only has to answer "which
squares changed, and what colour is on them now?" — the legal-move matcher
turns that into "Nf3". This is why the model can be tiny and why 3 classes is
enough.

Consequence worth internalising: if the board and the software disagree, the
fix is **Undo last move** or **Edit board** in the web UI, not more training.

---

## 1. Which model we use

| | |
|---|---|
| Base | `yolov8n-cls.pt` — YOLOv8-nano classifier, **pretrained on ImageNet** |
| What we do to it | **Fine-tune** it on your own square crops. Never trained from scratch |
| Input | 64×64 pixel crop of one square |
| Classes | 3 — `empty`, `white`, `black` |
| Size | ~2.9 MB as `best.pt` |
| On the Pi | exported to **NCNN** (fastest CPU backend on a Pi), run via ultralytics |

Starting from a pretrained model is the whole reason ~12 collection rounds per
setup is enough. The backbone already knows edges, shadows, texture and gloss;
you are only teaching it *your* board's three answers. From scratch this would
need orders of magnitude more images.

The current deployed model was trained to **98.1% top-1**. Anything much below
~97% on a setup you've collected for means that setup needs more data.

---

## 2. Where everything is saved

**On the Pi** (`~/C/PlayerKoi`)

| Path | What |
|---|---|
| `config/calibration-env<tag>.json` | board geometry — one per environment, created in step 3 |
| `config/calibration-env<tag>_preview.jpg` | warped preview to eyeball after calibrating |
| `training/datasets/squares/{train,val,test}/{empty,white,black}/` | the crops you collect |
| `training/datasets/squares/manifest.jsonl` | one line per collection run: env, notes, counts |
| `training/datasets/harvested/` | crops auto-labelled during play by `--harvest` |
| `models/square_classifier_ncnn_model/` | **the model actually being used** |

**On the training PC** (`~/MicroChess`)

| Path | What |
|---|---|
| `training/datasets/squares/` | the merged dataset, pulled from the Pi in step 4 |
| `runs/classify/train-N/weights/best.pt` | trained weights (step 5) |
| `runs/classify/train-N/weights/best_ncnn_model/` | export output (step 6) |
| `runs/classify/train-N/results.csv` | per-epoch accuracy history |

> ⚠️ `training/datasets/`, `runs/` and `*.pt` are **git-ignored**. They exist
> only on disk. The rsync in step 4 is the only copy of your collected images
> that lives on two machines — nothing else backs them up.

An image filename encodes everything about itself:

```
env2-20260911-142030_r003_e4_2.jpg
└──┬─┘ └──────┬─────┘ └─┬┘ └┬┘ │
  env      session    round sq frame
```

That `env2-` prefix is what lets `eval_by_env.py` (step 5) score each setup
separately.

---

## 3. Collect data — on the Pi

An **environment** is one combination of room, lighting, camera height, board
and piece set. Change any of those and it's a new environment with a new tag.
Do this section once per environment.

### 3a. Calibrate (once per environment)

Needs a display — HDMI or VNC — because you click on a preview window.

```bash
python3 src/calibrate.py --env 1
```

Click the board's 4 outer corners **in this order: a1, h1, h8, a8**, clicking
from the same physical side you'll play from. Click the actual board corners,
not the outer decorative frame.

**Check before continuing:** open `config/calibration-env1_preview.jpg`. It
must look like a clean, square 8×8 grid. Skewed or trapezoidal means you
mis-clicked — re-run with `--force` and click more precisely.

Re-running for an env that already has a calibration is refused unless you
pass `--force`. That's deliberate: camera position varies per environment, so
an accidental re-run used to destroy another environment's geometry.

### 3b. Collect crops

```bash
python3 src/collect_square_crops.py --env 1 --rounds 12 \
    --notes "living room, desk lamp, wooden set"
```

It prints a session id, then walks you through rounds:

```
Round 1/12 (train):
  WHITE (any piece) on: a3, c7, d2, f5, g1
  BLACK (any piece) on: b6, e4, e8, h2
  Leave every other square empty.
  Press Enter when the board matches...
```

Put **any** piece of the named colour on those squares — a pawn works for
"white" exactly as well as a queen, because type is not being learned. Clear
every other square. Press Enter. It captures a 6-frame burst and saves 64
labelled crops.

At the end:

```
Saved to /home/nightmare/C/PlayerKoi/training/datasets/squares/
  empty: 3312  white: 384  black: 288
  train/empty: +2208   val/empty: +552   test/empty: +552
  ...
Recorded this run in .../training/datasets/squares/manifest.jsonl
```

**Check before continuing:** all three splits got images. `train`, `val` and
`test` should each be non-zero.

Notes on what's happening:

- Whole **rounds** go to train/val/test (~70/15/15), never individual frames.
  The 6 frames in a burst are near-identical; splitting them apart would put
  near-copies of a training image into the test set and your accuracy number
  would be a lie. **Never re-split this data by file.**
- Runs are **additive**. `--env 2` never touches env 1's images.
- `--notes` is free text and goes in the manifest. Write what you'd want to
  know in six months: light source, time of day, which set.

> More rounds mainly buys **per-square coverage** (only ~10 of 64 squares get
> a piece each round). But 30 rounds in one sitting is still one lighting
> condition — 15 now and 15 this evening is worth more than 30 now.

Repeat 3a + 3b for `--env 2`, `--env 3`, … as you add setups.

---

## 4. Move the data to the training PC

Run this **on the PC**, pulling from the Pi:

```bash
cd ~/MicroChess && source .venv-train/bin/activate

rsync -av nightmare@192.168.0.106:~/C/PlayerKoi/training/datasets/squares/ \
          training/datasets/squares/
```

Mind the trailing slashes — both are required, and `rsync` **merges** into the
destination rather than replacing it, so older environments already on the PC
survive.

**Check before continuing** — class counts on the PC:

```bash
find training/datasets/squares -name "*.jpg" \
  | sed 's#.*/\([^/]*\)/[^/]*$#\1#' | sort | uniq -c
```

and that the new environment actually arrived:

```bash
ls training/datasets/squares/train/white | grep -c '^env1-'
tail -1 training/datasets/squares/manifest.jsonl
```

If the env-prefixed count is 0, the rsync didn't bring what you thought — fix
that before training, not after.

---

## 5. Train — on the PC

```bash
python training/train_classifier.py --data training/datasets/squares \
    --model runs/classify/train-2/weights/best.pt
```

Point `--model` at your **current best weights**, not at `yolov8n-cls.pt`.
Fine-tuning from what already works keeps everything the model learned about
your other environments; starting over throws it away for no benefit.

(First time only, with no previous run to build on, omit `--model` — it
defaults to `yolov8n-cls.pt`.)

It aborts if CUDA isn't available rather than silently crawling on CPU. It
then prints per-epoch top-1 against `val/`, and at the end:

```
Scoring the held-out test split...
Test top-1: 0.9814   top-5: 1.0000

Done. Best weights: /home/nightmare/MicroChess/runs/classify/train-3/weights/best.pt
```

**Read the two accuracy numbers correctly:**

- **val top-1** is *optimistic*. `best.pt` is chosen by picking the epoch that
  scored best on `val/`, so val has effectively been peeked at.
- **test top-1** is the honest number. Those rounds were never trained on and
  never used to select anything.

**Note the run directory it printed.** Ultralytics auto-increments with a
hyphen — `train-2` becomes `train-3` — and every remaining step needs that
exact path. Don't guess it.

### Then break the score down per environment

```bash
python training/eval_by_env.py --data training/datasets/squares \
    --weights runs/classify/train-3/weights/best.pt
```

```
test/ split, runs/classify/train-3/weights/best.pt

env           images  accuracy   worst confusions
-------------------------------------------------
1               1920    99.1%   empty->black x9
2               1856    91.4%   white->empty x71, empty->white x40
untagged        2560    98.0%   empty->black x22
-------------------------------------------------
ALL             6336    96.4%
```

How to act on it:

- A **weak row** (env 2 here) → go collect more rounds *in that environment*.
  Augmentation cannot invent a lighting condition the model has never seen.
- An **old row that dropped** since last time → the model is trading
  environments off against each other; that one needs more rounds too.
- `untagged` is data collected before env tagging existed. Normal.

If the overall test number is worse than your deployed model's, stop here and
fix the data. Don't deploy it.

---

## 6. Export to NCNN — on the PC

```bash
python training/export_ncnn.py --weights runs/classify/train-3/weights/best.pt --imgsz 64
```

`--imgsz 64` must match what you trained at, or inference will be subtly
wrong. This writes `best_ncnn_model/` (a `.param` + `.bin` pair) next to the
weights.

Rename it to the name the Pi expects:

```bash
mv runs/classify/train-3/weights/best_ncnn_model \
   runs/classify/train-3/weights/square_classifier_ncnn_model
```

`src/main.py`, `src/web_ui.py` and `src/debug_classifier.py` all default to
`models/square_classifier_ncnn_model` (see `DEFAULT_CLASSIFIER` in
[src/main.py:25](src/main.py#L25)). You could skip the rename and pass
`--classifier` everywhere instead, but renaming once is less to remember.

---

## 7. Get the model onto the Pi

**Delete the old model directory first.** This matters:

```bash
ssh nightmare@192.168.0.106 'rm -rf ~/C/PlayerKoi/models/square_classifier_ncnn_model'
```

`scp -r` copies a directory *into* an existing directory of the same name. If
you skip this you get
`square_classifier_ncnn_model/square_classifier_ncnn_model/` and the model
fails to load. This has actually happened.

Then copy:

```bash
python training/deploy.py nightmare@192.168.0.106 \
    --model-dir runs/classify/train-3/weights/square_classifier_ncnn_model \
    --dest ~/C/PlayerKoi/models/
```

Add `--dry-run` first if you want to see the `scp` command without running it.

---

## 8. Verify on the Pi

```bash
ssh nightmare@192.168.0.106
cd ~/C/PlayerKoi && source .venv/bin/activate

python3 src/debug_classifier.py
```

Set the board up in a position you can eyeball and check the printed grid
(`W` = white, `B` = black, `.` = empty, `?` = below the confidence threshold)
matches reality. **Do this before trusting the model in a game.** A handful of
`?` is survivable; a wrong `W`/`B` is not.

If moves aren't detected at all later, it's usually the motion gate, not the
model:

```bash
python3 src/debug_classifier.py --watch
```

Move a piece — you should see the score rise into `moving`, then fall back to
`settled`. If it never says `moving`, the classifier is never being run; lower
`--motion-thresh`.

Then run it for real:

```bash
python3 src/web_ui.py --harvest
```

Open <http://192.168.0.106:8000/>.

**Rolling back:** the previous run directory is still on the PC. Redeploy it
by repeating steps 6–7 with the older `train-N`. Nothing is destroyed by a bad
deploy.

---

## 9. Which path do I take?

| Situation | Do this |
|---|---|
| New room, lighting, board, piece set, or camera moved | New environment: step 3 with a fresh `--env` tag, then 4–8 |
| Misreads in a setup you've already collected for | More rounds in **that** env (step 3b, same tag), then 4–8 |
| Want more data for free | Run `web_ui.py --harvest` while playing, then [RUNBOOK § A](RUNBOOK.md#a-retrain-from-harvested-play-data) to merge |
| Accuracy dropped after deploying | Redeploy the previous `train-N` (steps 6–7), then investigate |
| One square consistently wrong | Collect more rounds — that square is under-covered (~10 of 64 per round) |
| Board diagram disagrees with reality | **Undo last move** / **Edit board** in the UI. Not a training problem |
| Moves never detected | Motion gate, not the model — `debug_classifier.py --watch`, lower `--motion-thresh` |
| Tempted to retrain from scratch | Don't. Always `--model <current best>` |

---

## 10. Troubleshooting

**`No calibration found at config/calibration-env1.json`**
You skipped step 3a for that env tag, or you're using a different tag than you
calibrated with. Tags are exact strings: `--env 1` and `--env 01` are
different environments.

**`config/calibration-env1.json already exists`**
Intentional guard. Pass `--force` if you really mean to re-calibrate that
environment, or use a new tag if this is actually a different setup.

**`CUDA is not available -- training would silently run on CPU`**
Fix the GPU environment with `bash training/setup.sh`. Only use `--allow-cpu`
if you're willing to wait a very long time.

**`Nothing to resume: ... last.pt not found`**
A run only becomes resumable after it saves its first epoch. Start a normal
run instead.

**Good accuracy on the PC, bad on the Pi**
Almost always the model, not the data. In order: (1) did you `rm -rf` the old
directory before deploying (step 7)? Check for a nested
`square_classifier_ncnn_model/square_classifier_ncnn_model/` on the Pi;
(2) did you export with `--imgsz 64`? (3) run `debug_classifier.py` and
compare against the `eval_by_env.py` numbers for that environment.

**Test accuracy much lower than val accuracy**
Expected to be a little lower. A *large* gap means the model memorised the
training rounds — collect more data rather than training more epochs.

**`No test/ split in the dataset`**
That dataset predates the test split. Collect once with the current
`collect_square_crops.py` and it'll appear.

**`eval_by_env.py` shows everything as `untagged`**
Those crops were collected before `--env` existed. Still perfectly usable for
training — you just can't attribute them to a setup.

For runtime behaviour (thresholds, engine, robot arm) see
[RUNBOOK § G](RUNBOOK.md#g-tuning-knobs).
