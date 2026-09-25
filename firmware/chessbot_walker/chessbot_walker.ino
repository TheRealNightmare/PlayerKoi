/*
   ***  BRING-UP WALKER — NOT THE GAME FIRMWARE.  ***

   Drives the carriage to every position the V2 machine is supposed to be able
   to reach — all 64 square centres and all 32 graveyard slots — pausing at
   each one so you can check it lands on the printed dot. It also has a
   calibration mode for nudging the carriage onto a dot and reading back the
   trim that got it there. Nothing else. No chess, no Pi, no serial protocol
   to speak.

   *** RE-FLASH firmware/chessbot_v1/chessbot_v1.ino BEFORE CONNECTING THE Pi. ***
   A board left with this sketch on it ignores everything the Pi sends, and the
   symptom looks like a dead serial link rather than the wrong sketch.

   WHY THIS EXISTS

   The V2 frame reaches past every board edge — measured at 480x470mm of
   travel around a 400x400mm board — to a graveyard ring the V1 machine
   physically could not enter. A belt that is fractionally short, a rod
   that binds near the end of its span, or a panel mounted a few millimetres
   off will all first show up out there. Without this, the first symptom would
   be a captured piece being dragged into the frame mid-game.

   THE COIL IS NEVER ENERGISED

   AIN1/AIN2 are driven LOW in setup() and never touched again. There is no
   magHold, no magRelease, no analogWrite anywhere in this file — the verbs do
   not exist here, so no later edit can quietly switch the magnet on. That is
   deliberate: it means the walker can be run over a board with pieces on it
   and cannot drag, drop or fling anything. The price is that it proves REACH
   but says nothing about GRIP; the magnet's pull at 50mm pitch still has to be
   tested separately, with the real firmware (MAG 0|1|2 in chessbot_v1).

   NOTHING MOVES ON POWER-UP

   There are no limit switches on this machine. HOME is an assumption, not a
   measurement: if the carriage is not hand-parked in the origin corner (the
   corner of travel beyond h1: as far towards the h-file and rank 1 as it
   goes) when the board powers on, every coordinate below is wrong by that
   offset. So this waits for a keypress rather than starting on its own,
   because a walker that began moving by itself would drive into the frame.

   THESE CONSTANTS ARE COPIED FROM chessbot_v1.ino.

   Copied, not shared — Arduino has no good way to share a header between two
   sketch folders, and this file has to compile alone. Two copies drift, and a
   walker that has drifted is confirming a machine that no longer exists, which
   is worse than having no walker. tests/test_firmware_geometry.py exists to
   fail the day they disagree: it parses both sketches and src/rig.py and
   compares every shared constant. If you change geometry in one place, that
   test will tell you about the others.

   USAGE  (serial monitor, 115200, newline or no line ending — both fine)

     1   the four board corners: a1, h1, h8, a8
     2   all 64 square centres, snaking so it never crosses the board
     3   all 32 graveyard slots, in ring order
     4   everything: corners, then slots, then squares
     5   the travel limits: the four measured corners, slowly -- the frame test
     g <name>  go to a cell and stay there (see NAMES below)
     h   return to the origin corner
     c   calibration / trim mode (see below)
     p   pause / resume
     q   abort and return to the origin
     + - faster / slower
     ?   reprint this menu

   CALIBRATION MODE  ('c' from the main menu; one command per line)

     w [mm]    nudge +Y (towards rank 8)      default: the current jog step
     s [mm]    nudge -Y (towards rank 1)
     d [mm]    nudge +X (towards the h-file)
     a [mm]    nudge -X (towards the a-file)
     arrows    same as w/a/s/d by one jog step (terminals that send ESC [ A..D)
     j <mm>    set the jog step (0.1 .. 25)
     g <name>  go to a named cell, e.g.  g e4   g a0   g gr3   g y9
     g s<n>    go to graveyard slot n, e.g.  g s12
     m <x> <y> go to any point, mm from the origin corner, e.g.  m -240 235
               (or  m -240,235) -- x is 0 or negative, y 0 or positive
     t         print the trim, and the two lines to paste into this file
     z         zero the trim
     x         leave calibration mode and return to the origin
     ?         this help

   NAMES  -- a 10x10 grid over the board and the graveyard ring

     columns  gr a b c d e f g h y      rows  0 1 2 3 4 5 6 7 8 9

     a1..h8 are the board. The ring around it is the graveyard: a0..h0
     (south), y1..y8 (east), a9..h9 (north), gr1..gr8 (west). gr0 y0 gr9 y9
     are the four corners, reachable but not slots. The west column is "gr"
     because g1..g8 are already squares. Slot numbers still work: s0 = a0.

   A nudge does not move a dot, it moves the TRIM: trimX_MM/trimY_MM are added
   to every position this sketch drives to, in calibration and in the walks
   alike. So nudge until the carriage sits dead centre on h1, then 'g a8' and
   see whether it lands there too. If it does, print 't' and paste the two
   TRIM_*_DEFAULT_MM lines below so the next power-up starts aligned.

   If a8 needs a DIFFERENT trim from h1, stop nudging: a single offset cannot
   fix that. The error grows with distance, so it is scale (belt pitch, pulley
   teeth, microsteps) or the frame is out of square — not a mis-parked origin.

   The trim lives in RAM; a reset loses anything not pasted into the defaults.
   Resolution is one step, 1/stepsPerMM = 0.1mm; nudges are rounded to it.
*/

#include <stdlib.h>
#include <string.h>
#include <ctype.h>

// ---------- pins ----------  (copied from chessbot_v1.ino)
const int stepPinA  = 2;
const int dirPinA   = 5;
const int stepPinB  = 3;
const int dirPinB   = 6;
const int enablePin = 8;

// Named only so setup() can force them low and leave them there. Nothing in
// this sketch ever writes to them again.
const int AIN1 = 9;
const int AIN2 = 10;

// ---------- motion scale ----------  (copied from chessbot_v1.ino)
const int   MICROSTEPS          = 2;
const float MOTOR_STEPS_PER_REV = 200.0;
const float PULLEY_TEETH        = 20.0;
const float BELT_PITCH_MM       = 2.0;

const float MM_PER_REV = PULLEY_TEETH * BELT_PITCH_MM;                     // 40
const float stepsPerMM = (MOTOR_STEPS_PER_REV * MICROSTEPS) / MM_PER_REV;  // 10

const bool invertX = false;
const bool invertY = false;

// ---------- travel limits ----------  (MEASURED on the machine)
//
// Origin (0,0) = park position = the corner of travel beyond h1. From there
// the carriage only goes -X (towards the a-file) and +Y (towards rank 8).
// Measured reach, by jogging to the frame in calibration mode:
//
//     (-480,470) ------------ (0,470)
//         |                      |
//     (-480,0)  ------------ (0,0)  <- park here at power-up
//
// i.e. 480 x 470 mm, not the 500 x 500 the frame was drawn for.
const bool ENFORCE_LIMITS = true;

const float MIN_X = -480.0, MAX_X =   0.0;
const float MIN_Y =    0.0, MAX_Y = 470.0;

// ---------- board geometry ----------
//
// 8 x 50mm squares = a 400 x 400 board. The graveyard ring is one more square
// pitch out on every side, so slot centres span 10 pitches minus one = 450mm
// each way. That fits the measured reach with 30mm to spare in X and 20mm in
// Y, split evenly: 15mm clearance at each X limit, 10mm at each Y limit.
//
//   ring X: -480 + 15 = -465  ..  0 - 15   = -15
//   ring Y:    0 + 10 =   10  ..  470 - 10 = 460
//   a1 = one pitch inside the ring's corner = (-415, 60); h1 = (-65, 60)
//
// This assumes the printed board is centred in the reach. Check it with walk
// 1: if every corner dot is off by the same amount, that is a trim; if not,
// move A1_X_MM / A1_Y_MM (the graveyard follows -- checkGeometry insists).
const float SQUARE_MM = 50.0;
const float BOARD_MM  = 400.0;     // 8 * SQUARE_MM

const float A1_X_MM = -415.0;
const float A1_Y_MM =   60.0;

// The graveyard ring: a row of eight on each of the four sides, on the board's
// own file and rank centre lines, one square pitch beyond the outer squares.
// The four corner cells are left empty.
const float GRAVEYARD_Y_LOW  =   10.0;  // beyond rank 1:     60 - 50
const float GRAVEYARD_Y_HIGH =  460.0;  // beyond rank 8:    410 + 50
const float GRAVEYARD_X_LOW  = -465.0;  // beyond the a-file: -415 - 50
const float GRAVEYARD_X_HIGH =  -15.0;  // beyond the h-file:  -65 + 50

const int SLOTS_PER_STRIP  = 8;
const int GRAVEYARD_SLOTS  = SLOTS_PER_STRIP * 4;   // 32

// ---------- alignment trim ----------
// Added to every target before it is driven to. Paste the values 't' prints
// in calibration mode over these two defaults to keep them across resets.
const float TRIM_X_DEFAULT_MM = 0.0;
const float TRIM_Y_DEFAULT_MM = 0.0;

// Larger than half a square is not a trim, it is a board in the wrong place --
// move A1_X_MM / A1_Y_MM instead. It is not a travel limit: it applies whether
// or not ENFORCE_LIMITS is on. Note the ring has only 10-15mm to the limits,
// so a trim bigger than that will be refused at the outer slots.
const float MAX_TRIM_MM = 25.0;

float trimX_MM = TRIM_X_DEFAULT_MM;
float trimY_MM = TRIM_Y_DEFAULT_MM;

// ---------- walker settings ----------
// Slower than the firmware's 40mm/s on purpose. This is a check, not a race,
// and a rod that binds is audible at 25 that is easy to miss at 40.
float feedRateMMS = 25.0;
const float MIN_FEED = 5.0, MAX_FEED = 60.0;

// How long it sits on each position. Long enough to look at the dot and
// decide, short enough that 96 of them is not an afternoon.
const unsigned long DWELL_MS = 900;

// Calibration jog step, and how long a half-typed line may sit before it is
// taken as complete (for "no line ending" in the serial monitor).
float jogStepMM = 1.0;
const float MIN_JOG = 0.1, MAX_JOG = 25.0;
const unsigned long LINE_IDLE_MS = 150;

unsigned int stepDelayUS;

// Machine position, in whole steps, measured from wherever the carriage was
// parked at power-up. Kept in steps rather than mm so sub-step nudges and a
// long walk cannot accumulate rounding error: the carriage is always exactly
// where these say, and every move is computed against them.
long curStepX = 0, curStepY = 0;
bool aborted = false;

// ============================================================
//  MOTION   (copied from chessbot_v1.ino, position kept in steps)
// ============================================================
void recalcSpeed() {
  stepDelayUS = (unsigned int)(1000000.0 / (2.0 * feedRateMMS * stepsPerMM));
}

long mmToSteps(float mm) { return lround(mm * stepsPerMM); }
float stepsToMM(long steps) { return steps / stepsPerMM; }
float quantizeMM(float mm) { return stepsToMM(mmToSteps(mm)); }

void moveCoreXYSteps(long dx, long dy) {
  if (invertX) dx = -dx;
  if (invertY) dy = -dy;

  // CoreXY mixer: A = X + Y, B = X - Y
  long stepsA = dx + dy;
  long stepsB = dx - dy;

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

bool withinLimits(float mx, float my) {
  if (!ENFORCE_LIMITS) return true;
  return mx >= MIN_X && mx <= MAX_X && my >= MIN_Y && my <= MAX_Y;
}

// The travel guard -- when ENFORCE_LIMITS is on, the only thing between a
// wrong number (or a wild trim) and the frame. Takes MACHINE coordinates:
// trim already applied.
bool moveMachine(float mx, float my) {
  if (!withinLimits(mx, my)) return false;
  long tx = mmToSteps(mx);
  long ty = mmToSteps(my);
  moveCoreXYSteps(tx - curStepX, ty - curStepY);
  curStepX = tx;
  curStepY = ty;
  return true;
}

// Drive to a NOMINAL board position: the geometry above, plus the trim.
bool moveTo(float tx, float ty) {
  return moveMachine(tx + trimX_MM, ty + trimY_MM);
}

// ============================================================
//  GEOMETRY
// ============================================================
float squareX(int file) { return A1_X_MM + file * SQUARE_MM; }
float squareY(int rank) { return A1_Y_MM + rank * SQUARE_MM; }

// Mirrors rig.graveyard_slot_to_mm(). Counter-clockwise from the strip past
// rank 1, which is why consecutive slots are always physically adjacent --
// including across the empty corners. That makes this both the shortest walk
// and directly comparable to what the Pi reports: "slot 20" on screen is the
// dot this stops at 21st.
void slotToMM(int slot, float &x, float &y) {
  int strip = slot / SLOTS_PER_STRIP;
  int i     = slot % SLOTS_PER_STRIP;
  switch (strip) {
    case 0:  x = squareX(i);        y = GRAVEYARD_Y_LOW;   break;  // south, a->h
    case 1:  x = GRAVEYARD_X_HIGH;  y = squareY(i);        break;  // east, 1->8
    case 2:  x = squareX(7 - i);    y = GRAVEYARD_Y_HIGH;  break;  // north, h->a
    default: x = GRAVEYARD_X_LOW;   y = squareY(7 - i);    break;  // west, 8->1
  }
}

String squareName(int file, int rank) {
  return String((char)('a' + file)) + String(rank + 1);
}

// ---------- the naming grid ----------
// Every position has a name on a 10x10 grid laid over the board and its
// graveyard ring:
//
//   columns   gr  a  b  c  d  e  f  g  h  y      (gr = west of a, y = east of h)
//   rows       0  1  2  3  4  5  6  7  8  9      (0 = south of 1, 9 = north of 8)
//
//     gr9  a9 .. h9  y9      a9..h9  north strip  (slots 23..16)
//     gr8  a8 .. h8  y8      y1..y8  east strip   (slots  8..15)
//      :    board     :      a0..h0  south strip  (slots  0..7)
//     gr1  a1 .. h1  y1      gr1..gr8 west strip  (slots 31..24)
//     gr0  a0 .. h0  y0      gr0 y0 gr9 y9: the empty corner cells
//
// The west column is "gr", not "g", because g1..g8 are real squares.
// Internally: col -1..8 (gr, a..h, y), row 0..9, and a board square is
// col 0..7, row 1..8 -- so squareX(col) and squareY(row - 1) still apply.

String colName(int col) {
  if (col < 0) return String(F("gr"));
  if (col > 7) return String(F("y"));
  return String((char)('a' + col));
}

String gridName(int col, int row) {
  return colName(col) + String(row);
}

// "e4" / "gr3" / "y0" -> col, row. False for anything else.
bool parseGrid(const char *s, int &col, int &row) {
  if (s[0] == 'g' && s[1] == 'r') { col = -1; s += 2; }
  else if (s[0] >= 'a' && s[0] <= 'h') { col = s[0] - 'a'; s += 1; }
  else if (s[0] == 'y') { col = 8; s += 1; }
  else return false;
  if (s[0] < '0' || s[0] > '9' || s[1] != '\0') return false;
  row = s[0] - '0';
  return true;
}

bool isBoardSquare(int col, int row) {
  return col >= 0 && col <= 7 && row >= 1 && row <= 8;
}

// Grid cell -> graveyard slot number, or -1 for a board square or a corner.
// The inverse of slotToGrid(); both follow slotToMM()'s ring order.
int gridToSlot(int col, int row) {
  bool inFile = (col >= 0 && col <= 7), inRank = (row >= 1 && row <= 8);
  if (row == 0 && inFile) return col;                          // south, a->h
  if (col == 8 && inRank) return SLOTS_PER_STRIP + (row - 1);  // east, 1->8
  if (row == 9 && inFile) return 2 * SLOTS_PER_STRIP + (7 - col);  // north, h->a
  if (col == -1 && inRank) return 3 * SLOTS_PER_STRIP + (8 - row); // west, 8->1
  return -1;
}

void slotToGrid(int slot, int &col, int &row) {
  int i = slot % SLOTS_PER_STRIP;
  switch (slot / SLOTS_PER_STRIP) {
    case 0:  col = i;      row = 0;     break;
    case 1:  col = 8;      row = i + 1; break;
    case 2:  col = 7 - i;  row = 9;     break;
    default: col = -1;     row = 8 - i; break;
  }
}

String slotName(int slot) {
  int col, row;
  slotToGrid(slot, col, row);
  return gridName(col, row);
}

// "e4" -> "e4", "y5" -> "y5 (slot 12)", "gr0" -> "gr0 (corner)"
String gridLabel(int col, int row) {
  String name = gridName(col, row);
  if (isBoardSquare(col, row)) return name;
  int slot = gridToSlot(col, row);
  if (slot < 0) return name + F(" (corner)");
  return name + F(" (slot ") + String(slot) + F(")");
}

// Re-derives the graveyard from the board, and proves every one of the 96
// positions (64 squares, 32 slots) is inside the travel limits. The literals
// above are what other tools parse, so they stay literals; this is what stops
// one of them being edited without the rest -- or the board being moved
// somewhere the carriage cannot reach.
bool approxEq(float a, float b) { return fabs(a - b) < 0.01; }

bool reachable(float x, float y) {
  return x >= MIN_X && x <= MAX_X && y >= MIN_Y && y <= MAX_Y;
}

bool checkGeometry() {
  bool ok = approxEq(BOARD_MM, 8 * SQUARE_MM)
         && MIN_X < MAX_X && MIN_Y < MAX_Y
         && approxEq(GRAVEYARD_Y_LOW,  squareY(0) - SQUARE_MM)
         && approxEq(GRAVEYARD_Y_HIGH, squareY(7) + SQUARE_MM)
         && approxEq(GRAVEYARD_X_LOW,  squareX(0) - SQUARE_MM)
         && approxEq(GRAVEYARD_X_HIGH, squareX(7) + SQUARE_MM)
         && fabs(TRIM_X_DEFAULT_MM) <= MAX_TRIM_MM
         && fabs(TRIM_Y_DEFAULT_MM) <= MAX_TRIM_MM;

  for (int f = 0; f < 8; f++)
    for (int r = 0; r < 8; r++)
      ok = ok && reachable(squareX(f), squareY(r));
  for (int slot = 0; slot < GRAVEYARD_SLOTS; slot++) {
    float x, y;
    slotToMM(slot, x, y);
    ok = ok && reachable(x, y);

    // the slot's grid name must name the same slot, at the same point
    int col, row;
    slotToGrid(slot, col, row);
    ok = ok && gridToSlot(col, row) == slot
            && approxEq(squareX(col), x) && approxEq(squareY(row - 1), y);
  }
  return ok;
}

// ============================================================
//  SERIAL / FLOW CONTROL
// ============================================================
void printSigned(float v) {
  if (v >= 0) Serial.print('+');
  Serial.print(v, 1);
}

void printTrim() {
  Serial.print(F("trim ("));
  printSigned(trimX_MM);
  Serial.print(F(", "));
  printSigned(trimY_MM);
  Serial.print(F(")"));
}

void printMenu() {
  Serial.println();
  Serial.println(F("  1  four corners (a1 h1 h8 a8)   -- do this first"));
  Serial.println(F("  2  all 64 squares"));
  Serial.println(F("  3  all 32 graveyard slots"));
  Serial.println(F("  4  everything"));
  Serial.println(F("  5  travel limits, 480x470 corners (no dots -- watch the frame)"));
  Serial.println(F("  g <name>  go to a cell and stay: e4, a0 (graveyard), gr3, y9, s12"));
  Serial.println(F("  h  home (origin corner)     c  calibration / trim mode"));
  Serial.println(F("  p  pause/resume    q  abort    + -  speed    ?  this menu"));
  Serial.print(F("  feed "));
  Serial.print(feedRateMMS, 0);
  Serial.print(F(" mm/s    "));
  printTrim();
  Serial.println();
  Serial.println();
}

// Sit on a position, staying responsive. Returns false if the walk was
// aborted, so every caller can unwind instead of finishing a pass nobody is
// watching any more.
bool dwell(unsigned long ms) {
  unsigned long until = millis() + ms;
  while (millis() < until) {
    if (Serial.available()) {
      char c = Serial.read();
      if (c == 'q' || c == 'Q') { aborted = true; return false; }
      if (c == 'p' || c == 'P') {
        Serial.println(F("  [paused -- p resumes, q aborts]"));
        while (true) {
          if (Serial.available()) {
            char k = Serial.read();
            if (k == 'p' || k == 'P') { Serial.println(F("  [resumed]")); break; }
            if (k == 'q' || k == 'Q') { aborted = true; return false; }
          }
          delay(10);
        }
        until = millis() + ms;
      }
      if (c == '+') { feedRateMMS = min(feedRateMMS + 5.0f, MAX_FEED); recalcSpeed();
                      Serial.print(F("  feed ")); Serial.println(feedRateMMS, 0); }
      if (c == '-') { feedRateMMS = max(feedRateMMS - 5.0f, MIN_FEED); recalcSpeed();
                      Serial.print(F("  feed ")); Serial.println(feedRateMMS, 0); }
    }
    delay(5);
  }
  return true;
}

// One stop on the walk: drive there, say where, wait to be looked at.
// `trimmed` is false only for the envelope walk, whose targets are the
// frame's limits rather than printed dots: trimming those would push half of
// them past the guard.
bool visit(const String &label, float x, float y, bool trimmed = true) {
  Serial.print(F("  "));
  Serial.print(label);
  Serial.print(F("  ("));
  Serial.print(x, 1);
  Serial.print(F(", "));
  Serial.print(y, 1);
  Serial.print(F(")"));

  if (!(trimmed ? moveTo(x, y) : moveMachine(x, y))) {
    // Not reachable. With zero trim, for a fixed table of positions, that
    // means the geometry and the limits in this file disagree -- a bug here,
    // not a bad input. With a trim, it may just be the trim pushing a slot
    // past the envelope.
    Serial.println(F("   *** OUT OF RANGE -- skipped ***"));
    return true;
  }
  Serial.println();
  return dwell(DWELL_MS);
}

// ============================================================
//  WALKS
// ============================================================
bool walkCorners() {
  Serial.println(F("[corners] a1 h1 h8 a8"));
  const int files[] = {0, 7, 7, 0};
  const int ranks[] = {0, 0, 7, 7};
  for (int i = 0; i < 4; i++) {
    if (!visit(squareName(files[i], ranks[i]),
               squareX(files[i]), squareY(ranks[i]))) return false;
  }
  return true;
}

bool walkSquares() {
  Serial.println(F("[squares] all 64"));
  // Boustrophedon: every rank reverses, so the carriage never crosses the
  // board to start the next row. Shorter, and easier to follow by eye.
  for (int rank = 0; rank < 8; rank++) {
    for (int n = 0; n < 8; n++) {
      int file = (rank % 2 == 0) ? n : (7 - n);
      if (!visit(squareName(file, rank), squareX(file), squareY(rank))) return false;
    }
  }
  return true;
}

// The four corners of the measured travel limits -- the only walk that goes
// past the graveyard ring to the limits themselves. Squares and slots are
// centres, so on their own they only ever span 350mm and 450mm; this is what
// re-proves the measured reach. Run at the slowest feed, because with no
// limit switches a frame that has shifted shows up here as a stall or a
// knock, and 'q' is the only stop. One corner is the origin itself.
bool walkEnvelope() {
  Serial.println(F("[limits] measured corners at 5 mm/s -- q aborts"));
  float savedFeed = feedRateMMS;
  feedRateMMS = MIN_FEED;
  recalcSpeed();

  const float xs[] = {MIN_X, MAX_X, MAX_X, MIN_X};
  const float ys[] = {MIN_Y, MIN_Y, MAX_Y, MAX_Y};
  const char *names[] = {"SW (beyond a1)", "SE (origin)", "NE (beyond h8)", "NW (beyond a8)"};
  bool ok = true;
  for (int i = 0; i < 4 && ok; i++) ok = visit(names[i], xs[i], ys[i], false);

  feedRateMMS = savedFeed;
  recalcSpeed();
  return ok;
}

bool walkSlots() {
  Serial.println(F("[slots] all 32, ring order -- THIS IS THE NEW GROUND"));
  for (int slot = 0; slot < GRAVEYARD_SLOTS; slot++) {
    float x, y;
    slotToMM(slot, x, y);
    if (!visit(String(F("slot ")) + slot + F("  ") + slotName(slot), x, y)) return false;
  }
  return true;
}

// Home is the MACHINE origin -- the corner the carriage was parked in at
// power-up -- not a square. Ending every run on the power-up position keeps
// the next power-up's assumption true.
void goHome() {
  Serial.println(F("[home] returning to the origin corner"));
  moveMachine(0.0, 0.0);
}

void runWalk(char choice) {
  aborted = false;
  bool ok = true;

  if (choice == '1' || choice == '4') ok = walkCorners();
  if (ok && (choice == '3' || choice == '4')) ok = walkSlots();
  if (ok && (choice == '2' || choice == '4')) ok = walkSquares();
  if (ok && choice == '5') ok = walkEnvelope();

  goHome();
  Serial.println(aborted ? F("[aborted]") : F("[done]"));
  printMenu();
}

// ============================================================
//  CALIBRATION
// ============================================================
// The dot being aligned to, as a nominal board position. Every nudge changes
// the trim and then re-seeks this target, so the carriage moves by exactly
// the nudge and the trim is exactly what it took to get there.
String calLabel = "h1";
float calNomX = A1_X_MM + 7 * SQUARE_MM, calNomY = A1_Y_MM;   // h1

void printCalStatus() {
  Serial.print(F("  "));
  Serial.print(calLabel);
  Serial.print(F("  nominal ("));
  Serial.print(calNomX, 1);
  Serial.print(F(", "));
  Serial.print(calNomY, 1);
  Serial.print(F(")  "));
  printTrim();
  Serial.print(F("  -> machine ("));
  Serial.print(stepsToMM(curStepX), 1);
  Serial.print(F(", "));
  Serial.print(stepsToMM(curStepY), 1);
  Serial.println(F(")"));
}

void printCalHelp() {
  Serial.println();
  Serial.println(F("[calibrate] one command per line; the magnet stays OFF"));
  Serial.println(F("  w/a/s/d [mm]  nudge +Y/-X/-Y/+X   (arrow keys: one jog step)"));
  Serial.println(F("  j <mm>        jog step            g e4 | g s12   go to square/slot"));
  Serial.println(F("  m <x> <y>     go to any point, mm from the origin corner (trim applied)"));
  Serial.println(F("  t  print trim to paste    z  zero trim    x  exit    ?  help"));
  Serial.print(F("  jog "));
  Serial.print(jogStepMM, 1);
  Serial.println(F(" mm"));
}

void printTrimRecord() {
  printCalStatus();
  Serial.println(F("  // paste over the defaults in chessbot_walker.ino to keep this:"));
  Serial.print(F("  const float TRIM_X_DEFAULT_MM = "));
  Serial.print(trimX_MM, 1);
  Serial.println(F(";"));
  Serial.print(F("  const float TRIM_Y_DEFAULT_MM = "));
  Serial.print(trimY_MM, 1);
  Serial.println(F(";"));
}

// Apply a trim, and put the carriage on the current target with it. Refuses
// (and leaves both trim and carriage alone) if the result would be past
// MAX_TRIM_MM or outside the travel envelope.
bool setTrim(float tx, float ty) {
  tx = quantizeMM(tx);
  ty = quantizeMM(ty);
  if (fabs(tx) > MAX_TRIM_MM || fabs(ty) > MAX_TRIM_MM) {
    Serial.println(F("  *** trim would exceed 25mm -- re-park the carriage instead ***"));
    return false;
  }
  if (!moveMachine(calNomX + tx, calNomY + ty)) {
    Serial.println(F("  *** that would pass the travel limits -- refused ***"));
    return false;
  }
  trimX_MM = tx;
  trimY_MM = ty;
  printCalStatus();
  return true;
}

// The nudge itself is rounded to whole steps, so "d 0.25" always moves 0.3
// wherever the trim currently sits, rather than depending on how the sum
// happens to round.
void nudge(float dx, float dy) {
  setTrim(trimX_MM + quantizeMM(dx), trimY_MM + quantizeMM(dy));
}

// Drive to an arbitrary point and make it the calibration target, so w/a/s/d
// then refine the trim against whatever dot is under it. The point is a
// NOMINAL position, like every other target: the trim is added before it is
// driven to ('z' first for raw machine coordinates). Returns false, and does
// not move, if the point is outside the travel envelope.
bool goToXY(float x, float y) {
  if (!moveTo(x, y)) {
    Serial.print(F("  *** ("));
    Serial.print(x, 1);
    Serial.print(F(", "));
    Serial.print(y, 1);
    Serial.print(F(") + trim is outside the limits x "));
    Serial.print(MIN_X, 0); Serial.print(F("..")); Serial.print(MAX_X, 0);
    Serial.print(F(", y "));
    Serial.print(MIN_Y, 0); Serial.print(F("..")); Serial.print(MAX_Y, 0);
    Serial.println(F(" -- not moved ***"));
    return false;
  }
  calLabel = "point";
  calNomX = x;
  calNomY = y;
  printCalStatus();
  return true;
}

// "m -175 87.5" / "m -175,87.5" -> goToXY(-175, 87.5)
void calMoveXY(const char *arg) {
  char *end;
  float x = strtod(arg, &end);
  bool okX = (end != arg);
  while (*end == ' ' || *end == ',') end++;
  const char *rest = end;
  float y = strtod(rest, &end);
  bool okY = (end != rest);
  while (*end == ' ') end++;
  if (!okX || !okY || *end != '\0') {
    Serial.println(F("  e.g.  m -175 87.5"));
    return;
  }
  goToXY(x, y);
}

// Go to a named cell -- "e4", "a0", "gr3", "y9" -- or a slot by number,
// "s12". Makes it the calibration target. Used from the main menu and from
// calibration mode alike.
void goToNamed(const char *arg) {
  float x, y;
  String label;
  int col, row;

  if (parseGrid(arg, col, row)) {
    x = squareX(col);
    y = squareY(row - 1);
    label = gridLabel(col, row);
  } else if (arg[0] == 's' && isdigit(arg[1])) {
    char *end;
    long slot = strtol(arg + 1, &end, 10);
    if (*end != '\0' || slot < 0 || slot >= GRAVEYARD_SLOTS) {
      Serial.println(F("  slots are s0 .. s31"));
      return;
    }
    slotToMM((int)slot, x, y);
    slotToGrid((int)slot, col, row);
    label = gridLabel(col, row);
  } else {
    Serial.println(F("  go where?  a1..h8 board, a0..h0 a9..h9 gr1..gr8 y1..y8 graveyard,"));
    Serial.println(F("             gr0 y0 gr9 y9 corners, or s0..s31 by slot number"));
    return;
  }

  if (!moveTo(x, y)) {
    Serial.print(F("  *** "));
    Serial.print(label);
    Serial.println(F(" is OUT OF RANGE with this trim -- not moved ***"));
    return;
  }
  calLabel = label;
  calNomX = x;
  calNomY = y;
  printCalStatus();
}

// One line of input. Returns false when it is time to leave calibration.
bool calCommand(char *line) {
  // trim leading/trailing whitespace, fold to lower case
  while (*line == ' ' || *line == '\t') line++;
  int n = strlen(line);
  while (n > 0 && (line[n - 1] == ' ' || line[n - 1] == '\t')) line[--n] = '\0';
  if (n == 0) return true;
  for (int i = 0; i < n; i++) line[i] = tolower(line[i]);

  char cmd = line[0];
  char *arg = line + 1;
  while (*arg == ' ') arg++;

  // Optional number after the command letter. hasVal is false for a bare
  // letter, so "w" means "one jog step" and "w 0" is an explicit no-op.
  char *end;
  float val = strtod(arg, &end);
  bool hasVal = (end != arg);
  bool clean  = (*end == '\0');

  switch (cmd) {
    case 'w': case 'a': case 's': case 'd': {
      if (!clean) { Serial.println(F("  e.g.  w 0.5")); break; }
      float mm = hasVal ? val : jogStepMM;
      if (cmd == 'w') nudge(0, +mm);
      if (cmd == 's') nudge(0, -mm);
      if (cmd == 'd') nudge(+mm, 0);
      if (cmd == 'a') nudge(-mm, 0);
      break;
    }
    case 'j':
      if (!hasVal || !clean || val < MIN_JOG || val > MAX_JOG) {
        Serial.println(F("  jog step is 0.1 .. 25 mm, e.g.  j 0.5"));
        break;
      }
      jogStepMM = quantizeMM(val);
      Serial.print(F("  jog "));
      Serial.print(jogStepMM, 1);
      Serial.println(F(" mm"));
      break;
    case 'm':
      calMoveXY(arg);
      break;
    case 'g':
      goToNamed(arg);
      break;
    case 't':
      printTrimRecord();
      break;
    case 'z':
      setTrim(0.0, 0.0);
      break;
    case '?':
      printCalHelp();
      printCalStatus();
      break;
    case 'x': case 'q':
      printTrimRecord();
      return false;
    default:
      Serial.println(F("  ? for help"));
      break;
  }
  return true;
}

void calibrate() {
  printCalHelp();

  // Start on the current target with whatever trim is current: h1 after
  // power-up, or wherever the main menu's 'g' last went. The first thing the
  // carriage does is show where it now thinks that dot is.
  if (!moveTo(calNomX, calNomY)) Serial.println(F("  *** target out of range with this trim ***"));
  printCalStatus();

  char buf[24];
  byte n = 0;
  byte esc = 0;               // 0 idle, 1 got ESC, 2 got ESC [
  unsigned long lastRx = 0;

  while (true) {
    if (Serial.available()) {
      char c = Serial.read();
      lastRx = millis();

      // Arrow keys from a real terminal arrive as ESC [ A/B/C/D, no newline.
      if (esc == 0 && c == 27) { esc = 1; continue; }
      if (esc == 1) { esc = (c == '[') ? 2 : 0; continue; }
      if (esc == 2) {
        esc = 0;
        if (c == 'A') nudge(0, +jogStepMM);
        if (c == 'B') nudge(0, -jogStepMM);
        if (c == 'C') nudge(+jogStepMM, 0);
        if (c == 'D') nudge(-jogStepMM, 0);
        continue;
      }

      if (c == '\n' || c == '\r') {
        if (n > 0) {
          buf[n] = '\0';
          n = 0;
          if (!calCommand(buf)) break;
        }
        continue;
      }
      if (n < sizeof(buf) - 1) buf[n++] = c;
    } else if (n > 0 && millis() - lastRx > LINE_IDLE_MS) {
      // "No line ending": the monitor sends the whole line at once, then
      // nothing. A pause that long means the line is finished.
      buf[n] = '\0';
      n = 0;
      if (!calCommand(buf)) break;
    }
  }

  goHome();
  Serial.println(F("[calibrate] done -- the trim stays active until reset"));
  printMenu();
}

// ============================================================
void setup() {
  pinMode(stepPinA, OUTPUT);
  pinMode(dirPinA,  OUTPUT);
  pinMode(stepPinB, OUTPUT);
  pinMode(dirPinB,  OUTPUT);
  pinMode(enablePin, OUTPUT);
  digitalWrite(enablePin, LOW);          // drivers enabled (active low)

  // The coil, off, for good. Nothing below ever writes to these again.
  pinMode(AIN1, OUTPUT);
  pinMode(AIN2, OUTPUT);
  digitalWrite(AIN1, LOW);
  digitalWrite(AIN2, LOW);

  recalcSpeed();

  Serial.begin(115200);
  while (!Serial) { ; }

  Serial.println();
  Serial.println(F("READY ChessBot-V2 WALKER -- bring-up only, NOT the game firmware"));
  Serial.println(F("Re-flash chessbot_v1.ino before connecting the Pi."));
  Serial.println(F("The magnet stays OFF for the whole run."));
  if (!ENFORCE_LIMITS) {
    Serial.println(F("*** TRAVEL LIMITS ARE OFF -- nothing stops a bad coordinate ***"));
  }

  if (!checkGeometry()) {
    // Drivers off so the carriage can be pushed by hand; nothing will move.
    digitalWrite(enablePin, HIGH);
    Serial.println();
    Serial.println(F("*** GEOMETRY CONSTANTS DISAGREE WITH EACH OTHER -- HALTED ***"));
    Serial.println(F("Fix the board geometry block (or the trim defaults) and re-flash."));
    while (true) { delay(1000); }
  }

  Serial.println();
  Serial.println(F("Park the carriage on the ORIGIN CORNER by hand before this"));
  Serial.println(F("powers on -- there are no limit switches, so every position"));
  Serial.println(F("below is measured from wherever it happened to start."));
  printMenu();
}

// Read the rest of a line, for main-menu commands that take an argument.
// Blocks until a line ending, or LINE_IDLE_MS of quiet after some text (the
// serial monitor's "no line ending"). A bare line ending returns empty.
void readLine(char *buf, byte size) {
  byte n = 0;
  unsigned long lastRx = millis();
  while (true) {
    if (Serial.available()) {
      char c = Serial.read();
      lastRx = millis();
      if (c == '\n' || c == '\r') break;
      if (n < size - 1) buf[n++] = tolower(c);
    } else if (n > 0 && millis() - lastRx > LINE_IDLE_MS) {
      break;
    } else if (n == 0 && millis() - lastRx > 2000) {
      break;                                   // "g" and nothing else
    }
  }
  buf[n] = '\0';
}

void loop() {
  if (!Serial.available()) return;
  char c = Serial.read();

  switch (c) {
    case '1': case '2': case '3': case '4': case '5':
      runWalk(c);
      break;
    case 'c': case 'C':
      calibrate();
      break;
    case 'g': case 'G': {
      char buf[16];
      readLine(buf, sizeof(buf));
      char *arg = buf;
      while (*arg == ' ') arg++;
      goToNamed(arg);
      break;
    }
    case 'h': case 'H':
      goHome();
      break;
    case '?':
      printMenu();
      break;
    case '+':
      feedRateMMS = min(feedRateMMS + 5.0f, MAX_FEED); recalcSpeed();
      Serial.print(F("feed ")); Serial.println(feedRateMMS, 0);
      break;
    case '-':
      feedRateMMS = max(feedRateMMS - 5.0f, MIN_FEED); recalcSpeed();
      Serial.print(F("feed ")); Serial.println(feedRateMMS, 0);
      break;
    default:
      break;   // newlines and stray characters are not errors
  }
}
