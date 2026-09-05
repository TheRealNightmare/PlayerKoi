# Robot arm hardware

The gantry that plays Black's moves: a CoreXY frame under the board, an
electromagnet on the carriage, an Arduino Uno driving it, and the Pi telling
the Uno where to go.

Firmware: [`firmware/chess_gantry/chess_gantry.ino`](../firmware/chess_gantry/chess_gantry.ino).
Pi side: [`src/robot.py`](../src/robot.py) and [`src/robot_moves.py`](../src/robot_moves.py).

## Board geometry

| | |
|---|---|
| Square | **30 mm** |
| Playing area | **240 × 240 mm** (8 × 30) |
| PCB | **280 × 300 mm**, M3 mounting holes at the corners |
| a1 → h8 centres | 210 mm on each axis |

The PCB *is* the playing surface — the printed sticker
(`ChessBot_Sticker_green_grey.pdf`, 295 × 300 mm) is 1:1 and carries the
grid, with a dot at each square's centre. Those dots are the positions the
gantry's `GOTO` coordinates address, and they're what to click when running
`src/calibrate.py`.

Everything downstream falls out of the 30 mm square, including the tight
clearances — see [Clearances](#clearances-the-thing-to-watch) below, which is
the main hazard on a board this small.

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

## Why 1/4 microstepping, and where 600 steps/square comes from

```
NEMA 17, 1.8°           200 full steps / rev
1/4 microstepping       200 × 4        =  800 steps / rev
20-tooth GT2 pulley     20 × 2 mm      =   40 mm / rev
                        800 / 40 mm    =   20 steps / mm
30 mm square            20 × 30        =  600 steps / square
```

`STEPS_PER_SQUARE = 600` is therefore **derived, not measured** — the bring-up
step below verifies it rather than tuning it.

1/4 rather than 1/8 because on a 30 mm square the Uno's step rate is the
limit, not precision. 1/4 still resolves 0.05 mm, which is 600× finer than
anything that matters here, and it halves the pulses per millimetre:

| | steps/square | speed at 1500 steps/s | board traverse |
|---|---|---|---|
| 1/8 | 1200 | 37.5 mm/s | ~6.4 s |
| **1/4** | **600** | **75 mm/s** | **~3.2 s** |

The TMC2208 interpolates internally to 256 microsteps regardless, so coarser
external stepping costs nothing in smoothness or noise.

## Clearances: the thing to watch

30 mm squares are small, and this is where it shows. Half a square — the
midline of the gap between two pieces — is only **15 mm** from each of them.

| | |
|---|---|
| Gap midline → each flanking piece's centre | 15.0 mm |
| Piece base diameter | 13–15 mm |
| **Clearance when squeezing between two pieces** | **0–2 mm** |
| Magnet radius (25 mm coil) | 12.5 mm |
| Magnet edge → flanking piece's centre | 2.5 mm |

`src/robot_moves.py` handles this by shifting the routing line toward
whichever flank it can prove is empty (`LATTICE_BIAS`), which takes the
traversal to 21 mm and the move's worst point to 17.4 mm. That covers about
two moves in three. It cannot help when both flanks are occupied — most
notably a knight leaving the back rank in the opening, where the rank-7 pawn
wall is solid — and there 15 mm is simply the geometric maximum.

A unit-test invariant asserts the arm never routes closer than 15 mm to a
piece it isn't carrying (verified over ~32,000 planned moves; ~2.5% of moves
actually reach that floor).

**If pieces get caught or dragged on the rig, in order:** lower `MAG_EDGE` in
`src/robot_moves.py`; then check the bases really are ≤ 15 mm; then consider
a smaller coil, which is the proper fix for a 30 mm board.

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
gantry> GOTO 7 0        # must travel exactly 210 mm (7 × 30) along a→h
gantry> GOTO 7 7        # 210 mm on the other axis too, both motors together
```

Measure with a ruler. If it isn't 210 mm, **fix the hardware, don't fudge the
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
gantry> GOTO 1.5 5.5    # the squeeze: 15 mm from b7 and c7
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
