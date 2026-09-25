# Robot arm hardware

The gantry that plays Black's moves: a CoreXY frame under the board, an
electromagnet on the carriage, an Arduino Uno driving it, and the Pi telling
the Uno where to go.

Firmware: [`firmware/chess_gantry/chess_gantry.ino`](../firmware/chess_gantry/chess_gantry.ino).
Pi side: [`src/robot.py`](../src/robot.py) and [`src/robot_moves.py`](../src/robot_moves.py).

## Board geometry

This is the **V2** board. V1 was 230 × 230 mm on 28.75 mm squares, on a
295 × 300 mm PCB; git history has those numbers if you are driving the old
hardware.

| | |
|---|---|
| Square | **50 mm** |
| Playing area | **400 × 400 mm** (8 × 50) |
| Gantry travel | **480 × 470 mm**, measured — room for a graveyard ring one square past every board edge |
| Panel | **570 × 570 mm**, 7.5 mm corner radius, M3 holes on a 7.5 mm inset |
| a1 → h8 centres | 350 mm on each axis |
| Captured pieces | 50 mm strip on **all four sides**, 8 slots each — 32 shared, corners left empty. The arm parks them itself (`BURY`, firmware r4) |

The panel is the playing surface, and unlike V1 it is **not** a PCB — 570 mm
is past most fabs' limits and the board carries no copper. It is laser-cut
5 mm acrylic or 6 mm MDF. The printed sticker
(`design/chessboard_400mm_sq50mm.pdf`, 570 × 570 mm) goes on top at 1:1 and
carries the grid, with a dot at each square's centre and at each graveyard
slot. Those dots are the positions the gantry's `GOTO` coordinates address,
and they're what to click when running `src/calibrate.py`.

Both are generated, not drawn by hand:

```bash
python3 tools/make_board.py     # design/ChessBot_V2_board.{dxf,ai,svg,step}
python3 tools/make_sticker.py   # design/chessboard_400mm_sq50mm.pdf
```

| File | For |
|---|---|
| `.dxf` | the laser shop — `CUT` / `HOLES` / `GUIDE` layers |
| `.ai` | Illustrator, every path editable |
| `.svg` | a quick look in a browser |
| `.step` | 3D solid, to drop into the CAD assembly next to `ChessBot V1.step` |

The STEP model is **5 mm thick** by default (cast acrylic); pass
`--thickness 6` for MDF. It carries only the cut geometry — outline and
holes. The grid and graveyard lines are sticker registration marks, not
features of the part, so they are deliberately absent from the solid.

`tools/read_board_ai.py` reads the original V1 Illustrator file, which is
where the hole pattern and corner radius came from.

## Bill of materials

| Part | Spec | Qty |
|---|---|---|
| Arduino Uno | ATmega328P, USB-B | 1 |
| Stepper driver | TMC2208 (or A4988-footprint equivalent) | 2 |
| Stepper motor | NEMA 17, 22.5 mm body | 2 |
| Motor driver | DRV8872 (H-bridge, for the magnet) | 1 |
| Electromagnet | 5 V, 50 N, 25 mm dia, 20 mm high | 1 |
| Momentary switch | 6×6 mm | 2 |
| Power supply | 7.5 V, 36 W, 5.5×2.1 mm barrel | 1 |
| Barrel jack extension | 5.5×2.1 mm | 1 |
| Electrolytic capacitor | 100 µF / 25 V | 2 |

### Mechanical — V2 (what changes when scaling up)

The electronics above are unchanged from V1. The frame is not:

| Part | V1 | **V2** | Qty |
|---|---|---|---|
| Linear rod, Ø8 mm hardened | 250 mm | **545 mm** | 2 |
| Linear rod, Ø8 mm hardened | 265 mm | **580 mm** | 2 |
| Panel | 295 × 300 mm PCB | **570 × 570 mm**, 5 mm acrylic / 6 mm MDF | 1 |
| GT2 6 mm open belt | — | **2.5 m** | 2 |
| LM8UU bearings, pulleys, steppers | unchanged | unchanged | — |

Rod lengths are the V1 lengths scaled by the travel ratio (500 / 230 ≈ 2.174)
and rounded up. Buy a 600 × 600 mm sheet and cut the panel from it — that is a
standard stock size and 570 is not.

**Rod sag is the accepted risk at this size.** An unsupported Ø8 mm rod over a
~500 mm span deflects under the carriage in a way it did not over 250 mm. If
the carriage binds, or the magnet's grip varies noticeably between the middle
of the board and the edges, the fix is Ø10/Ø12 mm rod or SBR12 supported rail
— both of which change the bearing blocks and therefore the panel's hole
pattern, so it is a re-cut, not a swap.

## Two power domains

There are exactly two supplies, and they meet at one place only — the ground
rail. The Pi powers the Uno over USB; the 7.5 V supply powers the motors and
the magnet.

```
  ┌──────────────────┐         USB-A → USB-B          ┌──────────────────┐
  │  Raspberry Pi 5  │ ═══════════════════════════════│   Arduino UNO    │
  │  (own 27W PSU)   │   /dev/ttyACM0, 115200 8N1     │  powered by USB  │
  │                  │   + 5V @ ~50mA for the Uno     │  ONLY — leave    │
  │  IMX219 camera   │                                │  the barrel jack │
  │  on CSI ribbon   │                                │  EMPTY           │
  └──────────────────┘                                └──────────────────┘

  ┌────────────────────┐   ┌───────────────────────┐   ┌──────────────────┐
  │ 7.5V / 36W / 4.8A  │──▶│ barrel jack extension │──▶│ screw terminal / │
  │ PSU (5.5×2.1mm)    │   │ 5.5×2.1mm             │   │ distribution pair│
  └────────────────────┘   └───────────────────────┘   │  +7.5V  ──┐      │
                                                       │  GND    ──┼──┐   │
                                                       └───────────┼──┼───┘
                                                                   │  │
                    ┌──────────────────────────────────────────────┘  │
                    │                                                 │
                    ├──▶ TMC2208 #1  VM                               │
                    ├──▶ TMC2208 #2  VM                               │
                    ├──▶ DRV8872     VM                               │
                    │                                                 │
                    │   100µF/25V across VM–GND at EACH driver        │
                    │                                                 │
   GND rail ────────┴──── TMC#1 GND ── TMC#2 GND ── DRV8872 GND ──────┘
                     └─── Arduino UNO GND     ◀── the one required tie
```

## Signal wiring

### Uno → TMC2208 #1 (motor M1)

| UNO | TMC2208 #1 | |
|---|---|---|
| D2 | STEP | one pulse = one microstep |
| D4 | DIR | |
| D12 | EN | active LOW, shared with #2 |
| 5V | VIO | logic supply, ~10 mA |
| GND | GND (logic) | |
| — | MS1 → GND | } MS1=0, MS2=1 = **1/4 microstepping** |
| — | MS2 → **5 V** | } (see "Why 1/4" below) |
| — | PDN_UART | leave unconnected (standalone mode) |
| — | VM | +7.5 V rail, with the 100 µF |
| — | GND (power) | GND rail |
| — | 1A 1B 2A 2B | → NEMA 17 #1 coils |

### Uno → TMC2208 #2 (motor M2)

Identical, except:

| UNO | TMC2208 #2 |
|---|---|
| D7 | STEP |
| D8 | DIR |
| D12 | EN (same wire as #1) |

Pin *names* are silkscreened on the module; the physical order differs
slightly between clones, so wire by label, not by position.

### NEMA 17 → driver coils

Identify the two coils with a multimeter before plugging anything in: ~2–4 Ω
between wires of the same coil, open circuit between coils. One coil →
`1A`/`1B`, the other → `2A`/`2B`.

Swapping the two wires *within* one coil reverses that motor's direction.
That's the fix if an axis homes the wrong way — no code change needed.

### Uno → DRV8872 → electromagnet

| UNO | DRV8872 | |
|---|---|---|
| D9 | IN1 | Timer1 PWM |
| D10 | IN2 | Timer1 PWM |
| GND | GND | |
| — | VM | +7.5 V rail, with the 100 µF |
| — | OUT1, OUT2 | → the two electromagnet leads |
| — | FAULT | optional, open-drain; leave floating |

The H-bridge is what gives both polarities, and both are needed:

| IN1 | IN2 | Result | Firmware command |
|---|---|---|---|
| PWM | LOW | attract | `MAG <duty>` — dragging a piece |
| LOW | PWM | repel | `PULSE` (recentre), `TOPPLE` (knock over) |
| LOW | LOW | coast, coil off | `MAG 0` — resting |

### Limit switches

No resistors needed — `INPUT_PULLUP` does it. Reads LOW when pressed.

```
   UNO A0 ──────┬── [X limit switch] ── GND     (at the a-file end)
                └── internal pull-up

   UNO A1 ──────┬── [Y limit switch] ── GND     (at the rank-1 end)
                └── internal pull-up
```

On CoreXY neither switch belongs to one motor: homing X drives both motors
the same direction, homing Y drives them opposite, so the axes are homed one
at a time.

### Pins left free

D0/D1 (USB serial — never use them), D3, D5, D6, D11, D13, A2–A5. D9/D10 are
Timer1, which `AccelStepper` never touches, so magnet PWM and stepping don't
interfere.

## Why 1/4 microstepping, and where 1000 steps/square comes from

```
NEMA 17, 1.8°           200 full steps / rev
1/4 microstepping       200 × 4        =  800 steps / rev
20-tooth GT2 pulley     20 × 2 mm      =   40 mm / rev
                        800 / 40 mm    =   20 steps / mm
50 mm square            20 × 50        = 1000 steps / square
```

`STEPS_PER_SQUARE = 1000` is therefore **derived, not measured** — the
bring-up step below verifies it rather than tuning it.

1/4 rather than 1/8 because the Uno's step rate is the limit, not precision —
and more so on V2, where every move is 1.74× longer in millimetres. 1/4 still
resolves 0.05 mm, which is 1000× finer than
anything that matters here, and it halves the pulses per millimetre:

| | steps/square | speed at 1500 steps/s | board traverse (350 mm) |
|---|---|---|---|
| 1/8 | 2000 | 37.5 mm/s | ~9.3 s |
| **1/4** | **1000** | **75 mm/s** | **~4.7 s** |

The TMC2208 interpolates internally to 256 microsteps regardless, so coarser
external stepping costs nothing in smoothness or noise.

## Clearances: no longer the thing to watch

This was V1's main hazard and it is the single biggest thing V2 bought. On
28.75 mm squares the midline of the gap between two pieces sat 14.4 mm from
each — *inside* the 13–15 mm bases — and the magnet's pole face came within
about 2 mm of a flanking piece's centre. On 50 mm squares:

| | V1 (28.75 mm) | **V2 (50 mm)** |
|---|---|---|
| Gap midline → each flanking piece's centre | 14.4 mm | **25.0 mm** |
| Piece base diameter | 13–15 mm | 13–15 mm |
| **Clearance when squeezing between two pieces** | **0–1.9 mm** | **≈10 mm** |
| Magnet radius (25 mm coil) | 12.5 mm | 12.5 mm |
| Magnet edge → flanking piece's centre | 1.9 mm | **12.5 mm** |

`src/robot_moves.py` still shifts the routing line toward whichever flank it
can prove is empty (`LATTICE_BIAS`), taking the traversal from 25 mm to
35 mm. That is now margin on top of margin rather than the thing keeping
pieces upright, and it is kept because it costs nothing and would matter
again if the pieces or the coil changed.

A unit-test invariant asserts the arm never routes closer than **half a
square** to a piece it isn't carrying. It is expressed in squares, not
millimetres, so it survives a rescale.

**If pieces get caught or dragged on the rig, in order:** lower `MAG_EDGE` in
`src/robot_moves.py`; then check the bases really are ≤ 15 mm. The "fit a
smaller coil" advice was specific to the 28.75 mm board and no longer
applies — if anything, a 50 mm square wants a *stronger* grab, because a
piece can now sit further from the pole face. See Risks in the V2 notes.

## Four things that will bite otherwise

- **The 5 V magnet is on a 7.5 V rail.** At full duty the coil cooks. The
  firmware clamps every magnet command to `MAG_MAX_PWM = 170` (≈66% → ~5 V
  average). Don't raise it without putting a 5 V buck in front of the
  DRV8872. Also check whether your DRV8872 breakout has a fixed `RILIM`
  resistor or a pad to populate — size it for ~1 A per the datasheet if it's
  yours to choose.
- **The 100 µF across each driver's VM is not optional.** The TMC2208
  datasheet requires it; without it, plugging in the supply can destroy the
  driver.
- **Never unplug a stepper while the TMC2208 is powered** — same outcome.
- **The Uno resets when the Pi opens the serial port.** The firmware prints
  `READY` on boot and `GantryLink` waits for it, then homes. That's why the
  first command of every session is `HOME`.

## Power-on order

1. USB from the Pi → Uno. The firmware boots, prints `READY`, and leaves the
   drivers disabled (EN idles HIGH), so nothing can move yet.
2. Set each TMC2208's Vref for your motors (~0.6–0.9 A) **before** the first
   move — probe the trimmer with the motor unplugged.
3. Then the 7.5 V barrel jack.
4. Shut down in reverse: 7.5 V off first, USB last.

Use 1/4 microstepping (MS1 → GND, MS2 → 5 V). Finer stepping just doubles the
pulse rate for resolution you can't use, and an Uno starts losing steps past
~4 kHz with two motors running.

## Bring-up and calibration

Flash `firmware/chess_gantry/chess_gantry.ino` (Arduino IDE, board "Arduino
Uno", library **AccelStepper** installed), then drive it by hand:

```bash
python3 src/robot.py --port /dev/ttyACM0 --console
```

**1. Link.** `PING` should answer `OK PONG`. If it hangs, the port is wrong
(`ls /dev/ttyACM* /dev/ttyUSB*`) or the sketch isn't flashed.

**2. Magnet.** `MAG 170` — it should hold a piece firmly. Leave it on for 30 s
and check the coil isn't getting hot. `MAG 0` releases. `PULSE` should nudge
a piece to centre, not fling it; `TOPPLE` should knock one over without
launching it. Tune `PULSE_*_MS` / `TOPPLE_MS` in the sketch.

**3. Homing.** `HOME` — the carriage should find both switches, back off, and
re-approach slowly. If an axis runs *away* from its switch, swap one coil
pair on that motor (see above).

**4. Verify steps per square.** `STEPS_PER_SQUARE = 600` is arithmetic, not a
guess, so this checks the hardware matches rather than tuning the number:

```
gantry> HOME
gantry> GOTO 7 0        # must travel exactly 350 mm (7 × 50) along a→h
gantry> GOTO 7 7        # 350 mm on the other axis too, both motors together
```

Measure with a ruler. If it isn't 350 mm, **fix the hardware, don't fudge the
constant** — the pulley isn't 20-tooth, or the MS1/MS2 jumpers aren't set for
1/4 microstepping. A wrong `STEPS_PER_SQUARE` compounds: the error grows with
every square travelled, so the arm drifts further off with each move.

**5. Home offset.** If `GOTO 0 0` doesn't sit under a1's centre (the printed
dot), set `HOME_OFFSET_X` / `HOME_OFFSET_Y` in squares — so 6 mm off is
`0.2` — and re-flash.

**6. Speed.** `MAX_SPEED` is 1500 steps/s = 75 mm/s. `MultiStepper` does not
accelerate, so this is also the *start* speed — if the motors buzz and stall
instead of turning, it's too high for the load. There's headroom to ~2500
(125 mm/s) once the belts are tensioned.

**7. Clearance — do this before any game.** The one test this board's
geometry demands. Set up the standard opening position and, from the console,
drive a knight out by hand:

```
gantry> GOTO 1 7        # b8
gantry> MAG 170
gantry> GOTO 1.5 6.5
gantry> MAG 110         # MAG_EDGE
gantry> GOTO 1.5 5.5    # the squeeze: 25 mm from b7 and c7
gantry> MAG 170
gantry> GOTO 2 5        # c6
gantry> PULSE
gantry> MAG 0
```

Watch the pawns on b7 and c7. Neither may be nudged or dragged. If they move,
lower `MAG_EDGE`; if the knight physically scrapes them, the bases are too
wide for 30 mm squares. Repeat on the kingside (`g8 → f6`) — same test,
different pieces.

Then a dry run with no motion at all before letting it near the pieces:

```bash
python3 src/web_ui.py --robot mock       # logs every command, moves nothing
python3 src/web_ui.py --robot /dev/ttyACM0
```

## Emergency stop

- The **HALT** button in the web UI sends `!`, which interrupts motion
  mid-travel and drops the coil.
- Pulling the 7.5 V barrel jack stops the motors and the magnet instantly and
  leaves the Uno alive on USB. Re-home afterwards (**Home / re-enable**) —
  the software will not move again until you do, because the carriage
  position is no longer known.
