# Workflow: collect data → train → deploy

One complete cycle, start to finish, with every command spelled out. Follow
this top to bottom the first time and any time you've been away from the
project long enough to have forgotten it.

- For *why* any of this is designed the way it is → [training/NOTES.md](training/NOTES.md)
- For the terse "just the commands for task X" version → [RUNBOOK.md](RUNBOOK.md)
  (its **How the scripts find your model** section covers path resolution)
- For runtime tuning knobs (thresholds, robot arm) → [RUNBOOK.md § I](RUNBOOK.md#i-tuning-knobs)

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

New to a room and not sure whether the current model already handles it?
Skip to [step 9](#9-testing-a-new-environment-before-training) —
it's a 2-minute check that can save you a whole collection session.

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

Reference points from real runs on this rig: **96.9%** test top-1 after only
3 epochs on a single 12-round environment, **98.1%** after a full 50-epoch run
on a larger set. Anything much below ~97% on a setup you've actually collected
for means that setup needs more data, not more epochs.

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
| `runs/classify/<run>/weights/best.pt` | trained weights (step 5); `<run>` is `train`, then `train-2`, … |
| `runs/classify/<run>/weights/best_ncnn_model/` | export output (step 6) |
| `runs/classify/<run>/results.csv` | per-epoch accuracy history |

> ⚠️ `training/datasets/`, `runs/` and `*.pt` are **git-ignored** — they exist
> only on disk, and `git checkout` will not bring them back.
>
> A trained `best.pt` lives in exactly one place: this PC's `runs/`. The Pi
> holds only an NCNN export, and **NCNN cannot be converted back to `.pt`**.
> Clearing `runs/` therefore destroys that model permanently. This has already
> happened once here.
>
> Weights are cheap — retraining takes seconds. **The dataset is what's
> irreplaceable**: 30 rounds of collection cannot be regenerated. The rsync in
> step 4 is what keeps it on two machines. Nothing else backs it up.

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

The manifest line it appends is your record of that run — this is a real one:

```json
{"env": "1", "session": "env1-20260911-133146", "rounds": 12,
 "calibration": ".../config/calibration-env1.json",
 "round_splits": ["val","train","train","train","val","train","train",
                  "train","train","train","test","test"],
 "notes": "miraz basha day",
 "counts": {"train/empty": 2136, "train/white": 438, "train/black": 498,
            "val/empty": 570, "val/white": 78, "val/black": 120,
            "test/empty": 588, "test/white": 96, "test/black": 84}}
```

Note `empty` outnumbers the two piece classes about 4:1. That's inherent —
only ~10 of 64 squares hold a piece per round — and it's expected, not a bug.

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

### 3c. How much data do I need?

**"Perfect everywhere" is not a reachable target** — it's an asymptote, not a
number of images. Here's why, and what to aim for instead.

A move only resolves if **all 64 squares** read correctly, so per-square
accuracy compounds:

| Per-square accuracy | Chance the whole board reads right | Moves between failures |
|---|---|---|
| 98% | 27% | ~1 |
| 99% | 53% | ~2 |
| 99.5% | 73% | ~4 |
| 99.9% | 94% | ~16 |
| 99.99% | 99.4% | ~157 |

To never miss across a 40-move game you'd need ~99.996% per square. Don't
chase that.

**What saves you is that failure is safe.** `resolve_from_deltas` requires an
*exact* match against exactly one legal move, so a misread never produces a
wrong move — it produces no match, and the UI asks you to correct it. Squares
below `--min-conf` are skipped rather than trusted, and the 3-frame majority
vote discards random noise. So the real question is "how often do I click
Undo?", not "is it perfect."

> The consensus vote only fixes **random** error. A square with permanent
> glare or a hard shadow reads wrong in all 3 frames, so voting does nothing
> for it. Systematic errors like that are exactly what more data fixes.

**Count rounds, not images.** Each round writes 64 squares × 6 burst frames =
384 files, but those 6 frames are near-identical — about 64 genuinely new
observations. A 15,000-image dataset is really ~2,500 unique square views,
roughly 40 rounds.

| Per environment | Rounds | Images | What you get |
|---|---|---|---|
| Bare minimum | 12 | ~4,600 | ~97–98%, frequent corrections |
| **Recommended** | **25–30** | **~10–11k** | **~99%+, occasional correction** |
| Diminishing returns past | ~50 | ~19k | more rounds stop helping |

Also note only ~10 of 64 squares hold a piece each round, so 12 rounds gives
only ~120 piece views spread across 64 squares — under 2 per square. That
under-coverage is why individual squares misread, and it's why round count
matters more than it first appears.

**How many environments?** 5–6 is genuinely enough — *if they differ*. Two
setups that vary only by time of day teach almost nothing new. Spread them
across the axes that actually break the model, in this order: lighting
direction and intensity (the worst offender by far), camera height, board and
piece set, then background.

Split rounds across sittings too. 15 rounds in daylight plus 15 under
lamplight beats 30 in one sitting, because one sitting is one lighting
condition no matter how many rounds it contains.

**Don't collect blind toward a target.** Collect 12 rounds, train, run
`eval_by_env.py` (step 5), and give another 10 rounds to any environment
below ~99%. Stop when every row is ≥99% and the overall test number stops
moving. `--harvest` during play then grows the set for free.


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

**First, check what weights you actually have:**

```bash
ls runs/classify/*/weights/best.pt
```

That output decides which of the two commands you run.

**If it lists something** — fine-tune from your current best:

```bash
python training/train_classifier.py --data training/datasets/squares \
    --model runs/classify/train/weights/best.pt
```

Fine-tuning from what already works keeps everything the model learned about
your other environments; starting over throws that away for no benefit.

**If it lists nothing** — a fresh machine, or `runs/` was cleared — omit
`--model` entirely:

```bash
python training/train_classifier.py --data training/datasets/squares
```

It then starts from the pretrained `yolov8n-cls.pt`, which is correct. Don't
pass a `--model` path that doesn't exist; it will fail rather than fall back.

It aborts if CUDA isn't available rather than silently crawling on CPU. It
then prints per-epoch top-1 against `val/`, and at the end:

```
Scoring the held-out test split...
Test top-1: 0.9688   top-5: 1.0000

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

**You don't need to retype that path.** Every step below resolves it for you:
`export_ncnn.py`, `eval_by_env.py` and `deploy.py` default to the newest run
and print which one they picked. Pass `--run NAME` to target an older run, or
`--yes` to skip the confirmation prompt.

### Then break the score down per environment

```bash
python training/eval_by_env.py --data training/datasets/squares
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
python training/export_ncnn.py --imgsz 64
```

`--imgsz 64` must match what you trained at, or inference will be subtly
wrong. This writes `best_ncnn_model/` (a `.param` + `.bin` pair) next to the
weights.

Rename it to the name the Pi expects:

```bash
mv runs/classify/train/weights/best_ncnn_model \
   runs/classify/train/weights/square_classifier_ncnn_model
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
python training/deploy.py nightmare@192.168.0.106 --dest ~/C/PlayerKoi/models/
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
by repeating steps 6–7 with `--run <older run>`. Nothing is destroyed by a bad
deploy.

---

## 9. Testing a new environment before training

You've moved to a new room, or swapped the board, and you don't yet know
whether the existing model copes. Run this **before** committing to 25 rounds
of collection. Two tiers: the first takes two minutes, the second gives you a
number.

### Tier 1 — eyeball it (2 minutes, no data collected)

Calibrate the new setup, then point the *current* model at it.

```bash
# on the Pi
python3 src/calibrate.py --env 5
python3 src/debug_classifier.py --calibration config/calibration-env5.json
```

Set up a position you can check by eye. You get a grid plus:

```
predicted: empty=52 white=6 black=6
3/64 below --min-conf=0.5
    e4  white  0.41
```

Read it like this:

| What you see | Verdict |
|---|---|
| Grid matches reality, 0–2 squares below min-conf | Probably fine — go to Tier 2 to confirm |
| Grid matches but 5–15 squares low-confidence | Marginal. It'll work but flag often — Tier 2 |
| Any square shows the **wrong** colour, or >15 low-conf | Training needed. Skip Tier 2, go collect |

Try 2–3 different positions, including pieces on both light and dark squares
and a few in corners. A single lucky position proves nothing.

If it fails badly here, you've saved yourself the trouble — go straight to
step 3b with a fresh `--env` tag.

### Tier 2 — measure it (15 minutes, gives a real number)

Tier 1 is a vibe. To get an actual accuracy figure you need labelled images,
so collect a few rounds into a **throwaway directory** rather than the real
dataset:

```bash
# on the Pi
python3 src/collect_square_crops.py --env 5 --rounds 4 \
    --out training/datasets/probe-env5 \
    --notes "PROBE: spare room, ceiling light"
```

Four rounds is ~1,500 images and takes about ten minutes. Pull it to the PC
and score the **current deployed model** against it:

```bash
# on the training PC
rsync -av nightmare@192.168.0.106:~/C/PlayerKoi/training/datasets/probe-env5/ \
          training/datasets/probe-env5/

python training/eval_by_env.py --data training/datasets/probe-env5 --split train
```

Use `--split train` — it's the largest share of a 4-round probe. Repeat with
`--split val` / `--split test` if you want the rest; with so few rounds the
splits are tiny and noisy individually.

Point `--weights` at the model **currently deployed on the Pi**, not at
something newer. The question is "does what I already have cope here?"

### Decide

| Probe accuracy | Verdict | Do this |
|---|---|---|
| **≥ 99%** | No training needed | Keep the calibration, play. Delete the probe dir |
| **97 – 99%** | Borderline | Collect 10–12 rounds in this env, fine-tune (steps 3b → 8) |
| **< 97%** | Genuinely new environment | Collect the full 25–30 rounds, fine-tune |
| **< 90%** | Something is broken, not just unfamiliar | Check the calibration preview first — a skewed grid crops the wrong pixels and looks exactly like a model failure |

Also read the confusion column, not just the percentage. `white->empty` in
bulk usually means the pieces are under-lit or the exposure lock caught a
different brightness; `empty->white` in bulk often means glare on the board
surface. Both are worth fixing physically before fixing with data — moving a
lamp is cheaper than 30 rounds.

### Don't waste the probe

If you decide to train for this environment, fold the probe images into the
real dataset rather than recollecting them — they're already labelled and
already tagged `env5-`:

```bash
rsync -av training/datasets/probe-env5/ training/datasets/squares/
rm -rf training/datasets/probe-env5
```

Then collect the remaining rounds normally (step 3b, same `--env 5`) and
continue from step 4.

If you decide **not** to train, just delete the probe directory. Keep the
calibration file — it costs nothing and you'll want it next time you're in
that room.

> A probe that passes is not permanent. Lighting drifts with the seasons and
> the time of day. If a setup that used to be fine starts flagging, re-probe
> it before assuming the model has degraded — it's usually the room, not the
> weights.

---

## 10. Which path do I take?

| Situation | Do this |
|---|---|
| Moved somewhere new, unsure if it needs training | **Probe it first — step 9.** Don't collect 30 rounds on a hunch |
| Probe says it needs training | New environment: step 3 with a fresh `--env` tag, then 4–8 |
| Misreads in a setup you've already collected for | More rounds in **that** env (step 3b, same tag), then 4–8 |
| Want more data for free | Run `web_ui.py --harvest` while playing, then [RUNBOOK § D](RUNBOOK.md#d-harvest-from-play) to merge |
| Accuracy dropped after deploying | Redeploy the previous run: steps 6–7 with `--run <older run>` |
| One square consistently wrong | Collect more rounds — that square is under-covered (~10 of 64 per round) |
| Board diagram disagrees with reality | **Undo last move** / **Edit board** in the UI. Not a training problem |
| Moves never detected | Motion gate, not the model — `debug_classifier.py --watch`, lower `--motion-thresh` |
| Tempted to retrain from scratch | Don't. Always `--model <current best>` |
| Unsure how many rounds/environments to aim for | [Step 3c](#3c-how-much-data-do-i-need) |

---

## 11. Troubleshooting

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
[RUNBOOK § I](RUNBOOK.md#i-tuning-knobs).
