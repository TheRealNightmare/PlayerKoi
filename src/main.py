"""Main loop: capture -> detect -> track board state -> print/log on change.

    python3 src/main.py [--interval 0.75] [--model models/best_ncnn_model] [--log board.log]

Requires config/calibration.json (run calibrate.py first) and a trained,
NCNN-exported model (see training/NOTES.md).
"""

import argparse
import datetime as dt
import time
from pathlib import Path

from board_state import BoardStateTracker, format_matrix, load_calibration
from capture import Camera
from detect import detect, load_model

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CALIBRATION = REPO_ROOT / "config" / "calibration.json"
DEFAULT_MODEL = REPO_ROOT / "models" / "best_ncnn_model"


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=float, default=0.75, help="seconds between frames")
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--calibration", type=Path, default=DEFAULT_CALIBRATION)
    parser.add_argument("--conf", type=float, default=0.4, help="detection confidence threshold")
    parser.add_argument(
        "--log", type=Path, default=None, help="optional path to append board-state changes to"
    )
    return parser.parse_args()


def main():
    args = parse_args()

    if not args.calibration.exists():
        raise SystemExit(f"No calibration found at {args.calibration} -- run calibrate.py first.")
    if not args.model.exists():
        raise SystemExit(f"No model found at {args.model} -- see training/NOTES.md to train/export one.")

    calibration_matrix = load_calibration(args.calibration)
    model = load_model(str(args.model))
    tracker = BoardStateTracker(calibration_matrix)

    log_file = open(args.log, "a") if args.log else None

    print("Starting board tracking. Press Ctrl+C to stop.")
    try:
        with Camera() as cam:
            while True:
                frame = cam.read_frame()
                detections = detect(model, frame, conf=args.conf)
                matrix, changed = tracker.update(detections)

                if changed:
                    timestamp = dt.datetime.now().isoformat(timespec="seconds")
                    output = f"[{timestamp}] Board state changed:\n{format_matrix(matrix)}\n"
                    print(output)
                    if log_file:
                        log_file.write(output + "\n")
                        log_file.flush()

                time.sleep(args.interval)
    except KeyboardInterrupt:
        print("Stopped.")
    finally:
        if log_file:
            log_file.close()


if __name__ == "__main__":
    main()
