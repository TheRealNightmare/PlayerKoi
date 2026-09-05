/*
   chess_gantry -- CoreXY gantry + electromagnet executor for the Player Koi
   chess robot. Arduino Uno, talking to a Raspberry Pi over USB serial.

   This firmware knows NOTHING about chess. It moves a magnet to float
   coordinates on an 8x8 grid, switches the coil, and homes. Every decision
   about *which* squares to visit -- captures, castling, knight routing,
   promotion -- is made on the Pi (see src/robot_moves.py), which has the
   real board state and python-chess. Keeping the rules on one side is the
   whole point: this sketch stays small enough to trust.

   PROTOCOL (line-based ASCII, 115200 8N1, "\n" terminated)

     boot           -> READY
     PING           -> OK PONG
     HOME           -> OK              seek both limits, zero the motors
     GOTO <x> <y>   -> OK              float square coords, 0,0 = a1 centre
     MAG <0-255>    -> OK              attract, clamped to MAG_MAX_PWM
     PULSE          -> OK              reverse-then-forward kick; recentres a piece
     TOPPLE         -> OK              reverse-polarity kick; knocks a piece over
     OFF            -> OK              coil off, drivers disabled
     STATUS         -> OK <x> <y> <homed>
     !              -> ERR ABORT       soft e-stop, valid mid-move

   Every command blocks until the action is finished, then acks. The Pi
   therefore never has to guess when motion completed -- it just waits for
   the line. Anything unparseable, or motion before homing, gets
   "ERR <reason>" and the Pi halts the robot.

   COORDINATES: x = file (0 = a .. 7 = h), y = rank (0 = rank 1 .. 7 = rank 8).
   Halves are legal and expected -- 3.5 is the lattice line between files d
   and e, which is how pieces are routed through the gaps between squares
   without brushing their neighbours.
*/

#include <AccelStepper.h>
#include <MultiStepper.h>

// ---------------------------------------------------------------- pin map
// See docs/HARDWARE.md for the full circuit. D0/D1 are the USB serial pair
// and must stay free. D9/D10 are Timer1, which AccelStepper never touches,
// so magnet PWM and stepping don't interfere.
const int M1_STEP = 2;
const int M1_DIR  = 4;
const int M2_STEP = 7;
const int M2_DIR  = 8;
const int DRIVER_EN = 12;   // both TMC2208 EN pins, active LOW
const int MAG_IN1 = 9;      // DRV8872 IN1 -- attract
const int MAG_IN2 = 10;     // DRV8872 IN2 -- repel
const int X_LIMIT = A0;     // momentary to GND, INPUT_PULLUP: LOW = pressed
const int Y_LIMIT = A1;

// ------------------------------------------------------------- rig tuning
// This board: 30mm squares, 240x240mm playing area, on a 280x300mm PCB.
// STEPS_PER_SQUARE is derived, not measured -- verify it rather than tune it
// (docs/HARDWARE.md): after homing, GOTO 7 0 must travel exactly 210mm.
//   NEMA17 at 1/4 microstepping   200 * 4  = 800 steps/rev
//   20-tooth GT2 pulley           20 * 2mm =  40 mm/rev
//   -> 800 / 40mm                          =  20 steps/mm
//   -> 20 * 30mm square                    = 600 steps/square
// If GOTO 7 0 doesn't land on 210mm, the pulley isn't 20T or the MS1/MS2
// jumpers aren't set for 1/4 -- fix the hardware, don't fudge this number.
const float SQUARE_MM        = 30.0;
const float STEPS_PER_MM     = 20.0;
const long  STEPS_PER_SQUARE = (long)(SQUARE_MM * STEPS_PER_MM);   // 600

const float MAX_SPEED     = 1500.0;   // steps/s = 75mm/s at 20 steps/mm.
                                      // MultiStepper doesn't accelerate, so
                                      // this is also the START speed: 1500
                                      // steps/s is 112 RPM at the motor,
                                      // which a NEMA17 starts from rest
                                      // comfortably. There's headroom to
                                      // ~2500 (125mm/s) if the belts behave;
                                      // past what the motors can start at,
                                      // it silently loses steps instead.
const float HOME_SPEED    = 800.0;    // 40mm/s, slower into the switch
const long  HOME_BACKOFF  = 200;      // 10mm back off the switch before the
                                      // slow re-approach

// Where the limit switches sit relative to a1's centre, in squares. After
// homing the firmware drives here and calls it (0,0), so GOTO 0 0 parks the
// magnet exactly under a1.
const float HOME_OFFSET_X = 0.0;
const float HOME_OFFSET_Y = 0.0;

// The electromagnet is a 5V coil on a 7.5V rail: at full duty it cooks. 170
// (~66%) puts roughly 5V average across it. Do not raise this without
// putting a 5V buck in front of the DRV8872.
const int MAG_MAX_PWM = 170;

const int  PULSE_REVERSE_MS = 10;   // recentring: brief repel...
const int  PULSE_HOLD_MS    = 100;  // ...then attract to settle it
const int  TOPPLE_MS        = 25;   // repel kick that knocks a piece over
const long MOVE_TIMEOUT_MS  = 30000;

// ------------------------------------------------------------------ state
AccelStepper M1(AccelStepper::DRIVER, M1_STEP, M1_DIR);
AccelStepper M2(AccelStepper::DRIVER, M2_STEP, M2_DIR);
MultiStepper motors;

bool  homed = false;
float posX = 0.0, posY = 0.0;   // last commanded position, in squares
int   magDuty = 0;              // current attract duty, 0 = coil off

char  line[32];
byte  lineLen = 0;


void setup() {
  pinMode(DRIVER_EN, OUTPUT);
  digitalWrite(DRIVER_EN, HIGH);      // drivers OFF until we're asked to move
  pinMode(MAG_IN1, OUTPUT);
  pinMode(MAG_IN2, OUTPUT);
  analogWrite(MAG_IN1, 0);
  analogWrite(MAG_IN2, 0);
  pinMode(X_LIMIT, INPUT_PULLUP);
  pinMode(Y_LIMIT, INPUT_PULLUP);

  M1.setMaxSpeed(MAX_SPEED);
  M2.setMaxSpeed(MAX_SPEED);
  motors.addStepper(M1);
  motors.addStepper(M2);

  Serial.begin(115200);
  while (!Serial);
  // The Uno resets when the Pi opens the port, so this banner is how the Pi
  // knows the link is live and that everything below is un-homed.
  Serial.println("READY");
}


void loop() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      line[lineLen] = '\0';
      if (lineLen) handleCommand(line);
      lineLen = 0;
    }
    else if (lineLen < sizeof(line) - 1) {
      line[lineLen++] = c;
    }
    else {                            // overlong garbage; drop the whole line
      lineLen = 0;
      Serial.println("ERR LINE TOO LONG");
    }
  }
}


void handleCommand(char *cmd) {
  if (!strcmp(cmd, "PING")) {
    Serial.println("OK PONG");
  }
  else if (!strcmp(cmd, "HOME")) {
    home();
    Serial.println("OK");
  }
  else if (!strcmp(cmd, "STATUS")) {
    Serial.print("OK ");
    Serial.print(posX, 2);
    Serial.print(' ');
    Serial.print(posY, 2);
    Serial.print(' ');
    Serial.println(homed ? 1 : 0);
  }
  else if (!strncmp(cmd, "GOTO ", 5)) {
    if (!homed) {
      Serial.println("ERR NOT HOMED");
      return;
    }
    char *xs = cmd + 5;
    char *ys = strchr(xs, ' ');
    if (!ys) {
      Serial.println("ERR GOTO NEEDS X AND Y");
      return;
    }
    *ys++ = '\0';
    float x = atof(xs), y = atof(ys);
    // Travel is exactly the board, so anything outside it would drive the
    // carriage into the frame. Refuse rather than stall the motors. The
    // planner never asks for more than +/-0.5 squares (15mm) beyond a1/h8's
    // centres, which is the board's own edge; 0.6 leaves a little slack for
    // rounding without allowing a real overrun.
    if (x < -0.6 || x > 7.6 || y < -0.6 || y > 7.6) {
      Serial.println("ERR OUT OF RANGE");
      return;
    }
    if (goTo(x, y)) Serial.println("OK");
    else            Serial.println("ERR ABORT");
  }
  else if (!strncmp(cmd, "MAG ", 4)) {
    setMagnet(atoi(cmd + 4));
    Serial.println("OK");
  }
  else if (!strcmp(cmd, "PULSE")) {
    pulse();
    Serial.println("OK");
  }
  else if (!strcmp(cmd, "TOPPLE")) {
    topple();
    Serial.println("OK");
  }
  else if (!strcmp(cmd, "OFF")) {
    setMagnet(0);
    digitalWrite(DRIVER_EN, HIGH);
    Serial.println("OK");
  }
  else if (!strcmp(cmd, "!")) {
    // Abort outside a move: nothing is running, but drop the coil anyway so
    // a panicking operator always gets the same effect.
    setMagnet(0);
    Serial.println("ERR ABORT");
  }
  else {
    Serial.print("ERR UNKNOWN COMMAND ");
    Serial.println(cmd);
  }
}


// Returns false if the move was aborted with '!'. Blocks until the carriage
// arrives -- run()/poll rather than MultiStepper::runSpeedToPosition(),
// which can't be interrupted, so the e-stop would be useless mid-travel.
bool goTo(float x, float y) {
  long positions[2];
  // CoreXY: both motors together moves X, opposed moves Y.
  positions[0] = (long)(STEPS_PER_SQUARE * (x - y));
  positions[1] = (long)(STEPS_PER_SQUARE * (x + y));

  digitalWrite(DRIVER_EN, LOW);
  motors.moveTo(positions);

  unsigned long deadline = millis() + MOVE_TIMEOUT_MS;
  while (motors.run()) {
    if (Serial.available() && Serial.peek() == '!') {
      Serial.read();
      motors.moveTo(currentPositions());   // stop where we stand
      setMagnet(0);
      homed = false;                       // position is no longer trusted
      return false;
    }
    if ((long)(millis() - deadline) > 0) {
      setMagnet(0);
      homed = false;
      return false;
    }
  }

  posX = x;
  posY = y;
  return true;
}


long *currentPositions() {
  static long here[2];
  here[0] = M1.currentPosition();
  here[1] = M2.currentPosition();
  return here;
}


// Attract at `duty`, clamped so a 5V coil survives the 7.5V rail. 0 coasts
// the bridge (IN1 = IN2 = LOW), which is how the magnet rests.
void setMagnet(int duty) {
  if (duty < 0) duty = 0;
  if (duty > MAG_MAX_PWM) duty = MAG_MAX_PWM;
  magDuty = duty;
  analogWrite(MAG_IN2, 0);
  analogWrite(MAG_IN1, duty);
}


// Recentres a piece that arrived slightly off-square: a brief repel shoves
// it off the pole face, then a longer attract pulls it back to centre.
void pulse() {
  int duty = magDuty ? magDuty : MAG_MAX_PWM;
  analogWrite(MAG_IN1, 0);
  analogWrite(MAG_IN2, duty);
  delay(PULSE_REVERSE_MS);
  analogWrite(MAG_IN2, 0);
  analogWrite(MAG_IN1, duty);
  delay(PULSE_HOLD_MS);
  analogWrite(MAG_IN1, 0);
  magDuty = 0;
}


// Knocks over the piece directly above: a hard repel kick, long enough to
// topple it but not to fling it off the board. Tune TOPPLE_MS on the rig.
void topple() {
  analogWrite(MAG_IN1, 0);
  analogWrite(MAG_IN2, MAG_MAX_PWM);
  delay(TOPPLE_MS);
  analogWrite(MAG_IN2, 0);
  magDuty = 0;
}


// Seeks both limit switches at constant speed. On CoreXY neither switch
// belongs to one motor: X is both motors turning the same way, Y is both
// turning opposite -- so the axes have to be homed one at a time.
void home() {
  setMagnet(0);
  digitalWrite(DRIVER_EN, LOW);

  seek(-HOME_SPEED, -HOME_SPEED, X_LIMIT);   // X: motors together
  seek(-HOME_SPEED,  HOME_SPEED, Y_LIMIT);   // Y: motors opposed

  M1.setCurrentPosition(0);
  M2.setCurrentPosition(0);
  posX = -HOME_OFFSET_X;
  posY = -HOME_OFFSET_Y;
  homed = true;

  // Drive off the switches and onto a1's centre, so (0,0) means a1 for the
  // rest of the session regardless of where the switches physically sit.
  if (HOME_OFFSET_X != 0.0 || HOME_OFFSET_Y != 0.0) goTo(0.0, 0.0);
  else { posX = 0.0; posY = 0.0; }
}


void seek(float speed1, float speed2, int limitPin) {
  M1.setSpeed(speed1);
  M2.setSpeed(speed2);
  while (digitalRead(limitPin)) {     // HIGH = not pressed (INPUT_PULLUP)
    M1.runSpeed();
    M2.runSpeed();
  }
  M1.setSpeed(0);
  M2.setSpeed(0);

  // Back off and re-approach slowly: the first hit is at speed and the
  // switch's trip point varies with how hard it's struck.
  M1.setCurrentPosition(0);
  M2.setCurrentPosition(0);
  M1.setSpeed(speed1 > 0 ? -HOME_SPEED : HOME_SPEED);
  M2.setSpeed(speed2 > 0 ? -HOME_SPEED : HOME_SPEED);
  while (abs(M1.currentPosition()) < HOME_BACKOFF) {
    M1.runSpeed();
    M2.runSpeed();
  }

  M1.setSpeed(speed1 * 0.3);
  M2.setSpeed(speed2 * 0.3);
  while (digitalRead(limitPin)) {
    M1.runSpeed();
    M2.runSpeed();
  }
  M1.setSpeed(0);
  M2.setSpeed(0);
}
