"""Live web UI -- a chess.com-style 8x8 board diagram plus the live camera
feed, with move descriptions in real algebraic notation as pieces are moved.

The board diagram is driven by src/tracking_loop.py's event-gated
occupancy-read/delta/legal-move pipeline (see that module's docstring)
rather than a fixed-interval full-frame detection loop: the camera feed
updates continuously and cheaply, but the board only re-evaluates when
something has actually settled. Moves are real algebraic notation (SAN) via
python-chess (src/move_resolver.py), not just a physical before/after
description.

There is no automatic rescan in this design -- when a settle can't be
resolved with confidence, the UI flags it and offers a manual "Fix board"
correction affordance instead (POST /board/correct).

    python3 src/web_ui.py                      # http://<this-pi>:8000/
    python3 src/web_ui.py --port 9000

Requires config/calibration.json (run calibrate.py first) and a trained,
NCNN-exported empty/white/black classifier (see src/collect_square_crops.py
and training/train_classifier.py).
"""

import argparse
import json
import time
from pathlib import Path
from threading import Lock, Thread

import chess
import cv2

from board_state import load_calibration, matrix_to_fen_placement
from capture import Camera, CaptureStream
from square_classifier import load_classifier
from tracking_loop import TrackingLoop

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CALIBRATION = REPO_ROOT / "config" / "calibration.json"
DEFAULT_CLASSIFIER = REPO_ROOT / "models" / "square_classifier_ncnn_model"

_VALID_LABELS = {
    f"{color}-{piece}"
    for color in ("white", "black")
    for piece in ("king", "queen", "rook", "bishop", "knight", "pawn")
}

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
    position: relative;
  }
  .sq { display: flex; align-items: center; justify-content: center;
        font-size: min(8vw, 46px); user-select: none; line-height: 1; position: relative; }
  .light { background: #eeeed2; }
  .dark  { background: #769656; }
  .white-piece { color: #fff; text-shadow: 0 0 2px #000, 0 1px 3px rgba(0,0,0,.6); }
  .black-piece { color: #111; }
  .sq.editable { cursor: pointer; outline: 2px solid transparent; }
  .sq.editable:hover { outline-color: #ffcc00; }
  #stream { max-width: min(90vw, 640px); border-radius: 6px; box-shadow: 0 8px 30px rgba(0,0,0,0.5); }
  #lastMove { color: #eee; font-size: 18px; min-height: 24px; }
  #moveLog { color: #999; font-size: 13px; max-width: 320px; text-align: center; max-height: 140px; overflow-y: auto; }
  #moveLog div { padding: 2px 0; }
  #flagBox { display: none; flex-direction: column; align-items: center; gap: 8px;
             background: #3a2a1a; border: 1px solid #d9534f; border-radius: 6px; padding: 10px 14px; color: #eee; }
  #flagReason { color: #f0ad4e; font-size: 13px; text-align: center; max-width: 320px; }
  button { background: #444; color: #eee; border: 1px solid #666; border-radius: 4px;
           padding: 6px 14px; font-size: 14px; cursor: pointer; }
  button:hover { background: #555; }
  #editControls { display: none; flex-direction: column; align-items: center; gap: 8px; }
  #turnRow { display: flex; gap: 10px; align-items: center; color: #eee; font-size: 14px; }
  #saveCancelRow { display: flex; gap: 10px; }
  #picker {
    display: none; position: absolute; z-index: 10; background: #222; border: 1px solid #666;
    border-radius: 6px; padding: 6px; grid-template-columns: repeat(4, 36px); gap: 4px;
  }
  #picker .opt { width: 36px; height: 36px; display: flex; align-items: center; justify-content: center;
                 font-size: 24px; background: #333; border-radius: 4px; cursor: pointer; }
  #picker .opt:hover { background: #555; }
</style>
</head>
<body>
  <div class="col">
    <div id="board"><div id="picker"></div></div>
    <div id="flagBox">
      <div id="flagReason"></div>
      <button id="fixBtn">Fix board</button>
    </div>
    <div id="editControls">
      <div id="turnRow">
        Side to move:
        <label><input type="radio" name="turn" value="white" checked> White</label>
        <label><input type="radio" name="turn" value="black"> Black</label>
      </div>
      <div id="saveCancelRow">
        <button id="saveBtn">Save</button>
        <button id="cancelBtn">Cancel</button>
      </div>
      <div id="editHint" style="color:#999; font-size:12px;">Click a square to set its piece</div>
    </div>
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
const PICKER_OPTIONS = [null, ...Object.keys(GLYPHS)];

const boardEl = document.getElementById("board");
const pickerEl = document.getElementById("picker");
const cells = [];
for (let rank = 7; rank >= 0; rank--) {
  for (let file = 0; file < 8; file++) {
    const cell = document.createElement("div");
    cell.className = "sq " + ((rank + file) % 2 === 0 ? "dark" : "light");
    boardEl.appendChild(cell);
    cells.push({ el: cell, rank, file });
  }
}

let liveMatrix = null;   // last matrix received from the server
let editMatrix = null;   // working copy while in edit mode, null when not editing

function render(matrix) {
  for (const { el, rank, file } of cells) {
    const label = matrix[rank][file];
    if (!label) { el.textContent = ""; el.className = el.className.replace(/ (white|black)-piece/, ""); continue; }
    el.textContent = GLYPHS[label] || "?";
    const colorClass = label.startsWith("white") ? "white-piece" : "black-piece";
    el.className = el.className.replace(/ (white|black)-piece/, "") + " " + colorClass;
  }
}

function closePicker() { pickerEl.style.display = "none"; }

function openPicker(cellInfo) {
  pickerEl.innerHTML = "";
  for (const label of PICKER_OPTIONS) {
    const opt = document.createElement("div");
    opt.className = "opt";
    opt.textContent = label ? (GLYPHS[label] || "?") : "\\u2716";
    opt.title = label || "empty";
    opt.onclick = (ev) => {
      ev.stopPropagation();
      editMatrix[cellInfo.rank][cellInfo.file] = label;
      render(editMatrix);
      closePicker();
    };
    pickerEl.appendChild(opt);
  }
  const rect = cellInfo.el.getBoundingClientRect();
  const boardRect = boardEl.getBoundingClientRect();
  pickerEl.style.left = (rect.left - boardRect.left) + "px";
  pickerEl.style.top = (rect.top - boardRect.top) + "px";
  pickerEl.style.display = "grid";
}

for (const cellInfo of cells) {
  cellInfo.el.addEventListener("click", () => {
    if (!editMatrix) return;
    openPicker(cellInfo);
  });
}
boardEl.addEventListener("click", (ev) => { if (ev.target === boardEl) closePicker(); });

const statusEl = document.getElementById("status");
const lastMoveEl = document.getElementById("lastMove");
const moveLogEl = document.getElementById("moveLog");
const flagBoxEl = document.getElementById("flagBox");
const flagReasonEl = document.getElementById("flagReason");
const editControlsEl = document.getElementById("editControls");
const fixBtn = document.getElementById("fixBtn");
const saveBtn = document.getElementById("saveBtn");
const cancelBtn = document.getElementById("cancelBtn");
let lastOk = Date.now();
let lastMoveSeq = 0;

function logMove(text) {
  const line = document.createElement("div");
  line.textContent = text;
  moveLogEl.prepend(line);
  while (moveLogEl.children.length > 8) moveLogEl.removeChild(moveLogEl.lastChild);
}

function enterEditMode() {
  editMatrix = liveMatrix.map(row => row.slice());
  for (const { el } of cells) el.classList.add("editable");
  editControlsEl.style.display = "flex";
  render(editMatrix);
}

function exitEditMode() {
  editMatrix = null;
  closePicker();
  for (const { el } of cells) el.classList.remove("editable");
  editControlsEl.style.display = "none";
  render(liveMatrix);
}

fixBtn.onclick = enterEditMode;
cancelBtn.onclick = exitEditMode;

saveBtn.onclick = async () => {
  const turn = document.querySelector('input[name="turn"]:checked').value;
  saveBtn.disabled = true;
  try {
    const res = await fetch("/board/correct", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ matrix: editMatrix, turn }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      alert("Could not save: " + (body.error || res.statusText));
      return;
    }
    exitEditMode();
  } catch (e) {
    alert("Could not save: " + e);
  } finally {
    saveBtn.disabled = false;
  }
};

async function poll() {
  try {
    const res = await fetch("/board.json", { cache: "no-store" });
    const data = await res.json();
    liveMatrix = data.matrix;
    if (!editMatrix) render(liveMatrix);

    if (data.move_seq > lastMoveSeq) {
      if (data.last_move) {
        lastMoveEl.textContent = data.last_move;
        logMove(data.last_move);
      } else if (data.flagged) {
        lastMoveEl.textContent = "";
        logMove("(flagged: " + (data.flag_reason || "unresolved") + ")");
      }
    }
    lastMoveSeq = data.move_seq;

    if (data.flagged && !editMatrix) {
      flagBoxEl.style.display = "flex";
      flagReasonEl.textContent = data.flag_reason || "Board state could not be resolved automatically.";
    } else if (!data.flagged) {
      flagBoxEl.style.display = "none";
      if (editMatrix) exitEditMode();
    }

    const turnRadio = document.querySelector('input[name="turn"][value="' + data.turn + '"]');
    if (turnRadio && !editMatrix) turnRadio.checked = true;

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
    """Holds the latest board matrix, move text, flagged status/reason, and
    JPEG frame for the HTTP handler to read -- one lock guards the board
    fields since tracking_loop's on_update sets them together; the frame is
    set separately and far more often (every capture tick, for a live
    feed)."""

    def __init__(self):
        self._lock = Lock()
        self._matrix = None
        self._updated_at = 0.0
        self._last_move = None
        self._move_seq = 0
        self._flagged = False
        self._flag_reason = None
        self._jpeg = None

    def set_board(self, matrix, move_text, flagged, reason):
        with self._lock:
            self._matrix = [row[:] for row in matrix]
            self._updated_at = time.time()
            self._last_move = move_text
            self._flagged = flagged
            self._flag_reason = reason
            self._move_seq += 1

    def set_frame(self, frame):
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if ok:
            with self._lock:
                self._jpeg = buf.tobytes()

    def get_board(self):
        with self._lock:
            return self._matrix, self._updated_at, self._last_move, self._move_seq, self._flagged, self._flag_reason

    def get_frame(self):
        with self._lock:
            return self._jpeg


def _validate_correction(body):
    """Returns an error string, or None if body is well-formed enough to
    attempt (matrix shape/labels valid, turn valid, and the resulting
    position parses as a legal chess.Board)."""
    matrix = body.get("matrix")
    turn = body.get("turn")

    if turn not in ("white", "black"):
        return "turn must be \"white\" or \"black\""
    if not isinstance(matrix, list) or len(matrix) != 8:
        return "matrix must be an 8x8 array"
    for row in matrix:
        if not isinstance(row, list) or len(row) != 8:
            return "matrix must be an 8x8 array"
        for label in row:
            if label is not None and label not in _VALID_LABELS:
                return f"invalid piece label: {label!r}"

    placement = matrix_to_fen_placement(matrix)
    turn_char = "w" if turn == "white" else "b"
    try:
        board = chess.Board(f"{placement} {turn_char} - - 0 1")
    except ValueError as exc:
        return f"invalid position: {exc}"
    if not board.is_valid():
        return "position is not a legal chess position (check kings/pawns)"
    return None


def start_server(host, port, buffer, loop, capture_stream):
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
                matrix, updated_at, last_move, move_seq, flagged, flag_reason = buffer.get_board()
                rows = matrix if matrix is not None else [[None] * 8 for _ in range(8)]
                body = json.dumps(
                    {
                        "matrix": rows,
                        "updated_at": updated_at,
                        "last_move": last_move,
                        "move_seq": move_seq,
                        "flagged": flagged,
                        "flag_reason": flag_reason,
                        "turn": loop.turn,
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

        def do_POST(self):
            if self.path != "/board/correct":
                self.send_error(404)
                return

            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                self._send_json(400, {"error": "invalid JSON body"})
                return

            error = _validate_correction(body)
            if error is not None:
                self._send_json(400, {"error": error})
                return

            frame, _timestamp = capture_stream.get_latest()
            loop.apply_manual_correction(body["matrix"], body["turn"], frame)
            self._send_json(200, {"ok": True})

        def _send_json(self, status, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass  # keep the terminal clean

    server = ThreadingHTTPServer((host, port), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    return server


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--poll-interval", type=float, default=0.12,
                        help="seconds between cheap motion-gate polls (not a detection interval)")
    parser.add_argument("--calibration", type=Path, default=DEFAULT_CALIBRATION)
    parser.add_argument("--classifier", type=Path, default=DEFAULT_CLASSIFIER, help="per-square classifier")
    parser.add_argument("--min-conf", type=float, default=0.7, help="classifier confidence threshold")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="0.0.0.0")
    return parser.parse_args()


def main():
    args = parse_args()

    if not args.calibration.exists():
        raise SystemExit(f"No calibration found at {args.calibration} -- run calibrate.py first.")
    if not args.classifier.exists():
        raise SystemExit(
            f"No classifier model found at {args.classifier} -- see src/collect_square_crops.py "
            "and training/train_classifier.py to train/export one."
        )

    calibration_matrix = load_calibration(args.calibration)
    print("Loading classifier...")
    classifier_model = load_classifier(str(args.classifier))

    buffer = BoardBuffer()
    print("Running. Ctrl+C to stop.")
    try:
        with Camera() as cam, CaptureStream(cam) as stream:
            frame = None
            while frame is None:
                frame, _timestamp = stream.get_latest()
            image_size = (frame.shape[1], frame.shape[0])

            def on_update(matrix, move_text, frame, flagged, reason):
                buffer.set_board(matrix, move_text, flagged, reason)

            loop = TrackingLoop(
                capture_stream=stream,
                calibration_matrix=calibration_matrix,
                image_size=image_size,
                classifier_model=classifier_model,
                on_update=on_update,
                poll_interval=args.poll_interval,
                classifier_min_conf=args.min_conf,
            )
            buffer.set_board(loop.current_matrix, None, False, None)  # seed the UI before any move happens

            start_server(args.host, args.port, buffer, loop, stream)
            print(f"Serving at http://<this-pi>:{args.port}/")

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
