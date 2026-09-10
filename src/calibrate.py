"""One-time board calibration.

Captures a frame from the camera, then asks you to click the board's 4
outer corners in a fixed order (this also encodes board orientation):

    1. a1 corner (White's near-left)
    2. h1 corner (White's near-right)
    3. h8 corner (far-right, Black's side)
    4. a8 corner (far-left, Black's side)

From those 4 points it computes a perspective transform from image pixels
to board space (a 0..8 x 0..8 grid, one unit per square) and saves it to
config/calibration.json.

That's all this needs -- occupancy/color detection is a trained classifier
(src/square_classifier.py) rather than hand-derived pixel statistics, so
there's no baseline-capture step here. See src/collect_square_crops.py to
gather training data using this same calibration.

Re-run this any time the camera or board physically moves.

Each environment (different room, lighting, camera height, board) needs its
own calibration, so pass --env to keep them side by side instead of
overwriting one another:

    python3 src/calibrate.py                 # config/calibration.json
    python3 src/calibrate.py --env 2         # config/calibration-env2.json

Requires a display (HDMI or VNC) to click on the preview window.
"""

import argparse
import json

import cv2
import numpy as np

from board_state import CONFIG_DIR, calibration_paths
from capture import Camera

CORNER_LABELS = ["a1 corner", "h1 corner", "h8 corner", "a8 corner"]
BOARD_SIZE = 8  # squares per side
PREVIEW_PX = 800  # pixels per side of the warped preview


def collect_corners(frame):
    points = []
    window = "Calibration - click corners in order, then press any key"
    display = frame.copy()

    def on_click(event, x, y, flags, userdata):
        if event == cv2.EVENT_LBUTTONDOWN and len(points) < 4:
            points.append((x, y))
            cv2.circle(display, (x, y), 6, (0, 0, 255), -1)
            cv2.putText(
                display,
                CORNER_LABELS[len(points) - 1],
                (x + 10, y),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                (0, 0, 255),
                2,
            )
            cv2.imshow(window, display)

    cv2.namedWindow(window)
    cv2.setMouseCallback(window, on_click)
    cv2.imshow(window, display)

    print("Click the following corners in order, then press any key:")
    for label in CORNER_LABELS:
        print(f"  - {label}")

    while len(points) < 4:
        cv2.waitKey(50)

    cv2.waitKey(0)
    cv2.destroyWindow(window)
    return points


def compute_transform(corners):
    src = np.array(corners, dtype=np.float32)
    dst = np.array(
        [[0, 0], [BOARD_SIZE, 0], [BOARD_SIZE, BOARD_SIZE], [0, BOARD_SIZE]],
        dtype=np.float32,
    )
    return cv2.getPerspectiveTransform(src, dst)


def save_preview(frame, matrix, preview_path):
    scale = PREVIEW_PX / BOARD_SIZE
    scaled_matrix = np.diag([scale, scale, 1.0]) @ matrix
    warped = cv2.warpPerspective(frame, scaled_matrix, (PREVIEW_PX, PREVIEW_PX))

    for i in range(BOARD_SIZE + 1):
        pos = round(i * scale)
        cv2.line(warped, (pos, 0), (pos, PREVIEW_PX), (0, 255, 0), 1)
        cv2.line(warped, (0, pos), (PREVIEW_PX, pos), (0, 255, 0), 1)

    cv2.imwrite(str(preview_path), warped)
    return warped


def parse_args():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--env", default=None,
                        help="environment tag (e.g. 1, 2, kitchen). Each environment -- "
                             "different room, lighting, camera height or board -- needs its "
                             "own calibration. Omit for the default config/calibration.json")
    parser.add_argument("--force", action="store_true",
                        help="overwrite an existing calibration for this environment")
    return parser.parse_args()


def main():
    args = parse_args()
    calibration_path, preview_path, reference_path = calibration_paths(args.env)

    # Camera position is one of the things that varies per environment, so an
    # accidental re-run used to silently destroy another environment's board
    # geometry.
    if calibration_path.exists() and not args.force:
        raise SystemExit(
            f"{calibration_path} already exists -- pass --force to overwrite it, "
            "or use a different --env tag."
        )

    with Camera() as cam:
        frame = cam.read_frame()

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(reference_path), frame)

    corners = collect_corners(frame)
    matrix = compute_transform(corners)
    save_preview(frame, matrix, preview_path)

    calibration = {
        "env": args.env,
        "corners_image_px": corners,
        "board_size_squares": BOARD_SIZE,
        "perspective_matrix": matrix.tolist(),
        "image_size": [frame.shape[1], frame.shape[0]],
    }
    with open(calibration_path, "w") as f:
        json.dump(calibration, f, indent=2)

    print(f"Saved calibration to {calibration_path}")
    print(f"Saved warped preview to {preview_path} -- check it looks like a clean 8x8 grid")
    if args.env is not None:
        print(f"\nNext: python3 src/collect_square_crops.py --env {args.env}")


if __name__ == "__main__":
    main()
