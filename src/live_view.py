"""Live annotated camera view -- watch detection happen in real time.

Shows the camera feed with detection boxes, labels, confidences, the calibrated
board grid, and a live FPS/among-detections readout. Use it to aim the camera,
sanity-check calibration, and tune --conf interactively before committing a
value to main.py.

    python3 src/live_view.py                 # window on the Pi's display
    python3 src/live_view.py --stream 8000   # also serve to a browser
    python3 src/live_view.py --no-window --stream 8000   # headless / over SSH

With --stream, open http://<pi-host>:8000/ from any machine on the network.

Keys (window mode):
    q / Esc  quit                 s  save a snapshot
    + / -    adjust confidence    g  toggle the board grid
    b        print the board matrix to the terminal

Runs capture + full-frame detection back-to-back with no throttle, by
design -- this is a debug/aiming tool where continuous full detection is
the point, not a bug. It is intentionally NOT the same code path as
production: see src/tracking_loop.py, which only runs full detection on
an event-gated fallback rather than every frame.
"""

import argparse
import time
from collections import deque
from pathlib import Path
from threading import Lock, Thread

import cv2

from board_state import BoardStateTracker, format_matrix, load_calibration
from debug_detect import draw_grid
from detect import detect, load_model

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CALIBRATION = REPO_ROOT / "config" / "calibration.json"
DEFAULT_MODEL = REPO_ROOT / "models" / "best_ncnn_model"

WINDOW = "MicroChess live -- q quit, +/- conf, g grid, s snapshot, b board"


class FrameBuffer:
    """Holds the most recent encoded JPEG for the HTTP streamer."""

    def __init__(self):
        self._lock = Lock()
        self._jpeg = None

    def set(self, frame):
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if ok:
            with self._lock:
                self._jpeg = buf.tobytes()

    def get(self):
        with self._lock:
            return self._jpeg


def start_stream_server(port, buffer):
    """Serve the annotated feed as MJPEG so a browser on the network can watch."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ("/", "/index.html"):
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            try:
                while True:
                    jpeg = buffer.get()
                    if jpeg is None:
                        time.sleep(0.05)
                        continue
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n")
                    self.wfile.write(jpeg)
                    self.wfile.write(b"\r\n")
                    time.sleep(0.03)
            except (BrokenPipeError, ConnectionResetError):
                pass  # viewer closed the tab

        def log_message(self, *args):
            pass  # keep the terminal clean

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    return server


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--conf", type=float, default=0.25, help="starting confidence threshold")
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--calibration", type=Path, default=DEFAULT_CALIBRATION)
    parser.add_argument("--scale", type=float, default=0.6, help="display scale factor")
    parser.add_argument("--stream", type=int, default=None, metavar="PORT",
                        help="also serve the feed over HTTP on this port")
    parser.add_argument("--no-window", action="store_true",
                        help="don't open a local window (use with --stream over SSH)")
    return parser.parse_args()


def annotate(frame, detections, matrix, show_grid, fps, conf):
    canvas = frame.copy()
    if matrix is not None and show_grid:
        draw_grid(canvas, matrix)

    for det in detections:
        x1, y1, x2, y2 = (int(v) for v in det["bbox"])
        colour = (255, 128, 0) if det["class"].startswith("white") else (0, 128, 255)
        cv2.rectangle(canvas, (x1, y1), (x2, y2), colour, 2)
        cv2.circle(canvas, ((x1 + x2) // 2, y2), 4, (0, 0, 255), -1)
        label = f"{det['class']} {det['confidence']:.2f}"
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.4, 1)
        cv2.rectangle(canvas, (x1, max(y1 - th - 4, 0)), (x1 + tw, max(y1, th)), colour, -1)
        cv2.putText(canvas, label, (x1, max(y1 - 3, th)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 255), 1)

    banner = f"{len(detections)} pieces | conf {conf:.2f} | {fps:.1f} FPS"
    cv2.rectangle(canvas, (0, 0), (canvas.shape[1], 30), (0, 0, 0), -1)
    cv2.putText(canvas, banner, (8, 21), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
    return canvas


def main():
    args = parse_args()
    if not args.model.exists():
        raise SystemExit(f"No model at {args.model} -- see training/NOTES.md.")

    matrix = None
    if args.calibration.exists():
        matrix = load_calibration(args.calibration)
    else:
        print(f"WARNING: no calibration at {args.calibration} -- no grid/board readout.")

    print("Loading model...")
    model = load_model(str(args.model))
    tracker = BoardStateTracker(matrix) if matrix is not None else None

    buffer = FrameBuffer()
    if args.stream:
        start_stream_server(args.stream, buffer)
        print(f"Streaming at http://<this-pi>:{args.stream}/")
    if args.no_window and not args.stream:
        raise SystemExit("--no-window without --stream would show nothing.")

    from capture import Camera

    conf = args.conf
    show_grid = True
    times = deque(maxlen=15)
    snapshots = 0
    print("Running. Ctrl+C to stop." if args.no_window else "Running. Press q in the window to quit.")

    try:
        with Camera() as cam:
            while True:
                start = time.time()
                frame = cam.read_frame()
                detections = detect(model, frame, conf=conf)
                if tracker is not None:
                    tracker.update(detections)

                times.append(time.time() - start)
                fps = len(times) / sum(times) if sum(times) else 0.0
                canvas = annotate(frame, detections, matrix, show_grid, fps, conf)

                if args.scale != 1.0:
                    canvas = cv2.resize(canvas, None, fx=args.scale, fy=args.scale)
                if args.stream:
                    buffer.set(canvas)

                if args.no_window:
                    continue

                cv2.imshow(WINDOW, canvas)
                key = cv2.waitKey(1) & 0xFF
                if key in (ord("q"), 27):
                    break
                elif key in (ord("+"), ord("=")):
                    conf = min(conf + 0.05, 0.95)
                    print(f"conf -> {conf:.2f}")
                elif key in (ord("-"), ord("_")):
                    conf = max(conf - 0.05, 0.01)
                    print(f"conf -> {conf:.2f}")
                elif key == ord("g"):
                    show_grid = not show_grid
                elif key == ord("s"):
                    snapshots += 1
                    path = REPO_ROOT / f"live_snapshot_{snapshots}.jpg"
                    cv2.imwrite(str(path), canvas)
                    print(f"saved {path}")
                elif key == ord("b") and tracker is not None:
                    board, _ = tracker.update(detections)
                    print(format_matrix(board))
    except KeyboardInterrupt:
        print("\nStopped.")
    except cv2.error as exc:
        raise SystemExit(
            f"OpenCV display failed ({exc.err.strip() if exc.err else exc}).\n"
            "No display available? Re-run headless, e.g.:\n"
            f"  python3 src/live_view.py --no-window --stream 8000"
        )
    finally:
        if not args.no_window:
            cv2.destroyAllWindows()

    print(f"Final confidence: {conf:.2f}  ->  python3 src/main.py --conf {conf:.2f}")


if __name__ == "__main__":
    main()
