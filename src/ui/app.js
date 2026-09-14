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
const MODE_LABELS = { menu: "menu", ai_vs_ai: "AI vs AI", normal: "play the engine" };

function applyMode(mode) {
  if (mode === currentMode) return;
  currentMode = mode;
  const inGame = mode && mode !== "menu";
  menuEl.style.display = inGame ? "none" : "flex";
  gameEl.style.display = inGame ? "flex" : "none";

  const ai = mode === "ai_vs_ai";
  modeBadgeEl.textContent = MODE_LABELS[mode] || mode || "";
  modeBadgeEl.dataset.color = inGame ? (ai ? "magenta" : "cyan") : "black";
  videoCol.style.display = ai || !inGame ? "none" : "flex";
  aiNoteEl.hidden = !ai;
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
    "Is the carriage parked on " + PARK_SQUARE + "?",
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
          noob: setNoob.checked,
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
    await fetch("/engine", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ skill: Number(setSkill.value) }),
    });
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
    "The carriage will drive to " + PARK_SQUARE + ", crossing the whole " +
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
