# Training / calibration notes

**As of the occupancy/color redesign, there is no model to train for the
live tracking pipeline.** `src/detect.py` (12-class full-frame YOLO
detector) and `src/square_classifier.py` (13-class per-square classifier),
along with their exported NCNN models and the classifier's training
pipeline (`training/prepare_square_crops.py`, `training/train_classifier.py`),
have been removed.

Why: the tracker already knows the full starting position (32 pieces,
standard layout) and thereafter maintains piece *type* purely in software
by applying resolved legal moves (see `src/move_resolver.py`). Vision only
ever needs to answer empty/white/black per square -- a signal simple enough
to read with plain pixel-color comparisons (`src/occupancy_color.py`)
against a calibration-time baseline, no model inference at all. See that
module's docstring, and `src/tracking_loop.py`'s, for the full design.

## Building the baseline this now needs

Run `python3 src/calibrate.py` on the Pi. Beyond the usual 4-corner click,
it now also walks through two capture steps -- clear the board, then set up
the standard starting position -- and derives the empty-square and
white/black piece-color baselines from those captures, saving everything
into `config/calibration.json`. Re-run it whenever the camera, board,
lighting, or piece set changes. If it warns about poor white/black
luminance separation, fix lighting/contrast before trusting that
calibration.

## Orphaned scripts (not part of the current pipeline)

`training/prepare_chessred.py`, `train.py`, `val.py`, `export_ncnn.py`,
`deploy.py`, `prepare_dataset.py`, `drop_class.py`, `setup.sh`, and
`requirements-train.txt` built and exported the now-removed full-frame
detector. They're left in place in case a full-frame detector is ever
wanted again (e.g. as a future opt-in sanity check), but nothing in
`src/` currently imports their output, and `training/datasets/` (git-ignored)
no longer needs to be populated to run the app.
