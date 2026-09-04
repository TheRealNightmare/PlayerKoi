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

## 1. Calibrate

```bash
python3 src/calibrate.py
```

Just the 4-corner perspective step now -- no baseline capture needed for
an ML classifier.

## 2. Collect training data (on the Pi)

```bash
python3 src/collect_square_crops.py --out training/datasets/squares --rounds 12
```

Each round randomly assigns some squares "white", some "black" -- place
*any* piece of the right color there (type doesn't matter, only color
does), leave the rest empty, press Enter. This collects real photos from
your actual camera/board/lighting rather than a public dataset, which is
what caused the domain-gap problems the original 13-class classifier had.
More rounds = more data; spreading rounds across different times of
day/lighting helps the model generalize instead of overfitting to one
lighting condition. Copy the resulting `training/datasets/squares/`
directory to your training PC.

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

## 4. Export to NCNN

```bash
python training/export_ncnn.py --weights runs/classify/train/weights/best.pt --imgsz 64
```

Same generic export script the old detector/classifier pipelines used --
no changes needed, it's task-agnostic.

## 5. Deploy to the Pi

```bash
python training/deploy.py pi@<pi-hostname> --model-dir runs/classify/train/weights/best_ncnn_model --dest ~/MicroChess/models/square_classifier_ncnn_model
```

`src/main.py`/`src/web_ui.py` expect the classifier at
`models/square_classifier_ncnn_model` by default (override with
`--classifier`).

## Changing the board (or the lighting)

The crops the classifier learns from include the board surface as
background, so a **different board is a different problem** -- a model
trained on one board is guessing on another. Same goes for a big lighting
change. When that happens:

1. `python3 src/calibrate.py` -- the physical corners moved.
2. `python3 src/collect_square_crops.py --session <name> --rounds 12` --
   collect on the new board. The `--session` label (or an automatic
   timestamp) keeps these files from colliding with earlier runs, so this
   **adds** to the dataset rather than replacing it.
3. Fine-tune from what you already have, rather than starting over:

```bash
python training/train_classifier.py --data training/datasets/squares \
    --model runs/classify/train/weights/best.pt
```

Keeping both boards' crops in one dataset gives you one model that handles
both. Watch the per-epoch top-1 accuracy ultralytics prints against the
`val/` split to confirm it actually learned the new board.

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
`export_ncnn.py`, `deploy.py`, and `setup.sh` are still actively used by
the classifier pipeline above.
