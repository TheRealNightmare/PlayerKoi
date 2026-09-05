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
from threading import Event, Lock, Thread

import chess
import cv2

from board_state import load_calibration, matrix_to_fen_placement
from capture import Camera, CaptureStream
from engine import DEFAULT_SKILL, DEFAULT_THINK_S, ChessEngine, describe_move
from harvest import CropHarvester
from robot import GantryError, RobotController, open_gantry
from robot_moves import DEFAULT_TOPPLE_DELAY_S
from square_classifier import DEFAULT_MIN_CONF, load_classifier
from square_geometry import square_pixel_bboxes
from tracking_loop import TrackingLoop

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CALIBRATION = REPO_ROOT / "config" / "calibration.json"
DEFAULT_CLASSIFIER = REPO_ROOT / "models" / "square_classifier_ncnn_model"
DEFAULT_HARVEST = REPO_ROOT / "training" / "datasets" / "harvested"

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
  .expect-from { box-shadow: inset 0 0 0 4px #ffcc00; }
  .expect-to   { box-shadow: inset 0 0 0 4px #4a9eff; }
  #controls { display: flex; gap: 10px; }
  #engineBox {
    display: flex; flex-direction: column; align-items: center; gap: 6px;
    background: #23282e; border: 1px solid #444; border-radius: 6px;
    padding: 12px 18px; color: #eee; min-width: 300px;
  }
  #engineTitle { color: #888; font-size: 12px; text-transform: uppercase; letter-spacing: 1px; }
  #engineMove { font-size: 30px; font-weight: bold; color: #4a9eff; min-height: 36px; letter-spacing: 2px; }
  #engineExtra { color: #f0ad4e; font-size: 13px; text-align: center; max-width: 320px; }
  #engineMsg { color: #888; font-size: 12px; text-align: center; max-width: 320px; }
  #engineRow { display: flex; align-items: center; gap: 12px; font-size: 13px; color: #bbb; }
  #robotBox {
    display: none; flex-direction: column; align-items: center; gap: 6px;
    background: #23282e; border: 1px solid #444; border-radius: 6px;
    padding: 12px 18px; color: #eee; min-width: 300px;
  }
  #robotBox.halted { border-color: #d9534f; background: #3a2a2a; }
  #robotTitle { color: #888; font-size: 12px; text-transform: uppercase; letter-spacing: 1px; }
  #robotState { font-size: 16px; font-weight: bold; }
  #robotNote { color: #bbb; font-size: 13px; text-align: center; min-height: 18px; }
  #robotPrompt { color: #f0ad4e; font-size: 14px; font-weight: bold; text-align: center; max-width: 320px; }
  #robotMsg { color: #d9534f; font-size: 12px; text-align: center; max-width: 320px; }
  #robotRow { display: flex; gap: 10px; }
  #haltBtn { background: #8a2b2b; border-color: #d9534f; }
  #haltBtn:hover { background: #a33; }
  #editControls { display: none; flex-direction: column; align-items: center; gap: 8px; }
  #editShortcuts { display: flex; gap: 10px; }
  #pausedNote { display: none; color: #f0ad4e; font-size: 13px; font-weight: bold; }
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
    </div>
    <div id="controls">
      <button id="editBtn">Edit board</button>
      <button id="undoBtn">Undo last move</button>
    </div>
    <div id="pausedNote">tracking paused while editing</div>
    <div id="editControls">
      <div id="turnRow">
        Side to move:
        <label><input type="radio" name="turn" value="white" checked> White</label>
        <label><input type="radio" name="turn" value="black"> Black</label>
      </div>
      <div id="editShortcuts">
        <button id="resetStartBtn">Reset to start</button>
        <button id="clearBoardBtn">Clear board</button>
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
    <div id="engineBox">
      <div id="engineTitle">Engine (Black)</div>
      <div id="engineMove"></div>
      <div id="engineExtra"></div>
      <div id="engineMsg"></div>
      <div id="engineRow">
        <label><input type="checkbox" id="engineToggle"> on</label>
        <label>skill <input type="range" id="engineSkill" min="0" max="20" step="1"></label>
        <span id="engineSkillVal"></span>
      </div>
    </div>
    <div id="robotBox">
      <div id="robotTitle">Robot arm</div>
      <div id="robotState"></div>
      <div id="robotNote"></div>
      <div id="robotPrompt"></div>
      <div id="robotMsg"></div>
      <div id="robotRow">
        <button id="haltBtn">HALT</button>
        <button id="homeBtn">Home / re-enable</button>
      </div>
    </div>
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

let expectedUci = null;   // e.g. "d7d5" -- the engine move awaiting placement

function squareName(file, rank) {
  return "abcdefgh"[file] + (rank + 1);
}

function render(matrix) {
  for (const { el, rank, file } of cells) {
    const label = matrix[rank][file];
    if (!label) { el.textContent = ""; el.className = el.className.replace(/ (white|black)-piece/, ""); }
    else {
      el.textContent = GLYPHS[label] || "?";
      const colorClass = label.startsWith("white") ? "white-piece" : "black-piece";
      el.className = el.className.replace(/ (white|black)-piece/, "") + " " + colorClass;
    }
    // Mark where the engine wants the piece taken from and put down.
    const name = squareName(file, rank);
    el.classList.toggle("expect-from", !!expectedUci && expectedUci.slice(0, 2) === name);
    el.classList.toggle("expect-to", !!expectedUci && expectedUci.slice(2, 4) === name);
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
const controlsEl = document.getElementById("controls");
const pausedNoteEl = document.getElementById("pausedNote");
const editBtn = document.getElementById("editBtn");
const undoBtn = document.getElementById("undoBtn");
const resetStartBtn = document.getElementById("resetStartBtn");
const clearBoardBtn = document.getElementById("clearBoardBtn");
const saveBtn = document.getElementById("saveBtn");
const cancelBtn = document.getElementById("cancelBtn");

const engineMoveEl = document.getElementById("engineMove");
const engineExtraEl = document.getElementById("engineExtra");
const engineMsgEl = document.getElementById("engineMsg");
const engineToggle = document.getElementById("engineToggle");
const engineSkill = document.getElementById("engineSkill");
const engineSkillVal = document.getElementById("engineSkillVal");
const robotBox = document.getElementById("robotBox");
const robotStateEl = document.getElementById("robotState");
const robotNoteEl = document.getElementById("robotNote");
const robotPromptEl = document.getElementById("robotPrompt");
const robotMsgEl = document.getElementById("robotMsg");
const haltBtn = document.getElementById("haltBtn");
const homeBtn = document.getElementById("homeBtn");

const BACK_RANK = ["rook", "knight", "bishop", "queen", "king", "bishop", "knight", "rook"];

function emptyMatrix() {
  return Array.from({ length: 8 }, () => Array(8).fill(null));
}

function startingMatrix() {
  const m = emptyMatrix();
  for (let file = 0; file < 8; file++) {
    m[0][file] = "white-" + BACK_RANK[file];
    m[1][file] = "white-pawn";
    m[6][file] = "black-pawn";
    m[7][file] = "black-" + BACK_RANK[file];
  }
  return m;
}

async function setPaused(paused) {
  try {
    await fetch("/board/pause", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paused }),
    });
  } catch (e) { /* the pause lapses on its own if this never lands */ }
}
let lastOk = Date.now();
let lastMoveSeq = 0;

function logMove(text) {
  const line = document.createElement("div");
  line.textContent = text;
  moveLogEl.prepend(line);
  while (moveLogEl.children.length > 8) moveLogEl.removeChild(moveLogEl.lastChild);
}

function enterEditMode() {
  editMatrix = (liveMatrix || emptyMatrix()).map(row => row.slice());
  for (const { el } of cells) el.classList.add("editable");
  editControlsEl.style.display = "flex";
  controlsEl.style.display = "none";
  pausedNoteEl.style.display = "block";
  setPaused(true);   // held open by the ?editing=1 poll below
  render(editMatrix);
}

function exitEditMode() {
  editMatrix = null;
  closePicker();
  for (const { el } of cells) el.classList.remove("editable");
  editControlsEl.style.display = "none";
  controlsEl.style.display = "flex";
  pausedNoteEl.style.display = "none";
  setPaused(false);
  if (liveMatrix) render(liveMatrix);
}

editBtn.onclick = enterEditMode;
cancelBtn.onclick = exitEditMode;
resetStartBtn.onclick = () => { editMatrix = startingMatrix(); render(editMatrix); };
clearBoardBtn.onclick = () => { editMatrix = emptyMatrix(); render(editMatrix); };

async function postEngine(payload) {
  try {
    await fetch("/engine", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  } catch (e) { /* next poll re-syncs the displayed state */ }
}

engineToggle.onchange = () => postEngine({ enabled: engineToggle.checked });
engineSkill.oninput = () => { engineSkillVal.textContent = engineSkill.value; };
engineSkill.onchange = () => postEngine({ skill: Number(engineSkill.value) });

async function postRobot(payload, button) {
  if (button) button.disabled = true;
  try {
    const res = await fetch("/robot", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) alert(body.error || res.statusText);
  } catch (e) {
    alert("Robot command failed: " + e);
  } finally {
    if (button) button.disabled = false;
  }
}

haltBtn.onclick = () => postRobot({ halt: true }, haltBtn);
homeBtn.onclick = () => {
  if (confirm("Home the gantry? It will cross the whole board -- keep hands clear.")) {
    postRobot({ home: true }, homeBtn);
  }
};

function renderRobot(bot) {
  if (!bot) { robotBox.style.display = "none"; return; }
  robotBox.style.display = "flex";
  robotBox.classList.toggle("halted", !!bot.halted);

  let state = "ready";
  let color = "#5cb85c";
  if (bot.halted) { state = "HALTED"; color = "#d9534f"; }
  else if (bot.busy) { state = "moving"; color = "#4a9eff"; }
  else if (!bot.homed) { state = "not homed"; color = "#f0ad4e"; }
  robotStateEl.textContent = state + " (" + bot.port + ")";
  robotStateEl.style.color = color;

  robotNoteEl.textContent = bot.note || "";
  robotPromptEl.textContent = bot.prompt || "";
  robotMsgEl.textContent = bot.message || "";
  haltBtn.disabled = bot.halted;
}

undoBtn.onclick = async () => {
  undoBtn.disabled = true;
  try {
    const res = await fetch("/board/undo", { method: "POST" });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      alert(body.error || res.statusText);
      return;
    }
    logMove("(undid " + body.undone + ")");
    lastMoveEl.textContent = "";
  } catch (e) {
    alert("Could not undo: " + e);
  } finally {
    undoBtn.disabled = false;
  }
};

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
    // ?editing=1 refreshes the server-side pause while the editor is open;
    // if this tab goes away, the pause lapses and tracking resumes.
    const res = await fetch("/board.json" + (editMatrix ? "?editing=1" : ""), { cache: "no-store" });
    const data = await res.json();
    liveMatrix = data.matrix;

    renderRobot(data.robot);

    const eng = data.engine || {};
    expectedUci = eng.expected_uci || null;
    engineMoveEl.textContent = eng.thinking ? "thinking..." : (eng.instruction || "");
    engineExtraEl.textContent = eng.thinking ? "" : (eng.extra || "");
    engineMsgEl.textContent = eng.message || (eng.available ? "" : "engine unavailable");
    // Don't fight the user mid-drag of the slider or mid-click of the toggle.
    if (document.activeElement !== engineToggle) engineToggle.checked = !!eng.enabled;
    if (document.activeElement !== engineSkill && eng.skill !== undefined) {
      engineSkill.value = eng.skill;
      engineSkillVal.textContent = eng.skill;
    }
    engineToggle.disabled = !eng.available;
    engineSkill.disabled = !eng.available;

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

    // The flag box is informational only -- the editor is reachable at any
    // time from #controls, since a wrongly-accepted move raises no flag.
    if (data.flagged) {
      flagBoxEl.style.display = "flex";
      flagReasonEl.textContent = data.flag_reason || "Board state could not be resolved automatically.";
    } else {
      flagBoxEl.style.display = "none";
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


class EngineController:
    """Runs the engine on its own thread and publishes the move to place.

    Deliberately not driven inline from TrackingLoop.on_update: that fires
    with the loop's lock held, so a ~0.5s search there would stall every
    /board.json poll. on_update just pokes the event; this thread does the
    thinking.
    """

    def __init__(self, loop, engine, think_s=DEFAULT_THINK_S, robot=None):
        self._loop = loop
        self._engine = engine
        self._think_s = think_s
        self._robot = robot
        self._lock = Lock()
        self._wake = Event()
        self._enabled = False
        self._thinking = False
        self._headline = None
        self._extra = None
        self._message = None if engine.available else engine.error
        Thread(target=self._run, daemon=True).start()

    def state(self):
        with self._lock:
            expected = self._loop.expected_move
            return {
                "available": self._engine.available,
                "enabled": self._enabled,
                "thinking": self._thinking,
                "instruction": self._headline,
                "extra": self._extra,
                "expected_uci": expected.uci() if expected is not None else None,
                "skill": self._engine.skill,
                "message": self._message,
            }

    def configure(self, enabled=None, skill=None):
        with self._lock:
            if skill is not None:
                self._engine.set_skill(skill)
            if enabled is not None and enabled != self._enabled:
                self._enabled = bool(enabled) and self._engine.available
                if not self._enabled:
                    self._headline = self._extra = None
                    self._loop.set_expected_move(None)
        self.notify()

    def notify(self):
        self._wake.set()

    def _run(self):
        while True:
            self._wake.wait(timeout=1.0)
            self._wake.clear()
            try:
                self._maybe_move()
            except Exception as exc:
                with self._lock:
                    self._thinking = False
                    self._message = f"engine error: {exc}"

    def _maybe_move(self):
        with self._lock:
            if not self._enabled or not self._engine.available:
                return
        # Engine plays Black, and only when nothing is already pending.
        if self._loop.turn != "black" or self._loop.expected_move is not None:
            return

        board = self._loop.board_copy
        if board.is_game_over():
            with self._lock:
                self._headline, self._extra = None, None
                self._message = "no legal moves -- game over"
            return

        with self._lock:
            self._thinking = True
            self._message = None
        move = self._engine.best_move(board, self._think_s)
        headline, extra = describe_move(board, move) if move is not None else (None, None)

        with self._lock:
            self._thinking = False
            self._headline, self._extra = headline, extra
        if move is None:
            return

        # Arm verification before the arm moves: whatever ends up on the
        # board next -- robot or human -- must match this move or it flags.
        self._loop.set_expected_move(move)

        if self._robot is not None and self._robot.ready:
            # Blocking, but this is the engine's own thread with no lock
            # held, which is exactly why the search lives here too.
            ok, error = self._robot.execute(board, move)
            if not ok:
                with self._lock:
                    self._message = f"robot stopped: {error}"

    def close(self):
        self._engine.close()


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


def start_server(host, port, buffer, loop, capture_stream, engine_controller, robot_controller=None):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path, _, query = self.path.partition("?")
            if path == "/":
                body = PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/board.json":
                # The UI polls with ?editing=1 while its board editor is
                # open; that refreshes the pause so tracking stays held.
                # Stop polling (close the tab) and the pause lapses.
                if "editing=1" in query:
                    loop.set_paused(True)
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
                        "paused": loop.is_paused,
                        "engine": engine_controller.state(),
                        "robot": robot_controller.state() if robot_controller is not None else None,
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/stream.mjpg":
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
            path, _, _query = self.path.partition("?")
            if path not in ("/board/correct", "/board/undo", "/board/pause", "/engine", "/robot"):
                self.send_error(404)
                return

            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                self._send_json(400, {"error": "invalid JSON body"})
                return

            if path == "/engine":
                engine_controller.configure(
                    enabled=body.get("enabled"), skill=body.get("skill")
                )
                self._send_json(200, {"ok": True, "engine": engine_controller.state()})
                return

            if path == "/robot":
                if robot_controller is None:
                    self._send_json(400, {"error": "no robot attached -- start with --robot"})
                    return
                if body.get("halt"):
                    robot_controller.halt()
                    self._send_json(200, {"ok": True, "robot": robot_controller.state()})
                    return
                if body.get("home"):
                    # Homing crosses the board, so it must not race a settle.
                    loop.set_paused(True, lapse_s=60.0)
                    try:
                        ok, error = robot_controller.home()
                    finally:
                        loop.set_paused(False)
                    if not ok:
                        self._send_json(400, {"error": error, "robot": robot_controller.state()})
                        return
                    self._send_json(200, {"ok": True, "robot": robot_controller.state()})
                    return
                self._send_json(400, {"error": "expected {\"home\": true} or {\"halt\": true}"})
                return

            if path == "/board/pause":
                loop.set_paused(bool(body.get("paused")))
                self._send_json(200, {"ok": True, "paused": loop.is_paused})
                return

            if path == "/board/undo":
                frame, _timestamp = capture_stream.get_latest()
                san = loop.undo_last_move(frame)
                if san is None:
                    self._send_json(400, {"error": "nothing to undo"})
                else:
                    self._send_json(200, {"ok": True, "undone": san})
                return

            error = _validate_correction(body)
            if error is not None:
                self._send_json(400, {"error": error})
                return

            frame, _timestamp = capture_stream.get_latest()
            loop.apply_manual_correction(body["matrix"], body["turn"], frame)
            # Saving the editor ends the edit session, so lift the pause.
            loop.set_paused(False)
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
    parser.add_argument("--min-conf", type=float, default=DEFAULT_MIN_CONF,
                        help="classifier confidence threshold")
    parser.add_argument("--motion-thresh", type=float, default=None,
                        help="board-ROI motion threshold; tune with debug_classifier.py --watch")
    parser.add_argument("--engine-command", default="stockfish",
                        help="UCI engine binary for the Black side")
    parser.add_argument("--engine-skill", type=int, default=DEFAULT_SKILL,
                        help="Stockfish Skill Level 0-20 (adjustable live in the UI)")
    parser.add_argument("--engine-think", type=float, default=DEFAULT_THINK_S,
                        help="seconds the engine may think per move")
    parser.add_argument("--robot", default=None, metavar="PORT",
                        help="serial port of the gantry Arduino (e.g. /dev/ttyACM0), or "
                             "'mock' for a dry run. Omitted: no arm, you place Black's "
                             "moves by hand as before")
    parser.add_argument("--topple-delay", type=float, default=DEFAULT_TOPPLE_DELAY_S,
                        help="seconds to wait after toppling a captured piece, for you "
                             "to lift it off the board")
    parser.add_argument("--harvest", type=Path, nargs="?", const=DEFAULT_HARVEST, default=None,
                        help="save labelled crops from every resolved move, to grow the training "
                             f"set as you play (default dir: {DEFAULT_HARVEST})")
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

    engine = ChessEngine(command=args.engine_command, skill=args.engine_skill)
    print("Engine: Stockfish ready." if engine.available else f"Engine: {engine.error}")

    robot = None
    if args.robot:
        try:
            robot = open_gantry(args.robot, topple_delay_s=args.topple_delay)
            print(f"Robot: gantry on {robot.port}.")
        except GantryError as exc:
            raise SystemExit(f"Robot: {exc}")
        except ImportError:
            raise SystemExit("Robot: pyserial is missing -- pip install -r requirements.txt")

    buffer = BoardBuffer()
    engine_controller = None
    robot_controller = None
    print("Running. Ctrl+C to stop.")
    try:
        with Camera() as cam, CaptureStream(cam) as stream:
            frame = None
            while frame is None:
                frame, _timestamp = stream.get_latest()
            image_size = (frame.shape[1], frame.shape[0])

            harvester = None
            if args.harvest is not None:
                harvester = CropHarvester(
                    args.harvest, square_pixel_bboxes(calibration_matrix, image_size)
                )
                print(f"Harvesting labelled crops to {args.harvest}")

            def on_update(matrix, move_text, frame, flagged, reason):
                buffer.set_board(matrix, move_text, flagged, reason)
                # Only a resolved move is trustworthy ground truth. Undo
                # reports the reverted position while the physical board
                # still shows the post-move one, so harvesting there would
                # write mislabelled crops -- see harvest.py.
                if harvester is not None and move_text is not None and not flagged:
                    harvester.record(matrix, frame)
                # An unresolvable settle right after the arm moved means the
                # physical board and the tracked position have diverged.
                # Stop the arm before it stacks another move on top.
                if flagged and robot_controller is not None:
                    robot_controller.note_flag(reason)
                # Just a poke -- the engine thinks on its own thread, since
                # this runs with TrackingLoop's lock held.
                if engine_controller is not None:
                    engine_controller.notify()

            loop = TrackingLoop(
                capture_stream=stream,
                calibration_matrix=calibration_matrix,
                image_size=image_size,
                classifier_model=classifier_model,
                on_update=on_update,
                poll_interval=args.poll_interval,
                classifier_min_conf=args.min_conf,
                motion_thresh=args.motion_thresh,
            )
            buffer.set_board(loop.current_matrix, None, False, None)  # seed the UI before any move happens

            if robot is not None:
                robot_controller = RobotController(robot, loop)
                print("Homing the gantry -- keep hands clear...")
                ok, error = robot_controller.home()
                print("Robot: homed and ready." if ok else f"Robot: {error}")

            engine_controller = EngineController(
                loop, engine, think_s=args.engine_think, robot=robot_controller
            )
            start_server(
                args.host, args.port, buffer, loop, stream, engine_controller, robot_controller
            )
            print(f"Serving at http://<this-pi>:{args.port}/")

            while True:
                live_frame, _timestamp = stream.get_latest()
                if live_frame is not None:
                    buffer.set_frame(live_frame)
                loop.tick()
                time.sleep(args.poll_interval)
    except KeyboardInterrupt:
        print("Stopped.")
    finally:
        engine.close()  # don't leave the Stockfish subprocess behind
        if robot_controller is not None:
            robot_controller.close()  # drops the coil, closes the port
        elif robot is not None:
            robot.close()


if __name__ == "__main__":
    main()
