// /admin: drive the arm by hand. Go to a square, or drag a piece from one
// square to another. The server does all the safety work (engine pause,
// tracking hold, busy/halt checks); this page only says what was clicked.

const nrDialog = document.getElementById("nrDialog");
const nrDialogTitle = document.getElementById("nrDialogTitle");
const nrDialogText = document.getElementById("nrDialogText");
const nrDialogOk = document.getElementById("nrDialogOk");
const nrDialogCancel = document.getElementById("nrDialogCancel");

function nrConfirm(title, text, okLabel = "OK", okColor = "green") {
  nrDialogTitle.textContent = title;
  nrDialogText.textContent = text;
  nrDialogOk.textContent = okLabel;
  nrDialogOk.dataset.color = okColor;
  nrDialogCancel.hidden = false;
  return new Promise((resolve) => {
    nrDialog.addEventListener("close", () => resolve(nrDialog.returnValue === "ok"), { once: true });
    nrDialog.showModal();
    nrDialogCancel.focus();  // a stray Enter must not start the arm moving
  });
}

const GLYPHS = {
  "white-king": "♔", "white-queen": "♕", "white-rook": "♖",
  "white-bishop": "♗", "white-knight": "♘", "white-pawn": "♙",
  "black-king": "♚", "black-queen": "♛", "black-rook": "♜",
  "black-bishop": "♝", "black-knight": "♞", "black-pawn": "♟",
};

const boardEl = document.getElementById("adminBoard");
const ranksEl = document.getElementById("adminRanks");
const filesEl = document.getElementById("adminFiles");
const hintEl = document.getElementById("adminHint");
const modeEl = document.getElementById("adminMode");
const robotEl = document.getElementById("adminRobot");
const posEl = document.getElementById("adminPos");
const messageEl = document.getElementById("adminMessage");
const gameNoteEl = document.getElementById("adminGameNote");
const enginePausedEl = document.getElementById("adminEnginePaused");
const backEl = document.getElementById("adminBack");
const originEl = document.getElementById("adminOrigin");
const rotatedEl = document.getElementById("adminRotated");
const logEl = document.getElementById("adminLog");
const moveOptionsEl = document.getElementById("moveOptions");

const toolGoto = document.getElementById("toolGoto");
const toolMove = document.getElementById("toolMove");
const pathStraight = document.getElementById("pathStraight");
const pathWeave = document.getElementById("pathWeave");
const clearFrom = document.getElementById("clearFrom");
const homeBtn = document.getElementById("homeBtn");
const haltBtn = document.getElementById("haltBtn");
const posBtn = document.getElementById("posBtn");
const magOff = document.getElementById("magOff");
const magAttract = document.getElementById("magAttract");
const magRepel = document.getElementById("magRepel");

let tool = "goto";       // "goto" | "move"
let weave = false;
let fromSquare = null;
let sending = false;
let state = null;
let lastError = null;

const cells = {};
for (let rank = 7; rank >= 0; rank--) {
  const label = document.createElement("div");
  label.textContent = rank + 1;
  ranksEl.appendChild(label);
  for (let file = 0; file < 8; file++) {
    const name = "abcdefgh"[file] + (rank + 1);
    const cell = document.createElement("div");
    cell.className = "sq editable " + ((rank + file) % 2 === 0 ? "dark" : "light");
    cell.title = name;
    cell.onclick = () => clickSquare(name);
    boardEl.appendChild(cell);
    cells[name] = cell;
  }
}
for (const f of "abcdefgh") {
  const label = document.createElement("div");
  label.textContent = f;
  filesEl.appendChild(label);
}

function pieceAt(name) {
  // matrix[rank_idx][file_idx], rank_idx 0 = rank 1 -- board_state's convention.
  if (!state || !state.matrix) return null;
  return state.matrix[Number(name[1]) - 1]["abcdefgh".indexOf(name[0])];
}

function isKnightShape(a, b) {
  const df = Math.abs(a.charCodeAt(0) - b.charCodeAt(0));
  const dr = Math.abs(Number(a[1]) - Number(b[1]));
  return (df === 1 && dr === 2) || (df === 2 && dr === 1);
}

function pick(on, off) {
  on.dataset.color = "blue";
  off.dataset.color = "black";
}

function renderToggles() {
  hintEl.classList.toggle("error", !!lastError);
  pick(tool === "goto" ? toolGoto : toolMove, tool === "goto" ? toolMove : toolGoto);
  pick(weave ? pathWeave : pathStraight, weave ? pathStraight : pathWeave);
  moveOptionsEl.hidden = tool !== "move";
  if (lastError) {
    hintEl.textContent = lastError;
  } else if (tool === "goto") {
    hintEl.textContent = "Click a square to send the carriage there (magnet untouched).";
  } else if (fromSquare) {
    hintEl.textContent = `From ${fromSquare}: now click the square to move it to.`;
  } else {
    hintEl.textContent = "Click the square the piece is on, then where it goes.";
  }
}

function renderBoard() {
  for (const [name, cell] of Object.entries(cells)) {
    const label = pieceAt(name);
    cell.textContent = label ? GLYPHS[label] : "";
    cell.classList.toggle("white-piece", !!label && label.startsWith("white"));
    cell.classList.toggle("black-piece", !!label && label.startsWith("black"));
    cell.classList.toggle("expect-from", name === fromSquare);
  }
}

function render() {
  renderToggles();
  renderBoard();
  if (!state) return;

  modeEl.textContent = state.mode.replace(/_/g, " ");
  backEl.textContent = state.running ? "← Back to game" : "← Back to menu";
  gameNoteEl.hidden = !state.running;
  enginePausedEl.hidden = !state.engine_paused;

  originEl.textContent =
    `origin ${state.origin} \u00b7 ${state.rotated ? "ROTATED" : "no rotation"} \u00b7 ${state.version}`;
  originEl.dataset.color = state.rotated ? "red" : "green";
  rotatedEl.hidden = !state.rotated;
  rotatedEl.textContent = state.rotated
    ? `Squares are rotated: a1 is sent as ${state.sample.a1}, h1 as ${state.sample.h1}. ` +
      `The server was started with --board-origin ${state.origin}; restart it without that ` +
      "flag unless the board really is seated that way."
    : "";

  const r = state.robot;
  let message = null;
  if (!r) {
    robotEl.textContent = "no robot";
    robotEl.dataset.color = "red";
    message = "No robot attached -- start the server with --robot.";
  } else {
    const word = r.busy ? "busy" : r.halted ? "halted" : r.homed ? "ready" : "not homed";
    robotEl.textContent = `${r.port} · ${word}`;
    robotEl.dataset.color = r.busy ? "yellow" : r.halted ? "red" : r.homed ? "green" : "yellow";
    if (!r.supported) message = "/admin needs the legacy (chessbot_v1) firmware protocol.";
    else if (r.message) message = r.message;
    else if (!r.homed) message = `Not homed. Park the carriage beyond ${state.park_square} and press Home.`;
  }
  messageEl.hidden = !message;
  messageEl.textContent = message || "";

  posEl.textContent = state.pos
    ? `X ${state.pos.x_mm.toFixed(1)} · Y ${state.pos.y_mm.toFixed(1)} mm`
    : "X ? · Y ?";

  const blocked = !r || !r.supported || sending;
  for (const b of [homeBtn, posBtn, magOff, magAttract, magRepel]) b.disabled = blocked;
  haltBtn.disabled = !r;
  boardEl.classList.toggle("locked", blocked || !r.homed || r.halted);

  logEl.innerHTML = "";
  for (const e of state.log) {
    const li = document.createElement("li");
    li.className = e.ok ? "ok" : "err";
    const time = new Date(e.t * 1000).toLocaleTimeString();
    const detail = e.ok ? (e.reply || "OK") : e.error;
    li.textContent = `${time}  ${e.command}  →  ${e.ok ? "" : "ERR "}${detail}`;
    logEl.appendChild(li);
  }
}

async function post(body) {
  sending = true;
  lastError = null;
  render();
  try {
    const res = await fetch("/admin", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (data.mode !== undefined) state = data;
    if (!res.ok) lastError = data.error || `failed (${res.status})`;
    return res.ok;
  } catch (err) {
    lastError = `request failed: ${err}`;
    return false;
  } finally {
    sending = false;
    render();
  }
}

async function clickSquare(name) {
  if (sending || boardEl.classList.contains("locked")) return;
  if (tool === "goto") {
    await post({ action: "goto", square: name });
    return;
  }
  if (!fromSquare) {
    fromSquare = name;
    lastError = null;
    render();
    return;
  }
  if (name === fromSquare) {
    fromSquare = null;
    render();
    return;
  }
  const from = fromSquare;
  // An L-shaped hop would clip the piece it jumps, so suggest the weave.
  // Only on the second click, and only if the human hasn't chosen already.
  const useWeave = weave || isKnightShape(from, name);
  const ok = await post({ action: "move", from, to: name, weave: useWeave });
  if (ok) fromSquare = null;
  render();
}

toolGoto.onclick = () => { tool = "goto"; fromSquare = null; render(); };
toolMove.onclick = () => { tool = "move"; render(); };
pathStraight.onclick = () => { weave = false; render(); };
pathWeave.onclick = () => { weave = true; render(); };
clearFrom.onclick = () => { fromSquare = null; render(); };

homeBtn.onclick = async () => {
  const ok = await nrConfirm(
    "Home the arm?",
    `The carriage drives to the corner beyond ${state ? state.park_square : "the origin"}, ` +
      "crossing the board. Keep hands clear.",
    "Home", "yellow");
  if (ok) await post({ action: "home" });
};
haltBtn.onclick = () => post({ action: "halt" });
posBtn.onclick = () => post({ action: "pos" });
magOff.onclick = () => post({ action: "mag", mode: "off" });
magAttract.onclick = () => post({ action: "mag", mode: "attract" });
magRepel.onclick = () => post({ action: "mag", mode: "repel" });

async function poll() {
  if (!sending) {
    try {
      const res = await fetch("/admin/state.json");
      if (res.ok) state = await res.json();
    } catch (err) {
      // The Pi is on a LAN; a missed poll is just a stale screen for a second.
    }
    render();
  }
  setTimeout(poll, 1000);
}

render();
poll();
