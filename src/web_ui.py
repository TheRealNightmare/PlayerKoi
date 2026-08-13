"""Live web UI -- a chess.com-style 8x8 board diagram plus the live camera
feed, with move descriptions in real algebraic notation as pieces are moved.

The board diagram is driven by src/tracking_loop.py's event-gated
diff/classify/legal-move pipeline (see that module's docstring) rather
than a fixed-interval full-frame detection loop: the camera feed updates
continuously and cheaply, but the board only re-evaluates when something
has actually settled. Moves are real algebraic notation (SAN) via
python-chess (src/move_resolver.py), not just a physical before/after
description.

    python3 src/web_ui.py                      # http://<this-pi>:8000/
    python3 src/web_ui.py --port 9000 --conf 0.25

Requires config/calibration.json (run calibrate.py first), a trained,
NCNN-exported detector (see training/NOTES.md) for the full-frame fallback
path, and a trained, NCNN-exported per-square classifier (see
training/prepare_square_crops.py and training/train_classifier.py) for the
fast routine per-move path.
"""

import argparse
import json
import time
from pathlib import Path
from threading import Lock, Thread

import cv2

from board_state import load_calibration
from capture import Camera, CaptureStream
from detect import load_model
from square_classifier import load_classifier
from tracking_loop import TrackingLoop

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CALIBRATION = REPO_ROOT / "config" / "calibration.json"
DEFAULT_MODEL = REPO_ROOT / "models" / "best_ncnn_model"
DEFAULT_CLASSIFIER = REPO_ROOT / "models" / "square_classifier_ncnn_model"

PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MicroChess</title>
<style>
  :root { color-scheme: dark; }
  body {
    margin: 0; min-height: 100vh; display: flex; flex-wrap: wrap;
    align-items: flex-start; justify-content: center; gap: 24px; padding: 24px;
    background: #1a1a1a; font-family: -apple-system, Helvetica, Arial, sans-serif;
    box-sizing: border-box;
  }
  .col { display: flex; flex-direction: column; align-items: center; gap: 12px; }
  #status { color: #888; font-size: 14px; }
  #status.stale { color: #d9534f; }
  #board {
    display: grid; grid-template-columns: repeat(8, min(11vw, 64px));
    grid-template-rows: repeat(8, min(11vw, 64px));
    border: 3px solid #3a2a1a; box-shadow: 0 8px 30px rgba(0,0,0,0.5);
  }
  .sq { display: flex; align-items: center; justify-content: center;
        font-size: min(8vw, 46px); user-select: none; line-height: 1; }
  .light { background: #eeeed2; }
  .dark  { background: #769656; }
  .white-piece { color: #fff; text-shadow: 0 0 2px #000, 0 1px 3px rgba(0,0,0,.6); }
  .black-piece { color: #111; }
  #stream { max-width: min(90vw, 640px); border-radius: 6px; box-shadow: 0 8px 30px rgba(0,0,0,0.5); }
  #lastMove { color: #eee; font-size: 18px; min-height: 24px; }
  #moveLog { color: #999; font-size: 13px; max-width: 320px; text-align: center; max-height: 140px; overflow-y: auto; }
  #moveLog div { padding: 2px 0; }
</style>
</head>
<body>
  <div class="col">
    <div id="board"></div>
    <div id="lastMove"></div>
    <div id="moveLog"></div>
    <div id="status">connecting...</div>
  </div>
  <div class="col">
    <img id="stream" src="/stream.mjpg">
  </div>
<script>
const GLYPHS = {
  "white-king": "\\u2654", "white-queen": "\\u2655", "white-rook": "\\u2656",
  "white-bishop": "\\u2657", "white-knight": "\\u2658", "white-pawn": "\\u2659",
  "black-king": "\\u265A", "black-queen": "\\u265B", "black-rook": "\\u265C",
  "black-bishop": "\\u265D", "black-knight": "\\u265E", "black-pawn": "\\u265F",
};

const boardEl = document.getElementById("board");
const cells = [];
for (let rank = 7; rank >= 0; rank--) {
  for (let file = 0; file < 8; file++) {
    const cell = document.createElement("div");
    cell.className = "sq " + ((rank + file) % 2 === 0 ? "dark" : "light");
    boardEl.appendChild(cell);
    cells.push({ el: cell, rank, file });
  }
}

function render(matrix) {
  for (const { el, rank, file } of cells) {
    const label = matrix[rank][file];
    if (!label) { el.textContent = ""; el.className = el.className.replace(/ (white|black)-piece/, ""); continue; }
    el.textContent = GLYPHS[label] || "?";
    const colorClass = label.startsWith("white") ? "white-piece" : "black-piece";
    el.className = el.className.replace(/ (white|black)-piece/, "") + " " + colorClass;
  }
}

const statusEl = document.getElementById("status");
const lastMoveEl = document.getElementById("lastMove");
const moveLogEl = document.getElementById("moveLog");
let lastOk = Date.now();
let lastMoveSeq = 0;

function logMove(text) {
  const line = document.createElement("div");
  line.textContent = text;
  moveLogEl.prepend(line);
  while (moveLogEl.children.length > 8) moveLogEl.removeChild(moveLogEl.lastChild);
}

async function poll() {
  try {
    const res = await fetch("/board.json", { cache: "no-store" });
    const data = await res.json();
    render(data.matrix);
    if (data.move_seq > lastMoveSeq) {
      if (data.last_move) {
        lastMoveEl.textContent = data.last_move;
        logMove(data.last_move);
      } else if (data.flagged) {
        lastMoveEl.textContent = "board re-synced -- please verify the position";
        logMove("(flagged: no legal move matched, even after a rescan)");
      }
    }
    lastMoveSeq = data.move_seq;
    lastOk = Date.now();
    statusEl.textContent = "live -- updated " + new Date(data.updated_at * 1000).toLocaleTimeString();
    statusEl.classList.remove("stale");
  } catch (e) {
    statusEl.textContent = "connection lost, retrying...";
  }
  if (Date.now() - lastOk > 5000) statusEl.classList.add("stale");
  setTimeout(poll, 500);
}
poll();
</script>
</body>
</html>
"""


class BoardBuffer:
    """Holds the latest board matrix, move text, flagged status, and JPEG
    frame for the HTTP handler to read -- one lock guards the board fields
    since tracking_loop's on_update sets them together; the frame is set
    separately and far more often (every capture tick, for a live feed)."""

    def __init__(self):
        self._lock = Lock()
        self._matrix = None
        self._updated_at = 0.0
        self._last_move = None
        self._move_seq = 0
        self._flagged = False
        self._jpeg = None

    def set_board(self, matrix, move_text, flagged):
        with self._lock:
            self._matrix = [row[:] for row in matrix]
            self._updated_at = time.time()
            self._last_move = move_text
            self._flagged = flagged
            self._move_seq += 1

    def set_frame(self, frame):
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if ok:
            with self._lock:
                self._jpeg = buf.tobytes()

    def get_board(self):
        with self._lock:
            return self._matrix, self._updated_at, self._last_move, self._move_seq, self._flagged

    def get_frame(self):
        with self._lock:
            return self._jpeg


def start_server(host, port, buffer):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/":
                body = PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/board.json":
                matrix, updated_at, last_move, move_seq, flagged = buffer.get_board()
                rows = matrix if matrix is not None else [[None] * 8 for _ in range(8)]
                body = json.dumps(
                    {
                        "matrix": rows,
                        "updated_at": updated_at,
                        "last_move": last_move,
                        "move_seq": move_seq,
                        "flagged": flagged,
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/stream.mjpg":
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.end_headers()
                try:
                    while True:
                        jpeg = buffer.get_frame()
                        if jpeg is None:
                            time.sleep(0.05)
                            continue
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n")
                        self.wfile.write(jpeg)
                        self.wfile.write(b"\r\n")
                        time.sleep(0.03)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # viewer closed the tab
            else:
                self.send_error(404)

        def log_message(self, *args):
            pass  # keep the terminal clean

    server = ThreadingHTTPServer((host, port), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    return server


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--poll-interval", type=float, default=0.12,
                        help="seconds between cheap motion-gate polls (not a detection interval)")
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL, help="full-frame fallback detector")
    parser.add_argument("--classifier", type=Path, default=DEFAULT_CLASSIFIER, help="per-square classifier")
    parser.add_argument("--calibration", type=Path, default=DEFAULT_CALIBRATION)
    parser.add_argument("--conf", type=float, default=0.4, help="fallback detector confidence threshold")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="0.0.0.0")
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

    buffer = BoardBuffer()
    start_server(args.host, args.port, buffer)
    print(f"Serving at http://<this-pi>:{args.port}/")

    def on_update(matrix, move_text, frame, flagged):
        buffer.set_board(matrix, move_text, flagged)

    print("Running. Ctrl+C to stop.")
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
                poll_interval=args.poll_interval,
                fallback_conf=args.conf,
            )
            buffer.set_board(loop.current_matrix, None, False)  # seed the UI before any move happens

            while True:
                live_frame, _timestamp = stream.get_latest()
                if live_frame is not None:
                    buffer.set_frame(live_frame)
                loop.tick()
                time.sleep(args.poll_interval)
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()
