"""Which mode is running, and what it takes to swap one for another.

The two ways of playing need very different stacks -- normal mode wants a
camera, a classifier and a tracker; AI vs AI wants none of them -- but they
share the one thing that is genuinely expensive to rebuild: the gantry.

    for the whole process          rebuilt per mode
    -------------------------      --------------------------------------
    Robot + the open serial port   RobotController (it binds to one loop)
    ChessEngine (one Stockfish)    EngineController (binds loop/sides/style)
    BoardBuffer, the HTTP server   TrackingLoop or HeadlessLoop
                                   the camera, and the tick thread

The serial port is on the left-hand column for a hard reason: opening it
toggles DTR, which reboots the Uno. On a rig with no limit switches a reboot
loses the carriage position for good -- the only recovery is a human parking
it by hand again. So a mode switch must never reopen it, and `Robot.homed`
survives from one mode to the next.

Teardown order is the fiddly part, and `stop()` documents why each step comes
where it does. The short version: nothing may be left blocked on a prompt that
nobody is going to answer.
"""

from pathlib import Path
from threading import Event, Lock, Thread

MENU = "menu"
NORMAL = "normal"
AI_VS_AI = "ai_vs_ai"

MODE_TITLES = {
    NORMAL: "Play the engine",
    AI_VS_AI: "AI vs AI",
}

MODE_BLURBS = {
    NORMAL: "You move White by hand; the camera reads the board and the arm "
            "answers for Black.",
    AI_VS_AI: "Stockfish plays both sides and the arm places every move. No "
              "camera -- set all 32 pieces up first.",
}


class ModeError(RuntimeError):
    """A mode was asked for that can't run, or asked for at the wrong time."""


class Session:
    """Owns the running mode and the things that outlive it.

    `build_normal` and `build_ai` are injected rather than imported so this
    module stays free of the camera stack (and so the tests can drive the
    whole state machine with no hardware). Each returns
    (loop, capture_stream, engine_controller, tick) where `tick` is a callable
    to poll, or None for a mode that doesn't need polling.
    """

    def __init__(self, engine, robot=None, calibration=None, classifier=None,
                 build_normal=None, build_ai=None, poll_interval=0.12, on_mode_change=None,
                 defaults=None):
        self._engine = engine
        self._robot = robot
        self._calibration = Path(calibration) if calibration else None
        self._classifier = Path(classifier) if classifier else None
        self._build_normal = build_normal
        self._build_ai = build_ai
        self._poll_interval = poll_interval
        self._on_mode_change = on_mode_change or (lambda mode: None)
        # What the menu's controls start at -- whatever the process was
        # launched with, so the command line still sets the defaults even
        # though the choosing now happens in a browser.
        self._defaults = dict(defaults or {})

        self._lock = Lock()
        self._mode = MENU
        self._settings = {}
        self._stop_tick = Event()
        self._tick_thread = None

        # The per-mode column. All None while on the menu, which is what every
        # route checks before touching them.
        self.loop = None
        self.capture_stream = None
        self.engine_controller = None
        self.robot_controller = None

    # ------------------------------------------------------------------ state

    @property
    def mode(self):
        with self._lock:
            return self._mode

    @property
    def robot(self):
        return self._robot

    @property
    def running(self):
        return self.mode != MENU

    def unavailable_reason(self, mode):
        """Why `mode` can't be started, or None if it can.

        Phrased for someone standing at the machine: what is missing and what
        makes it.
        """
        if mode == AI_VS_AI:
            if not self._engine.available:
                return self._engine.error or "no chess engine"
            if self._robot is None:
                return "no arm attached -- start with --robot"
            return None
        if mode == NORMAL:
            if self._calibration is None or not self._calibration.exists():
                return f"no calibration at {self._calibration} -- run src/calibrate.py"
            if self._classifier is None or not self._classifier.exists():
                return (f"no classifier at {self._classifier} -- see "
                        "training/train_classifier.py and deploy.py")
            if not self._engine.available:
                return self._engine.error or "no chess engine"
            return None
        return f"unknown mode {mode!r}"

    def available_modes(self):
        """What to draw on the menu."""
        return [
            {
                "mode": mode,
                "title": MODE_TITLES[mode],
                "blurb": MODE_BLURBS[mode],
                "reason": self.unavailable_reason(mode),
                "available": self.unavailable_reason(mode) is None,
            }
            for mode in (AI_VS_AI, NORMAL)
        ]

    def state(self):
        with self._lock:
            mode, settings = self._mode, dict(self._settings)
        return {
            "mode": mode,
            "settings": settings,
            "modes": self.available_modes(),
            "defaults": dict(self._defaults),
            "has_robot": self._robot is not None,
        }

    # ------------------------------------------------------------- start/stop

    def start(self, mode, settings=None):
        """Builds the stack for `mode`. Raises ModeError if it can't run."""
        if mode not in (NORMAL, AI_VS_AI):
            raise ModeError(f"unknown mode {mode!r}")
        reason = self.unavailable_reason(mode)
        if reason is not None:
            raise ModeError(reason)
        if self.running:
            raise ModeError(f"{self._mode} is already running -- stop it first")

        settings = dict(settings or {})
        builder = self._build_ai if mode == AI_VS_AI else self._build_normal
        if builder is None:
            raise ModeError(f"{mode} is not wired up in this process")

        loop, capture_stream, engine_controller, tick = builder(settings)
        self.loop = loop
        self.capture_stream = capture_stream
        self.engine_controller = engine_controller

        if self._robot is not None:
            # Rebuilt, not reused: it binds to one loop, and a fresh one also
            # starts with clean prompt/keep-alive state. The Robot underneath
            # -- and its homed flag -- carries over untouched.
            from robot import RobotController

            self.robot_controller = RobotController(self._robot, loop)

        # The builder makes the EngineController before there is a
        # RobotController to hand it -- the controller binds to the loop, so
        # it cannot exist first. Attaching here closes that gap without
        # either object outliving the other.
        if engine_controller is not None:
            engine_controller.attach_robot(self.robot_controller)

        with self._lock:
            self._mode = mode
            self._settings = settings

        if tick is not None:
            self._stop_tick.clear()
            self._tick_thread = Thread(target=self._run_tick, args=(tick,), daemon=True)
            self._tick_thread.start()

        self._on_mode_change(mode)
        return self.state()

    def _run_tick(self, tick):
        while not self._stop_tick.is_set():
            try:
                tick()
            except Exception:
                # A camera hiccup must not kill the loop; the UI goes stale
                # and says so, which is the existing behaviour.
                pass
            self._stop_tick.wait(self._poll_interval)

    def stop(self, park=True):
        """Tears the mode down and returns to the menu.

        The order matters, and each step is what makes the next one safe:

        1. Engine off, so no new move can start while we are dismantling.
        2. Release any blocked capture prompt as CANCELLED. Skipping this
           strands the robot thread on that Event forever -- it is holding the
           gantry, and if it were ever released it would resume dragging a
           piece onto a board nobody is watching.
        3. Park. Only now is the arm certainly idle.
        4. Drop the per-mode objects.
        """
        if not self.running:
            return self.state()

        if self.engine_controller is not None:
            self.engine_controller.configure(enabled=False)

        if self.robot_controller is not None:
            self.robot_controller.cancel()  # no-op unless something is waiting

        parked, park_error = (True, None)
        if park:
            parked, park_error = self.park()

        self._stop_tick.set()
        if self._tick_thread is not None:
            self._tick_thread.join(timeout=2.0)
            self._tick_thread = None

        if self.engine_controller is not None:
            self.engine_controller.close()  # ends its thread; leaves the engine
        if self.robot_controller is not None:
            self.robot_controller.detach()  # leaves the serial port open
        if self.capture_stream is not None:
            close = getattr(self.capture_stream, "close", None)
            if close is not None:
                close()

        self.loop = None
        self.capture_stream = None
        self.engine_controller = None
        self.robot_controller = None

        with self._lock:
            self._mode = MENU
            self._settings = {}
        self._on_mode_change(MENU)

        state = self.state()
        if not parked:
            state["park_error"] = park_error
        return state

    # ----------------------------------------------------------- park / reset

    def park(self):
        """Drive the carriage to (0,0) and drop the coil. Returns (ok, error).

        There is no separate park verb, because on this rig there is nothing
        to distinguish: with no limit switches the firmware's HOME is
        moveTo(0, 0) from the tracked position, and it releases the magnet on
        arrival. Homing IS parking here.
        """
        if self.robot_controller is None:
            if self._robot is None:
                return True, None  # nothing to park
            from robot import GantryError

            try:
                self._robot.home()
                return True, None
            except GantryError as exc:
                return False, str(exc)
        return self.robot_controller.home()

    def reset(self):
        """New game plus park, staying in whatever mode is running.

        From the menu there is no game, so this parks and nothing more --
        which is exactly what the menu's Reset button is for.
        """
        if self.engine_controller is not None:
            self.engine_controller.configure(enabled=False)
        if self.robot_controller is not None:
            self.robot_controller.cancel()

        ok, error = self.park()

        if self.loop is not None:
            self.loop.reset()

        return ok, error

    def close(self):
        """Process shutdown: stop the mode, then close what it borrowed."""
        try:
            self.stop(park=True)
        finally:
            if self._robot is not None:
                self._robot.close()  # drops the coil, closes the port
            self._engine.close()
