"""The handful of rig settings a human tunes at the board, on disk.

Everything in rig.py is a measurement -- it changes when the machine changes,
and it belongs in source. This is the other kind: a setting you find by trying
it, and which must survive a restart once you have.

Right now that is two values -- the white-magnet polarity and how slowly the
grip fades when a piece is set down. Both earn a file for the same reason:
they are only judged by watching the arm, and finding the right answer used to
cost a reflash per guess.

The value also has to be re-sent to the Arduino on every connect: opening the
serial port toggles DTR and reboots the Uno, so anything it was told last time
is gone. This file is the host's copy of the truth; see robot.open_gantry.
"""

import json
from pathlib import Path

import rig

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "rig.json"

# What the coil must do to HOLD a white piece. Named for what you can watch at
# the bench rather than a bare boolean, because "repel" is checkable with a
# piece in your hand and "true" is not.
ATTRACT = "attract"
REPEL = "repel"
POLARITIES = (ATTRACT, REPEL)


def _default():
    return REPEL if rig.WHITE_IS_REVERSED else ATTRACT


def _clamp_release(ms):
    """None when it isn't a usable number, so load() can fall back."""
    try:
        ms = int(ms)
    except (TypeError, ValueError):
        return None
    if not rig.MIN_RELEASE_MS <= ms <= rig.MAX_RELEASE_MS:
        return None
    return ms


def load(path=None):
    """The saved settings, falling back to rig.py's measured defaults.

    Never raises: a missing, unreadable or corrupt file just means defaults.
    A rig that won't start because its config file has a stray comma is worse
    than one that starts with the compiled-in values and says so.
    """
    path = Path(path or CONFIG_PATH)
    settings = {"white_polarity": _default(), "release_ms": rig.DEFAULT_RELEASE_MS}
    try:
        stored = json.loads(path.read_text())
    except (OSError, ValueError):
        return settings
    if not isinstance(stored, dict):
        return settings
    if stored.get("white_polarity") in POLARITIES:
        settings["white_polarity"] = stored["white_polarity"]
    release_ms = _clamp_release(stored.get("release_ms"))
    if release_ms is not None:
        settings["release_ms"] = release_ms
    return settings


def save(white_polarity=None, release_ms=None, path=None):
    """Writes the settings, creating config/ if it isn't there.

    Either value may be omitted, in which case whatever is currently stored is
    kept -- so changing one control from the UI can't silently reset the other.
    Returns the settings actually stored.
    """
    path = Path(path or CONFIG_PATH)
    settings = load(path)
    if white_polarity is not None:
        if white_polarity not in POLARITIES:
            raise ValueError(
                f"white_polarity must be one of {POLARITIES}, not {white_polarity!r}")
        settings["white_polarity"] = white_polarity
    if release_ms is not None:
        clamped = _clamp_release(release_ms)
        if clamped is None:
            raise ValueError(
                f"release_ms must be {rig.MIN_RELEASE_MS}-{rig.MAX_RELEASE_MS}, "
                f"not {release_ms!r}")
        settings["release_ms"] = clamped
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n")
    return settings


def white_reversed(white_polarity):
    """Polarity -> the firmware's POL flag. Reversed means white is held by
    repel, the opposite of black."""
    return white_polarity == REPEL


def pol_command(white_polarity):
    """The firmware command that applies this setting."""
    return f"POL {1 if white_reversed(white_polarity) else 0}"


def release_command(release_ms):
    """The firmware command that sets the release fade."""
    return f"RELEASE {int(release_ms)}"
