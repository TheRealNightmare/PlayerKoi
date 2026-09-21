"""Tests for rig_config -- the settings that have to survive a restart.

Two kinds of thing live in this file, and they fail differently:

  white_polarity / release_ms   found by a human at the bench. Losing one
                                means the arm shoves pieces until somebody
                                notices and re-tunes it.
  graveyard                     bookkeeping the rig writes for itself.
                                Losing it means the arm may drive a captured
                                piece on top of another one.

Neither is worth refusing to start over, so every read is defensive: a
missing, truncated or hand-mangled file falls back to defaults rather than
raising. These tests are mostly about that, and about the three settings not
clobbering each other on the way through.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import rig  # noqa: E402
import rig_config  # noqa: E402


class ConfigCase(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.path)          # start with no file at all
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def write(self, payload):
        with open(self.path, "w") as fh:
            fh.write(payload if isinstance(payload, str) else json.dumps(payload))

    def load(self):
        return rig_config.load(self.path)


class TestGraveyardRoundTrip(ConfigCase):
    def test_a_missing_file_means_an_empty_pile(self):
        self.assertEqual(self.load()["graveyard"], [])

    def test_slots_survive_a_save_and_load(self):
        rig_config.save(graveyard=[5, 2, 31], path=self.path)
        self.assertEqual(self.load()["graveyard"], [5, 2, 31])

    def test_burial_order_is_preserved_not_sorted(self):
        """The list is the order pieces were buried, which is what a
        correction undoes from the end of. Sorting it would point the human
        at whichever piece happened to have the lowest slot number."""
        rig_config.save(graveyard=[31, 0, 17], path=self.path)
        self.assertEqual(self.load()["graveyard"], [31, 0, 17])

    def test_duplicates_collapse_keeping_the_first(self):
        """A doubled slot is not a second piece, it is the same one -- and the
        FIRST occurrence is when it was actually buried."""
        rig_config.save(graveyard=[7, 3, 7], path=self.path)
        self.assertEqual(self.load()["graveyard"], [7, 3])

    def test_an_empty_list_clears_it(self):
        rig_config.save(graveyard=[1, 2], path=self.path)
        rig_config.save(graveyard=[], path=self.path)
        self.assertEqual(self.load()["graveyard"], [])

    def test_every_slot_index_is_accepted(self):
        every = list(range(rig.GRAVEYARD_SLOTS))
        rig_config.save(graveyard=every, path=self.path)
        self.assertEqual(self.load()["graveyard"], every)


class TestGraveyardIsDefensive(ConfigCase):
    """A bad pile is discarded, never raised. Starting with an empty pile
    costs a nudged piece; refusing to start costs the whole session."""

    def test_an_out_of_range_slot_discards_the_pile(self):
        self.write({"graveyard": [1, rig.GRAVEYARD_SLOTS]})
        self.assertEqual(self.load()["graveyard"], [])

    def test_a_negative_slot_discards_the_pile(self):
        self.write({"graveyard": [-1]})
        self.assertEqual(self.load()["graveyard"], [])

    def test_a_non_list_discards_the_pile(self):
        self.write({"graveyard": "3,4"})
        self.assertEqual(self.load()["graveyard"], [])

    def test_a_float_discards_the_pile(self):
        self.write({"graveyard": [1.5]})
        self.assertEqual(self.load()["graveyard"], [])

    def test_a_bool_discards_the_pile(self):
        """bool is an int in Python, so True would quietly become slot 1."""
        self.write({"graveyard": [True]})
        self.assertEqual(self.load()["graveyard"], [])

    def test_corrupt_json_falls_back_to_every_default(self):
        self.write("{ not json")
        settings = self.load()
        self.assertEqual(settings["graveyard"], [])
        self.assertEqual(settings["release_ms"], rig.DEFAULT_RELEASE_MS)

    def test_saving_a_bad_pile_is_refused_loudly(self):
        """Reading is forgiving; writing is not. A bad value reaching save()
        is a bug in the caller, not a mangled file."""
        for bad in ([rig.GRAVEYARD_SLOTS], [-1], ["3"], [2.0]):
            with self.assertRaises(ValueError, msg=repr(bad)):
                rig_config.save(graveyard=bad, path=self.path)


class TestSettingsDoNotClobberEachOther(ConfigCase):
    def test_burying_a_piece_keeps_the_bench_settings(self):
        """The pile is written during an ordinary move. If that reset the
        polarity, the next capture would shove the piece off the carriage."""
        rig_config.save(white_polarity=rig_config.REPEL, release_ms=120,
                        path=self.path)
        rig_config.save(graveyard=[4], path=self.path)
        settings = self.load()
        self.assertEqual(settings["white_polarity"], rig_config.REPEL)
        self.assertEqual(settings["release_ms"], 120)
        self.assertEqual(settings["graveyard"], [4])

    def test_retuning_the_bench_keeps_the_pile(self):
        rig_config.save(graveyard=[4, 9], path=self.path)
        rig_config.save(release_ms=300, path=self.path)
        self.assertEqual(self.load()["graveyard"], [4, 9])

    def test_save_returns_what_was_stored(self):
        stored = rig_config.save(graveyard=[3, 1], path=self.path)
        self.assertEqual(stored["graveyard"], [3, 1])

    def test_the_file_is_created_if_config_does_not_exist(self):
        missing = os.path.join(tempfile.mkdtemp(), "nested", "rig.json")
        rig_config.save(graveyard=[0], path=missing)
        self.addCleanup(os.unlink, missing)
        self.assertEqual(rig_config.load(missing)["graveyard"], [0])


if __name__ == "__main__":
    unittest.main()
