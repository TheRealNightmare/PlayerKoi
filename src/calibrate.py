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

It then walks through two more capture steps to build the baseline that
src/occupancy_color.py's classical-CV occupancy/color reader needs at
runtime (see that module's docstring for why vision only needs to read
empty/white/black per square, not full piece type): clear the board for an
empty-square color reference, then set up the standard starting position
for a white/black piece-color reference. Both baselines, the derived
occupancy/foreground decision thresholds, and an averaged empty-board
reference image (used to background-subtract piece-color reads -- see
occupancy_color.py) are saved alongside calibration.json.

Re-run this any time the camera, board, lighting, or piece set changes.

Requires a display (HDMI or VNC) to click on the preview window.

    python3 src/calibrate.py
"""

import json
import time
from pathlib import Path

import cv2
import numpy as np

from capture import Camera
from move_resolver import standard_starting_matrix
from occupancy_color import EMPTY_BOARD_REFERENCE_FILENAME, compute_baselines
from square_geometry import square_pixel_bboxes

REPO_ROOT = Path(__file__).resolve().parent.parent
CALIBRATION_PATH = REPO_ROOT / "config" / "calibration.json"
PREVIEW_PATH = REPO_ROOT / "config" / "calibration_preview.jpg"
REFERENCE_FRAME_PATH = REPO_ROOT / "config" / "reference_frame.jpg"
EMPTY_BOARD_REFERENCE_PATH = REPO_ROOT / "config" / EMPTY_BOARD_REFERENCE_FILENAME

CORNER_LABELS = ["a1 corner", "h1 corner", "h8 corner", "a8 corner"]
BOARD_SIZE = 8  # squares per side
PREVIEW_PX = 800  # pixels per side of the warped preview
# Spread over several seconds (not ~1s) so the calibrated noise/threshold
# statistics reflect realistic real-world variation (shot noise, minor
# vibration) rather than an artificially quiet short window -- see
# debug_occupancy.py and capture.py's exposure warm-up/lock for the related
# real-hardware tuning this was built to address.
BASELINE_BURST_FRAMES = 24
BASELINE_BURST_INTERVAL_S = 0.2


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


def capture_burst(cam, num_frames=BASELINE_BURST_FRAMES, interval_s=BASELINE_BURST_INTERVAL_S):
    frames = []
    for i in range(num_frames):
        if i:
            time.sleep(interval_s)
        frames.append(cam.read_frame())
    return frames


def main():
    with Camera() as cam:
        frame = cam.read_frame()
        cv2.imwrite(str(REFERENCE_FRAME_PATH), frame)

        corners = collect_corners(frame)
        matrix = compute_transform(corners)
        save_preview(frame, matrix)

        image_size = (frame.shape[1], frame.shape[0])
        footprint_bboxes = square_pixel_bboxes(matrix, image_size, margin_up_px=0)
        square_bboxes = square_pixel_bboxes(matrix, image_size)

        input("\nClear the board completely, then press Enter...")
        empty_frames = capture_burst(cam)

        input("Set up the standard starting position (all 32 pieces), then press Enter...")
        start_frames = capture_burst(cam)

    (
        empty_baseline,
        piece_color_baseline,
        occupancy_diff_threshold,
        foreground_pixel_diff_threshold,
        background_image,
        warning,
    ) = compute_baselines(empty_frames, start_frames, footprint_bboxes, square_bboxes, standard_starting_matrix())

    if warning:
        print(f"\nWARNING: {warning}\n")

    CALIBRATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(EMPTY_BOARD_REFERENCE_PATH), background_image)

    calibration = {
        "corners_image_px": corners,
        "board_size_squares": BOARD_SIZE,
        "perspective_matrix": matrix.tolist(),
        "image_size": [frame.shape[1], frame.shape[0]],
        "empty_baseline": empty_baseline,
        "piece_color_baseline": piece_color_baseline,
        "occupancy_diff_threshold": occupancy_diff_threshold,
        "foreground_pixel_diff_threshold": foreground_pixel_diff_threshold,
    }
    with open(CALIBRATION_PATH, "w") as f:
        json.dump(calibration, f, indent=2)

    print(f"Saved calibration to {CALIBRATION_PATH}")
    print(f"Saved empty-board reference to {EMPTY_BOARD_REFERENCE_PATH}")
    print(f"Saved warped preview to {PREVIEW_PATH} -- check it looks like a clean 8x8 grid")


if __name__ == "__main__":
    main()
