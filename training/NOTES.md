# Training the piece-detection model (do this off the Pi)

The Pi 5 is the *deployment* target, not the training machine. Train on a
laptop/desktop with a GPU, or a free Google Colab GPU instance, then copy
the exported model onto the Pi's SD card.

## 0. One-time setup on the training PC

This repo ships Python wrappers for every step below under `training/`, so the
pipeline is repeatable. First bootstrap the environment:

```bash
bash training/setup.sh
source .venv-train/bin/activate   # in each new shell
```

`setup.sh` creates a dedicated `.venv-train`, installs a **CUDA 12.8 PyTorch
build** (required for recent NVIDIA GPUs like RTX 50-series / Blackwell — a
plain `pip install ultralytics` can pull a torch that won't drive the GPU),
then ultralytics, and finally verifies the GPU is visible to torch. If that
verification fails, training would silently fall back to CPU, so fix it before
continuing.

The raw `yolo`/`pip` commands in the sections below still work; the
`python training/*.py` equivalents just wrap them with the right defaults and
guardrails for this project.

## 1. Get a dataset

**The dataset actually used for the deployed models is [ChessReD](https://data.4tu.nl/datasets/99b5c721-280b-450b-b058-b2900b69a90f)**
(`annotations.json` + the `chessred2k.zip` subset, unzipped so `training/datasets/chessred_raw/images/`
exists). It's what both `training/prepare_chessred.py` (full-frame fallback
detector) and `training/prepare_square_crops.py` (per-square classifier,
see step 6 below) are built against -- prefer it over the generic Roboflow
path below unless you have a specific reason not to:

```bash
python training/prepare_chessred.py \
    --annotations training/datasets/chessred_raw/annotations.json \
    --images-root training/datasets/chessred_raw \
    --out training/datasets/chessred_yolo
```

The rest of this section describes the generic alternative: any public,
bounding-box-labeled chess piece dataset in YOLO format covering the 12
standard classes:

```
white-king, white-queen, white-rook, white-bishop, white-knight, white-pawn
black-king, black-queen, black-rook, black-bishop, black-knight, black-pawn
```

Search **Roboflow Universe** (universe.roboflow.com) for "chess pieces
detection" -- there are several public datasets with a few hundred to a
few thousand annotated images in this format. Pick one, export it in
"YOLOv8" format, and download the zip in your browser (no Roboflow API key
needed). Unzip it into `training/datasets/<name>/` -- you'll get a
ready-to-use `data.yaml` + `train/valid/test` image+label folders.
(`training/datasets/` is git-ignored, so the images never get committed.)

**Check the class names before training.** They must resolve to the
`{color}-{piece}` convention above; some datasets use `b_pawn`,
`Black-Pawn`, single FEN letters (`P`, `n`), etc. Let the prep script check
and, where it can, fix them:

```bash
python training/prepare_dataset.py training/datasets/<name>        # report only
python training/prepare_dataset.py training/datasets/<name> --fix  # rewrite data.yaml
```

`--fix` rewrites `data.yaml`'s `names:` to canonical spelling (backing up the
original to `data.yaml.bak`) when every class maps unambiguously. Anything it
reports as UNMAPPED you must hand-edit in `data.yaml`, or add to `NAME_MAP` in
`../src/detect.py`.

## 2. Train

```bash
python training/train.py --data training/datasets/<name>/data.yaml
```

Wraps `yolo detect train` with the defaults below (all overridable via flags):

- `yolov8n.pt` (nano) is the right size class for Pi 5 CPU inference.
- `epochs=100`, `imgsz=480`. `imgsz` matches what `src/detect.py` requests at
  inference time -- keep them in sync, or update `detect.py`'s default `imgsz`
  if you train at a different resolution.
- The script **aborts if CUDA isn't available** so you don't accidentally train
  on CPU (pass `--allow-cpu` to override).
- Training writes to `runs/detect/train/weights/best.pt`.

## 3. Sanity check before exporting

```bash
python training/val.py --weights runs/detect/train/weights/best.pt \
                       --data training/datasets/<name>/data.yaml
```

Prints per-class precision/recall -- if some piece types are much worse than
others (common for queen vs. bishop, or pawn under-annotated datasets), it's
worth knowing before you're debugging it live on the Pi.

## 4. Export to NCNN

NCNN is the fastest CPU inference backend on ARM (Raspberry Pi), which is
why we target it instead of plain PyTorch/ONNX for a CPU-only Pi 5:

```bash
python training/export_ncnn.py --weights runs/detect/train/weights/best.pt
```

This produces a `best_ncnn_model/` directory (param + bin files) next to the
weights.

## 5. Deploy to the Pi

Copy the whole `best_ncnn_model/` directory onto the Pi, into
`MicroChess/models/`, over scp:

```bash
python training/deploy.py pi@<pi-hostname>
# add --dry-run first to preview the scp command
```

`src/main.py`/`src/web_ui.py` expect it at `models/best_ncnn_model` by
default (override with `--model` if you name it differently).

## 6. Train the per-square classifier

The fallback detector above is only used when the fast path can't resolve
a move. Routine per-move recognition instead runs a small classifier on
just the handful of square crops `src/roi_diff.py` flags as changed --
much cheaper than a full-frame detection pass every move. It needs its
own dataset and its own training run:

```bash
python training/prepare_square_crops.py \
    --annotations training/datasets/chessred_raw/annotations.json \
    --images-root training/datasets/chessred_raw \
    --out training/datasets/chessred_squares

python training/train_classifier.py --data training/datasets/chessred_squares

python training/export_ncnn.py \
    --weights runs/classify/train/weights/best.pt --imgsz 64
```

Deploy the resulting `best_ncnn_model/` directory to
`models/square_classifier_ncnn_model` on the Pi (rename it on the way, or
pass `--dest`/`--model-dir` to `deploy.py`).

**Domain gap warning**: ChessReD is handheld smartphone photos from
varied boards/angles/lighting; your Pi's camera is one fixed overhead
rig. Expect a real accuracy gap on first deploy -- tune `min_conf` in
`src/square_classifier.py`'s caller, and consider recording real,
auto-labeled crops during play (every square, whenever `move_resolver`
confidently accepts a move -- ground truth comes from the legal-move
match, not manual labeling) for a periodic fine-tune pass on top of the
ChessReD-trained weights.

## Expected accuracy caveat

A model trained purely on a public dataset will not perfectly match your
specific board, pieces, lighting, and camera. Expect to tune the
`--conf` confidence threshold in `main.py` against your real setup, and
if accuracy isn't good enough, the next step (not built yet) is
fine-tuning `best.pt` on a modest number of your own captured, labeled
images before re-exporting to NCNN.
