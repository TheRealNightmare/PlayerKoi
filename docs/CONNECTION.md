# Connecting and flashing the board

Everything you need at the bench, in the order you need it. For how the
software above the serial port works, see `docs/DESIGN.md`; for why the
machine is built the way it is, `docs/HARDWARE.md`.

## Which sketch to flash

**`firmware/chessbot_v1/chessbot_v1.ino`.**

- Board: **Arduino Uno**
- Baud: **115200**
- Success looks like: `READY ChessBot-V1` printed on the serial monitor

> **Do not flash `firmware/chess_gantry/`.** That sketch is for a different,
> unbuilt machine — TMC2208 drivers, limit switches, a different pin map, and
> twice the steps per millimetre. On this rig it would home into switches that
> aren't fitted and drive every distance 2× too far.

There are two sketches in this repo on purpose. `chessbot_v1` is what runs the
machine. `chess_gantry` is kept for a future rebuild with limit switches, where
the Pi plans the paths instead of the Uno.

## Wiring

Arduino Uno + CNC Shield V3, CoreXY belts, DRV8833 driving the electromagnet.

| Signal | Pin | Notes |
|---|---|---|
| Motor A step / dir | `D2` / `D5` | CNC Shield X |
| Motor B step / dir | `D3` / `D6` | CNC Shield Y |
| Driver enable | `D8` | active **LOW** |
| Magnet `AIN1` (attract) | `D9` | DRV8833 |
| Magnet `AIN2` (repel) | `D10` | DRV8833 |

Three things that are easy to get wrong:

- **The belts are CoreXY, not plain XY.** Neither motor corresponds to an axis.
  The firmware mixes them (`A = X+Y`, `B = X−Y`); if a belt is routed as though
  motor A were the X axis, every move comes out diagonal.
- **Set the microstepping jumpers to 1/2.** A CNC Shield commonly ships at 1/8.
  The firmware assumes 1/2 (10 steps/mm), so 1/8 makes every distance 4× too
  large.
- **The white pieces' magnets are reversed.** They were built the other way
  up, so the coil polarity that grips a black piece pushes a white one away.
  `MOVE` and `KNIGHT` therefore take a trailing `w` or `b` saying which colour
  is being carried, and the firmware flips both the hold and the release kick
  to match (`WHITE_IS_REVERSED` in the sketch). Omitting it means "not
  reversed", so the bench commands in this document still work as written.
  Check a piece by hand with `MAG 1` (attract: holds black) and `MAG 2`
  (repel: holds white).
- **There are no limit switches on this build.** Position is dead reckoning
  from an assumed park at h1. Nothing can detect that it is wrong.
- **"h1" here means the firmware's h1, which is physically the a8 corner.**
  This sketch assumes the board is seated with h1 at the origin; on the built
  rig it is rotated 180 degrees. The Python side corrects for that
  (`rig.ORIGIN_SQUARE`), so *game* moves are rotated before they arrive but
  the bench commands below are not. Everything in this document is in the
  firmware's own naming.

## Bring-up, in order

Do these in sequence. Each one has an expected reply; if you don't get it,
stop there rather than continuing — later steps assume the earlier ones.

| # | Do | Expect |
|---|---|---|
| 1 | Flash `chessbot_v1.ino` | compiles and uploads |
| 2 | Open the serial monitor at 115200 | `READY ChessBot-V1` |
| 3 | **Park the carriage on the origin corner by hand** (physically a8 — see above) | — |
| 4 | `PING` | `OK PONG` |
| 5 | `POS` | `OK POS 0.0 0.0` |
| 6 | `MAG 1` then `MAG 0` | `OK MAG 1` / `OK MAG 0`, coil audibly grabs and releases |
| 7 | `GOTO a1`, then `POS` | `OK POS -210.0 0.0` — **this is the pitch check** |
| 8 | `HOME` | `OK HOME`, carriage returns to h1 |
| 9 | `MOVE e2e4 w\|b` | `OK MOVE e2e4`, a pawn is dragged cleanly |
| 10 | `KNIGHT b1c3 w\|b` | `OK KNIGHT b1c3`, weaves without disturbing the pawns |

Step 7 is the one that matters. If `POS` doesn't read `-210 0`, the 30mm pitch
is wrong and everything downstream — the planner's clearance arithmetic, the
camera's square mapping — is built on a false number. Fix it here.

Then the Python side, on the Pi:

```sh
python3 src/robot.py --port auto --console   # same commands, through the Pi
python3 src/web_ui.py --robot auto           # the full stack
```

`--port auto` detects the Uno. `--robot mock` runs the whole stack with no
hardware at all, logging the commands it would have sent.

## Command reference

115200 baud, newline terminated. One command, exactly one reply line —
`OK ...` or `ERR ...`. **Motion is blocking**, so the reply doubles as "the
move has finished"; the Pi never has to guess.

| Command | Reply | Does |
|---|---|---|
| `PING` | `OK PONG` | liveness check |
| `MOVE e7e5 w\|b` | `OK MOVE e7e5` | straight drag between square centres |
| `KNIGHT b8c6 w\|b` | `OK KNIGHT b8c6` | weaves along the gridlines, for knights and castling rooks |
| `GOTO e4` | `OK GOTO e4` | repositions the carriage, magnet untouched |
| `MAG 0\|1\|2` | `OK MAG n` | coil off / attract / repel |
| `PULSE` | `OK PULSE` | brief reverse kick, clears residual magnetism so the piece lets go |
| `HOME` | `OK HOME` | returns to the origin (h1) and drops the coil |
| `POS` | `OK POS x y` | current position in mm |
| `MM -105 100` | `OK MM x y` | move to raw machine coordinates |
| `JOG -5 0` | `OK JOG x y` | relative nudge, mm |
| `SPEED 25` | `OK SPEED 25` | feed rate, 1–100 mm/s |

`MM` and `JOG` exist in firmware but have no Python wrapper — they are for
manual bench work through the console.

Errors: `ERR bad ply`, `ERR bad square`, `ERR out of range`, `ERR mag 0|1|2`,
`ERR usage MM <x> <y>`, `ERR usage JOG <dx> <dy>`, `ERR speed 1-100`,
`ERR unknown <cmd>`.

## Measured constants

These were measured on the machine. The firmware is the source of truth;
`src/rig.py` mirrors them for the Python side. If the two disagree, the
firmware is right.

| Constant | Value |
|---|---|
| Square pitch | 30.0 mm, both axes |
| Origin `(0, 0)` | centre of **h1** — also the park position |
| a1 | `(-210, 0)` — x runs negative toward the a-file |
| Travel limits | x `-210..0`, y `0..210` |
| Steps/mm | 10 (200 steps/rev × 1/2 microstepping ÷ 40 mm/rev) |
| Feed rate | 40 mm/s |
| Magnet, dragging | PWM 255 |
| Magnet, weaving | PWM 155 |

The measured corners **are** the travel limits — there is no room past a1 or
h8 in any direction. That is why edge weaves fold back inside the board
(`clampWeave` in firmware, `_fold_to_travel` in `src/robot_moves.py`) instead
of stepping half a square out as they would prefer to.

`RunChess/chess.txt` holds an older calibration (25.7mm pitch, a1 at
`-197, 12`) from the 200×200 frame, before belt slip was fixed by re-belting.
It is superseded. Ignore it.

## Troubleshooting

**No `READY` banner.** Wrong port, wrong baud, or the sketch isn't flashed.
Opening the port resets the Uno, so anything sent in the first second or two is
lost in the bootloader — that's what the banner is for. `--port auto` picks the
first port whose description looks like an Arduino; pass the path explicitly if
you have more than one device attached.

**Every distance is 4× too far (or too short).** Microstepping jumpers. The
firmware assumes 1/2; a CNC Shield usually ships at 1/8. Check with step 7
above rather than adjusting `SQUARE_MM` to compensate — fudging the constant
hides the real problem and breaks the camera mapping too.

**Moves come out diagonal.** The belts are routed as plain XY. This is CoreXY;
both motors contribute to both axes.

**A piece drags its neighbour along.** Either the weave duty is too high for
your piece bases, or the move was an edge weave — those fold onto a real square
centre line rather than a gap, so they pass closer than an interior move does.
Knight moves off the back rank in the opening are the tightest case, because
the pawn wall leaves no gap to bias toward. The fixes are mechanical: narrower
piece bases, or a lower `MAG_DIAG`.

**`ERR out of range`.** The carriage's dead-reckoned position has drifted from
reality — a missed step, a belt slip, or the board was powered on with the
carriage somewhere other than h1. There are no limit switches to recover with,
so: power down, park the carriage on h1 by hand, power up, `HOME`.

**The arm won't move and the UI says HALTED.** Something failed mid-sequence
and the position is no longer trustworthy. Check the board physically, then
press Home / re-enable — homing is the recovery path, because re-establishing a
known position is exactly what a halt is waiting for.

**HALT doesn't stop the arm immediately.** Known limitation. This firmware has
no soft e-stop and motion is blocking, so a halt only takes effect once the
current command finishes. The arm will refuse the *next* move. If you need to
stop a move in progress, cut power — or fit a hardware e-stop on the driver
enable line (`D8`).
