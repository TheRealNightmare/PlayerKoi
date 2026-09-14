"""Serial link to the gantry, and the thing that walks a plan.

Three layers, deliberately separable so the interesting ones can be tested
and driven without hardware:

    GantryLink      raw serial: send a line, wait for OK/ERR
    MockGantry      the same interface, logging instead of moving
    Robot           walks a robot_moves.plan(), owns halted/homed state
    RobotController holds the tracker still while the arm moves, then makes
                    the camera confirm what it did

The path planning itself lives in robot_moves.py -- no chess rules here.
None of this imports the camera stack, so the whole layer is testable off
the Pi (see tests/test_robot.py).

Run this file directly for a bench console, which is how the rig gets
calibrated:

    python3 src/robot.py --port /dev/ttyACM0 --console
"""

import argparse
import sys
import time
from threading import Event, Lock, Thread

import robot_moves
from tracking_loop import PAUSE_LAPSE_S

# The Uno reboots when the port is opened, so the first read after connect
# is the bootloader delay plus the sketch's banner. Nothing works until it
# arrives, so this is generous rather than tight.
READY_TIMEOUT_S = 8.0

# Homing crosses the whole board twice at homing speed, and a topple wait
# holds the line while a human reaches in.
COMMAND_TIMEOUT_S = 40.0
BAUD = 115200


class GantryError(RuntimeError):
    """The gantry refused a command, went silent, or was aborted. Always
    halts the robot -- the carriage position is no longer trustworthy."""


class GantryLink:
    """Line protocol over USB serial. See firmware/chess_gantry."""

    def __init__(self, port, baud=BAUD, timeout=COMMAND_TIMEOUT_S):
        import serial  # pyserial; imported here so MockGantry works without it

        self._timeout = timeout
        # Read timeout is short and polled, so a long command (homing) can
        # still be given its full budget while an unplugged board fails
        # fast rather than blocking for the whole window.
        self._serial = serial.Serial(port, baud, timeout=0.2)
        self._lock = Lock()
        self.port = port
        self._await_ready()

    def _await_ready(self):
        deadline = time.monotonic() + READY_TIMEOUT_S
        while time.monotonic() < deadline:
            line = self._serial.readline().decode("ascii", "replace").strip()
            # startswith, not ==: chessbot_v1 announces itself as
            # "READY ChessBot-V1" while chess_gantry sends a bare "READY".
            if line.startswith("READY"):
                return
        raise GantryError(
            f"no READY banner from {self.port} within {READY_TIMEOUT_S:.0f}s -- "
            "wrong port, or the sketch isn't flashed?"
        )

    def send(self, command):
        """Sends one command and blocks until the firmware acks it. Returns
        the OK line's payload (empty for a bare OK)."""
        with self._lock:
            self._serial.reset_input_buffer()
            self._serial.write((command + "\n").encode("ascii"))
            self._serial.flush()

            deadline = time.monotonic() + self._timeout
            while time.monotonic() < deadline:
                line = self._serial.readline().decode("ascii", "replace").strip()
                if not line:
                    continue
                if line.startswith("OK"):
                    return line[2:].strip()
                if line.startswith("ERR"):
                    raise GantryError(f"{command} -> {line}")
                # Anything else is a stray banner (the board reset
                # mid-session) -- that invalidates the homing, so say so
                # rather than carrying on with a bogus position.
                if line.startswith("READY"):
                    raise GantryError("the Arduino reset mid-session -- position lost")
        raise GantryError(f"{command} timed out after {self._timeout:.0f}s")

    def abort(self):
        """Soft e-stop. Bypasses the lock on purpose: the point is to
        interrupt a send() that is currently blocked waiting for its ack."""
        try:
            self._serial.write(b"!\n")
            self._serial.flush()
        except Exception:
            pass  # already closed/unplugged; nothing better to do

    def close(self):
        try:
            self._serial.close()
        except Exception:
            pass


class MockGantry:
    """Acks everything, moves nothing. Makes `--robot mock` a full dry run
    of the whole stack -- planner, controller, web UI -- with no hardware."""

    def __init__(self, log=None):
        self.commands = []
        self.port = "mock"
        self._log = log

    def send(self, command):
        self.commands.append(command)
        if self._log:
            self._log(f"[gantry] {command}")
        return "0.00 0.00 1" if command == "STATUS" else ""

    def abort(self):
        self.commands.append("!")

    def close(self):
        pass


class Robot:
    """Plays a chess move on the physical board.

    Owns two pieces of state worth being careful about: `homed` (the
    firmware's idea of where the carriage is, invalidated by any error) and
    `halted` (a human must intervene before anything else moves). Both fail
    closed -- execute() refuses rather than guessing.
    """

    def __init__(self, link, topple_delay_s=robot_moves.DEFAULT_TOPPLE_DELAY_S, on_status=None,
                 planner=robot_moves, on_prompt=None):
        self._link = link
        self._topple_delay_s = topple_delay_s
        self._on_status = on_status or (lambda note, prompt: None)
        # Which planner turns a move into steps. robot_moves emits low-level
        # GOTO waypoints for firmware/chess_gantry; robot_moves_legacy emits
        # MOVE/KNIGHT for firmware/chessbot_v1, which plans its own paths.
        self._planner = planner
        # Called for blocking prompt steps; returns True to continue, False
        # to abandon the move. Default refuses, because a blocking prompt
        # with nobody to answer it would otherwise be silently skipped and
        # the arm would drag a piece onto an occupied square.
        self._on_prompt = on_prompt or (lambda step: False)
        self._lock = Lock()
        self.homed = False
        self.halted = False
        self.message = None
        self.busy = False

    @property
    def port(self):
        return self._link.port

    def set_prompt_callback(self, on_prompt):
        """Who answers a blocking prompt. Set by RobotController, which is
        built after the robot itself."""
        self._on_prompt = on_prompt

    def set_status_callback(self, on_status):
        """Where per-step progress goes. Set by whoever owns the UI, which
        is built after the robot itself."""
        self._on_status = on_status

    def home(self):
        """Homes the gantry and clears a halt. This is also the re-enable
        path after an error, which is why it clears `halted`: the whole
        point of re-homing is to re-establish a trustworthy position."""
        with self._lock:
            self.busy = True
        try:
            self._on_status("homing", None)
            self._link.send("HOME")
        except GantryError as exc:
            self._fail(f"homing failed: {exc}")
            raise
        else:
            with self._lock:
                self.homed = True
                self.halted = False
                self.message = None
        finally:
            with self._lock:
                self.busy = False
            self._on_status(None, None)

    def raw(self, command):
        """Sends a firmware command directly. For the bench console during
        bring-up -- the game path always goes through play()."""
        return self._link.send(command)

    def halt(self, reason):
        """Stops the gantry now and refuses further moves until home()."""
        self._link.abort()
        self._fail(reason)

    def _fail(self, reason):
        with self._lock:
            self.halted = True
            self.homed = False  # an aborted move leaves the carriage nowhere known
            self.message = reason
            self.busy = False

    def play(self, board, move):
        """Executes `move` (legal in `board`, the position before it) on the
        physical board. Blocks until the gantry is parked. Raises
        GantryError -- with the robot halted and the coil dropped -- if
        anything goes wrong mid-sequence."""
        with self._lock:
            if self.halted:
                raise GantryError(f"robot is halted: {self.message}")
            if not self.homed:
                raise GantryError("robot has not been homed")
            self.busy = True

        steps = self._planner.plan(board, move, topple_delay_s=self._topple_delay_s)
        try:
            for step in steps:
                self._on_status(step.note, step.prompt)
                if step.kind == "command":
                    self._link.send(step.command)
                elif step.kind == "wait":
                    time.sleep(step.seconds)
                elif step.kind == "prompt" and step.blocking:
                    # A capture: the victim has to be off the board before
                    # the attacker is dragged in. Nothing moves until a human
                    # says so, and refusing abandons the move rather than
                    # pressing on into an occupied square.
                    if not self._on_prompt(step):
                        raise GantryError(f"cancelled while waiting: {step.prompt}")
                # Non-blocking prompts (promotion) are advisory and just ride
                # along in the status line.
        except GantryError as exc:
            # Drop the coil before anything else: a halted robot still
            # gripping a piece drags it if the carriage is nudged.
            try:
                self._link.send("OFF")
            except GantryError:
                pass
            self._fail(str(exc))
            raise
        finally:
            with self._lock:
                self.busy = False
            self._on_status(None, None)

        return steps

    def close(self):
        try:
            self._link.send("OFF")
        except Exception:
            pass
        self._link.close()


class RobotController:
    """Drives the gantry for the engine's moves, and holds the tracker still
    while it does.

    Three things here are load-bearing:

    * **The pause keep-alive.** TrackingLoop's pause deliberately lapses
      after PAUSE_LAPSE_S so a closed browser tab can't strand tracking
      forever. A gantry move plus a topple delay easily outlasts that, so a
      helper thread refreshes the pause while the arm is moving -- keeping
      the safety property (a crashed robot thread lets tracking resume)
      rather than replacing it with one long unattended pause.

    * **force_settle() afterwards.** The motion gate consumes the gantry's
      own motion while paused, so waiting for it to fire again would wait
      forever. See TrackingLoop.force_settle.

    * **Halting on a flag.** If the camera can't confirm the move the robot
      just made, something physical is wrong (slipped belt, dropped piece,
      a hand in the way). Continuing would stack a second move on top of a
      position that isn't real, so the arm stops until a human re-homes it.
    """

    def __init__(self, robot, loop, buffer=None):
        self._robot = robot
        self._loop = loop
        self._lock = Lock()
        self._note = None
        self._prompt = None
        self._stop_keepalive = Event()
        # Blocking-prompt handshake: _answered is set by confirm() or
        # cancel(), _confirmed carries which one it was.
        self._awaiting = None
        self._answered = Event()
        self._confirmed = False
        robot.set_status_callback(self._set_status)
        robot.set_prompt_callback(self._await_confirmation)

    def _set_status(self, note, prompt):
        """A prompt is sticky -- it survives the rest of the sequence and the
        end-of-move clear, and is only dropped when the next move starts.
        The promotion prompt is the *last* step of its plan, so clearing on
        completion would flash it away before it could be read."""
        with self._lock:
            self._note = note
            if prompt is not None:
                self._prompt = prompt

    def _await_confirmation(self, step):
        """Blocks the robot thread until a human confirms (or cancels) via
        the web UI. Runs on the engine's thread, never the HTTP thread.

        There is deliberately no timeout: a capture prompt that gave up on
        its own would resume with the victim still on the board, which is
        the exact situation the prompt exists to prevent. The keep-alive
        thread holds the tracker paused for as long as this takes.
        """
        with self._lock:
            self._awaiting = step.prompt
        self._answered.clear()
        self._answered.wait()
        with self._lock:
            self._awaiting = None
            return self._confirmed

    def confirm(self):
        """The human says the board is ready. Returns False if nothing was
        waiting, so the UI can tell a stale click from a real one."""
        if self._awaiting is None:
            return False
        self._confirmed = True
        self._answered.set()
        return True

    def cancel(self):
        """Abandon the move instead. The robot halts -- it is mid-sequence
        with the coil possibly live, so a human needs to look at it."""
        if self._awaiting is None:
            return False
        self._confirmed = False
        self._answered.set()
        return True

    def state(self):
        with self._lock:
            return {
                "port": self._robot.port,
                "homed": self._robot.homed,
                "halted": self._robot.halted,
                "busy": self._robot.busy,
                "note": self._note,
                "prompt": self._prompt,
                "awaiting_confirm": self._awaiting,
                "message": self._robot.message,
            }

    @property
    def ready(self):
        return self._robot.homed and not self._robot.halted

    def home(self):
        """Also the recovery path: home() clears the halt, because re-homing
        is what re-establishes a position worth trusting."""
        try:
            self._robot.home()
            return True, None
        except GantryError as exc:
            return False, str(exc)

    def halt(self, reason="halted from the web UI"):
        # Free a blocked prompt first, otherwise a halt requested while the
        # arm waits for a captured piece would leave the robot thread parked
        # on the Event with no way out.
        self._confirmed = False
        self._answered.set()
        self._robot.halt(reason)

    def note_flag(self, reason):
        """Called from on_update when a settle couldn't be resolved."""
        if not self._robot.halted:
            self._robot.halt(f"board didn't match after a robot move: {reason}")

    def execute(self, board, move):
        """Plays `move` physically, then makes the camera confirm it.
        Returns (ok, message). Never raises -- the engine thread has to keep
        running either way."""
        if not self.ready:
            return False, self._robot.message or "robot not homed"

        with self._lock:
            self._prompt = None  # last move's prompt has had its moment

        keepalive = Thread(target=self._hold_pause, daemon=True)
        self._stop_keepalive.clear()
        self._loop.set_paused(True)
        keepalive.start()
        try:
            self._robot.play(board, move)
        except GantryError as exc:
            return False, str(exc)
        except ValueError as exc:  # planner refused the move
            self._robot.halt(str(exc))
            return False, str(exc)
        finally:
            self._stop_keepalive.set()
            keepalive.join(timeout=1.0)
            self._loop.set_paused(False)

        # The gantry says it's parked; now make vision agree. resolve only
        # accepts the expected move, so a piece that ended up on the wrong
        # square flags instead of being adopted as truth. A False here means
        # either a flag (note_flag has already halted us) or a board that
        # didn't change at all -- the arm moved and nothing followed it,
        # which is exactly as wrong.
        if not self._loop.force_settle():
            reason = "the camera couldn't confirm the move"
            if not self._robot.halted:
                self._robot.halt(reason)
            return False, self._robot.message or reason
        return True, None

    def _hold_pause(self):
        # Refresh well inside the lapse window so a slow tick can't let it
        # expire mid-move.
        while not self._stop_keepalive.wait(PAUSE_LAPSE_S / 3.0):
            self._loop.set_paused(True)

    def close(self):
        self._stop_keepalive.set()
        # Release anyone blocked on a prompt, or the robot thread never
        # returns and shutdown hangs. Cancelling is the safe answer: we are
        # shutting down, so the move must not continue.
        self._confirmed = False
        self._answered.set()
        self._robot.close()


def open_gantry(target, topple_delay_s=robot_moves.DEFAULT_TOPPLE_DELAY_S, on_status=None,
                log=print, protocol="legacy"):
    """Builds a Robot from a CLI argument: a serial port path, "auto" to
    detect the Uno, or "mock". Does not home -- the caller decides when the
    board is clear enough for the carriage to move.

    `protocol` selects the firmware being talked to:

        "legacy"  firmware/chessbot_v1 -- what is actually flashed on the
                  machine. High-level MOVE/KNIGHT, paths planned on the Uno.
        "native"  firmware/chess_gantry -- the unbuilt limit-switch rig.
                  Low-level GOTO waypoints, paths planned here.
    """
    if protocol == "legacy":
        import gantry_legacy
        import robot_moves_legacy

        planner, link_factory = robot_moves_legacy, gantry_legacy.LegacyGantryLink
    elif protocol == "native":
        planner, link_factory = robot_moves, GantryLink
    else:
        raise ValueError(f"unknown robot protocol {protocol!r} (legacy|native)")

    link = MockGantry(log=log) if target == "mock" else link_factory(target)
    return Robot(link, topple_delay_s=topple_delay_s, on_status=on_status, planner=planner)


def _console(robot):
    """Interactive bench console for rig bring-up: measuring
    STEPS_PER_SQUARE, finding the home offset, and picking a magnet duty.

    Sends what you type straight to the firmware, so it works before any
    chess logic is involved -- which is the whole point during assembly.
    """
    print(f"Connected to {robot.port}. Firmware commands go through verbatim.")
    print("chessbot_v1: PING / POS / MAG 0|1|2 / GOTO e4 / MOVE e2e4 / KNIGHT b1c3 / HOME")
    print("  (check the pitch: GOTO a1 then POS should read -210 0)")
    print("chess_gantry: PING / HOME / GOTO 3.5 4 / MAG 170 / PULSE / TOPPLE / OFF / STATUS")
    print("Ctrl-C or 'quit' to leave (drops the magnet on the way out).\n")
    try:
        while True:
            try:
                line = input("gantry> ").strip()
            except EOFError:
                break
            if not line:
                continue
            if line in ("quit", "exit"):
                break
            try:
                print(robot.raw(line) or "OK")
            except GantryError as exc:
                print(f"!! {exc}")
    except KeyboardInterrupt:
        print()
    finally:
        robot.close()
        print("Magnet off, port closed.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", default="auto", help="serial port, 'auto', or 'mock'")
    parser.add_argument("--protocol", default="legacy", choices=("legacy", "native"),
                        help="which firmware is flashed (default: legacy = chessbot_v1)")
    parser.add_argument("--console", action="store_true", help="interactive bench console")
    parser.add_argument("--home", action="store_true", help="home the gantry and exit")
    args = parser.parse_args()

    try:
        robot = open_gantry(args.port, protocol=args.protocol)
    except GantryError as exc:
        raise SystemExit(f"Could not open the gantry: {exc}")
    except ImportError:
        raise SystemExit("pyserial is missing -- pip install -r requirements.txt")

    if args.home:
        robot.home()
        print("Homed.")
        robot.close()
        return
    if args.console:
        _console(robot)
        return

    parser.print_help(sys.stderr)
    robot.close()


if __name__ == "__main__":
    main()
