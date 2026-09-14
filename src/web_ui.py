"""Player Koi's web UI -- the menu, and both ways of playing behind it.

One launch serves a mode menu; src/session.py owns which mode is running and
what it takes to swap one for another. The two are very different stacks:

    Play the engine   camera + classifier + tracking_loop: you move White by
                      hand, vision reads the board, the arm answers for Black.
                      The board diagram is driven by tracking_loop's
                      event-gated occupancy-read/delta/legal-move pipeline
                      rather than a fixed-interval detection loop -- the feed
                      updates continuously and cheaply, but the board only
                      re-evaluates once something has settled.

    AI vs AI          headless_loop: no camera at all. The engine plays both
                      sides and the arm places every move, so the position is
                      known rather than observed.

Moves are real algebraic notation (SAN) via python-chess, not a physical
before/after description. There is no automatic rescan in this design -- when
a settle can't be resolved with confidence the UI flags it and offers a manual
"Fix board" correction instead (POST /board/correct).

    python3 src/web_ui.py --robot auto         # http://<this-pi>:8000/
    python3 src/web_ui.py --ai-vs-ai --robot auto    # skip the menu

Nothing is required to reach the menu. "Play the engine" additionally needs
config/calibration.json (run calibrate.py) and a trained, NCNN-exported
empty/white/black classifier (see src/collect_square_crops.py and
training/train_classifier.py); the menu greys it out and says so when they are
missing, rather than the process refusing to start.
"""

import argparse
import json
import time
from pathlib import Path
from threading import Event, Lock, Thread

import chess
import cv2

from board_state import load_calibration, matrix_to_fen_placement
from engine import DEFAULT_SKILL, DEFAULT_THINK_S, ChessEngine, describe_move
from headless_loop import HeadlessLoop, NullStream
import move_policy
import rig
from robot import GantryError, RobotController, open_gantry
from robot_moves import DEFAULT_TOPPLE_DELAY_S
from session import AI_VS_AI, MENU, NORMAL, ModeError, Session
from square_classifier import DEFAULT_MIN_CONF

# picamera2 (capture) and the ncnn classifier loader are imported inside
# main()'s tracking path rather than here, so --ai-vs-ai runs off the Pi and
# without a trained model -- it needs neither.

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
<title>Player Koi</title>
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

  /* --- shell: one heading, two screens ------------------------------- */
  #shell { width: 100%; display: flex; flex-direction: column; align-items: center; gap: 18px; }
  #brand { margin: 0; color: #eee; font-size: 30px; font-weight: 600; letter-spacing: 3px; }
  #brand span { color: #769656; }
  #screens { width: 100%; display: flex; flex-wrap: wrap; align-items: flex-start;
             justify-content: center; gap: 24px; }
  #menu { display: none; flex-direction: column; align-items: center; gap: 18px; max-width: 760px; }
  #menuCards { display: flex; flex-wrap: wrap; justify-content: center; gap: 18px; }
  .card {
    width: 300px; display: flex; flex-direction: column; gap: 10px; text-align: left;
    background: #23282e; border: 1px solid #444; border-radius: 8px; padding: 18px;
  }
  .card h2 { margin: 0; font-size: 18px; color: #eee; }
  .card p { margin: 0; font-size: 13px; color: #bbb; line-height: 1.5; }
  .card.unavailable { opacity: 0.55; }
  .card .reason { color: #f0ad4e; font-size: 12px; line-height: 1.5; }
  .card button { align-self: flex-start; }
  .card button:disabled { cursor: not-allowed; opacity: 0.5; }
  #menuSettings {
    display: flex; flex-wrap: wrap; justify-content: center; align-items: center; gap: 16px;
    color: #bbb; font-size: 13px; background: #23282e; border: 1px solid #444;
    border-radius: 8px; padding: 14px 18px;
  }
  #menuSettings label { display: flex; align-items: center; gap: 6px; }
  #menuSettings input[type="number"] { width: 62px; background: #1a1a1a; color: #eee;
                                       border: 1px solid #666; border-radius: 4px; padding: 3px 5px; }
  #menuError { color: #d9534f; font-size: 13px; min-height: 18px; text-align: center; }
  #game { display: none; }
  #sessionRow { display: flex; gap: 10px; }
  #resetBtn, #menuResetBtn { background: #6a5320; border-color: #f0ad4e; }
  #resetBtn:hover, #menuResetBtn:hover { background: #866828; }
</style>
</head>
<body>
<div id="shell">
  <h1 id="brand">PLAYER <span>KOI</span></h1>

  <div id="menu">
    <div id="menuCards"></div>
    <div id="menuSettings">
      <label>skill <input type="range" id="setSkill" min="0" max="20" step="1"></label>
      <span id="setSkillVal"></span>
      <label>think <input type="number" id="setThink" min="0.05" max="10" step="0.05"> s</label>
      <label>move delay <input type="number" id="setDelay" min="0" max="30" step="0.5"> s</label>
      <label><input type="checkbox" id="setNoob"> beginner style (few knight moves)</label>
    </div>
    <div id="menuError"></div>
    <button id="menuResetBtn">Reset &amp; park the arm</button>
  </div>

  <div id="screens">
  <div class="col" id="game">
    <div id="board"><div id="picker"></div></div>
    <div id="flagBox">
      <div id="flagReason"></div>
    </div>
    <div id="sessionRow">
      <button id="backBtn">&larr; Back to menu</button>
      <button id="resetBtn">Reset</button>
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
      <div id="aiNote" style="display:none;color:#f0ad4e;font-size:12px">
        no camera &mdash; the tracked position is trusted, not verified
      </div>
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
        <button id="confirmBtn">Done — piece removed</button>
        <button id="haltBtn">HALT</button>
        <button id="homeBtn">Home / re-enable</button>
      </div>
    </div>
  </div>
  <div class="col" id="videoCol">
    <img id="stream" src="/stream.mjpg">
  </div>
  </div>
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

// Which screen is up, and how the game screen is dressed for the mode.
// Driven from every poll rather than latched once: with a menu you can leave
// AI vs AI and come back to normal mode, so a one-way switch would be wrong.
let currentMode = null;
const menuEl = document.getElementById("menu");
const gameEl = document.getElementById("game");
const videoCol = document.getElementById("videoCol");
const engineTitleEl = document.getElementById("engineTitle");
const aiNoteEl = document.getElementById("aiNote");
const engineToggleLabel = document.querySelector("#engineRow label");

function applyMode(mode) {
  if (mode === currentMode) return;
  currentMode = mode;
  const inGame = mode && mode !== "menu";
  menuEl.style.display = inGame ? "none" : "flex";
  gameEl.style.display = inGame ? "flex" : "none";

  const ai = mode === "ai_vs_ai";
  videoCol.style.display = ai || !inGame ? "none" : "flex";
  aiNoteEl.style.display = ai ? "block" : "none";
  engineTitleEl.textContent = ai ? "Engine (both sides)" : "Engine (Black)";
  engineToggleLabel.lastChild.textContent = ai ? " play" : " on";

  // Reconnect the feed when returning to a mode that has one; the <img>
  // stops retrying once the server has answered 503.
  const img = document.getElementById("stream");
  if (img && !ai && inGame) img.src = "/stream.mjpg?t=" + Date.now();
}
const engineSkill = document.getElementById("engineSkill");
const engineSkillVal = document.getElementById("engineSkillVal");
const robotBox = document.getElementById("robotBox");
const robotStateEl = document.getElementById("robotState");
const robotNoteEl = document.getElementById("robotNote");
const robotPromptEl = document.getElementById("robotPrompt");
const robotMsgEl = document.getElementById("robotMsg");
const haltBtn = document.getElementById("haltBtn");
const homeBtn = document.getElementById("homeBtn");
const confirmBtn = document.getElementById("confirmBtn");
// Sent with every poll rather than baked into the page: --board-origin is
// applied after web_ui is imported, so a value substituted at import time
// would name the wrong corner.
let PARK_SQUARE = "?";

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
confirmBtn.onclick = () => postRobot({ confirm: true }, confirmBtn);
homeBtn.onclick = () => {
  // No limit switches: HOME drives to where it ASSUMES the origin is, so
  // this only tells the truth if the carriage really is parked there.
  if (confirm("Is the carriage parked on " + PARK_SQUARE + "?\\n\\n" +
              "There are no limit switches -- homing drives to the assumed " +
              "origin rather than finding it. If it is parked anywhere else, " +
              "every move afterwards will be wrong.\\n\\n" +
              "It will cross the whole board. Keep hands clear.")) {
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
  // A blocking prompt outranks everything else in the status line: the arm
  // is stopped mid-move and nothing continues until it is answered.
  if (bot.awaiting_confirm) { state = "WAITING FOR YOU"; color = "#f0ad4e"; }
  robotStateEl.textContent = state + " (" + bot.port + ")";
  robotStateEl.style.color = color;

  robotNoteEl.textContent = bot.note || "";
  robotPromptEl.textContent = bot.awaiting_confirm || bot.prompt || "";
  robotMsgEl.textContent = bot.message || "";
  haltBtn.disabled = bot.halted;
  confirmBtn.style.display = bot.awaiting_confirm ? "" : "none";
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

// ---- the menu -------------------------------------------------------

const menuCardsEl = document.getElementById("menuCards");
const menuErrorEl = document.getElementById("menuError");
const setSkill = document.getElementById("setSkill");
const setSkillVal = document.getElementById("setSkillVal");
const setThink = document.getElementById("setThink");
const setDelay = document.getElementById("setDelay");
const setNoob = document.getElementById("setNoob");
let settingsSeeded = false;

setSkill.oninput = () => { setSkillVal.textContent = setSkill.value; };

function seedSettings(defaults) {
  // Once only, from whatever the process was launched with -- after that the
  // controls belong to the user and polling must not fight them.
  if (settingsSeeded || !defaults) return;
  settingsSeeded = true;
  if (defaults.skill !== undefined) setSkill.value = defaults.skill;
  if (defaults.think !== undefined) setThink.value = defaults.think;
  if (defaults.move_delay !== undefined) setDelay.value = defaults.move_delay;
  if (defaults.noob !== undefined) setNoob.checked = !!defaults.noob;
  setSkillVal.textContent = setSkill.value;
}

function renderMenu(modes) {
  const signature = JSON.stringify(modes);
  if (menuCardsEl.dataset.signature === signature) return;
  menuCardsEl.dataset.signature = signature;
  menuCardsEl.innerHTML = "";
  for (const m of modes || []) {
    const card = document.createElement("div");
    card.className = "card" + (m.available ? "" : " unavailable");
    const h = document.createElement("h2");
    h.textContent = m.title;
    const p = document.createElement("p");
    p.textContent = m.blurb;
    card.append(h, p);
    if (!m.available) {
      const reason = document.createElement("div");
      reason.className = "reason";
      reason.textContent = m.reason;
      card.appendChild(reason);
    }
    const btn = document.createElement("button");
    btn.textContent = m.available ? "Start" : "Unavailable";
    btn.disabled = !m.available;
    btn.onclick = () => startMode(m.mode, btn);
    card.appendChild(btn);
    menuCardsEl.appendChild(card);
  }
}

async function startMode(mode, btn) {
  menuErrorEl.textContent = "";
  btn.disabled = true;
  btn.textContent = "Starting...";
  try {
    const res = await fetch("/mode", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        mode,
        settings: {
          skill: Number(setSkill.value),
          think: Number(setThink.value),
          move_delay: Number(setDelay.value),
          noob: setNoob.checked,
        },
      }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      menuErrorEl.textContent = body.error || res.statusText;
      return;
    }
    // Skill is an engine option, not a mode setting, so it goes the usual way.
    await fetch("/engine", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ skill: Number(setSkill.value) }),
    });
    applyMode(body.mode);
  } catch (e) {
    menuErrorEl.textContent = String(e);
  } finally {
    btn.disabled = false;
    btn.textContent = "Start";
    menuCardsEl.dataset.signature = "";  // force a redraw of the buttons
  }
}

const backBtn = document.getElementById("backBtn");
const resetBtn = document.getElementById("resetBtn");
const menuResetBtn = document.getElementById("menuResetBtn");

backBtn.onclick = async () => {
  backBtn.disabled = true;
  try {
    const res = await fetch("/mode/stop", { method: "POST" });
    const body = await res.json().catch(() => ({}));
    if (body.park_error) alert("The arm did not park: " + body.park_error);
    applyMode("menu");
    moveLogEl.innerHTML = "";
    lastMoveEl.textContent = "";
    lastMoveSeq = -1;
  } finally {
    backBtn.disabled = false;
  }
};

async function doReset(btn, withGame) {
  // The carriage crosses the whole board to reach (0,0) and will shove
  // anything standing in the way, so this always asks first.
  if (!confirm((withGame ? "Reset the game and park the arm?" : "Park the arm?") +
               "\\n\\nThe carriage will drive to " + PARK_SQUARE +
               ", crossing the whole board. Clear its path and keep hands clear.")) {
    return;
  }
  btn.disabled = true;
  try {
    const res = await fetch("/reset", { method: "POST" });
    const body = await res.json().catch(() => ({}));
    if (!res.ok || body.error) alert("Reset: " + (body.error || res.statusText));
    if (withGame) { moveLogEl.innerHTML = ""; lastMoveEl.textContent = ""; lastMoveSeq = -1; }
  } finally {
    btn.disabled = false;
  }
}

resetBtn.onclick = () => doReset(resetBtn, true);
menuResetBtn.onclick = () => doReset(menuResetBtn, false);

async function poll() {
  try {
    // ?editing=1 refreshes the server-side pause while the editor is open;
    // if this tab goes away, the pause lapses and tracking resumes.
    const res = await fetch("/board.json" + (editMatrix ? "?editing=1" : ""), { cache: "no-store" });
    const data = await res.json();

    if (data.park_square) PARK_SQUARE = data.park_square;
    seedSettings(data.defaults);
    applyMode(data.mode);
    if (data.mode === "menu") {
      // One poll drives both screens; on the menu there is no board to draw.
      renderMenu(data.modes);
      menuResetBtn.style.display = data.has_robot ? "inline-block" : "none";
      statusEl.textContent = "";
      lastOk = Date.now();
      statusEl.classList.remove("stale");
      setTimeout(poll, 500);
      return;
    }

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

# The page is a static string, so the one rig-dependent value in it is
# substituted here rather than at request time.
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


def _game_over_reason(board):
    """Why a finished game finished, in words. python-chess's result() gives
    the score but not the cause, and "1/2-1/2" alone leaves you guessing
    whether the arm stalled or the position is genuinely drawn."""
    if board.is_checkmate():
        return "checkmate"
    if board.is_stalemate():
        return "stalemate"
    if board.is_insufficient_material():
        return "insufficient material"
    if board.is_seventyfive_moves():
        return "75-move rule"
    if board.is_fivefold_repetition():
        return "fivefold repetition"
    return "no legal moves"


class EngineController:
    """Runs the engine on its own thread and publishes the move to place.

    Deliberately not driven inline from TrackingLoop.on_update: that fires
    with the loop's lock held, so a ~0.5s search there would stall every
    /board.json poll. on_update just pokes the event; this thread does the
    thinking.
    """

    def __init__(self, loop, engine, think_s=DEFAULT_THINK_S, robot=None,
                 both_sides=False, move_delay_s=0.0, noob=False):
        self._loop = loop
        self._engine = engine
        self._think_s = think_s
        self._robot = robot
        # AI vs AI: one engine plays itself and the arm places every move, so
        # the colour gate comes off and each completed move re-pokes the
        # thread. move_delay_s is purely so a human can follow along.
        self._both_sides = both_sides
        self._move_delay_s = move_delay_s
        # How a move gets picked. Plain best_move, or the beginner-ish,
        # weave-averse policy -- same signature either way.
        self._noob = noob
        self._pick_move = move_policy.choose_move if noob else (
            lambda engine, board, think_s: engine.best_move(board, think_s)
        )
        self._lock = Lock()
        self._wake = Event()
        self._enabled = False
        self._thinking = False
        self._headline = None
        self._extra = None
        self._message = None if engine.available else engine.error
        # Set by close() to end _run. A mode switch builds a new controller
        # around the same shared engine, so without this each switch would
        # leak a thread that goes on driving the old mode's loop.
        self._stopped = Event()
        self._thread = Thread(target=self._run, daemon=True)
        self._thread.start()

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
                "style": "noob" if self._noob else "normal",
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

    def attach_robot(self, robot):
        """Hands the controller its arm after the fact.

        Session builds this controller first (it binds to the loop) and the
        RobotController second (it binds to the same loop), so the link has
        to be made from outside rather than in either constructor.
        """
        with self._lock:
            self._robot = robot

    def notify(self):
        self._wake.set()

    def _run(self):
        while not self._stopped.is_set():
            self._wake.wait(timeout=1.0)
            self._wake.clear()
            if self._stopped.is_set():
                return
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
        # Engine plays Black -- unless it's playing both sides -- and only
        # when nothing is already pending.
        if self._loop.expected_move is not None:
            return
        if not self._both_sides and self._loop.turn != "black":
            return

        board = self._loop.board_copy
        if board.is_game_over():
            with self._lock:
                self._headline, self._extra = None, None
                self._message = f"game over -- {board.result()} ({_game_over_reason(board)})"
            return

        with self._lock:
            self._thinking = True
            self._message = None
        move = self._pick_move(self._engine, board, self._think_s)
        headline, extra = describe_move(board, move) if move is not None else (None, None)

        with self._lock:
            self._thinking = False
            self._headline, self._extra = headline, extra
        if move is None:
            return

        # Arm verification before the arm moves: whatever ends up on the
        # board next -- robot or human -- must match this move or it flags.
        self._loop.set_expected_move(move)

        if self._both_sides and (self._robot is None or not self._robot.ready):
            # Nobody else is going to place this move, so leaving it armed
            # would wedge the game: _maybe_move returns early for as long as
            # an expected move is pending. Clear it and say why, so homing
            # the arm is enough to get going again.
            self._loop.set_expected_move(None)
            with self._lock:
                state = self._robot.state() if self._robot is not None else {}
                self._message = (state.get("message")
                                 or "the arm is not homed -- press Home / re-enable")
            return

        if self._robot is not None and self._robot.ready:
            # Blocking, but this is the engine's own thread with no lock
            # held, which is exactly why the search lives here too.
            ok, error = self._robot.execute(board, move)
            if not ok:
                with self._lock:
                    self._message = f"robot stopped: {error}"
                return
            if self._both_sides:
                # Nobody else is going to wake us: in AI vs AI the arm's own
                # move *is* the board update, so the chain has to re-poke
                # itself. A failed move deliberately doesn't -- the robot is
                # halted and a human has to look at it.
                time.sleep(self._move_delay_s)
                self.notify()

    def close(self, timeout=2.0):
        """Stops the thread. Deliberately does NOT close the engine: the
        Stockfish process is shared across modes and owned by whoever built
        it. A move already in flight finishes -- the arm is mid-sequence and
        cutting it loose is worse than waiting."""
        self._stopped.set()
        with self._lock:
            self._enabled = False
        self._wake.set()
        self._thread.join(timeout=timeout)


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


def start_server(host, port, buffer, session):
    """HTTP server that reads `session` live rather than closing over one
    mode's objects, so a mode switch is visible to the very next request.

    Every game route answers 409 when nothing is running: the menu is a real
    state, not a degenerate game, and a stale tab polling /board.json after a
    stop must get a clear answer rather than an exception.
    """
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
            elif path == "/state.json":
                self._send_json(200, self._state_payload())
            elif path == "/board.json":
                loop = session.loop
                if loop is None:
                    # On the menu: answer with the session state alone, so
                    # one poll drives both screens.
                    self._send_json(200, self._state_payload())
                    return
                # The UI polls with ?editing=1 while its board editor is
                # open; that refreshes the pause so tracking stays held.
                # Stop polling (close the tab) and the pause lapses.
                if "editing=1" in query:
                    loop.set_paused(True)
                matrix, updated_at, last_move, move_seq, flagged, flag_reason = buffer.get_board()
                rows = matrix if matrix is not None else [[None] * 8 for _ in range(8)]
                robot_controller = session.robot_controller
                engine_controller = session.engine_controller
                payload = self._state_payload()
                payload.update(
                    {
                        "matrix": rows,
                        "updated_at": updated_at,
                        "last_move": last_move,
                        "move_seq": move_seq,
                        "flagged": flagged,
                        "flag_reason": flag_reason,
                        "turn": loop.turn,
                        "paused": loop.is_paused,
                        "engine": engine_controller.state() if engine_controller else None,
                        "robot": robot_controller.state() if robot_controller else None,
                    }
                )
                self._send_json(200, payload)
            elif path == "/stream.mjpg":
                if session.capture_stream is None:
                    # No camera in this mode. Answering rather than streaming
                    # nothing forever keeps a stray <img> from holding a
                    # ThreadingHTTPServer thread open for the whole game.
                    self.send_error(503, "no camera in this mode")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.end_headers()
                try:
                    while session.capture_stream is not None:
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

        def _state_payload(self):
            state = session.state()
            # The park corner belongs in the payload, not baked into PAGE:
            # --board-origin is applied after this module is imported, so a
            # page built at import time would name the wrong square.
            state["park_square"] = rig.ORIGIN_SQUARE
            return state

        def do_POST(self):
            path, _, _query = self.path.partition("?")
            known = ("/board/correct", "/board/undo", "/board/pause", "/engine",
                     "/robot", "/mode", "/mode/stop", "/reset")
            if path not in known:
                self.send_error(404)
                return

            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                self._send_json(400, {"error": "invalid JSON body"})
                return

            # ---- mode control: valid on the menu, unlike everything else ----

            if path == "/mode":
                try:
                    self._send_json(200, {"ok": True, **session.start(
                        body.get("mode"), body.get("settings"))})
                except ModeError as exc:
                    self._send_json(400, {"error": str(exc)})
                except Exception as exc:  # a camera or model that won't load
                    self._send_json(500, {"error": f"could not start: {exc}"})
                return

            if path == "/mode/stop":
                self._send_json(200, {"ok": True, **session.stop()})
                return

            if path == "/reset":
                ok, error = session.reset()
                payload = {"ok": ok, **self._state_payload()}
                if not ok:
                    payload["error"] = error
                self._send_json(200 if ok else 400, payload)
                return

            # ---- everything below needs a running mode --------------------

            loop = session.loop
            engine_controller = session.engine_controller
            robot_controller = session.robot_controller
            if loop is None:
                self._send_json(409, {"error": "no mode is running -- pick one first"})
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
                if body.get("confirm"):
                    # The human has lifted the captured piece. Releases the
                    # robot thread, which is blocked mid-sequence.
                    if not robot_controller.confirm():
                        self._send_json(400, {"error": "nothing is waiting for confirmation"})
                        return
                    self._send_json(200, {"ok": True, "robot": robot_controller.state()})
                    return
                if body.get("cancel"):
                    if not robot_controller.cancel():
                        self._send_json(400, {"error": "nothing is waiting for confirmation"})
                        return
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
                self._send_json(400, {"error": "expected one of home/halt/confirm/cancel"})
                return

            if path == "/board/pause":
                loop.set_paused(bool(body.get("paused")))
                self._send_json(200, {"ok": True, "paused": loop.is_paused})
                return

            if path == "/board/undo":
                san = loop.undo_last_move(self._frame())
                if san is None:
                    self._send_json(400, {"error": "nothing to undo"})
                else:
                    self._send_json(200, {"ok": True, "undone": san})
                return

            error = _validate_correction(body)
            if error is not None:
                self._send_json(400, {"error": error})
                return

            loop.apply_manual_correction(body["matrix"], body["turn"], self._frame())
            # Saving the editor ends the edit session, so lift the pause.
            loop.set_paused(False)
            self._send_json(200, {"ok": True})

        def _frame(self):
            """The latest camera frame, or None in a mode that has none."""
            stream = session.capture_stream
            if stream is None:
                return None
            frame, _timestamp = stream.get_latest()
            return frame

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
    parser.add_argument("--ai-vs-ai", action="store_true",
                        help="Stockfish plays itself and the arm places every move. No "
                             "camera, no calibration and no classifier are used -- the "
                             "position is tracked in software, so the board MUST start "
                             "with all 32 pieces in the standard setup. Requires --robot")
    parser.add_argument("--noob", action=argparse.BooleanOptionalAction, default=None,
                        help="play like a beginner: prefer pawn moves, and move a knight "
                             "or castle only when nothing else is legal (those are the "
                             "moves that make the arm weave, which is its riskiest "
                             "motion). Costs about 2x --engine-think per move. "
                             "Default: on with --ai-vs-ai, off otherwise")
    parser.add_argument("--board-origin", default=rig.ORIGIN_SQUARE,
                        choices=rig.SUPPORTED_ORIGINS,
                        help="which real square the carriage parks on, i.e. how the board "
                             f"is seated under the gantry (default: {rig.ORIGIN_SQUARE}, "
                             "measured on this rig). 'h1' is the firmware's own "
                             "assumption and applies no rotation")
    parser.add_argument("--move-delay", type=float, default=1.0,
                        help="--ai-vs-ai only: seconds to pause after each completed move, "
                             "so the game is watchable and you have time to hit Halt")
    parser.add_argument("--robot", default=None, metavar="PORT",
                        help="serial port of the gantry Arduino (e.g. /dev/ttyACM0), 'auto' "
                             "to detect it, or 'mock' for a dry run. Omitted: no arm, you "
                             "place Black's moves by hand as before")
    parser.add_argument("--robot-protocol", default="legacy", choices=("legacy", "native"),
                        help="which firmware is flashed. 'legacy' (default) is "
                             "firmware/chessbot_v1, what the machine actually runs; "
                             "'native' is firmware/chess_gantry, the unbuilt limit-switch rig")
    parser.add_argument("--topple-delay", type=float, default=DEFAULT_TOPPLE_DELAY_S,
                        help="native protocol only: seconds to wait after toppling a "
                             "captured piece, for you to lift it off. The legacy firmware "
                             "waits for you to confirm instead, with no time limit")
    parser.add_argument("--harvest", type=Path, nargs="?", const=DEFAULT_HARVEST, default=None,
                        help="save labelled crops from every resolved move, to grow the training "
                             f"set as you play (default dir: {DEFAULT_HARVEST})")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="0.0.0.0")
    return parser.parse_args()


def _open_robot(args):
    """Opens the gantry, or exits with a readable reason. Shared by both
    modes so the no-limit-switch warning is only written once."""
    # How the board is seated under the gantry is a property of the machine,
    # not of a single move, so it lives on rig rather than being threaded
    # through every planner call. Both planners read it from there.
    rig.ORIGIN_SQUARE = args.board_origin
    try:
        robot = open_gantry(args.robot, topple_delay_s=args.topple_delay,
                            protocol=args.robot_protocol)
    except GantryError as exc:
        raise SystemExit(f"Robot: {exc}")
    except ImportError:
        raise SystemExit("Robot: pyserial is missing -- pip install -r requirements.txt")

    print(f"Robot: gantry on {robot.port} ({args.robot_protocol} protocol).")
    if args.robot_protocol == "legacy":
        # No limit switches on this build: HOME drives to the assumed origin
        # rather than seeking it, so "homed" is a promise the human makes,
        # not something the machine measured.
        #
        # Name the square the human can actually see. rig.PARK_SQUARE is the
        # firmware's idea of the origin; ORIGIN_SQUARE is which real corner
        # that is on this rig, and telling them the wrong one is how the
        # whole board ends up rotated.
        print(f"Robot: park the carriage on {rig.ORIGIN_SQUARE} before homing -- "
              "there are no limit switches to find it.")
        if rig.ORIGIN_SQUARE != rig.PARK_SQUARE:
            print(f"Robot: board origin {rig.ORIGIN_SQUARE} -- squares are rotated "
                  "before they reach the firmware.")
    return robot


def _make_builders(args, engine, buffer, session_ref):
    """The two per-mode stacks, as callables for Session.

    Both are closures rather than methods so session.py stays free of the
    camera stack entirely -- importing picamera2 or ncnn is what would stop
    --ai-vs-ai running off the Pi, and that import must not happen until
    normal mode is actually asked for.

    Each returns (loop, capture_stream, engine_controller, tick).
    """

    def on_update_factory(harvester=None):
        def on_update(matrix, move_text, frame, flagged, reason):
            buffer.set_board(matrix, move_text, flagged, reason)
            if harvester is not None and move_text is not None and not flagged:
                harvester.record(matrix, frame)
            session = session_ref()
            # An unresolvable settle right after the arm moved means the
            # physical board and the tracked position have diverged. Stop the
            # arm before it stacks another move on top.
            if flagged and session is not None and session.robot_controller is not None:
                session.robot_controller.note_flag(reason)
            # Just a poke -- the engine thinks on its own thread, since this
            # runs with the loop's lock held.
            if session is not None and session.engine_controller is not None:
                session.engine_controller.notify()

        return on_update

    def setting(settings, name, fallback):
        value = settings.get(name)
        return fallback if value is None else value

    def build_ai(settings):
        """No camera anywhere in this path: HeadlessLoop is the position, so
        the physical board must start in the standard 32-piece setup or
        everything after the first move is a lie. Nothing verifies that."""
        loop = HeadlessLoop(on_update=on_update_factory())
        buffer.set_board(loop.current_matrix, None, False, None)  # seed the UI
        controller = EngineController(
            loop, engine,
            think_s=setting(settings, "think", args.engine_think),
            robot=None,  # attached below, once Session has built it
            both_sides=True,
            move_delay_s=setting(settings, "move_delay", args.move_delay),
            noob=bool(setting(settings, "noob", True)),
        )
        return loop, None, controller, None

    def build_normal(settings):
        # Imported here, not at module scope: picamera2 and the ncnn loader
        # are the reason this mode can't run off the Pi, and AI vs AI must not
        # pay for them.
        from capture import Camera, CaptureStream
        from harvest import CropHarvester
        from square_classifier import load_classifier
        from square_geometry import square_pixel_bboxes
        from tracking_loop import TrackingLoop

        calibration_matrix = load_calibration(args.calibration)
        classifier_model = load_classifier(str(args.classifier))

        camera = Camera()
        camera.open()
        stream = CaptureStream(camera)
        stream.start()
        frame = None
        while frame is None:
            frame, _timestamp = stream.get_latest()
        image_size = (frame.shape[1], frame.shape[0])

        harvester = None
        if args.harvest is not None:
            harvester = CropHarvester(
                args.harvest, square_pixel_bboxes(calibration_matrix, image_size)
            )

        loop = TrackingLoop(
            capture_stream=stream,
            calibration_matrix=calibration_matrix,
            image_size=image_size,
            classifier_model=classifier_model,
            on_update=on_update_factory(harvester),
            poll_interval=args.poll_interval,
            classifier_min_conf=args.min_conf,
            motion_thresh=args.motion_thresh,
        )
        buffer.set_board(loop.current_matrix, None, False, None)
        controller = EngineController(
            loop, engine,
            think_s=setting(settings, "think", args.engine_think),
            robot=None,
            both_sides=False,
            noob=bool(setting(settings, "noob", False)),
        )

        def tick():
            live_frame, _timestamp = stream.get_latest()
            if live_frame is not None:
                buffer.set_frame(live_frame)
            loop.tick()

        # CaptureStream.stop() is the teardown hook; Session calls .close().
        stream.close = lambda: (stream.stop(), camera.close())
        return loop, stream, controller, tick

    return build_normal, build_ai


def main():
    args = parse_args()

    engine = ChessEngine(command=args.engine_command, skill=args.engine_skill)
    print("Engine: Stockfish ready." if engine.available else f"Engine: {engine.error}")

    robot = _open_robot(args) if args.robot else None
    if robot is not None:
        # Once, here: the port stays open for the life of the process, so a
        # mode switch never costs a re-home. The human has already parked the
        # carriage -- this is where that promise is cashed in.
        print("Homing the gantry -- keep hands clear...")
        try:
            robot.home()
            print("Robot: homed and ready.")
        except GantryError as exc:
            print(f"Robot: {exc}")

    buffer = BoardBuffer()
    holder = {}
    build_normal, build_ai = _make_builders(args, engine, buffer, lambda: holder.get("session"))

    session = Session(
        engine, robot=robot,
        calibration=args.calibration, classifier=args.classifier,
        build_normal=build_normal, build_ai=build_ai,
        poll_interval=args.poll_interval,
        defaults={
            "skill": args.engine_skill,
            "think": args.engine_think,
            "move_delay": args.move_delay,
            "noob": True if args.noob is None else args.noob,
        },
    )
    holder["session"] = session

    try:
        start_server(args.host, args.port, buffer, session)
        print(f"Serving at http://<this-pi>:{args.port}/")

        if args.ai_vs_ai:
            # The old command line still lands straight in the game.
            session.start(AI_VS_AI, {"noob": True if args.noob is None else args.noob,
                                     "move_delay": args.move_delay,
                                     "think": args.engine_think})
            print("Started AI vs AI. Set up all 32 pieces, then press play.")
        else:
            print("Open the page and pick a mode.")

        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("Stopped.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
