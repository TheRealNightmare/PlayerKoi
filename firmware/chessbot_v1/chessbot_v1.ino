/*
   ***  THIS IS THE SKETCH TO FLASH.  ***

   This is the firmware running on the live rig: Arduino Uno + CNC Shield V3
   + DRV8833, CoreXY belts, no limit switches. Wiring, bring-up order and the
   full command reference are in docs/CONNECTION.md.

   Do NOT flash firmware/chess_gantry/ instead. That is a different, unbuilt
   machine (TMC2208 drivers, limit switches, a different pin map); on this rig
   it would home into switches that aren't fitted.

   Provenance: copied verbatim from RunChess/chessbot_firmware.ino, which is
   the single source of truth for every measurement here -- the constants
   below were measured on the machine, not derived. RunChess/chess.txt holds
   an older 200x200-frame calibration (25.7mm pitch); it is stale and
   superseded. Python-side mirrors of these numbers live in src/rig.py.

   ChessBot-V1 firmware — serial command protocol
   Board:  Arduino Uno + CNC Shield V3
   Motion: CoreXY, origin = BOTTOM RIGHT corner of travel, beyond h1
   Magnet: DRV8833, AIN1 -> D9, AIN2 -> D10

   PROTOCOL  (115200 baud, newline terminated)
     Every command replies with exactly one line.
     "OK ..."  success        "ERR ..."  failure

     PING              -> OK PONG
     MOVE e7e5         -> OK MOVE e7e5      straight drag between centres
     KNIGHT b8c6       -> OK KNIGHT b8c6    L along gridlines, axis moves only
                          Every piece on this set has its magnet the same way
                          up, so one polarity holds them all (POL). Each carry is:
                          coil off -> drive to the source -> coil on, grip
                          pause -> carry -> coil off -> settle pause.
     GOTO e4           -> OK GOTO e4        reposition, magnet untouched
     BURY e4 -215 10   -> OK BURY e4        lift the piece on e4 and park it
                          on a graveyard slot, given as a RAW MACHINE
                          COORDINATE -- the slots sit outside the 8x8, so
                          there is no square name for them. Travels like a
                          knight: half a square onto a gridline, along it off
                          the board, along the board's edge line, then into
                          the slot -- never across a square centre. Set down
                          the same way a move is. The host picks the slot;
                          see src/graveyard.py.
     POL               -> OK POL 0          which way the coil holds EVERY
                                            piece: 0 = attract, 1 = repel
     POL 0|1           -> OK POL <n>        set it (RAM only; host re-sends)
     GRID              -> OK GRID 100       magnet power on every gridline
                                            leg (knight, castling rook, BURY)
     GRID <pct>        -> OK GRID <pct>     0-100 % of full (RAM only)
     DWELL             -> OK DWELL 150 1200 pause after gripping, before the
                                            drag / pause after setting down,
                                            coil fully off, before moving on
     DWELL <g> <s>     -> OK DWELL <g> <s>  each 0-2000 ms
     MAG 0|1|2         -> OK MAG n          off / attract / repel
     PULSE             -> OK PULSE          raw full-power reverse kick, for
                                            the bench only.
     HOME              -> OK HOME           return to the origin corner
     POS               -> OK POS x y        current mm position
     MM -105 100       -> OK MM x y         raw machine coordinates, mm
     JOG -5 0          -> OK JOG x y        relative nudge, mm
     SPEED 25          -> OK SPEED 25       feed rate mm/s

   The Pi sends one command and waits for its reply before sending the
   next. Moves are blocking, so the reply doubles as "motion finished".
*/

// ---------- pins ----------
const int stepPinA  = 2;
const int dirPinA   = 5;
const int stepPinB  = 3;
const int dirPinB   = 6;
const int enablePin = 8;

const int AIN1 = 9;
const int AIN2 = 10;

// ---------- motion scale ----------
const int   MICROSTEPS          = 2;
const float MOTOR_STEPS_PER_REV = 200.0;
const float PULLEY_TEETH        = 20.0;
const float BELT_PITCH_MM       = 2.0;

const float MM_PER_REV = PULLEY_TEETH * BELT_PITCH_MM;                     // 40
const float stepsPerMM = (MOTOR_STEPS_PER_REV * MICROSTEPS) / MM_PER_REV;  // 10

const bool invertX = false;
const bool invertY = false;

// ---------- board geometry ----------
// V2 BOARD: 400x400mm playing area, 50mm squares, 480x470mm of travel.
// (V1 was 230x230 at 28.75mm with travel that stopped dead at the board
// edge. If you are driving the old hardware, git log has those numbers.)
//
// Origin (0,0) = park position = the corner of travel beyond h1: as far
// towards the h-file and rank 1 as the carriage goes. It is parked there by
// hand before power-up (no limit switches), and HOME returns there. From it
// the carriage only goes -X (towards the a-file) and +Y (towards rank 8).
// Measured reach, by jogging to the frame with the walker's calibration mode:
//
//     (-480,470) ------------ (0,470)
//         |                      |
//     (-480,0)  ------------ (0,0)  <- park here at power-up
//
// The graveyard ring's slot centres span 450mm each way, which fits that
// reach with 15mm clearance at each X limit and 10mm at each Y limit. The
// board sits one square pitch inside the ring:
//   a1 centre = (-415.0,  60.0)
//   h1 centre = ( -65.0,  60.0)
//   a8 centre = (-415.0, 410.0)
//   h8 centre = ( -65.0, 410.0)
//
// Board is square: 50mm pitch on both axes. (The earlier 200mm Y span was
// belt slip, fixed by re-belting — do NOT reintroduce a separate Y pitch.)
// firmware/chessbot_walker/ carries a copy of these numbers and walks every
// square and slot; tests/test_firmware_geometry.py keeps the copies equal.
const float SQUARE_MM = 50.0;

const float A1_X_MM = -415.0;
const float A1_Y_MM =   60.0;

// Travel limits, as measured. Knight and castling weaves step half a square
// outside the board (x -440 / -40, y 35 / 435), still inside these.
const float MIN_X = -480.0, MAX_X =   0.0;
const float MIN_Y =    0.0, MAX_Y = 470.0;

// Captured pieces. The 50mm margin is deep enough for a row of eight on each
// of the four sides -- 32 slots in all, shared between both colours, on the
// board's own file and rank centre lines. The four corner cells are left
// empty: a corner slot sits on no centre line, and 32 already outnumbers the
// 30 pieces that can ever be captured.
//
// The sketch does not CHOOSE a slot -- that needs to know what is already in
// the pile, which only the host tracks (src/graveyard.py). It only drives to
// the coordinate it is given, via BURY. So these four numbers are the
// machine's own record of where the ring is, alongside SQUARE_MM and the
// travel limits; nothing below reads them. src/rig.py mirrors them.
const float GRAVEYARD_Y_LOW  =   10.0;  // beyond rank 1:     60 - 50
const float GRAVEYARD_Y_HIGH =  460.0;  // beyond rank 8:    410 + 50
const float GRAVEYARD_X_LOW  = -465.0;  // beyond the a-file: -415 - 50
const float GRAVEYARD_X_HIGH =  -15.0;  // beyond the h-file:  -65 + 50

// Knight and castling weaves step half a square OUTSIDE the board. On V1
// that exceeded the travel limits at the four edges and had to be folded
// inward; with 35-40mm of travel past every board edge a 25mm weave fits, so
// the fold is no longer needed. See clampWeave(), which is now a no-op.
const bool  FOLD_EDGE_WEAVE = false;

// ---------- speed ----------
// 40mm/s, the proven feed. SPEED changes it at runtime; the host also sends
// SPEED on connect.
float feedRateMMS = 40.0;

// Every leg ramps up from START_MMS to the feed rate and back down again,
// instead of starting and stopping dead. A carried piece rides on nothing but
// the magnet, and an instant start at full feed jerked knights off the pole
// on the L's long gridline run. 150 mm/s^2 reaches 40 mm/s in ~5 mm, so even
// the half-square steps of a weave get most of their ramp.
const float START_MMS   = 8.0;
const float ACCEL_MMS2  = 150.0;

// The same three, in step units along the leg's longer motor axis -- the
// unit the step loop counts in. Set by recalcSpeed().
float vMaxSteps, vMinSq, twoAccelSteps;

// ---------- magnet ----------
// Cap at 255 only if the buck really is at ~6V. Lower if running on cells.
// Bumped whenever the serial protocol changes in a way the host must know
// about. It rides in the READY banner, and src/robot.py refuses to drive a
// board older than it expects -- r1 had no polarity suffix at all and would
// silently attract for every move, shoving every white piece off its square.
// r5 is not a protocol change but a geometry one: the origin moved to the
// travel corner, so an r4 board would take the same square names and BURY
// coordinates and drive somewhere else. See rig.FIRMWARE_REV.
// r6 adds the grip/settle pauses (DWELL), the tunable de-cling kick (KICK)
// and the gridline path for BURY. The host sends DWELL and KICK on connect,
// which an r5 board would answer with ERR.
// r7: every piece is re-magnetised the same way up, so one polarity holds all
// of them. POL now picks that one polarity (attract by default); POLTEST,
// RELEASE and KICK are gone, as is the w|b suffix on MOVE/KNIGHT/BURY; a
// set-down is a plain coil-off plus the settle pause.
// An r6 board would still hold white by repel and shove it off its square.
// r8 adds GRID, the gridline magnet power, which the host sends on connect --
// an r7 board would answer ERR unknown GRID.
#define FIRMWARE_REV 8

// Which way the coil drives to HOLD a piece -- the same for every piece.
// Attract is what the set was magnetised for; POL 1 flips it to repel, in case
// the magnets turn out to be fitted the other way up, without a reflash.
const bool HOLD_BY_REPEL = false;
bool holdByRepel = HOLD_BY_REPEL;

// FULL drags a piece square-to-square, and is fixed.
const int MAG_FULL  = 255;

// Power on every gridline leg -- knights, the castling rook and BURY -- as a
// percentage of FULL. Runtime, set by GRID. Full by default: 60% and then 80%
// both sometimes lost the piece partway along the L.
const int GRID_PCT = 100;
int gridPct = GRID_PCT;

int gridDuty() { return (int)((long)MAG_FULL * gridPct / 100); }

// Pauses around a carried piece. GRIP: coil on, carriage still, so the piece
// is pulled flat onto the pole before it is asked to slide. SETTLE: after the
// set-down has switched the coil fully OFF, stay put so the core's residual
// magnetism fades and the piece is at rest before the carriage drives away --
// leaving at once is what tows it. Both runtime, set by DWELL.
// SETTLE is 1.2 s: 300 ms, and later 1000 ms, sometimes left the core
// magnetised enough to tow the piece a few millimetres.
const int GRIP_MS   = 150;
const int SETTLE_MS = 1200;
int gripMS   = GRIP_MS;
int settleMS = SETTLE_MS;

// ---------- state ----------
float posX = 0.0, posY = 0.0;

float clampWeave(float v);
void magHold(int duty);
void magRelease();

void setup() {
  pinMode(stepPinA, OUTPUT);
  pinMode(dirPinA, OUTPUT);
  pinMode(stepPinB, OUTPUT);
  pinMode(dirPinB, OUTPUT);
  pinMode(enablePin, OUTPUT);
  pinMode(AIN1, OUTPUT);
  pinMode(AIN2, OUTPUT);

  digitalWrite(enablePin, LOW);   // enable drivers
  magOff();

  recalcSpeed();

  Serial.begin(115200);
  while (!Serial);

  // The Uno resets when the Pi opens the port. This banner tells the
  // host the board is up and ready to accept commands.
  // The "READY ChessBot-V1" prefix is load-bearing -- robot.py waits on
  // "READY" and docs/CONNECTION.md quotes this line. The revision is appended
  // so a host can tell r1 from r2 without any new handshake.
  Serial.println("READY ChessBot-V1 r" + String(FIRMWARE_REV));
}

void loop() {
  if (!Serial.available()) return;

  String line = Serial.readStringUntil('\n');
  line.trim();
  if (line.length() == 0) return;

  handleCommand(line);
}

// ============================================================
//  COMMAND DISPATCH
// ============================================================
void handleCommand(String line) {

  int sp = line.indexOf(' ');
  String cmd = (sp < 0) ? line : line.substring(0, sp);
  String arg = (sp < 0) ? ""   : line.substring(sp + 1);
  arg.trim();
  cmd.toUpperCase();

  if (cmd == "PING") {
    Serial.println("OK PONG");
  }

  else if (cmd == "MOVE") {
    int f0, r0, f1, r1;
    if (!parsePly(arg, f0, r0, f1, r1))  { Serial.println("ERR bad ply"); return; }
    if (!doMove(f0, r0, f1, r1))         { Serial.println("ERR out of range"); return; }
    Serial.println("OK MOVE " + arg);
  }

  else if (cmd == "KNIGHT") {
    int f0, r0, f1, r1;
    if (!parsePly(arg, f0, r0, f1, r1))  { Serial.println("ERR bad ply"); return; }
    if (!doKnight(f0, r0, f1, r1))       { Serial.println("ERR out of range"); return; }
    Serial.println("OK KNIGHT " + arg);
  }

  // BURY <square> <x_mm> <y_mm>  -- park a captured piece off the board
  else if (cmd == "BURY") {
    int f, r;
    float tx, ty;
    if (!parseBury(arg, f, r, tx, ty)) { Serial.println("ERR usage BURY <square> <x> <y>"); return; }
    if (!doBury(f, r, tx, ty))         { Serial.println("ERR out of range"); return; }
    Serial.println("OK BURY " + arg.substring(0, 2));
  }

  else if (cmd == "GOTO") {
    if (arg.length() < 2) { Serial.println("ERR bad square"); return; }
    int f = arg.charAt(0) - 'a';
    int r = arg.charAt(1) - '1';
    if (f < 0 || f > 7 || r < 0 || r > 7) { Serial.println("ERR bad square"); return; }
    if (!gotoSquare(f, r)) { Serial.println("ERR out of range"); return; }
    Serial.println("OK GOTO " + arg);
  }

  else if (cmd == "MAG") {
    int m = arg.toInt();
    if (m == 0)      magOff();
    else if (m == 1) magAttract(MAG_FULL);
    else if (m == 2) magRepel(MAG_FULL);
    else { Serial.println("ERR mag 0|1|2"); return; }
    Serial.println("OK MAG " + String(m));
  }

  // Which polarity holds every piece: 0 = attract, 1 = repel. RAM only --
  // opening the port reboots the Uno, so the host re-sends it on connect.
  else if (cmd == "POL") {
    if (arg.length() > 0) {
      if (arg == "0")      holdByRepel = false;
      else if (arg == "1") holdByRepel = true;
      else { Serial.println("ERR pol 0|1"); return; }
    }
    Serial.println("OK POL " + String(holdByRepel ? 1 : 0));
  }

  // Gridline power, in percent of full: GRID <0-100>.
  else if (cmd == "GRID") {
    if (arg.length() > 0) {
      long pct = arg.toInt();
      if (pct < 0 || pct > 100 || (pct == 0 && arg != "0")) { Serial.println("ERR grid 0-100"); return; }
      gridPct = (int)pct;
    }
    Serial.println("OK GRID " + String(gridPct));
  }

  // Pauses around a carried piece: DWELL <grip_ms> <settle_ms>.
  else if (cmd == "DWELL") {
    if (arg.length() > 0) {
      float g, st;
      if (!parseXY(arg, g, st))            { Serial.println("ERR usage DWELL <grip_ms> <settle_ms>"); return; }
      if (g < 0 || g > 2000 || st < 0 || st > 2000) { Serial.println("ERR dwell 0-2000"); return; }
      gripMS = (int)g;
      settleMS = (int)st;
    }
    Serial.println("OK DWELL " + String(gripMS) + " " + String(settleMS));
  }

  else if (cmd == "PULSE") {
    magPulse();
    Serial.println("OK PULSE");
  }

  else if (cmd == "HOME") {
    magOff();
    if (!moveTo(0.0, 0.0)) { Serial.println("ERR out of range"); return; }
    Serial.println("OK HOME");
  }

  else if (cmd == "MM") {
    float tx, ty;
    if (!parseXY(arg, tx, ty)) { Serial.println("ERR usage MM <x> <y>"); return; }
    if (!moveTo(tx, ty))       { Serial.println("ERR out of range"); return; }
    Serial.println("OK MM " + String(posX, 1) + " " + String(posY, 1));
  }

  else if (cmd == "JOG") {
    float dx, dy;
    if (!parseXY(arg, dx, dy))            { Serial.println("ERR usage JOG <dx> <dy>"); return; }
    if (!moveTo(posX + dx, posY + dy))    { Serial.println("ERR out of range"); return; }
    Serial.println("OK JOG " + String(posX, 1) + " " + String(posY, 1));
  }

  else if (cmd == "POS") {
    Serial.println("OK POS " + String(posX, 1) + " " + String(posY, 1));
  }

  else if (cmd == "SPEED") {
    float v = arg.toFloat();
    if (v < 1.0 || v > 100.0) { Serial.println("ERR speed 1-100"); return; }
    feedRateMMS = v;
    recalcSpeed();
    Serial.println("OK SPEED " + String(feedRateMMS, 1));
  }

  else {
    Serial.println("ERR unknown " + cmd);
  }
}

// Parse "<x> <y>" from a command argument. Accepts negatives and decimals.
bool parseXY(String a, float &x, float &y) {
  a.trim();
  int sp = a.indexOf(' ');
  if (sp < 0) return false;
  String sx = a.substring(0, sp);
  String sy = a.substring(sp + 1);
  sx.trim(); sy.trim();
  if (sx.length() == 0 || sy.length() == 0) return false;
  x = sx.toFloat();
  y = sy.toFloat();
  return true;
}

// Parse "e7e5" into file/rank indices.
bool parsePly(String p, int &f0, int &r0, int &f1, int &r1) {
  if (p.length() < 4) return false;
  f0 = p.charAt(0) - 'a';
  r0 = p.charAt(1) - '1';
  f1 = p.charAt(2) - 'a';
  r1 = p.charAt(3) - '1';
  return (f0 >= 0 && f0 <= 7 && r0 >= 0 && r0 <= 7 &&
          f1 >= 0 && f1 <= 7 && r1 >= 0 && r1 <= 7);
}

// Parse "e4 -215 10": the square to lift from and the raw machine coordinate
// to park on.
bool parseBury(String a, int &f, int &r, float &x, float &y) {
  a.trim();
  if (a.length() < 2) return false;

  f = a.charAt(0) - 'a';
  r = a.charAt(1) - '1';
  if (f < 0 || f > 7 || r < 0 || r > 7) return false;

  return parseXY(a.substring(2), x, y);
}

// ============================================================
//  PIECE MOVES
// ============================================================
// Every carry is the same cycle: coil OFF while driving to the source (so
// nothing is dragged on the way there), coil ON and a grip pause, the carry,
// then magRelease() -- coil OFF and a settle pause -- before anything moves.
bool doMove(int f0, int r0, int f1, int r1) {
  magOff();
  if (!gotoSquare(f0, r0)) return false;
  magHold(MAG_FULL);
  delay(gripMS);
  if (!gotoSquare(f1, r1)) { magOff(); return false; }
  magRelease();
  return true;
}

// Park a captured piece on a graveyard slot. Grip, carry, set down the same
// way a move does -- but the destination is a raw machine
// coordinate rather than a square, because the slots sit OUTSIDE the 8x8 and
// have no file/rank to name them.
//
// It travels the way a knight does, never across a square centre: half a
// square onto the gridline beside its own file (or rank), along that gridline
// to the board's edge line, along the edge line to the slot's file (or rank),
// then out into the slot. Every leg runs 25mm from the centres it passes --
// the same clearance as a knight weave -- both from the pieces still on the
// board and from the ones already parked in the ring.
//
// moveTo() still guards every waypoint: a bad host coordinate is an
// "ERR out of range" and not the carriage walking into the frame.
//
// The host owns which slot: it knows what is already in the pile and the
// firmware does not. See src/graveyard.py.
bool doBury(int f0, int r0, float tx, float ty) {
  // The slot in board units. Slots sit on file/rank centre lines, one pitch
  // outside the board, so exactly one of these is -1 or 8.
  float fs = (tx - A1_X_MM) / SQUARE_MM;
  float rs = (ty - A1_Y_MM) / SQUARE_MM;
  bool acrossRanks = (rs < -0.5 || rs > 7.5);   // south or north strip
  bool acrossFiles = (fs < -0.5 || fs > 7.5);   // west or east strip
  if (acrossRanks == acrossFiles) return false; // on the board, or a corner

  magOff();
  if (!gotoSquare(f0, r0)) return false;
  magHold(MAG_FULL);
  delay(gripMS);

  bool ok;
  if (acrossRanks) {
    // Off the rank-1 or rank-8 edge. Step sideways onto the file gridline
    // nearer the slot, run along it to the edge line, then along that.
    float edge = rs < 0 ? -0.5 : 7.5;
    float hx = fs > f0 ? 0.5 : (fs < f0 ? -0.5 : (f0 < 7 ? 0.5 : -0.5));
    ok = gotoSquareF(f0 + hx, r0);
    if (ok) { magHold(gridDuty()); ok = gotoSquareF(f0 + hx, edge); }
    if (ok) ok = gotoSquareF(fs, edge);
  } else {
    float edge = fs < 0 ? -0.5 : 7.5;
    float hy = rs > r0 ? 0.5 : (rs < r0 ? -0.5 : (r0 < 7 ? 0.5 : -0.5));
    ok = gotoSquareF(f0, r0 + hy);
    if (ok) { magHold(gridDuty()); ok = gotoSquareF(edge, r0 + hy); }
    if (ok) ok = gotoSquareF(edge, rs);
  }
  if (ok) { magHold(MAG_FULL); ok = moveTo(tx, ty); }
  if (!ok) { magOff(); return false; }

  magRelease();
  return true;
}

// Knight: a pure L, axis moves only. Half a square along the short axis
// puts the piece on the gridline between its file (or rank) and the
// target's, it runs the long axis there, then half a square back onto the
// destination centre. Every piece it passes is 25mm centre to centre --
// 9mm edge to edge with 16mm bases on 50mm squares. Castling's rook
// (dr = 0) steps off its rank the same way, runs, and steps back.
bool doKnight(int f0, int r0, int f1, int r1) {
  int df = f1 - f0, dr = r1 - r0;
  float sx = df > 0 ? 0.5 : -0.5;
  float sy = dr > 0 ? 0.5 : -0.5;
  bool shortIsFile = abs(df) < abs(dr);
  float hx = shortIsFile ? sx : 0.0;
  float hy = shortIsFile ? 0.0 : sy;

  magOff();
  if (!gotoSquare(f0, r0)) return false;
  magHold(MAG_FULL);
  delay(gripMS);

  if (!gotoSquareF(clampWeave(f0 + hx), clampWeave(r0 + hy))) {
    magOff(); return false;
  }
  magHold(gridDuty());

  if (!gotoSquareF(clampWeave(f1 - hx), clampWeave(r1 - hy))) {
    magOff(); return false;
  }
  magHold(MAG_FULL);
  delay(5);

  if (!gotoSquare(f1, r1)) { magOff(); return false; }
  magRelease();
  return true;
}

bool gotoSquare(int file, int rank) {
  return gotoSquareF((float)file, (float)rank);
}

bool gotoSquareF(float file, float rank) {
  float tx = A1_X_MM + file * SQUARE_MM;
  float ty = A1_Y_MM + rank * SQUARE_MM;
  return moveTo(tx, ty);
}

// Keep a weave waypoint inside the reachable board. Without this a knight
// touching the a-file, h-file, rank 1 or rank 8 would try to step half a
// square past the edge, where the carriage physically cannot go.
float clampWeave(float v) {
  if (!FOLD_EDGE_WEAVE) return v;
  if (v < 0.0) return 0.0;
  if (v > 7.0) return 7.0;
  return v;
}

bool moveTo(float tx, float ty) {
  if (tx < MIN_X || tx > MAX_X || ty < MIN_Y || ty > MAX_Y) return false;
  moveCoreXY(tx - posX, ty - posY);
  posX = tx;
  posY = ty;
  return true;
}

// ============================================================
//  MAGNET
// ============================================================
void magAttract(int duty) { analogWrite(AIN1, duty); digitalWrite(AIN2, LOW); }
void magRepel(int duty)   { digitalWrite(AIN1, LOW); analogWrite(AIN2, duty); }
void magOff()             { digitalWrite(AIN1, LOW); digitalWrite(AIN2, LOW); }

// Every piece on this set has its magnet the same way up, so one polarity
// holds all of them -- white and black alike. Attract unless POL 1 says repel.
// These two are the only places a move decides what the coil does.
void magHold(int duty) {
  if (holdByRepel) magRepel(duty);
  else             magAttract(duty);
}

// Set a piece down: coil straight off, then stay put for settleMS so the
// core's residual magnetism dies away before the carriage leaves -- driving
// off at once is what tows a piece. Pieces still towed: raise settle (DWELL).
void magRelease() {
  magOff();
  delay(settleMS);
}

// A raw de-magnetising pulse, for the bench only. No move uses it.
void magPulse() {
  magRepel(MAG_FULL);
  delay(15);
  magOff();
}

// ============================================================
//  MOTION
// ============================================================
void recalcSpeed() {
  vMaxSteps = feedRateMMS * stepsPerMM;
  float vMin = min(START_MMS, feedRateMMS) * stepsPerMM;
  vMinSq = vMin * vMin;
  twoAccelSteps = 2.0 * ACCEL_MMS2 * stepsPerMM;
}

// Half a step period, in us, for the step `d` steps from the nearer end of
// the leg: v = sqrt(vMin^2 + 2*a*d), capped at the feed. Symmetric, so the
// leg decelerates into its end exactly as it accelerated out of its start.
unsigned int halfPeriodUS(long d) {
  float v = sqrt(vMinSq + twoAccelSteps * (float)d);
  if (v > vMaxSteps) v = vMaxSteps;
  return (unsigned int)(500000.0 / v);
}

void moveCoreXY(float mmX, float mmY) {
  if (invertX) mmX = -mmX;
  if (invertY) mmY = -mmY;

  long targetX = lround(mmX * stepsPerMM);
  long targetY = lround(mmY * stepsPerMM);

  // CoreXY mixer: A = X + Y, B = X - Y
  long stepsA = targetX + targetY;
  long stepsB = targetX - targetY;

  digitalWrite(dirPinA, stepsA > 0 ? HIGH : LOW);
  digitalWrite(dirPinB, stepsB > 0 ? HIGH : LOW);

  stepsA = labs(stepsA);
  stepsB = labs(stepsB);

  long maxSteps = max(stepsA, stepsB);
  if (maxSteps == 0) return;

  long errA = maxSteps / 2;
  long errB = maxSteps / 2;

  for (long i = 0; i < maxSteps; i++) {
    bool pA = false, pB = false;
    long fromEnd = maxSteps - 1 - i;
    unsigned int halfUS = halfPeriodUS(i < fromEnd ? i : fromEnd);

    errA -= stepsA;
    if (errA < 0) { errA += maxSteps; pA = true; }
    errB -= stepsB;
    if (errB < 0) { errB += maxSteps; pB = true; }

    if (pA) digitalWrite(stepPinA, HIGH);
    if (pB) digitalWrite(stepPinB, HIGH);
    delayMicroseconds(halfUS);

    if (pA) digitalWrite(stepPinA, LOW);
    if (pB) digitalWrite(stepPinB, LOW);
    delayMicroseconds(halfUS);
  }

  delay(60);
}
