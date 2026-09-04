"""Main loop: capture -> event-gated classify/delta/legal-move tracking -> print/log on change.

    python3 src/main.py [--classifier models/square_classifier_ncnn_model]

Requires config/calibration.json (run calibrate.py first) and a trained,
NCNN-exported empty/white/black classifier (see src/collect_square_crops.py
and training/train_classifier.py). See src/tracking_loop.py for how a
settle is resolved: a full 64-square classify pass plus legal-move
matching -- there's no automatic full-frame rescan in this design, so an
unresolved settle is printed as flagged and left for manual correction
(the web UI, src/web_ui.py, is where that correction actually happens).
"""

import argparse
import datetime as dt
from pathlib import Path

from board_state import format_matrix, load_calibration
from capture import Camera, CaptureStream
from square_classifier import load_classifier
from tracking_loop import TrackingLoop

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CALIBRATION = REPO_ROOT / "config" / "calibration.json"
DEFAULT_CLASSIFIER = REPO_ROOT / "models" / "square_classifier_ncnn_model"


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--calibration", type=Path, default=DEFAULT_CALIBRATION)
    parser.add_argument("--classifier", type=Path, default=DEFAULT_CLASSIFIER, help="per-square classifier")
    parser.add_argument("--min-conf", type=float, default=0.7, help="classifier confidence threshold")
    parser.add_argument("--motion-thresh", type=float, default=None,
                        help="board-ROI motion threshold; tune with debug_classifier.py --watch")
    parser.add_argument(
        "--log", type=Path, default=None, help="optional path to append board-state changes to"
    )
    return parser.parse_args()


def main():
    args = parse_args()

    if not args.calibration.exists():
        raise SystemExit(f"No calibration found at {args.calibration} -- run calibrate.py first.")
    if not args.classifier.exists():
        raise SystemExit(
            f"No classifier model found at {args.classifier} -- see src/collect_square_crops.py "
            "and training/train_classifier.py to train/export one."
        )

    calibration_matrix = load_calibration(args.calibration)
    print("Loading classifier...")
    classifier_model = load_classifier(str(args.classifier))

    log_file = open(args.log, "a") if args.log else None

    def on_update(matrix, move_text, frame, flagged, reason):
        timestamp = dt.datetime.now().isoformat(timespec="seconds")
        header = f"FLAGGED ({reason}) -- please correct via the web UI" if flagged else f"move: {move_text}"
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
                classifier_model=classifier_model,
                on_update=on_update,
                classifier_min_conf=args.min_conf,
                motion_thresh=args.motion_thresh,
            )
            loop.run_forever()
    except KeyboardInterrupt:
        print("Stopped.")
    finally:
        if log_file:
            log_file.close()


if __name__ == "__main__":
    main()
