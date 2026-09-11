# Training / calibration notes

## Why there's a model again

The project went through a classical-CV phase (`occupancy_color.py`,
now removed) that tried to read empty/white/black per square with
hand-derived pixel-color thresholds instead of a model, on the theory
that occupancy+color is a much easier signal than full piece type. The
*theory* held (occupancy detection worked well), but the color read
never became reliable on real hardware -- several rounds of tuning
(exposure locking, wider calibration bursts, background-subtraction
masking) improved things but didn't fix it, and the failure mode (correct
looking masks, wrong resulting statistics) pointed at the approach itself
being too brittle for this board/piece/lighting combination, not at one
more constant to tune.

`src/square_classifier.py` replaces it with a small **3-class** (empty /
white / black) classifier trained on real photos from your own rig. This
is a much easier problem than the original 12/13-class piece-type
classifier this project used to have (see git history) -- it needs far
less data and training time, and a trained model can learn to disregard
exactly the kind of variation (shadows, glossy highlights, low-contrast
pieces) that broke the pixel-threshold approach, instead of fighting it
with more thresholds. `src/move_resolver.py` still only ever needs
occupancy+color from vision, not type -- piece type is maintained purely
in software by applying resolved legal moves.

**Nothing here trains a model from scratch.** `yolov8n-cls.pt` is an
ImageNet-pretrained classifier; `train_classifier.py` fine-tunes it on
your own 64x64 square crops. That is why 12 collection rounds is enough
where a from-scratch model would need orders of magnitude more. It is
also why it stays small (~2.9 MB) and exports cleanly to NCNN for the Pi.

## Environments

Everything below is done **per environment**. An "environment" is any
combination of room, lighting, camera height/position, board and piece
set -- change any of those and the crops look different enough that a
model which has only seen the old one is guessing.

Each environment gets:

- its own calibration (`config/calibration-env<tag>.json`) -- camera
  position is part of what varies, so board geometry can't be shared;
- an `env<tag>-` prefix on every crop it contributes, which is what lets
  `training/eval_by_env.py` tell you *which* environment is weakest.

All environments' crops live in **one** dataset and train **one** model.
Collecting env 2 never touches env 1's images.

## 1. Calibrate (once per environment)

```bash
python3 src/calibrate.py --env 1
```

Just the 4-corner perspective step -- no baseline capture needed for an
ML classifier. Check `config/calibration-env1_preview.jpg` is a clean 8x8
grid. Re-running for an env that already has a calibration refuses unless
you pass `--force`, so one environment can't silently clobber another.

## 2. Collect training data (on the Pi)

```bash
python3 src/collect_square_crops.py --env 1 --rounds 12 --notes "desk lamp, wooden set"
```

Each round randomly assigns some squares "white", some "black" -- place
*any* piece of the right color there (type doesn't matter, only color
does), leave the rest empty, press Enter. This collects real photos from
your actual camera/board/lighting rather than a public dataset, which is
what caused the domain-gap problems the original 13-class classifier had.

Rounds are split **whole** into `train/` / `val/` / `test/` (~70/15/15) --
never per-frame, because the 6 frames in a round's burst are near
duplicates and splitting them apart would leak the answer across splits
and inflate your accuracy. Each run appends a line to
`training/datasets/squares/manifest.jsonl` recording the env, session,
calibration, counts and your `--notes`, so a 6-environment dataset stays
auditable months later.

Repeat for each environment (`--env 2`, `--env 3`, ...). Then copy
`training/datasets/squares/` to your training PC.

## 3. Train (on your GPU machine)

```bash
bash training/setup.sh                      # one-time env bootstrap, see below
source .venv-train/bin/activate
python training/train_classifier.py --data training/datasets/squares
```

Wraps `yolo classify train` -- `yolov8n-cls.pt` base, `imgsz=64`,
`epochs=50` by default (all overridable), matching
`src/square_classifier.py`'s inference size. A 3-class problem this small
trains fast even on a modest GPU. Aborts if CUDA isn't available (pass
`--allow-cpu` to override).

`best.pt` is selected on `val/`, so the val number flatters the model.
When a `test/` split exists the script scores it once at the end -- that's
the honest number. Then break it down by environment:

```bash
python training/eval_by_env.py --data training/datasets/squares
```

```
env           images  accuracy   worst confusions
-------------------------------------------------
1               1920    99.1%   empty->black x9
2               1856    91.4%   white->empty x71, empty->white x40
```

A row like env 2 means **collect more rounds in env 2** -- augmentation
can't invent a lighting condition the model has never seen. Crops from
before tagging existed are grouped as `untagged`.

## 4. Export to NCNN

```bash
python training/export_ncnn.py --imgsz 64
```

With no `--weights` it exports the newest run under `runs/classify/` after
asking you to confirm; `--run NAME` picks an older one.

## 5. Deploy to the Pi

```bash
python training/deploy.py pi@<pi-hostname> --dest ~/MicroChess/models/square_classifier_ncnn_model
```

`src/main.py`/`src/web_ui.py` expect the classifier at
`models/square_classifier_ncnn_model` by default (override with
`--classifier`).

## Adding a new environment

The crops the classifier learns from include the board surface as
background, so a **different board is a different problem** -- a model
trained on one board is guessing on another. Same goes for a big lighting
or camera-position change. When that happens, add it as a new environment
rather than replacing the old one:

1. `python3 src/calibrate.py --env 3` -- new geometry, kept alongside the
   existing ones.
2. `python3 src/collect_square_crops.py --env 3 --rounds 12 --notes "..."` --
   the env-prefixed filenames keep these from colliding with earlier runs,
   so this **adds** to the dataset rather than replacing it.
3. Fine-tune from what you already have, rather than starting over:

```bash
python training/train_classifier.py --data training/datasets/squares \
    --model runs/classify/train/weights/best.pt
```

Keeping every environment's crops in one dataset gives you one model that
handles all of them. Confirm with `eval_by_env.py` that the new
environment scores well **and** that the older ones didn't regress -- a
drop in an old row means the model is trading environments off against
each other, and that one needs more rounds too.

## Growing the dataset by playing

`python3 src/web_ui.py --harvest` saves labelled crops for all 64 squares
every time a move resolves -- the tracker knows the true board state at
that moment, so the labels come free.

These land in `training/datasets/harvested/`, deliberately *not* mixed
into the curated set: auto-labels are only as good as the tracker was that
day, so merging them in is a decision you make, and a bad run can just be
deleted. When you're happy with a batch, copy it into
`training/datasets/squares/` and fine-tune again.

Note it only harvests on a *resolved move*. Undo is excluded on purpose --
it reports the reverted position while the physical board still shows the
post-move one, which would write confidently mislabelled crops.

## Orphaned scripts (not part of the current pipeline)

`training/prepare_chessred.py`, `train.py`, `val.py`, `prepare_dataset.py`,
`drop_class.py` built and trained the retired full-frame 12-class
detector (`src/detect.py`, removed during the original occupancy/color
redesign). They're left in place in case a full-frame detector is ever
wanted again, but nothing in `src/` currently imports their output.
`export_ncnn.py`, `deploy.py`, `eval_by_env.py` and `setup.sh` are still
actively used by the classifier pipeline above.
