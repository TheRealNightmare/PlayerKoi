# Player Koi — full system design

A physical chess robot. An overhead camera watches a real board and tracks
your moves; Stockfish replies; a CoreXY gantry under the board drags the
reply into place with an electromagnet; the camera then confirms the arm did
what it was told.

This document is the complete picture — architecture, hardware, geometry,
protocols, the reasoning behind each decision, and what's still risky. For
day-to-day commands see [RUNBOOK.md](../RUNBOOK.md); for wiring detail and
bring-up see [HARDWARE.md](HARDWARE.md); for the classifier's training
pipeline see [training/NOTES.md](../training/NOTES.md).

---

## 1. System at a glance

```
   ┌──────────────────────────────────────────────────────────────────┐
   │  Raspberry Pi 5                                                  │
   │                                                                  │
   │  IMX219 overhead camera                                          │
   │        │                                                         │
   │        ▼                                                         │
   │  capture.py ──▶ roi_diff.py ──▶ square_classifier.py             │
   │                 (motion gate)   (64 squares: empty/white/black)  │
   │                      │                    │                      │
   │                      └────────┬───────────┘                      │
   │                               ▼                                  │
   │                        tracking_loop.py                          │
   │                   observed delta ──▶ move_resolver.py            │
   │                                      (match vs every legal move) │
   │                               │                                  │
   │                    ┌──────────┴──────────┐                       │
   │                    ▼                     ▼                       │
   │               web_ui.py             engine.py                    │
   │            (board, camera,          (Stockfish,                  │
   │             controls)                plays Black)                │
   │                    │                     │                       │
   │                    └──────────┬──────────┘                       │
   │                               ▼                                  │
   │                    robot_moves.py  (plan the path)               │
   │                    robot.py        (drive + verify)              │
   └───────────────────────────────┬──────────────────────────────────┘
                                   │ USB serial, 115200 8N1
                                   ▼
                          ┌─────────────────┐
                          │  Arduino Uno    │  chess_gantry.ino
                          │  GOTO / MAG /   │  (knows no chess)
                          │  PULSE / TOPPLE │
                          └────────┬────────┘
                                   │
              ┌────────────────────┼────────────────────┐
              ▼                    ▼                    ▼
       2× TMC2208          DRV8872 H-bridge      2× limit switch
       2× NEMA 17          25mm electromagnet    (X and Y homing)
       (CoreXY belts)      (on the carriage)
```

**The central idea**: vision never identifies piece *type*. The board starts
at the known standard position, and the tracker applies every resolved legal
move to its own `chess.Board()`, so it always knows what every piece is in
software. Vision only has to answer three-way per square — empty, white,
black — which is a far easier learning problem, needs far less training data,
and is robust to the things that break type classification (shadows, glare,
similar-looking pieces).

---

## 2. Hardware

### Bill of materials

| Part | Spec | Qty |
|---|---|---|
| Raspberry Pi 5 | 8 GB, CPU-only inference | 1 |
| Camera | third-party IMX219 CSI, rigid overhead mount | 1 |
| Arduino Uno | ATmega328P, USB-B | 1 |
| Stepper driver | TMC2208 | 2 |
| Stepper motor | NEMA 17, 22.5 mm body | 2 |
| Motor driver | DRV8872 H-bridge (drives the magnet) | 1 |
| Electromagnet | 5 V, 50 N, 25 mm dia × 20 mm | 1 |
| Momentary switch | 6 × 6 mm (X and Y limits) | 2 |
| Power supply | 7.5 V, 36 W, 5.5 × 2.1 mm barrel | 1 |
| Barrel jack extension | 5.5 × 2.1 mm | 1 |
| Electrolytic capacitor | 100 µF / 25 V, one per driver VM | 2 |
| PCB | 280 × 300 mm, M3 corner holes — the playing surface | 1 |
| Printed sticker | 295 × 300 mm at 1:1, 240 mm grid, dot per square | 1 |

### Power — two domains, one tie

The Pi powers and programs the Uno over USB. The 7.5 V supply powers *only*
the motors and the magnet. They meet at the ground rail and nowhere else.

- **Leave the Uno's barrel jack empty.** USB from the Pi is its only supply.
- **100 µF across each driver's VM is mandatory** — the TMC2208 datasheet
  requires it; without it, plugging in the supply can kill the driver.
- **Never unplug a stepper while its driver is powered** — same outcome.
- Budget is comfortable: ~9 W steppers + ~5 W magnet against 36 W.

### Pin map (Arduino Uno)

| Pin | Connects to | Notes |
|---|---|---|
| D2 / D4 | TMC2208 #1 STEP / DIR | motor M1 |
| D7 / D8 | TMC2208 #2 STEP / DIR | motor M2 |
| D12 | both TMC2208 EN | active LOW; idles disabled |
| D9 / D10 | DRV8872 IN1 / IN2 | Timer1 PWM — both polarities |
| A0 / A1 | X / Y limit switch | `INPUT_PULLUP`, other leg to GND |
| 5 V | TMC2208 VIO ×2 | logic only |
| GND | PSU GND, all three drivers | the one required tie |

Free: D0/D1 are USB serial (never use them), plus D3, D5, D6, D11, D13,
A2–A5. D9/D10 are Timer1, which `AccelStepper` never touches, so magnet PWM
and stepping don't interfere.

**Microstepping: 1/4** — MS1 → GND, MS2 → 5 V.

### The magnet's three jobs

The DRV8872 is an H-bridge, which is the whole reason it's there: one coil,
both polarities.

| IN1 | IN2 | Effect | Command |
|---|---|---|---|
| PWM | LOW | attract | `MAG <duty>` — drag a piece |
| LOW | PWM | repel | `PULSE` (recentre), `TOPPLE` (knock over) |
| LOW | LOW | coast, coil off | `MAG 0` — resting |

The coil is 5 V on a 7.5 V rail, so firmware clamps every duty to
`MAG_MAX_PWM = 170` (≈66 % → ~5 V average). Raising it needs a 5 V buck in
front of the DRV8872.

Full wiring tables, per-module pinouts and the power-on order:
[HARDWARE.md](HARDWARE.md).

---

## 3. Board geometry

| | |
|---|---|
| Square | **30 mm** |
| Playing area | **240 × 240 mm** |
| PCB | **280 × 300 mm** |
| a1 → h8 centres | 210 mm per axis |
| Coordinate origin | a1's centre = `(0, 0)` |

Coordinates are in **squares**, x = file (0 = a … 7 = h), y = rank (0 = rank
1 … 7 = rank 8), and fractions are meaningful: `1.5` is the lattice line
between files b and c. The firmware converts to steps; nothing above it
thinks in millimetres.

### Steps per square is arithmetic, not a measurement

```
NEMA 17, 1.8°        200 full steps / rev
1/4 microstepping    × 4            =  800 steps / rev
20-tooth GT2         20 × 2 mm      =   40 mm / rev
                     800 / 40       =   20 steps / mm
30 mm square         × 30           =  600 steps / square
```

`STEPS_PER_SQUARE = 600`. Bring-up *verifies* this (`GOTO 7 0` must travel
exactly 210 mm) rather than tuning it. If it's wrong, the pulley isn't 20T or
the MS jumpers are wrong — fix the hardware, because the error compounds with
every square travelled.

1/4 rather than 1/8 because on a 30 mm square the Uno's step rate is the
limit, not precision. 1/4 still resolves 0.05 mm:

| | steps/square | at 1500 steps/s | board traverse |
|---|---|---|---|
| 1/8 | 1200 | 37.5 mm/s | ~6.4 s |
| **1/4** | **600** | **75 mm/s** | **~3.2 s** |

The TMC2208 interpolates to 256 microsteps internally, so coarser external
stepping costs nothing in smoothness.

### Clearances — the defining constraint

30 mm squares are small, and this is where it bites. Half a square — the
midline of the gap between two pieces — is only **15 mm** from each.

| | |
|---|---|
| Gap midline → each flanking piece's centre | 15.0 mm |
| Piece base diameter | 13–15 mm |
| **Clearance squeezing between two pieces** | **0–2 mm** |
| Magnet radius (25 mm coil) | 12.5 mm |
| Magnet edge → flanking piece's centre | 2.5 mm |

Two separate hazards, one cause: the carried piece can physically catch a
neighbour, and the magnet can drag one. Both are addressed by the routing
bias in §6.

---

## 4. Software architecture

Everything on the Pi is plain Python in [`src/`](../src). No framework, no
async, one thread per concern.

| Module | Lines | Responsibility |
|---|---|---|
| `capture.py` | 133 | IMX219 via picamera2. `Camera` (1640×1232, AE/AWB warmed then **locked** so exposure can't drift mid-game) and `CaptureStream` (background grab thread) |
| `calibrate.py` | 129 | Click a1, h1, h8, a8 → homography → `config/calibration.json` |
| `square_geometry.py` | 80 | Inverts that homography: square → pixel bbox. One definition shared by training-crop generation and live inference, so they can't drift apart |
| `roi_diff.py` | 116 | `BoardMotionGate` — grayscale diff of the board ROI, no model. Emits `idle` / `moving` / `settled`, firing **once** per move |
| `square_classifier.py` | 130 | NCNN 3-class model over all 64 squares. Multi-frame consensus; low confidence returns `UNRESOLVED`, never a guess |
| `board_state.py` | 74 | The 8×8 matrix convention, pretty-printing, FEN placement |
| `move_resolver.py` | 220 | The chess brain. Maintains a `chess.Board()`; matches an observed delta against every legal move's expected delta |
| `tracking_loop.py` | 340 | Orchestrates gate → classify → delta → resolve → accept or flag. Owns pause, undo, manual correction, `force_settle` |
| `engine.py` | 117 | Stockfish over UCI, plus `describe_move()` — the physical instructions for a move |
| `harvest.py` | 76 | Saves labelled square crops from resolved moves, to grow the training set as you play |
| `web_ui.py` | 980 | HTTP server, board diagram, MJPEG feed, board editor, `EngineController` |
| `robot_moves.py` | 364 | **Pure** path planning: (position, move) → gantry steps. No hardware, no threads |
| `robot.py` | 440 | `GantryLink` / `MockGantry` / `Robot` / `RobotController`, plus a bench console |
| `firmware/chess_gantry.ino` | 354 | CoreXY + magnet executor. Knows no chess |

Tooling: `collect_square_crops.py` (gather training photos from your own
rig), `debug_classifier.py` (per-square confidence, live motion scores),
`main.py` (headless tracker).

### Two rules that shape everything

**1. Vision answers three-way, never piece type.** Justified in §1. It's why
`move_resolver.resolve_from_deltas` matches on occupancy/colour deltas rather
than full board states, and why promotion always resolves to a queen (colour
alone can't reveal the promoted type).

**2. Never guess.** There is no automatic rescan fallback anywhere. A
low-confidence square returns `UNRESOLVED`; a delta matching zero or multiple
legal moves leaves state untouched and flags. Recovery is always a human —
**Undo last move** or **Edit board** in the web UI. The reasoning: a wrong
move silently applied corrupts every subsequent position, while a flag costs
ten seconds.

---

## 5. How a human move is read

`TrackingLoop.tick()` runs every `--poll-interval` (default 0.12 s). Most
calls do nothing but a cheap grayscale diff.

1. **Gate.** `BoardMotionGate` watches the board ROI. A move is a discrete
   event — still, hand enters, still again — so it fires `settled` exactly
   once, after motion has been elevated and then quiet. No model runs until
   then.

2. **Classify.** All **64** squares, not a shortlist, through the NCNN
   classifier. Several frames are sampled and must agree
   (`read_settled_state`); a square that can't reach confidence comes back
   `UNRESOLVED`.

3. **Delta.** Every square whose confirmed colour differs from the tracked
   state. `UNRESOLVED` squares are *skipped*, not blocked — they carry no
   information, so the tracker keeps its prior belief. This is safe because a
   move touches ≥ 2 squares: if the moved square is the unresolved one, the
   delta comes out incomplete and matches nothing, so it flags rather than
   resolving wrongly. Only a systematic failure
   (> `MAX_UNRESOLVED_SQUARES` = 8) flags on unresolved reads alone.

4. **Resolve.** `resolve_from_deltas` computes each legal move's expected
   delta by diffing python-chess's own `piece_map()` before and after a
   scratch `push()`. That gets captures, castling (4 squares) and en passant
   (a square that is neither `from` nor `to`) exactly right *for free*,
   because `push()` already implements chess's side effects. A unique match
   is played for real and yields SAN (`Nf3`); zero or multiple matches flag.

5. **Or flag.** State untouched, reason named ("no unique legal move explains
   the change on d4, e5"), UI offers correction.

Guards: `MAX_PLAUSIBLE_SQUARES = 6` (more squares than any legal move
touches → flag), and `PAUSE_LAPSE_S = 10 s` (a pause held by the browser's
board editor lapses on its own, so a closed tab can't strand tracking).

---

## 6. How a machine move is played

### The split

The Arduino knows **no chess at all**. It takes `GOTO` / `MAG` / `PULSE` /
`TOPPLE` and nothing else. Every decision about which squares to visit —
captures, castling, en passant, knight routing, promotion — is made in
`robot_moves.py`, from python-chess, where it can be unit-tested. This is the
single most important structural decision in the arm: rules on a
microcontroller are rules you cannot test.

### Serial protocol

Line-based ASCII, 115200 8N1, `\n` terminated. Every command **blocks until
the action finishes**, then acks — so the Pi never has to guess when motion
completed.

| Pi → Uno | Uno → Pi | Meaning |
|---|---|---|
| *(on boot)* | `READY` | firmware alive, not yet homed |
| `PING` | `OK PONG` | liveness |
| `HOME` | `OK` | seek both limits, zero the motors |
| `GOTO <x> <y>` | `OK` | float square coords, `0 0` = a1's centre |
| `MAG <0-255>` | `OK` | attract, clamped to `MAG_MAX_PWM` |
| `PULSE` | `OK` | repel-then-attract kick; recentres a piece |
| `TOPPLE` | `OK` | repel kick; knocks a piece over |
| `OFF` | `OK` | coil off, drivers disabled |
| `STATUS` | `OK <x> <y> <homed>` | |
| `!` *(any time)* | `ERR ABORT` | soft e-stop, valid mid-move |

Anything unparseable, out of range, or attempted before homing returns
`ERR <reason>`, which halts the robot. Blocking moves run as
`while (motors.run())` polling for `!`, **not** `runSpeedToPosition()` —
otherwise the e-stop would be useless mid-travel, which is exactly when you
need it.

The Uno resets when the Pi opens the port, so the firmware prints `READY` on
boot and `GantryLink` waits for it. Timeouts: 8 s for `READY`, 40 s per
command (homing crosses the board twice; a topple wait holds the line while a
human reaches in).

### CoreXY

```cpp
positions[0] = STEPS_PER_SQUARE * (x - y);   // motors together = X
positions[1] = STEPS_PER_SQUARE * (x + y);   // motors opposed  = Y
```

Homing follows from it: neither switch belongs to one motor, so X and Y are
homed one at a time — both motors the same direction, then opposed. Each seek
hits the switch at speed, backs off 200 steps (10 mm), and re-approaches
slowly, because a switch's trip point varies with how hard it's struck.

### Path planning

`plan(board, move)` returns an ordered list of `Step`s — a firmware command,
a wait, or a human prompt. It **raises** if the move isn't legal in the given
position: python-chess answers `is_capture()` for the side to move, so a move
belonging to the other side would plan a topple on the mover's own departure
square. For a machine that throws physical objects, refusing beats guessing.

**Straight moves slide centre to centre.** Chess legality already guarantees
a sliding piece has a clear path, so this covers most moves and is the fast
case.

**Everything else rides the lattice** — knights always, and the castling rook,
which has to get past the king that just jumped over it. Step off centre,
travel through the gap between squares at reduced magnet power, drop into the
destination centre.

**And the lattice line is not the midline.** This is the 30 mm-square
adaptation. `_flanking()` finds the two lines of squares a traversal passes
between; `_bias()` shifts the line `LATTICE_BIAS = 0.2` squares toward
whichever it can prove is empty **and on the board**:

| | traversal leg | move's worst point |
|---|---|---|
| Both flanks occupied (no bias possible) | 15.0 mm | 15.0 mm |
| One flank empty (biased) | **21.0 mm** | **17.4 mm** |

The biased move's worst point is 17.4 mm rather than 21 mm because once the
long traverse moves out, the short diagonal onto the lattice corner becomes
the tightest part of the path. Smaller than the traversal figure alone
suggests — but real, and it applies to **71 %** of the routed moves that
actually have a piece in the way (measured, §10). The other 29 % have pieces
on both sides and must split the gap.

An off-board flank is empty in a useless sense (the carriage can't go there,
and the magnet would overhang the PCB), so it is never a bias target. That
also keeps every waypoint within ±0.5 squares of the board, which is what
makes the firmware's `-0.6 … 7.6` range guard sufficient.

**Special moves**, all derived from python-chess rather than hand-written:

- **Capture** — `GOTO` the victim, `TOPPLE`, retreat to the park corner,
  wait `--topple-delay` (default 5 s) for you to lift it off, *then* play the
  move. The arm stands clear rather than hovering under the piece you're
  reaching for.
- **En passant** — topples the pawn *beside* the destination, not on it.
- **Castling** — two carries, king first; the rook is then edge-routed, which
  the router works out on its own from the post-king-move occupancy.
- **Promotion** — moves the pawn, then prompts you to swap in a queen. The
  arm can't fetch one and vision can't tell a queen from a pawn anyway;
  tracked state already records the promotion, so the board just has to be
  made to match. The prompt is sticky — it survives the end-of-move status
  clear and is only dropped when the next move starts.

Every plan ends `MAG 0`, `GOTO 0 0` — released and parked, so a resting
magnet never tugs at the piece above it.

---

## 7. Closed-loop verification

The arm's move is not trusted because the gantry said `OK`. It counts only
when the camera agrees.

```
engine picks move
  └─ loop.set_expected_move(move)      arm verification BEFORE the arm moves
  └─ RobotController.execute():
       set_paused(True) + keep-alive thread   tracker held still
       robot.play(board, move)                blocking, ~10-25 s
       set_paused(False)
       loop.force_settle()                    read the board NOW
          └─ resolve_from_deltas(only_move=expected)
                unique match → commit, SAN appears in the UI
                anything else → flag → halt the arm
```

Three details here are load-bearing:

**`set_expected_move` first.** It already existed for hand-played engine
moves — while one is pending it's the *only* move the tracker will accept.
The arm reuses it unchanged, so a slipped belt is rejected by the same code
path that rejects you putting the wrong piece down.

**The pause keep-alive.** `PAUSE_LAPSE_S` is 10 s, and a gantry move plus a
topple delay outlasts that. A helper thread refreshes the pause every ~3 s
while the arm moves — refreshing rather than taking one long pause, so the
existing safety property survives: if the robot thread dies, tracking
resumes instead of hanging silently forever.

**`force_settle()`, and why it had to be added.** `tick()` deliberately keeps
pumping the motion gate while paused, so the gate's reference frame stays
current. That means the gantry's own motion-then-quiet is consumed *during*
the pause and dropped. Unpausing afterwards leaves a board that is already
static — the gate would never fire again, and the robot's move would never be
verified. `force_settle()` runs the settle read directly, removing the race.

### Failure handling

| Failure | Response |
|---|---|
| Camera disagrees with the arm's move | Flag, **halt the arm**, wait for a human |
| Board didn't change at all (dropped piece, weak magnet) | Same — the arm moved and nothing followed it |
| `ERR` or timeout from the firmware mid-sequence | Send `OFF` (drop the coil *first* — a halted arm still gripping drags the piece if nudged), halt, surface the message |
| Arduino resets mid-session | `READY` mid-stream is detected: "position lost", halt |
| Abort (`!` / HALT button) | Motion stops, coil drops, `homed` cleared |

A halt clears `homed`, because an aborted move leaves the carriage nowhere
known. Recovery is **Edit board** / **Undo last move** to fix the position,
then **Home / re-enable** — which re-homes first, since re-establishing a
trustworthy position is the entire point.

---

## 8. Web UI

`python3 src/web_ui.py [--robot PORT|mock]` — served on port 8000.

| Endpoint | Method | Purpose |
|---|---|---|
| `/` | GET | The page |
| `/board.json` | GET | Matrix, last move, flag, turn, engine + robot state |
| `/stream.mjpg` | GET | Live camera feed |
| `/board/correct` | POST | Manual board correction (validated as a legal position) |
| `/board/undo` | POST | Revert one move exactly, restoring castling/en-passant rights |
| `/board/pause` | POST | Hold tracking while editing |
| `/engine` | POST | Enable, skill 0–20 |
| `/robot` | POST | `{home:true}` or `{halt:true}` |

The board diagram highlights the engine's pending from/to squares. The robot
box shows state (ready / moving / not homed / **HALTED**), the current step
("ease knight off b8"), any prompt ("Remove the toppled pawn on d5"), and
carries the HALT button.

`?editing=1` on the poll refreshes the server-side pause while the editor is
open — close the tab and it lapses, rather than leaving tracking dead with no
indication anywhere.

Notable flags: `--robot`, `--topple-delay`, `--harvest`, `--min-conf`,
`--motion-thresh`, `--engine-skill`, `--engine-think`, `--poll-interval`.

---

## 9. Decisions, and why

| Decision | Reasoning |
|---|---|
| Vision reads occupancy/colour only | Piece type comes from the tracker's own maintained state. Easier model, less data, robust to lighting |
| No automatic rescan fallback | A wrong move poisons every later position. Flagging costs seconds; guessing costs the game |
| Arduino is a dumb executor | Rules on a microcontroller are rules you can't test. All chess logic stays in python-chess |
| CoreXY | Both motors fixed to the frame; the 45° transform is three lines of firmware |
| Blocking, acked serial protocol | The Pi never has to infer when motion finished |
| Captures topple in place | Gantry travel is exactly the board — there's no room for a graveyard |
| Fixed topple delay, not vision-gated | Simpler; if it elapses and the piece is still there, the incoming move disturbs it and the settle flags anyway |
| Camera verifies the arm's own moves | Reuses `set_expected_move` unchanged. A mechanical slip becomes a flag instead of silent corruption |
| Halt on desync, don't retry | Never stack a second move on a position that isn't real |
| Robot off unless `--robot` | Nothing about the hand-played setup changes by default |
| Promotion prompts a human | The arm can't fetch a queen, and vision can't see one |
| 1/4 microstepping | Step rate is the limit on 30 mm squares, not precision. Doubles speed for resolution you can't use |
| Bias routing toward empty flanks | The only lever available against 15 mm midline clearance |

---

## 10. Testing

**92 tests, no camera, no gantry, no trained model, no Stockfish.**

```bash
python3 -m unittest discover -s tests
```

| File | Tests | Covers |
|---|---|---|
| `test_robot_moves.py` | 32 | Path planning, routing clearance, special moves |
| `test_robot.py` | 14 | Plan execution, halt semantics, the closed loop |
| `test_move_resolver.py` | 13 | Delta matching, special moves, resync |
| `test_tracking_loop.py` | 12 | Delta computation, flagging, pause, undo |
| `test_engine.py` | 8 | `describe_move` for every special case |
| `test_square_classifier.py` | 7 | Consensus, confidence, `UNRESOLVED` |
| `test_harvest.py` | 6 | Crop labelling and session naming |

Fakes throughout: `MockGantry` acks commands and moves nothing;
`read_settled_state` is monkeypatched with a synthetic 64-square consensus;
`_AngryGantry` fails on the *n*th command to prove mid-sequence failures drop
the coil and halt.

**The clearance invariant** is the most valuable test in the suite
(`TestClearanceInvariant`): while carrying a piece, the arm never comes
closer than half a square (15 mm) to any piece it isn't moving — asserted
over whole random games rather than hand-picked positions, because the cases
that would violate it are crowded middlegames nobody thinks to write down.
`closest_approach()` reconstructs the carried polyline from the plan and
measures point-to-segment distance to every occupied square, counting only
legs where the coil is energised.

Measured over a 25-game sweep (4,202 moves), closest approach while carrying:

| Closest approach | Share | What it is |
|---|---|---|
| **15.0 mm** | 2.5 % | Both flanks occupied — the floor, no bias possible |
| 17.4 mm | 4.2 % | Biased; the lattice corner is now binding |
| 21.0 mm | 1.8 % | Biased; the traversal itself is binding |
| 21.3 mm | 28.2 % | Routed, but both flanks empty — nothing to dodge |
| 30 mm+ | 63.3 % | Direct centre-to-centre slides |
| **Below 15 mm** | **0** | never, in any run |

Of the 15 % of moves that are lattice-routed, 353 had a flanking piece
actually in the way. **249 of those (71 %) were biased away from it**; the
remaining 104 (29 %) had pieces on both sides and split the 15 mm gap.

A separate 200-game sweep drives ~34,000 moves through the real planner and
`Robot` against `MockGantry`, asserting every plan ends released and parked
and every waypoint sits inside the firmware's range guard.

---

## 11. Bring-up

Full detail in [HARDWARE.md](HARDWARE.md#bring-up-and-calibration). The
order matters:

1. **Link** — `PING` → `OK PONG`.
2. **Magnet** — `MAG 170` holds a piece; check the coil isn't hot after 30 s.
   Tune `PULSE_*_MS` and `TOPPLE_MS`.
3. **Homing** — both switches found, backed off, re-approached. An axis
   running *away* from its switch is fixed by swapping one coil pair on that
   motor, not in code.
4. **Verify geometry** — `GOTO 7 0` must travel **exactly 210 mm**.
5. **Home offset** — `GOTO 0 0` must sit on a1's printed dot.
6. **Speed** — raise `MAX_SPEED` only if the motors start cleanly from rest.
7. **Clearance — before any game.** Drive a knight out of the opening
   position by hand and watch the pawns either side. Nothing may be nudged or
   dragged. This is the test this board's geometry demands.

Then `--robot mock` for a full silent game, then the real thing.

The bench console exists for exactly this:

```bash
python3 src/robot.py --port /dev/ttyACM0 --console
```

---

## 12. Known limitations and open risks

- **Clearance is the real limitation.** 15 mm midline against 13–15 mm bases
  and a 25 mm magnet. Routing helps where a flank is empty; in the opening
  the rank-7 pawn wall is solid and 15 mm is the geometric maximum. If it
  catches, the remedies are mechanical — weaker `MAG_EDGE`, narrower bases,
  or a smaller coil — not more code.
- **The corner approach runs at full `MAG_HOLD`** while the traversal runs at
  reduced `MAG_EDGE`, so the strongest field coincides with the now-tightest
  point of a biased route. Moving the power reduction earlier would help but
  risks losing grip on pickup — untested physics, left as the reference
  sketch had it, with `MAG_EDGE` documented as the knob.
- **No vision-based setup verification.** The tracker *assumes* the board
  starts at the standard position rather than confirming it. There's no "New
  Game" control either, though `TrackingLoop.reset()` does everything needed.
- **Captures need a human** within `--topple-delay`.
- **Changing the board or the lighting means retraining** the classifier —
  it sees the board surface as background. Recalibrate, collect a session,
  fine-tune from existing weights.
- **The classifier needs real training data from this rig.** No pretrained
  model ships. Consensus and delta logic are tested with fakes, but accuracy
  can only be judged after training on real photos.
- **`MultiStepper` does not accelerate**, so `MAX_SPEED` is also the start
  speed. Too high and it silently loses steps rather than failing loudly.
- Not built: puzzle mode, coach, match analysis, remote play.

---

## 13. File map

```
config/                  calibration.json (git-ignored, from calibrate.py)
docs/
  DESIGN.md              this document
  HARDWARE.md            circuit, wiring tables, bring-up, calibration
firmware/
  chess_gantry/          the Arduino Uno sketch — CoreXY + magnet, no chess
models/                  exported NCNN classifier (git-ignored)
src/                     capture, calibration, classification, tracking,
                         resolution, engine, web UI, robot
tests/                   92 unit tests — no hardware required
training/                dataset collection, training, export (mostly off-Pi)
README.md                overview and workflow
RUNBOOK.md               copy-pasteable day-to-day command sequences
```
