"""Main loop: capture -> event-gated diff/classify tracking -> print/log on change.

    python3 src/main.py [--model models/best_ncnn_model]
                         [--classifier models/square_classifier_ncnn_model]
                         [--log board.log]

Requires config/calibration.json (run calibrate.py first), a trained,
NCNN-exported detector (see training/NOTES.md) for the full-frame fallback
path, and a trained, NCNN-exported per-square classifier (see
training/prepare_square_crops.py and training/train_classifier.py) for the
fast routine per-move path. See src/tracking_loop.py for how the two are
combined: most moves are resolved from a handful of per-square crop
classifications plus legal-move matching, without ever running the
full-frame detector.
"""

import argparse
import datetime as dt
from pathlib import Path

from board_state import format_matrix, load_calibration
from capture import Camera, CaptureStream
from detect import load_model
from square_classifier import load_classifier
from tracking_loop import TrackingLoop

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CALIBRATION = REPO_ROOT / "config" / "calibration.json"
DEFAULT_MODEL = REPO_ROOT / "models" / "best_ncnn_model"
DEFAULT_CLASSIFIER = REPO_ROOT / "models" / "square_classifier_ncnn_model"


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL, help="full-frame fallback detector")
    parser.add_argument("--classifier", type=Path, default=DEFAULT_CLASSIFIER, help="per-square classifier")
    parser.add_argument("--calibration", type=Path, default=DEFAULT_CALIBRATION)
    parser.add_argument("--conf", type=float, default=0.4, help="fallback detector confidence threshold")
    parser.add_argument(
        "--log", type=Path, default=None, help="optional path to append board-state changes to"
    )
    return parser.parse_args()


def main():
    args = parse_args()

    if not args.calibration.exists():
        raise SystemExit(f"No calibration found at {args.calibration} -- run calibrate.py first.")
    if not args.model.exists():
        raise SystemExit(f"No fallback model found at {args.model} -- see training/NOTES.md to train/export one.")
    if not args.classifier.exists():
        raise SystemExit(
            f"No classifier model found at {args.classifier} -- see training/prepare_square_crops.py "
            "and training/train_classifier.py to train/export one."
        )

    calibration_matrix = load_calibration(args.calibration)
    print("Loading models...")
    fallback_model = load_model(str(args.model))
    classifier_model = load_classifier(str(args.classifier))

    log_file = open(args.log, "a") if args.log else None

    def on_update(matrix, move_text, frame, flagged):
        timestamp = dt.datetime.now().isoformat(timespec="seconds")
        header = "board re-synced (no legal move matched, even after a rescan)" if flagged else f"move: {move_text}"
        output = f"[{timestamp}] {header}\n{format_matrix(matrix)}\n"
        print(output)
        if log_file:
            log_file.write(output + "\n")
            log_file.flush()

    print("Starting board tracking. Press Ctrl+C to stop.")
    try:
        with Camera() as cam, CaptureStream(cam) as stream:
            frame = None
            while frame is None:
                frame, _timestamp = stream.get_latest()
            image_size = (frame.shape[1], frame.shape[0])

            loop = TrackingLoop(
                capture_stream=stream,
                calibration_matrix=calibration_matrix,
                image_size=image_size,
                fallback_model=fallback_model,
                classifier_model=classifier_model,
                on_update=on_update,
                fallback_conf=args.conf,
            )
            loop.run_forever()
    except KeyboardInterrupt:
        print("Stopped.")
    finally:
        if log_file:
            log_file.close()


if __name__ == "__main__":
    main()
