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
   Motion: CoreXY, origin = BOTTOM RIGHT of travel = h1 centre
   Magnet: DRV8833, AIN1 -> D9, AIN2 -> D10

   PROTOCOL  (115200 baud, newline terminated)
     Every command replies with exactly one line.
     "OK ..."  success        "ERR ..."  failure

     PING              -> OK PONG
     MOVE e7e5 b       -> OK MOVE e7e5 b    straight drag between centres
     KNIGHT b8c6 b     -> OK KNIGHT b8c6 b  weave along gridlines
                          The trailing w|b is which colour is being carried:
                          white magnets are reversed on this set, so the coil
                          polarity flips with it. Omitting it means "not
                          reversed", which is what the bench console wants.
     GOTO e4           -> OK GOTO e4        reposition, magnet untouched
     POL               -> OK POL 1          read the white-magnet polarity
     POL 0|1           -> OK POL <n>        1 = white is held by REPEL
     POLTEST e2        -> OK POLTEST e2     park there, then attract 2s /
                                            repel 2s, so you can see which
                                            way actually holds the piece
     MAG 0|1|2         -> OK MAG n          off / attract / repel
     PULSE             -> OK PULSE          reverse kick, clears residual
     HOME              -> OK HOME           return to origin
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
// Origin (0,0) = park position = centre of h1.
// a1 is 7 squares to the LEFT, so its X is negative.
// Measured from the machine, not from the printed sticker:
//   h1 centre = (   0.0,   0.0)   <- park position, the origin
//   a1 centre = (-210.0,   0.0)
//   h8 centre = (   0.0, 210.0)
//   a8 centre = (-210.0, 210.0)
//
// Board is square: 30mm pitch on both axes. (The earlier 200mm Y span was
// belt slip, fixed by re-belting — do NOT reintroduce a separate Y pitch.)
const float SQUARE_MM = 30.0;

const float A1_X_MM = -210.0;
const float A1_Y_MM =    0.0;

// The measured corners ARE the travel limits — there is no room beyond
// them in any direction. Anything outside is physically unreachable.
const float MIN_X = -210.0, MAX_X =   0.0;
const float MIN_Y =    0.0, MAX_Y = 210.0;

// Knight and castling weaves normally step half a square OUTSIDE the
// board. At the four edges that would exceed the limits above, so the
// weave is folded inward instead. See clampWeave().
const bool  FOLD_EDGE_WEAVE = true;

// ---------- speed ----------
float feedRateMMS = 40.0;
unsigned int stepDelayUS;

// ---------- magnet ----------
// Cap at 255 only if the buck really is at ~6V. Lower if running on cells.
// Bumped whenever the serial protocol changes in a way the host must know
// about. It rides in the READY banner, and src/robot.py refuses to drive a
// board older than it expects -- r1 had no polarity suffix at all and would
// silently attract for every move, shoving every white piece off its square.
#define FIRMWARE_REV 2

// Measured on this set: the white pieces' magnets are the other way up, so
// they need the opposite coil polarity from the black ones. This is only the
// power-on default -- POL changes it at runtime, because finding the right
// direction by reflashing once per guess is miserable.
const bool WHITE_IS_REVERSED = true;
bool whiteReversed = WHITE_IS_REVERSED;

const int MAG_FULL  = 255;
const int MAG_WEAVE = 170;
const int MAG_DIAG  = 155;

// ---------- state ----------
float posX = 0.0, posY = 0.0;

float clampWeave(float v);
void magHold(int duty, bool reversed);
void magRelease(bool reversed);
bool parseColour(const String &arg, bool &reversed);

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
    bool rev;
    if (!parsePly(arg, f0, r0, f1, r1))  { Serial.println("ERR bad ply"); return; }
    if (!parseColour(arg, rev))          { Serial.println("ERR colour w|b"); return; }
    if (!doMove(f0, r0, f1, r1, rev))    { Serial.println("ERR out of range"); return; }
    Serial.println("OK MOVE " + arg);
  }

  else if (cmd == "KNIGHT") {
    int f0, r0, f1, r1;
    bool rev;
    if (!parsePly(arg, f0, r0, f1, r1))  { Serial.println("ERR bad ply"); return; }
    if (!parseColour(arg, rev))          { Serial.println("ERR colour w|b"); return; }
    if (!doKnight(f0, r0, f1, r1, rev))  { Serial.println("ERR out of range"); return; }
    Serial.println("OK KNIGHT " + arg);
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

  // Which way the coil must drive to HOLD a white piece. 1 = the white
  // magnets are reversed, so white is held by repel and black by attract;
  // 0 = both colours the same, held by attract.
  //
  // RAM only, on purpose. Opening the serial port toggles DTR and reboots the
  // Uno, so the host has to re-send this on every connect regardless -- and
  // one source of truth beats an EEPROM copy that can disagree with it.
  else if (cmd == "POL") {
    if (arg.length() > 0) {
      if (arg == "0")      whiteReversed = false;
      else if (arg == "1") whiteReversed = true;
      else { Serial.println("ERR pol 0|1"); return; }
    }
    Serial.println("OK POL " + String(whiteReversed ? 1 : 0));
  }

  // Bench aid: park on a square and drive the coil each way in turn, so
  // "which polarity holds this piece?" is one command with the carriage
  // provably centred. Guessing that from a game move is how the polarity got
  // diagnosed backwards once already.
  else if (cmd == "POLTEST") {
    if (arg.length() < 2) { Serial.println("ERR bad square"); return; }
    int f = arg.charAt(0) - 'a';
    int r = arg.charAt(1) - '1';
    if (f < 0 || f > 7 || r < 0 || r > 7) { Serial.println("ERR bad square"); return; }
    if (!gotoSquare(f, r)) { Serial.println("ERR out of range"); return; }

    Serial.println("-- ATTRACT for 2s (holds BLACK on this set)");
    magAttract(MAG_FULL);
    delay(2000);
    magOff();
    delay(500);
    Serial.println("-- REPEL for 2s (holds WHITE on this set)");
    magRepel(MAG_FULL);
    delay(2000);
    magOff();
    Serial.println("OK POLTEST " + arg);
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

// Which piece the coil is about to pick up: "MOVE e2e4 w" or "... b".
//
// The colour is optional, and absent means NOT reversed -- that is what the
// firmware did before polarity existed, so a bench console typing a bare
// "MOVE e2e4" still behaves the way docs/CONNECTION.md describes. The Python
// planner always sends it explicitly; see robot_moves_legacy.plan.
bool parseColour(const String &arg, bool &reversed) {
  if (arg.length() <= 4) { reversed = false; return true; }

  String rest = arg.substring(4);
  rest.trim();
  rest.toLowerCase();
  if (rest.length() == 0) { reversed = false; return true; }
  // Black is the reference and is never reversed: attract holds it, which is
  // what r1 did for every piece and why black always played correctly. Only
  // white can differ, which is exactly what `whiteReversed` names.
  //
  // NOT !whiteReversed -- that quietly made black repel whenever white was
  // set normal, so POL 0 would have broken the colour that already worked.
  if (rest == "w")        { reversed = whiteReversed; return true; }
  if (rest == "b")        { reversed = false; return true; }
  return false;
}

// ============================================================
//  PIECE MOVES
// ============================================================
bool doMove(int f0, int r0, int f1, int r1, bool reversed) {
  if (!gotoSquare(f0, r0)) return false;
  magHold(MAG_FULL, reversed);
  if (!gotoSquare(f1, r1)) { magOff(); return false; }
  magRelease(reversed);
  return true;
}

// Knight: step half a square diagonally, run along the gridline,
// then step back onto the destination centre.
bool doKnight(int f0, int r0, int f1, int r1, bool reversed) {
  float sx = (f1 - f0) > 0 ? 0.5 : -0.5;
  float sy = (r1 - r0) > 0 ? 0.5 : -0.5;

  if (!gotoSquare(f0, r0)) return false;
  magHold(MAG_FULL, reversed);

  if (!gotoSquareF(clampWeave(f0 + sx), clampWeave(r0 + sy))) {
    magOff(); return false;
  }
  magHold(MAG_DIAG, reversed);

  if (!gotoSquareF(clampWeave(f1 - sx), clampWeave(r1 - sy))) {
    magOff(); return false;
  }
  magHold(MAG_FULL, reversed);
  delay(5);

  if (!gotoSquare(f1, r1)) { magOff(); return false; }
  magRelease(reversed);
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

// The white pieces on this set were built with their magnets the other way
// up, so the coil polarity that grips a black piece pushes a white one away.
// Every move therefore says which kind it is carrying, and these two are the
// only places that decide what the coil actually does.
//
// Both have to flip together. Holding with the wrong sign shoves the piece
// off the square; releasing with the wrong sign grabs it harder instead of
// kicking it free, and the carriage then drags it to the next square.

void magHold(int duty, bool reversed) {
  if (reversed) magRepel(duty);
  else          magAttract(duty);
}

void magRelease(bool reversed) {
  if (reversed) magAttract(MAG_FULL);
  else          magRepel(MAG_FULL);
  delay(15);
  magOff();
}

void magPulse() { magRelease(false); }   // bench verb: the un-reversed kick

// ============================================================
//  MOTION
// ============================================================
void recalcSpeed() {
  stepDelayUS = (unsigned int)(1000000.0 / (2.0 * feedRateMMS * stepsPerMM));
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

    errA -= stepsA;
    if (errA < 0) { errA += maxSteps; pA = true; }
    errB -= stepsB;
    if (errB < 0) { errB += maxSteps; pB = true; }

    if (pA) digitalWrite(stepPinA, HIGH);
    if (pB) digitalWrite(stepPinB, HIGH);
    delayMicroseconds(stepDelayUS);

    if (pA) digitalWrite(stepPinA, LOW);
    if (pB) digitalWrite(stepPinB, LOW);
    delayMicroseconds(stepDelayUS);
  }

  delay(60);
}
