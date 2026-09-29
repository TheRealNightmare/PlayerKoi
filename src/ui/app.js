// ---- dialogs ---------------------------------------------------------
//
// Native confirm()/alert() cannot be styled at all, and they carry the
// warnings that matter most here -- "the arm will cross the whole board".
// These replace them with the in-page dialog, keeping the same await-shaped
// call sites. The native <dialog> element does focus trapping, Escape and
// the top layer, so none of that is hand-rolled.

const nrDialog = document.getElementById("nrDialog");
const nrDialogTitle = document.getElementById("nrDialogTitle");
const nrDialogText = document.getElementById("nrDialogText");
const nrDialogOk = document.getElementById("nrDialogOk");
const nrDialogCancel = document.getElementById("nrDialogCancel");

function nrAsk({ title, text, okLabel = "OK", okColor = "green", cancel = true }) {
  nrDialogTitle.textContent = title;
  nrDialogText.textContent = text;
  nrDialogOk.textContent = okLabel;
  nrDialogOk.dataset.color = okColor;
  nrDialogCancel.hidden = !cancel;
  return new Promise((resolve) => {
    nrDialog.addEventListener("close", () => resolve(nrDialog.returnValue === "ok"), { once: true });
    nrDialog.showModal();
    // Focus Cancel, not OK: every confirm here guards something physical, so
    // a stray Enter must not start the arm moving.
    (cancel ? nrDialogCancel : nrDialogOk).focus();
  });
}

const nrConfirm = (title, text, opts = {}) => nrAsk({ title, text, ...opts });
const nrAlert = (title, text) =>
  nrAsk({ title, text, okLabel: "Dismiss", okColor: "black", cancel: false });

const GLYPHS = {
  "white-king": "\u2654", "white-queen": "\u2655", "white-rook": "\u2656",
  "white-bishop": "\u2657", "white-knight": "\u2658", "white-pawn": "\u2659",
  "black-king": "\u265A", "black-queen": "\u265B", "black-rook": "\u265C",
  "black-bishop": "\u265D", "black-knight": "\u265E", "black-pawn": "\u265F",
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

// Extra highlights and arrows, set each poll by the coach and puzzle renderers
// and drawn by render(). Never shown over the board editor.
let boardMarks = {};   // square name -> [class, ...]
let boardArrows = [];  // [{ from: "e2", to: "e4", color: "bright-green" }]
const MARK_CLASSES = ["hint-from", "hint-to", "hint-piece", "diff-add", "diff-remove",
                      "diff-swap", "mismatch"];
const arrowsEl = document.getElementById("arrows");
const SVG_NS = "http://www.w3.org/2000/svg";

function squareName(file, rank) {
  return "abcdefgh"[file] + (rank + 1);
}

function squareCenter(name) {
  // viewBox is 0..8 in both axes, rank 8 at the top: White sits at the bottom.
  const file = "abcdefgh".indexOf(name[0]);
  const rank = Number(name[1]) - 1;
  return [file + 0.5, 7 - rank + 0.5];
}

function drawArrows(list) {
  arrowsEl.replaceChildren();
  for (const a of list) {
    const [x1, y1] = squareCenter(a.from);
    const [x2, y2] = squareCenter(a.to);
    const len = Math.hypot(x2 - x1, y2 - y1) || 1;
    const ux = (x2 - x1) / len, uy = (y2 - y1) / len;
    const head = 0.45, half = 0.26;
    const bx = x2 - ux * head, by = y2 - uy * head;
    // Drawn as a line plus a hand-built head rather than an SVG marker, so
    // the colour can be a theme variable (markers don't inherit style).
    const line = document.createElementNS(SVG_NS, "line");
    line.setAttribute("x1", x1); line.setAttribute("y1", y1);
    line.setAttribute("x2", bx); line.setAttribute("y2", by);
    line.setAttribute("style", `stroke: var(--${a.color}); stroke-width: 0.17; stroke-linecap: round`);
    const tip = document.createElementNS(SVG_NS, "polygon");
    tip.setAttribute("points",
      `${x2},${y2} ${bx - uy * half},${by + ux * half} ${bx + uy * half},${by - ux * half}`);
    tip.setAttribute("style", `fill: var(--${a.color})`);
    arrowsEl.append(line, tip);
  }
}

function render(matrix) {
  const editing = !!editMatrix;
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
    for (const c of MARK_CLASSES) el.classList.remove(c);
    if (!editing) for (const c of boardMarks[name] || []) el.classList.add(c);
  }
  drawArrows(editing ? [] : boardArrows);
}

function addMark(name, cls) {
  (boardMarks[name] = boardMarks[name] || []).push(cls);
}

function closePicker() { pickerEl.style.display = "none"; }

function openPicker(cellInfo) {
  pickerEl.innerHTML = "";
  for (const label of PICKER_OPTIONS) {
    const opt = document.createElement("div");
    opt.className = "opt" + (label ? " " + label.split("-")[0] + "-piece" : "");
    opt.textContent = label ? (GLYPHS[label] || "?") : "\u2716";
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
const modeBadgeEl = document.getElementById("modeBadge");
const MODE_LABELS = { menu: "menu", ai_vs_ai: "AI vs AI", normal: "play the engine",
                      guided: "guided game", puzzle: "puzzles" };
const MODE_COLORS = { ai_vs_ai: "magenta", normal: "cyan", guided: "green", puzzle: "yellow" };
const engineBox = document.getElementById("engineBox");
const coachBox = document.getElementById("coachBox");
const puzzleBox = document.getElementById("puzzleBox");
const evalBar = document.getElementById("evalBar");

function applyMode(mode) {
  if (mode === currentMode) return;
  currentMode = mode;
  const inGame = mode && mode !== "menu";
  menuEl.style.display = inGame ? "none" : "flex";
  gameEl.style.display = inGame ? "flex" : "none";

  const ai = mode === "ai_vs_ai";
  const guided = mode === "guided";
  const puzzle = mode === "puzzle";
  modeBadgeEl.textContent = MODE_LABELS[mode] || mode || "";
  modeBadgeEl.dataset.color = inGame ? (MODE_COLORS[mode] || "cyan") : "black";
  videoCol.style.display = ai || !inGame ? "none" : "flex";
  aiNoteEl.hidden = !ai;
  coachBox.hidden = !guided;
  evalBar.hidden = !guided;
  puzzleBox.hidden = !puzzle;
  // Puzzles have no engine opponent: Black's replies are the stored line.
  engineBox.hidden = puzzle;
  boardMarks = {};
  boardArrows = [];
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
const staleFirmwareEl = document.getElementById("staleFirmware");
const polarityRadios = document.querySelectorAll('input[name="polarity"]');

// Which way the coil drives to hold a piece -- every piece, white and black
// alike. Attract is what the set was magnetised for; repel is here in case
// the magnets are fitted the other way up, so fixing it is a click, not a
// reflash.
for (const radio of polarityRadios) {
  radio.onchange = () => {
    if (radio.checked) postRobot({ polarity: radio.value }, radio);
  };
}
// Grip/settle pauses. Every carry is: coil off, drive to the piece, coil on
// + Grip, carry, coil off + Settle. A piece still towed after a set-down
// means Settle is too short for the core's residual magnetism to die away.
const TUNING = [
  { key: "grip_ms", el: document.getElementById("tuneGrip"),
    val: document.getElementById("tuneGripVal"), unit: " ms" },
  { key: "settle_ms", el: document.getElementById("tuneSettle"),
    val: document.getElementById("tuneSettleVal"), unit: " ms" },
  // Knights, the castling rook and graveyard trips; straight moves are always full.
  { key: "grid_pct", el: document.getElementById("tuneGridPct"),
    val: document.getElementById("tuneGridPctVal"), unit: "%" },
];
for (const t of TUNING) {
  t.el.oninput = () => { t.val.textContent = t.el.value + t.unit; };
  t.el.onchange = () => postRobot({ [t.key]: Number(t.el.value) }, t.el);
}
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
    if (!res.ok) await nrAlert("Robot", body.error || res.statusText);
  } catch (e) {
    await nrAlert("Robot", "Robot command failed: " + e);
  } finally {
    if (button) button.disabled = false;
  }
}

haltBtn.onclick = () => postRobot({ halt: true }, haltBtn);
confirmBtn.onclick = () => postRobot({ confirm: true }, confirmBtn);
homeBtn.onclick = async () => {
  // No limit switches: HOME drives to where it ASSUMES the origin is, so
  // this only tells the truth if the carriage really is parked there.
  const ok = await nrConfirm(
    "Is the carriage parked in the corner beyond " + PARK_SQUARE + "?",
    "There are no limit switches -- homing drives to the assumed origin " +
    "rather than finding it. If it is parked anywhere else, every move " +
    "afterwards will be wrong.\n\n" +
    "It will cross the whole board. Keep hands clear.",
    { okLabel: "Yes, home it", okColor: "yellow" });
  if (ok) postRobot({ home: true }, homeBtn);
};

function renderRobot(bot) {
  if (!bot) { robotBox.style.display = "none"; return; }
  robotBox.style.display = "flex";
  robotBox.classList.toggle("halted", !!bot.halted);

  let state = "ready";
  let color = "green";
  if (bot.halted) { state = "HALTED"; color = "red"; }
  else if (bot.busy) { state = "moving"; color = "blue"; }
  else if (!bot.homed) { state = "not homed"; color = "yellow"; }
  // A blocking prompt outranks everything else in the status line: the arm
  // is stopped mid-move and nothing continues until it is answered.
  if (bot.awaiting_confirm) { state = "WAITING FOR YOU"; color = "yellow"; }
  robotStateEl.textContent = state + " (" + bot.port + ")";
  robotStateEl.dataset.color = color;

  // An out-of-date board accepts every command and silently attracts for all
  // of them, so it would shove a white piece off the table. Say so plainly.
  staleFirmwareEl.hidden = !bot.stale_firmware;
  if (bot.stale_firmware) staleFirmwareEl.textContent = bot.message || "firmware is out of date";

  for (const radio of polarityRadios) {
    radio.disabled = !!bot.stale_firmware;
    // Don't fight a click that is still in flight.
    if (document.activeElement !== radio) radio.checked = bot.polarity === radio.value;
  }
  for (const t of TUNING) {
    t.el.disabled = !!bot.stale_firmware;
    const v = bot.tuning ? bot.tuning[t.key] : undefined;
    if (document.activeElement !== t.el && v !== undefined && v !== null) {
      t.el.value = v;
      t.val.textContent = v + t.unit;
    }
  }
  robotNoteEl.textContent = bot.note || "";
  // The alerts are real panels now, so an empty one would still draw a box.
  const prompt = bot.awaiting_confirm || bot.prompt || "";
  robotPromptEl.textContent = prompt;
  robotPromptEl.hidden = !prompt;
  robotMsgEl.textContent = bot.message || "";
  robotMsgEl.hidden = !bot.message;
  haltBtn.disabled = bot.halted;
  confirmBtn.style.display = bot.awaiting_confirm ? "" : "none";
}

undoBtn.onclick = async () => {
  undoBtn.disabled = true;
  try {
    const res = await fetch("/board/undo", { method: "POST" });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      await nrAlert("Undo", body.error || res.statusText);
      return;
    }
    logMove("(undid " + body.undone + ")");
    lastMoveEl.textContent = "";
  } catch (e) {
    await nrAlert("Undo", "Could not undo: " + e);
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
      await nrAlert("Save", "Could not save: " + (body.error || res.statusText));
      return;
    }
    exitEditMode();
  } catch (e) {
    await nrAlert("Save", "Could not save: " + e);
  } finally {
    saveBtn.disabled = false;
  }
};

// ---- the coach (guided game) ----------------------------------------

const evalFill = document.getElementById("evalFill");
const evalLabel = document.getElementById("evalLabel");
const coachDecisionEl = document.getElementById("coachDecision");
const coachDecisionTextEl = document.getElementById("coachDecisionText");
const coachTakebackEl = document.getElementById("coachTakeback");
const coachReviewEl = document.getElementById("coachReview");
const coachReviewTextEl = document.getElementById("coachReviewText");
const coachBetterEl = document.getElementById("coachBetter");
const coachOpponentEl = document.getElementById("coachOpponent");
const coachHintLabelEl = document.getElementById("coachHintLabel");
const coachHintEl = document.getElementById("coachHint");
const coachHintPlaceEl = document.getElementById("coachHintPlace");
const coachHintTextEl = document.getElementById("coachHintText");
const takebackBtn = document.getElementById("takebackBtn");
const continueBtn = document.getElementById("continueBtn");

const LABEL_COLORS = { Best: "green", Good: "blue", Inaccuracy: "yellow",
                       Mistake: "magenta", Blunder: "red" };

function winPercent(ev) {
  // The same curve the server grades with (coach.win_percent).
  if (ev.mate !== null && ev.mate !== undefined) return ev.mate > 0 ? 100 : 0;
  const cp = ev.cp || 0;
  return 50 + 50 * (2 / (1 + Math.exp(-0.00368 * cp)) - 1);
}

function renderEval(ev) {
  if (!ev) { evalFill.style.height = "50%"; evalLabel.textContent = ""; return; }
  evalFill.style.height = winPercent(ev) + "%";
  if (ev.mate !== null && ev.mate !== undefined) {
    evalLabel.textContent = (ev.mate > 0 ? "" : "-") + "M" + Math.abs(ev.mate);
  } else {
    const pawns = (ev.cp || 0) / 100;
    evalLabel.textContent = (pawns >= 0 ? "+" : "") + pawns.toFixed(1);
  }
}

function renderCoach(c) {
  if (!c) return;
  renderEval(c.eval);

  const r = c.review;
  coachReviewEl.hidden = !r;
  if (r) {
    coachReviewEl.textContent = r.san + " \u2014 " + r.label;
    coachReviewEl.dataset.color = LABEL_COLORS[r.label] || "black";
  }
  coachReviewTextEl.textContent = r ? r.text : "";
  coachBetterEl.textContent = r && r.better_san
    ? "Better was " + r.better_san + (r.better_line ? "  (" + r.better_line.join(" ") + ")" : "")
    : "";
  coachOpponentEl.textContent = c.opponent_note || "";

  const h = c.hint;
  coachHintLabelEl.hidden = !h;
  coachHintEl.textContent = h ? h.san : "";
  coachHintPlaceEl.textContent = h ? h.headline + (h.extra ? " \u2014 " + h.extra : "") : "";
  coachHintTextEl.textContent = h ? h.text : "";
  // The suggestion goes on the board too, unless a Black move is waiting to
  // be placed -- that move's own highlight is the one that matters then.
  if (h && !expectedUci) {
    const from = h.uci.slice(0, 2), to = h.uci.slice(2, 4);
    addMark(from, "hint-from");
    addMark(to, "hint-to");
    boardArrows.push({ from, to, color: "bright-green" });
  }

  coachDecisionEl.hidden = !c.awaiting_decision;
  if (c.awaiting_decision && r) {
    coachDecisionTextEl.textContent = r.label + ": " + r.san + " \u2014 " + r.text +
      ". The arm is waiting. Take it back?";
  }
  coachTakebackEl.hidden = !c.takeback;
  coachTakebackEl.textContent = c.takeback ? "On the board: " + c.takeback : "";
}

async function postCoach(decision, btn) {
  btn.disabled = true;
  try {
    const res = await fetch("/coach", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) await nrAlert("Coach", body.error || res.statusText);
  } catch (e) {
    await nrAlert("Coach", "Could not reach the coach: " + e);
  } finally {
    btn.disabled = false;
  }
}

takebackBtn.onclick = () => postCoach("takeback", takebackBtn);
continueBtn.onclick = () => postCoach("continue", continueBtn);

// ---- puzzles ----------------------------------------------------------

const puzzlePhaseEl = document.getElementById("puzzlePhase");
const puzzleInfoEl = document.getElementById("puzzleInfo");
const puzzleMsgEl = document.getElementById("puzzleMsg");
const puzzleWrongEl = document.getElementById("puzzleWrong");
const puzzleGraveyardEl = document.getElementById("puzzleGraveyard");
const puzzleStepsEl = document.getElementById("puzzleSteps");
const puzzleMoveEl = document.getElementById("puzzleMove");
const puzzleExtraEl = document.getElementById("puzzleExtra");
const puzzleSolutionEl = document.getElementById("puzzleSolution");
const puzzleRatingEl = document.getElementById("puzzleRating");
const puzzleReadyBtn = document.getElementById("puzzleReadyBtn");
const puzzleHintBtn = document.getElementById("puzzleHintBtn");
const puzzleSolutionBtn = document.getElementById("puzzleSolutionBtn");
const puzzleSkipBtn = document.getElementById("puzzleSkipBtn");
const puzzleNextBtn = document.getElementById("puzzleNextBtn");

const PHASES = {
  setup: ["Set up the board", "yellow"],
  opponent: ["Black is moving", "blue"],
  solving: ["White to move \u2014 find the best move", "green"],
  solved: ["Solved!", "green"],
  revealed: ["Solution shown", "magenta"],
};

// The board a puzzle wants shown: the layout to build during setup, the live
// board after that.
function puzzleBoard(p) {
  return p && p.phase === "setup" && p.target ? p.target : null;
}

function renderPuzzle(p) {
  if (!p) return;
  const [phaseText, phaseColor] = PHASES[p.phase] || ["No puzzle", "black"];
  puzzlePhaseEl.textContent = phaseText;
  puzzlePhaseEl.dataset.color = phaseColor;

  const info = p.puzzle;
  puzzleInfoEl.textContent = info
    ? "Rated " + info.rating + " \u00b7 " + info.pieces + " pieces" +
      (info.themes.length ? " \u00b7 " + info.themes.join(", ") : "")
    : "";

  puzzleMsgEl.hidden = !p.message;
  puzzleMsgEl.textContent = p.message || "";
  puzzleWrongEl.hidden = !p.wrong_note;
  puzzleWrongEl.textContent = p.wrong_note ? "Not quite: " + p.wrong_note : "";

  const setup = p.phase === "setup";
  puzzleGraveyardEl.hidden = !(setup && p.graveyard_pieces > 0);
  puzzleGraveyardEl.textContent = "First clear the " + p.graveyard_pieces +
    " captured piece(s) off the ring around the board.";

  // The to-do list. Rebuilt only when it changes, so it doesn't flicker.
  const steps = setup && p.diff ? p.diff.steps : [];
  const signature = JSON.stringify(steps);
  if (puzzleStepsEl.dataset.signature !== signature) {
    puzzleStepsEl.dataset.signature = signature;
    puzzleStepsEl.replaceChildren(...steps.map((text) => {
      const li = document.createElement("li");
      li.textContent = text;
      return li;
    }));
    if (setup && !steps.length) {
      const li = document.createElement("li");
      li.textContent = "Nothing to change \u2014 the board already matches.";
      puzzleStepsEl.append(li);
    }
  }
  puzzleStepsEl.hidden = !setup;
  if (setup && p.diff) {
    for (const sq of p.diff.add) addMark(sq, "diff-add");
    for (const sq of p.diff.remove) addMark(sq, "diff-remove");
    for (const sq of p.diff.swap) addMark(sq, "diff-swap");
  }
  if (setup && p.mismatches) for (const m of p.mismatches) addMark(m.square, "mismatch");

  puzzleMoveEl.textContent = p.phase === "opponent" ? (p.instruction || "") : "";
  puzzleExtraEl.textContent = p.phase === "opponent" ? (p.extra || "") : "";

  if (p.hint_square) addMark(p.hint_square, "hint-piece");
  puzzleSolutionEl.textContent = p.solution ? "Solution: " + p.solution.join(" ") : "";
  if (p.solution_uci && p.phase === "revealed") {
    p.solution_uci.forEach((uci, i) => boardArrows.push({
      from: uci.slice(0, 2), to: uci.slice(2, 4),
      color: i === 0 ? "bright-green" : "bright-blue",
    }));
  }

  const prog = p.progress || {};
  let rating = "Your rating " + prog.rating;
  if (p.rating_delta !== null && p.rating_delta !== undefined && p.result) {
    rating += " (" + (p.rating_delta >= 0 ? "+" : "") + p.rating_delta + ")";
  }
  rating += " \u00b7 streak " + prog.streak;
  puzzleRatingEl.textContent = rating;

  const live = p.phase === "opponent" || p.phase === "solving";
  puzzleReadyBtn.hidden = !setup;
  puzzleHintBtn.hidden = p.phase !== "solving";
  puzzleSolutionBtn.hidden = !live;
  puzzleSkipBtn.hidden = !(setup || live);
  puzzleNextBtn.hidden = !(p.phase === "solved" || p.phase === "revealed" || !p.phase);
}

async function postPuzzle(action, btn, title) {
  btn.disabled = true;
  try {
    const res = await fetch("/puzzle", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) await nrAlert(title, body.error || res.statusText);
  } catch (e) {
    await nrAlert(title, "Could not reach the puzzle: " + e);
  } finally {
    btn.disabled = false;
  }
}

puzzleReadyBtn.onclick = async () => {
  puzzleReadyBtn.textContent = "Checking the board...";
  try {
    await postPuzzle("ready", puzzleReadyBtn, "Board doesn't match yet");
  } finally {
    puzzleReadyBtn.textContent = "OK \u2014 board is set up";
  }
};
puzzleHintBtn.onclick = () => postPuzzle("hint", puzzleHintBtn, "Hint");
puzzleSolutionBtn.onclick = async () => {
  const ok = await nrConfirm("Show the solution?",
    "The puzzle will count as failed.", { okLabel: "Show it", okColor: "magenta" });
  if (ok) postPuzzle("solution", puzzleSolutionBtn, "Solution");
};
puzzleSkipBtn.onclick = () => postPuzzle("skip", puzzleSkipBtn, "Skip");
puzzleNextBtn.onclick = () => postPuzzle("next", puzzleNextBtn, "Next puzzle");

// ---- the menu -------------------------------------------------------

const menuCardsEl = document.getElementById("menuCards");
const menuErrorEl = document.getElementById("menuError");
const setSkill = document.getElementById("setSkill");
const setSkillVal = document.getElementById("setSkillVal");
const setThink = document.getElementById("setThink");
const setDelay = document.getElementById("setDelay");
const setNoob = document.getElementById("setNoob");
let settingsSeeded = false;
const puzzleThemesEl = document.getElementById("puzzleThemes");
const puzzleProgressEl = document.getElementById("puzzleProgress");
const puzzleMin = document.getElementById("puzzleMin");
const puzzleMax = document.getElementById("puzzleMax");
const puzzleMaxPieces = document.getElementById("puzzleMaxPieces");

setSkill.oninput = () => { setSkillVal.textContent = setSkill.value; };

// Lichess theme keys, as puzzles.MENU_THEMES names them, with a readable label.
const PUZZLE_THEMES = [
  ["mateIn1", "mate in 1"], ["mateIn2", "mate in 2"], ["mateIn3", "mate in 3"],
  ["fork", "fork"], ["pin", "pin"], ["skewer", "skewer"],
  ["hangingPiece", "hanging piece"], ["discoveredAttack", "discovered attack"],
  ["sacrifice", "sacrifice"], ["endgame", "endgame"], ["short", "short (2 moves)"],
];
for (const [key, label] of PUZZLE_THEMES) {
  const wrap = document.createElement("label");
  wrap.className = "nr-label";
  const box = document.createElement("input");
  box.type = "checkbox";
  box.className = "nr-check";
  box.dataset.color = "cyan";
  box.value = key;
  wrap.append(box, " " + label);
  puzzleThemesEl.append(wrap);
}

function puzzleSettings() {
  return {
    puzzle_rating_mode: document.querySelector('input[name="puzzleRatingMode"]:checked').value,
    puzzle_min: Number(puzzleMin.value),
    puzzle_max: Number(puzzleMax.value),
    puzzle_max_pieces: Number(puzzleMaxPieces.value),
    puzzle_themes: [...puzzleThemesEl.querySelectorAll("input:checked")].map((b) => b.value),
  };
}

function renderPuzzleProgress(prog) {
  puzzleProgressEl.textContent = prog
    ? "Your puzzle rating " + prog.rating + " \u00b7 streak " + prog.streak +
      " (best " + prog.best_streak + ") \u00b7 solved " + prog.solved + " of " + prog.attempted
    : "";
}

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
    const card = document.createElement("section");
    card.className = "card nr-card nr-clr nr-border nr-shadow" + (m.available ? "" : " unavailable");
    card.dataset.color = "black";

    const header = document.createElement("div");
    header.className = "nr-card-header";
    const h = document.createElement("h2");
    h.className = "nr-card-title";
    h.textContent = m.title;
    const p = document.createElement("p");
    p.className = "nr-card-desc";
    p.textContent = m.blurb;
    header.append(h, p);
    card.appendChild(header);

    if (!m.available) {
      // An alert, not dimmed text: the reason is the most useful thing on an
      // unavailable card, so it should be the most legible.
      const reason = document.createElement("div");
      reason.className = "reason nr-alert nr-clr nr-border";
      reason.dataset.color = "yellow";
      reason.textContent = m.reason;
      card.appendChild(reason);
    }

    const footer = document.createElement("div");
    footer.className = "nr-card-footer";
    const btn = document.createElement("button");
    btn.className = "nr-btn nr-clr nr-border nr-shadow nr-focus";
    btn.dataset.color = m.available ? "green" : "black";
    btn.dataset.pressable = "";
    btn.textContent = m.available ? "Start" : "Unavailable";
    btn.disabled = !m.available;
    btn.onclick = () => startMode(m.mode, btn);
    footer.appendChild(btn);
    card.appendChild(footer);
    menuCardsEl.appendChild(card);
  }
}

async function startMode(mode, btn) {
  menuErrorEl.textContent = "";
  menuErrorEl.hidden = true;
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
          // AI vs AI always plays full-strength Stockfish with every piece;
          // the beginner style is only for games against a human.
          noob: mode === "ai_vs_ai" ? false : setNoob.checked,
          ...puzzleSettings(),
        },
      }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      menuErrorEl.textContent = body.error || res.statusText;
      menuErrorEl.hidden = false;
      return;
    }
    // Skill is an engine option, not a mode setting, so it goes the usual way.
    // Puzzles have no engine opponent to set it on.
    // AI vs AI sets its own full strength on the server.
    if (mode !== "puzzle" && mode !== "ai_vs_ai") {
      await fetch("/engine", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ skill: Number(setSkill.value) }),
      });
    }
    applyMode(body.mode);
  } catch (e) {
    menuErrorEl.textContent = String(e);
    menuErrorEl.hidden = false;
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
    if (body.park_error) await nrAlert("Park failed", body.park_error);
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
  const ok = await nrConfirm(
    withGame ? "Reset the game and park the arm?" : "Park the arm?",
    "The carriage will drive to the corner beyond " + PARK_SQUARE + ", crossing the whole " +
    "board. Clear its path and keep hands clear.",
    { okLabel: withGame ? "Reset & park" : "Park", okColor: "yellow" });
  if (!ok) return;
  btn.disabled = true;
  try {
    const res = await fetch("/reset", { method: "POST" });
    const body = await res.json().catch(() => ({}));
    if (!res.ok || body.error) await nrAlert("Reset", body.error || res.statusText);
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
      renderPuzzleProgress(data.puzzle_progress);
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
    const pz = data.puzzle;
    expectedUci = (pz ? pz.expected_uci : eng.expected_uci) || null;
    boardMarks = {};
    boardArrows = [];
    renderCoach(eng.coach);
    renderPuzzle(pz);
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

    if (!editMatrix) render(puzzleBoard(pz) || liveMatrix);

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
