"""Camera capture wrapper for the IMX219 CSI camera via picamera2.

Run directly for a smoke test: captures one frame and saves it to disk so you
can confirm the camera is working and the whole board is in frame.

    python3 src/capture.py [output_path]
"""

import sys

from picamera2 import Picamera2


class Camera:
    def __init__(self, size=(1640, 1232)):
        self._picam2 = Picamera2()
        config = self._picam2.create_still_configuration(
            main={"size": size, "format": "RGB888"}
        )
        self._picam2.configure(config)

    def open(self):
        self._picam2.start()

    def read_frame(self):
        """Returns an HxWx3 numpy array in BGR order (OpenCV convention)."""
        frame = self._picam2.capture_array()
        return frame[:, :, ::-1]  # RGB888 -> BGR

    def close(self):
        self._picam2.stop()

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc_info):
        self.close()


if __name__ == "__main__":
    import cv2

    out_path = sys.argv[1] if len(sys.argv) > 1 else "capture_test.jpg"

    with Camera() as cam:
        frame = cam.read_frame()

    cv2.imwrite(out_path, frame)
    print(f"Saved {frame.shape[1]}x{frame.shape[0]} frame to {out_path}")
