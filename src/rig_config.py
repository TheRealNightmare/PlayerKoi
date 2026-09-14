"""The handful of rig settings a human tunes at the board, on disk.

Everything in rig.py is a measurement -- it changes when the machine changes,
and it belongs in source. This is the other kind: a setting you find by trying
it, and which must survive a restart once you have.

Right now that is one value, the white-magnet polarity. It earns a file
because getting it wrong is only visible as "the arm shoves white pieces off
the board", and finding the right answer used to cost a reflash per guess.

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


def load(path=None):
    """The saved settings, falling back to rig.py's measured defaults.

    Never raises: a missing, unreadable or corrupt file just means defaults.
    A rig that won't start because its config file has a stray comma is worse
    than one that starts with the compiled-in values and says so.
    """
    path = Path(path or CONFIG_PATH)
    settings = {"white_polarity": _default()}
    try:
        stored = json.loads(path.read_text())
    except (OSError, ValueError):
        return settings
    if isinstance(stored, dict) and stored.get("white_polarity") in POLARITIES:
        settings["white_polarity"] = stored["white_polarity"]
    return settings


def save(white_polarity, path=None):
    """Writes the settings, creating config/ if it isn't there. Returns the
    settings actually stored."""
    if white_polarity not in POLARITIES:
        raise ValueError(f"white_polarity must be one of {POLARITIES}, not {white_polarity!r}")
    path = Path(path or CONFIG_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    settings = {"white_polarity": white_polarity}
    path.write_text(json.dumps(settings, indent=2) + "\n")
    return settings


def white_reversed(white_polarity):
    """Polarity -> the firmware's POL flag. Reversed means white is held by
    repel, the opposite of black."""
    return white_polarity == REPEL


def pol_command(white_polarity):
    """The firmware command that applies this setting."""
    return f"POL {1 if white_reversed(white_polarity) else 0}"
