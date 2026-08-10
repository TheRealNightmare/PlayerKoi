"""Live web UI -- a chess.com-style 8x8 board diagram plus the annotated
camera feed, with plain-English move descriptions as pieces are moved.

Combines what live_view.py and main.py do separately into one page: the
raw camera feed with detection boxes (left/top), and a clean abstract
board rendered from the tracked, smoothed board_state matrix (right/
bottom). Whenever the smoothed state changes, it's diffed against the
previous state to describe what happened ("White Knight moves to A1",
"Black Bishop captures on D4") -- this is a physical-change description,
not a legal-move engine (no python-chess, no turn tracking).

    python3 src/web_ui.py                      # http://<this-pi>:8000/
    python3 src/web_ui.py --port 9000 --conf 0.25

Requires config/calibration.json (run calibrate.py first) and a trained,
NCNN-exported model (see training/NOTES.md).
"""

import argparse
import json
import time
from pathlib import Path
from threading import Lock, Thread

import cv2

from board_state import BOARD_SIZE, BoardStateTracker, load_calibration, square_name
from detect import detect, load_model
from live_view import annotate

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
    if (data.move_seq > lastMoveSeq && data.last_move) {
      lastMoveEl.textContent = data.last_move;
      logMove(data.last_move);
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


def _describe_piece(label):
    color, piece = label.split("-")
    return f"{color.capitalize()} {piece.capitalize()}"


def describe_move(old, new):
    """Diffs two stable board matrices into a human-readable description of
    what physically changed -- not a legal-move engine, just a description
    of which squares emptied and which squares gained a piece."""
    departures = []  # (square, label) squares that became empty
    arrivals = []  # (square, label, was_occupied_before)

    for rank_idx in range(BOARD_SIZE):
        for file_idx in range(BOARD_SIZE):
            old_label = old[rank_idx][file_idx]
            new_label = new[rank_idx][file_idx]
            if old_label == new_label:
                continue
            square = square_name(file_idx, rank_idx)
            if new_label is None:
                departures.append((square, old_label))
            else:
                arrivals.append((square, new_label, old_label is not None))

    messages = []
    for square, label, was_occupied in arrivals:
        piece_desc = _describe_piece(label)
        match_idx = next((i for i, (_, dep_label) in enumerate(departures) if dep_label == label), None)
        if match_idx is not None:
            departures.pop(match_idx)
            messages.append(f"{piece_desc} moves to {square.upper()}")
        elif was_occupied:
            messages.append(f"{piece_desc} captures on {square.upper()}")
        else:
            messages.append(f"{piece_desc} appears on {square.upper()}")

    return "; ".join(messages) if messages else None


class BoardBuffer:
    """Holds the latest board matrix, move description, and annotated JPEG
    frame for the HTTP handler to read -- one lock guards all three since
    they're updated together once per detection loop iteration."""

    def __init__(self):
        self._lock = Lock()
        self._matrix = None
        self._updated_at = 0.0
        self._last_move = None
        self._move_seq = 0
        self._jpeg = None

    def set_board(self, matrix):
        with self._lock:
            self._matrix = [row[:] for row in matrix]
            self._updated_at = time.time()

    def set_move(self, text):
        with self._lock:
            self._last_move = text
            self._move_seq += 1

    def set_frame(self, frame):
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if ok:
            with self._lock:
                self._jpeg = buf.tobytes()

    def get_board(self):
        with self._lock:
            return self._matrix, self._updated_at, self._last_move, self._move_seq

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
                matrix, updated_at, last_move, move_seq = buffer.get_board()
                rows = matrix if matrix is not None else [[None] * 8 for _ in range(8)]
                body = json.dumps(
                    {"matrix": rows, "updated_at": updated_at, "last_move": last_move, "move_seq": move_seq}
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

    previous_stable = None
    fps = 0.0
    last_tick = time.time()

    print("Running. Ctrl+C to stop.")
    try:
        with Camera() as cam:
            while True:
                frame = cam.read_frame()
                detections = detect(model, frame, conf=args.conf)
                matrix, changed = tracker.update(detections)

                if changed:
                    new_stable = [row[:] for row in matrix]
                    if previous_stable is not None:
                        move_text = describe_move(previous_stable, new_stable)
                        if move_text:
                            buffer.set_move(move_text)
                    previous_stable = new_stable

                buffer.set_board(matrix)

                now = time.time()
                fps = 1.0 / (now - last_tick) if now > last_tick else fps
                last_tick = now
                canvas = annotate(frame, detections, calibration_matrix, True, fps, args.conf)
                buffer.set_frame(canvas)

                time.sleep(args.interval)
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()
