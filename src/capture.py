"""Camera capture wrapper for the IMX219 CSI camera via picamera2.

Run directly for a smoke test: captures one frame and saves it to disk so you
can confirm the camera is working and the whole board is in frame.

    python3 src/capture.py [output_path]
"""

import sys
import time
from threading import Lock, Thread

from picamera2 import Picamera2

# How long to let auto-exposure/auto-white-balance converge before freezing
# them (see Camera._warm_up_and_lock). Every process that opens the camera
# converges independently -- without this, a frame grabbed right after
# start() can be mid-convergence, and a long-running process can see AE
# quietly drift mid-session (e.g. a hand briefly passing over the board
# changes the scene's average brightness). Keeping exposure/color
# consistent matters for square_classifier.py too: training photos
# (src/collect_square_crops.py) and live inference frames should look as
# similar as reasonably possible.
DEFAULT_WARMUP_S = 2.0


class Camera:
    def __init__(self, size=(1640, 1232), warmup_s=DEFAULT_WARMUP_S):
        self._picam2 = Picamera2()
        config = self._picam2.create_still_configuration(
            main={"size": size, "format": "RGB888"}
        )
        self._picam2.configure(config)
        self._warmup_s = warmup_s

    def open(self):
        self._picam2.start()
        self._warm_up_and_lock()

    def _warm_up_and_lock(self):
        """Lets AE/AWB converge to current lighting, then freezes them at
        whatever they converged to, so every frame from this point on in
        this process uses consistent exposure/color -- not still adjusting,
        and not silently drifting later in a long session."""
        deadline = time.monotonic() + self._warmup_s
        metadata = None
        while time.monotonic() < deadline:
            metadata = self._picam2.capture_metadata()  # blocks on a real frame, paces itself
        if metadata is None:
            return
        self._picam2.set_controls({
            "AeEnable": False,
            "AwbEnable": False,
            "ExposureTime": metadata["ExposureTime"],
            "AnalogueGain": metadata["AnalogueGain"],
            "ColourGains": metadata["ColourGains"],
        })

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


class CaptureStream:
    """Runs Camera.read_frame() on a background thread so callers never
    block on camera I/O -- get_latest() always returns immediately with
    whatever frame is newest. This lets a fast, cheap motion-gate poll run
    at its own pace instead of being throttled to the camera's own
    capture latency, and lets slower work (classification, a full YOLO
    rescan) happen on a separately-paced cadence without stalling capture.
    """

    def __init__(self, camera):
        self._camera = camera
        self._lock = Lock()
        self._frame = None
        self._timestamp = 0.0
        self._running = False
        self._thread = None

    def start(self):
        self._running = True
        self._thread = Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def _run(self):
        while self._running:
            frame = self._camera.read_frame()
            with self._lock:
                self._frame = frame
                self._timestamp = time.monotonic()

    def get_latest(self):
        """Returns (frame, timestamp) for the most recently captured frame,
        or (None, 0.0) if nothing has been captured yet."""
        with self._lock:
            return self._frame, self._timestamp

    def stop(self):
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc_info):
        self.stop()


if __name__ == "__main__":
    import cv2

    out_path = sys.argv[1] if len(sys.argv) > 1 else "capture_test.jpg"

    with Camera() as cam:
        frame = cam.read_frame()

    cv2.imwrite(out_path, frame)
    print(f"Saved {frame.shape[1]}x{frame.shape[0]} frame to {out_path}")
