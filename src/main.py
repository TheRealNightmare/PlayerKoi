"""Main loop: capture -> event-gated occupancy-read/delta/legal-move tracking -> print/log on change.

    python3 src/main.py [--log board.log]

Requires config/calibration.json (run calibrate.py first) including the
occupancy/color baseline captured by its empty-board and starting-position
steps. See src/tracking_loop.py for how a settle is resolved: a full
64-square classical-CV occupancy/color read (no model, see
src/occupancy_color.py) plus legal-move matching -- there's no automatic
full-frame rescan in this design, so an unresolved settle is printed as
flagged and left for manual correction (the web UI, src/web_ui.py, is where
that correction actually happens).
"""

import argparse
import datetime as dt
import json
from pathlib import Path

from board_state import format_matrix, load_calibration
from capture import Camera, CaptureStream
from occupancy_color import load_baseline
from tracking_loop import TrackingLoop

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CALIBRATION = REPO_ROOT / "config" / "calibration.json"


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--calibration", type=Path, default=DEFAULT_CALIBRATION)
    parser.add_argument(
        "--log", type=Path, default=None, help="optional path to append board-state changes to"
    )
    return parser.parse_args()


def main():
    args = parse_args()

    if not args.calibration.exists():
        raise SystemExit(f"No calibration found at {args.calibration} -- run calibrate.py first.")

    with open(args.calibration) as f:
        calibration = json.load(f)
    if "empty_baseline" not in calibration or "foreground_pixel_diff_threshold" not in calibration:
        raise SystemExit(
            f"{args.calibration} has no occupancy/color baseline -- re-run calibrate.py "
            "(it now captures an empty-board and starting-position reference, and saves a "
            "background-subtraction reference image, in addition to corners)."
        )

    calibration_matrix = load_calibration(args.calibration)
    occupancy_baseline = load_baseline(calibration, args.calibration)

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
                occupancy_baseline=occupancy_baseline,
                on_update=on_update,
            )
            loop.run_forever()
    except KeyboardInterrupt:
        print("Stopped.")
    finally:
        if log_file:
            log_file.close()


if __name__ == "__main__":
    main()
