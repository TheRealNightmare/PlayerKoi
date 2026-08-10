"""Live web UI -- a chess.com-style 8x8 board diagram driven by detection.

Unlike live_view.py (which shows the raw camera feed with boxes overlaid),
this renders a clean abstract board from the tracked board_state matrix:
no camera image, just squares + Unicode piece glyphs, updating as the
tracker's stable state changes.

    python3 src/web_ui.py                      # http://<this-pi>:8000/
    python3 src/web_ui.py --port 9000 --conf 0.35

Requires config/calibration.json (run calibrate.py first) and a trained,
NCNN-exported model (see training/NOTES.md).
"""

import argparse
import json
import time
from pathlib import Path
from threading import Lock, Thread

from board_state import BoardStateTracker, load_calibration
from detect import detect, load_model

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CALIBRATION = REPO_ROOT / "config" / "calibration.json"
DEFAULT_MODEL = REPO_ROOT / "models" / "best_ncnn_model"

PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MicroChess</title>
<style>
  :root { color-scheme: dark; }
  body {
    margin: 0; min-height: 100vh; display: flex; flex-direction: column;
    align-items: center; justify-content: center; gap: 12px;
    background: #1a1a1a; font-family: -apple-system, Helvetica, Arial, sans-serif;
  }
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
  #labels { display: flex; gap: 4px; color: #666; font-size: 12px; }
</style>
</head>
<body>
  <div id="board"></div>
  <div id="status">connecting...</div>
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
let lastOk = Date.now();

async function poll() {
  try {
    const res = await fetch("/board.json", { cache: "no-store" });
    const data = await res.json();
    render(data.matrix);
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
    """Holds the most recent stable board matrix for the HTTP handler to read."""

    def __init__(self):
        self._lock = Lock()
        self._matrix = None
        self._updated_at = 0.0

    def set(self, matrix):
        with self._lock:
            self._matrix = matrix
            self._updated_at = time.time()

    def get(self):
        with self._lock:
            return self._matrix, self._updated_at


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
                matrix, updated_at = buffer.get()
                rows = matrix if matrix is not None else [[None] * 8 for _ in range(8)]
                body = json.dumps({"matrix": rows, "updated_at": updated_at}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404)

        def log_message(self, *args):
            pass  # keep the terminal clean

    server = ThreadingHTTPServer((host, port), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    return server


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--interval", type=float, default=0.75, help="seconds between frames")
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--calibration", type=Path, default=DEFAULT_CALIBRATION)
    parser.add_argument("--conf", type=float, default=0.4, help="detection confidence threshold")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="0.0.0.0")
    return parser.parse_args()


def main():
    args = parse_args()

    if not args.calibration.exists():
        raise SystemExit(f"No calibration found at {args.calibration} -- run calibrate.py first.")
    if not args.model.exists():
        raise SystemExit(f"No model found at {args.model} -- see training/NOTES.md to train/export one.")

    calibration_matrix = load_calibration(args.calibration)
    print("Loading model...")
    model = load_model(str(args.model))
    tracker = BoardStateTracker(calibration_matrix)

    buffer = BoardBuffer()
    start_server(args.host, args.port, buffer)
    print(f"Serving at http://<this-pi>:{args.port}/")

    from capture import Camera

    print("Running. Ctrl+C to stop.")
    try:
        with Camera() as cam:
            while True:
                frame = cam.read_frame()
                detections = detect(model, frame, conf=args.conf)
                matrix, _ = tracker.update(detections)
                buffer.set(matrix)
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()
