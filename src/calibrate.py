"""One-time board calibration.

Captures a frame from the camera, then asks you to click the board's 4
outer corners in a fixed order (this also encodes board orientation):

    1. a1 corner (White's near-left)
    2. h1 corner (White's near-right)
    3. h8 corner (far-right, Black's side)
    4. a8 corner (far-left, Black's side)

From those 4 points it computes a perspective transform from image pixels
to board space (a 0..8 x 0..8 grid, one unit per square) and saves it to
config/calibration.json. Re-run this any time the camera or board moves.

Requires a display (HDMI or VNC) to click on the preview window.

    python3 src/calibrate.py
"""

import json
from pathlib import Path

import cv2
import numpy as np

from capture import Camera

REPO_ROOT = Path(__file__).resolve().parent.parent
CALIBRATION_PATH = REPO_ROOT / "config" / "calibration.json"
PREVIEW_PATH = REPO_ROOT / "config" / "calibration_preview.jpg"
REFERENCE_FRAME_PATH = REPO_ROOT / "config" / "reference_frame.jpg"

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


def save_preview(frame, matrix):
    scale = PREVIEW_PX / BOARD_SIZE
    scaled_matrix = np.diag([scale, scale, 1.0]) @ matrix
    warped = cv2.warpPerspective(frame, scaled_matrix, (PREVIEW_PX, PREVIEW_PX))

    for i in range(BOARD_SIZE + 1):
        pos = round(i * scale)
        cv2.line(warped, (pos, 0), (pos, PREVIEW_PX), (0, 255, 0), 1)
        cv2.line(warped, (0, pos), (PREVIEW_PX, pos), (0, 255, 0), 1)

    cv2.imwrite(str(PREVIEW_PATH), warped)
    return warped


def main():
    with Camera() as cam:
        frame = cam.read_frame()

    cv2.imwrite(str(REFERENCE_FRAME_PATH), frame)

    corners = collect_corners(frame)
    matrix = compute_transform(corners)
    save_preview(frame, matrix)

    calibration = {
        "corners_image_px": corners,
        "board_size_squares": BOARD_SIZE,
        "perspective_matrix": matrix.tolist(),
        "image_size": [frame.shape[1], frame.shape[0]],
    }
    CALIBRATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CALIBRATION_PATH, "w") as f:
        json.dump(calibration, f, indent=2)

    print(f"Saved calibration to {CALIBRATION_PATH}")
    print(f"Saved warped preview to {PREVIEW_PATH} -- check it looks like a clean 8x8 grid")


if __name__ == "__main__":
    main()
