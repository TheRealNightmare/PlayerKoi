"""The handful of rig settings a human tunes at the board, on disk.

Everything in rig.py is a measurement -- it changes when the machine changes,
and it belongs in source. This is the other kind: a setting you find by trying
it, and which must survive a restart once you have.

Right now that is the polarity that holds every piece and the grip and
settle pauses around a carried piece (plus the graveyard pile the rig records
for itself). They earn a file because they
are only judged by watching the arm, and finding the right answer used to
cost a reflash per guess.

Older files may still carry white_polarity / release_ms from before every
piece was magnetised the same way up; load() ignores them and the next save()
drops them. white_polarity is deliberately NOT carried over into `polarity`:
it meant "white is the opposite of black", which no longer exists.

The value also has to be re-sent to the Arduino on every connect: opening the
serial port toggles DTR and reboots the Uno, so anything it was told last time
is gone. This file is the host's copy of the truth; see robot.open_gantry.
"""

import json
from pathlib import Path

import rig

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "rig.json"

# What the coil must do to HOLD a piece -- any piece, since they are all
# magnetised the same way up. Named for what you can watch at the bench
# rather than a bare boolean.
ATTRACT = "attract"
REPEL = "repel"
POLARITIES = (ATTRACT, REPEL)


def _default_polarity():
    return REPEL if rig.HOLD_BY_REPEL else ATTRACT

def _clamp_tuning(name, value):
    """A motion-tuning value inside its range, or None if unusable."""
    _default_value, low, high = rig.MOTION_TUNING[name]
    if isinstance(value, bool):
        return None
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if low <= value <= high else None


def _clean_graveyard(slots):
    """Occupied slot indices in burial order, or None if unusable.

    Unlike the motion settings this one is not a human's tuning choice --
    it is bookkeeping the rig writes for itself, so the failure mode is a
    half-written or hand-edited file rather than a bad guess. Same answer
    either way: drop it and start from an empty pile. Losing the pile means
    the arm may drive a piece onto an occupied slot, which is a nudge you can
    see and fix; refusing to start is not.

    ORDER IS DATA HERE, so this deduplicates rather than sorting. The list is
    the order pieces were buried, which is what lets a correction undo the
    LAST capture and name the right slot to lift from -- sorting it would
    silently point the human at whichever piece happened to have the lowest
    slot number.
    """
    if not isinstance(slots, list):
        return None
    cleaned = []
    for slot in slots:
        # bool is an int in Python, and True would silently become slot 1.
        if isinstance(slot, bool) or not isinstance(slot, int):
            return None
        if not 0 <= slot < rig.GRAVEYARD_SLOTS:
            return None
        if slot not in cleaned:
            cleaned.append(slot)
    return cleaned


def load(path=None):
    """The saved settings, falling back to rig.py's measured defaults.

    Never raises: a missing, unreadable or corrupt file just means defaults.
    A rig that won't start because its config file has a stray comma is worse
    than one that starts with the compiled-in values and says so.
    """
    path = Path(path or CONFIG_PATH)
    settings = {"polarity": _default_polarity(), "graveyard": []}
    settings.update({name: spec[0] for name, spec in rig.MOTION_TUNING.items()})
    try:
        stored = json.loads(path.read_text())
    except (OSError, ValueError):
        return settings
    if not isinstance(stored, dict):
        return settings
    if stored.get("polarity") in POLARITIES:
        settings["polarity"] = stored["polarity"]
    graveyard = _clean_graveyard(stored.get("graveyard", []))
    if graveyard is not None:
        settings["graveyard"] = graveyard
    for name in rig.MOTION_TUNING:
        value = _clamp_tuning(name, stored.get(name))
        if value is not None:
            settings[name] = value
    return settings


def save(polarity=None, graveyard=None, path=None, **tuning):
    """Writes the settings, creating config/ if it isn't there.

    Any value may be omitted, in which case whatever is currently stored is
    kept -- so changing one control from the UI can't silently reset the other,
    and burying a piece can't reset the pauses found at the bench.

    `graveyard` is the full pile, not a slot to add: the caller owns it (see
    src/graveyard.py) and this only records it. Order is preserved because it
    is burial order, so pass a list and not a set. [] clears it.

    `tuning` takes any of rig.MOTION_TUNING's names (grip_ms, settle_ms);
    None leaves a value alone, like the others.

    Returns the settings actually stored.
    """
    unknown = set(tuning) - set(rig.MOTION_TUNING)
    if unknown:
        raise ValueError(f"unknown setting(s): {sorted(unknown)}")
    path = Path(path or CONFIG_PATH)
    settings = load(path)
    if polarity is not None:
        if polarity not in POLARITIES:
            raise ValueError(f"polarity must be one of {POLARITIES}, not {polarity!r}")
        settings["polarity"] = polarity
    if graveyard is not None:
        cleaned = _clean_graveyard(list(graveyard))
        if cleaned is None:
            raise ValueError(
                f"graveyard must be slot indices 0..{rig.GRAVEYARD_SLOTS - 1}, "
                f"not {graveyard!r}")
        settings["graveyard"] = cleaned
    for name, value in tuning.items():
        if value is None:
            continue
        clamped = _clamp_tuning(name, value)
        if clamped is None:
            _default_value, low, high = rig.MOTION_TUNING[name]
            raise ValueError(f"{name} must be {low}-{high}, not {value!r}")
        settings[name] = clamped
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n")
    return settings


def pol_command(polarity):
    """The firmware command that sets what holds every piece."""
    if polarity not in POLARITIES:
        raise ValueError(f"polarity must be one of {POLARITIES}, not {polarity!r}")
    return f"POL {1 if polarity == REPEL else 0}"


def dwell_command(grip_ms, settle_ms):
    """The firmware command that sets the grip and settle pauses."""
    return f"DWELL {int(grip_ms)} {int(settle_ms)}"


def grid_command(grid_pct):
    """The firmware command that sets the gridline magnet power, percent."""
    return f"GRID {int(grid_pct)}"


def speed_command(feed_mms=None):
    """The firmware command that sets the feed rate."""
    return f"SPEED {float(rig.FEED_MMS if feed_mms is None else feed_mms):g}"
