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
- **Every piece is held the same way: by attract.** White and black pieces
  have their magnets the same way up (since r7), so no command says which
  colour is being carried. If the magnets turn out to be the other way up,
  switch every piece to repel with `POL 1` (or *Pieces held by* in the web
  UI's Robot arm panel); it is saved in `config/rig.json` and re-sent on
  every connect. Every carry — `MOVE`, `KNIGHT` and `BURY` — is the
  same cycle:
  1. coil **off**, then drive to the source square (nothing is dragged there)
  2. coil **on** (attract, or repel if `POL 1`), wait the grip pause, then carry the piece
     (knights, castling rooks and `BURY` run the gridlines between squares
     at the gridline power — 100% by default, set with `GRID <pct>` or the
     web UI's Gridline power slider)
  3. coil **off**, then wait the settle pause (1200 ms by default) before
     the carriage moves on
  Every leg ramps its speed up from 8 mm/s to the feed rate and back down
  (150 mm/s², `START_MMS` / `ACCEL_MMS2` in the sketch) instead of starting
  and stopping dead, so a carried piece isn't jerked off the magnet.
  The settle pause is what stops a piece being towed: it gives the core's
  leftover magnetism time to die away. If a piece still follows the carriage,
  raise it (`DWELL <grip> <settle>`, or the web UI's Settle slider).
- **There are no limit switches on this build.** Position is dead reckoning
  from an assumed park in the origin corner — the corner of travel beyond h1,
  not h1's centre. Nothing can detect that it is wrong.
- **The firmware's square names are the real squares.** Since r5 the origin is
  the corner beyond h1, where the board is actually seated, so the Python side
  applies no rotation (`rig.ORIGIN_SQUARE = "h1"`): a game move and a bench
  command naming the same square go to the same place. `--board-origin` exists
  for a board seated any other way round.

## Bring-up, in order

Do these in sequence. Each one has an expected reply; if you don't get it,
stop there rather than continuing — later steps assume the earlier ones.

| # | Do | Expect |
|---|---|---|
| 1 | Flash `chessbot_v1.ino` | compiles and uploads |
| 2 | Open the serial monitor at 115200 | `READY ChessBot-V1 r8` — **if the revision is lower, the sketch is stale; re-upload it** |
| 3 | **Park the carriage in the origin corner by hand** — the corner of travel beyond h1 (White's right-hand corner) | — |
| 4 | `PING` | `OK PONG` |
| 5 | `POS` | `OK POS 0.0 0.0` |
| 5b | `DWELL`, then `GRID` | `OK DWELL 150 1200`, `OK GRID 100` — grip/settle pauses and gridline power; the web UI's Robot arm sliders change them |
| 6 | `MAG 1` then `MAG 0` | `OK MAG 1` / `OK MAG 0`, coil audibly grabs and releases |
| 7 | `GOTO a1`, then `POS` | `OK POS -415.0 60.0` — **this is the pitch check** |
| 8 | `HOME` | `OK HOME`, carriage returns to the origin corner |
| 9 | `MOVE e2e4` | `OK MOVE e2e4`, a pawn is dragged cleanly |
| 10 | `KNIGHT b1c3` | `OK KNIGHT b1c3`, weaves without disturbing the pawns |
| 11 | `MM -415 10`, `MM -465 60`, `MM -15 410` | `OK MM …` each time — **the graveyard reach check** |
| 12 | `BURY e2 -365 10` | `OK BURY e2`, the pawn is carried off the board and *set down*, not dropped |

Step 7 is the one that matters. If `POS` doesn't read `-415 60`, the 50mm pitch
is wrong and everything downstream — the planner's clearance arithmetic, the
camera's square mapping — is built on a false number. Fix it here.

Steps 11–12 are new with the V2 frame. Step 11 proves the carriage can reach
past the board edge at all: on the V1 travel limits every one of those three
coordinates was an `ERR out of range`, so it fails loudly if the sketch is
stale in a way the banner alone would not catch. Step 12 then proves a piece
survives the trip — watch the set-down, not the drive. If the piece is towed
as the carriage leaves, the settle pause is too short (`DWELL`); a normal move
sets down the same way, so it is worth fixing here rather than mid-game.

### Before all of that, on a newly built frame: the walker

The sequence above spot-checks a handful of positions by hand. On a frame that
has never moved before — new rods, new belts, a panel that may be a couple of
millimetres out — it is worth proving *every* position first, and doing it
with the magnet dead so nothing can be dragged or dropped while you find out.

Flash [`firmware/chessbot_walker/chessbot_walker.ino`](../firmware/chessbot_walker/chessbot_walker.ino)
and open the serial monitor at 115200. It moves nothing until you press a key.

| Key | Walks |
|---|---|
| `1` | the four board corners — **do this first** |
| `2` | all 64 square centres |
| `3` | all 32 graveyard slots, in ring order |
| `4` | everything |
| `5` | the four measured travel limits, at 5 mm/s — the frame test |
| `g e4` / `g a0` / `g s12` | go to one square or graveyard slot and stay |
| `c` | calibration: nudge onto a dot with `w` `a` `s` `d`, `t` prints the trim |
| `h` | back to the origin corner |
| `p` `q` `+` `-` | pause/resume, abort, faster, slower |

Corners first: four moves catch a wrong pitch or a wrong orientation in about
ten seconds, before you commit to a 96-position pass. Then `3` — the ring is
the new ground, the part of the envelope a V1 machine physically could not
enter, so it is where a short belt or a binding rod shows up. Watch each stop
land on its printed dot.

The coil is never energised: there is no `analogWrite` anywhere in that
sketch, which `tests/test_firmware_geometry.py` asserts. The same test keeps
the walker's copy of the geometry in step with the real firmware's and with
`src/rig.py` — three copies that would otherwise drift apart.

> **Re-flash `chessbot_v1.ino` before connecting the Pi.** A board left with
> the walker on it ignores everything the Pi sends, and the symptom looks like
> a dead serial link rather than the wrong sketch.

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
| `MOVE e7e5` | `OK MOVE e7e5` | straight drag between square centres |
| `KNIGHT b8c6` | `OK KNIGHT b8c6` | weaves along the gridlines, for knights and castling rooks |
| `GOTO e4` | `OK GOTO e4` | repositions the carriage, magnet untouched |
| `BURY e4 -215 10` | `OK BURY e4` | lifts the piece on `e4` and parks it on a graveyard slot. The destination is a **raw machine coordinate**, not a square — the slots sit outside the 8×8 and have no name. Set down the same way a move is. The host picks the slot (`src/graveyard.py`); the firmware only drives to it |
| `MAG 0\|1\|2` | `OK MAG n` | coil off / attract / repel |
| `PULSE` | `OK PULSE` | raw full-power reverse kick, for the bench only. No move uses it |
| `POL` | `OK POL 0` | what holds EVERY piece: 0 = attract (default), 1 = repel |
| `POL 0\|1` | `OK POL <n>` | set it. RAM only — re-sent by the host on every connect |
| `DWELL` | `OK DWELL 150 1200` | grip pause before a carry / settle pause after a set-down, ms |
| `DWELL <g> <s>` | `OK DWELL <g> <s>` | set both, 0–2000 ms each. RAM only — re-sent by the host on every connect |
| `GRID` | `OK GRID 100` | magnet power on every gridline leg (knights, castling rook, `BURY`), % of full |
| `GRID <pct>` | `OK GRID <pct>` | set it, 0–100. RAM only — re-sent by the host on every connect |
| `HOME` | `OK HOME` | returns to the origin corner (beyond h1) and drops the coil |
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
| Square pitch | 50.0 mm, both axes |
| Origin `(0, 0)` | the **corner of travel beyond h1** — also the park position |
| a1 / h1 / h8 | `(-415, 60)` / `(-65, 60)` / `(-65, 410)` — x runs negative toward the a-file |
| Travel limits | x `-480..0`, y `0..470` — measured, 480 × 470 mm |
| Graveyard slots | y `10` / `460` (past rank 1 / 8), x `-465` / `-15` (past the a / h file) |
| Steps/mm | 10 (200 steps/rev × 1/2 microstepping ÷ 40 mm/rev) |
| Feed rate | 40 mm/s |
| Magnet, dragging | PWM 255 |
| Magnet, weaving | PWM 155 |

The graveyard ring sits 15 mm inside the X limits and 10 mm inside the Y
limits, and the board is one square inside the ring. So edge weaves can step
half a square out past the board (x `-440` / `-40`, y `35` / `435`) and
`clampWeave` is a no-op.

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
piece bases, or a lower gridline power (`GRID`).

**`ERR out of range`.** The carriage's dead-reckoned position has drifted from
reality — a missed step, a belt slip, or the board was powered on with the
carriage somewhere other than the origin corner. There are no limit switches to
recover with, so: power down, park the carriage in the origin corner by hand,
power up, `HOME`.

**The arm won't move and the UI says HALTED.** Something failed mid-sequence
and the position is no longer trustworthy. Check the board physically, then
press Home / re-enable — homing is the recovery path, because re-establishing a
known position is exactly what a halt is waiting for.

**HALT doesn't stop the arm immediately.** Known limitation. This firmware has
no soft e-stop and motion is blocking, so a halt only takes effect once the
current command finishes. The arm will refuse the *next* move. If you need to
stop a move in progress, cut power — or fit a hardware e-stop on the driver
enable line (`D8`).
