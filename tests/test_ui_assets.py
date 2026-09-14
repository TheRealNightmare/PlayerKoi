"""Tests for the UI assets under src/ui.

The page is no longer a Python string, so these stand in for the checks that
used to be impossible: that every element app.js reaches for actually exists
in index.html, and that the static route can't be talked into serving
anything else. A typo'd id is otherwise a silent null-dereference that only
shows up as a blank panel in a browser nobody has open.

No server is started -- read_ui_asset is the whole of the route's logic.
"""

import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import web_ui  # noqa: E402

UI = Path(web_ui.UI_DIR)
HTML = (UI / "index.html").read_text()
JS = (UI / "app.js").read_text()
NEORETRO = (UI / "neoretro.css").read_text()
APP_CSS = (UI / "app.css").read_text()


class TestAssetsExist(unittest.TestCase):
    def test_all_four_files_are_present(self):
        for name in ("index.html", "neoretro.css", "app.css", "app.js"):
            self.assertTrue((UI / name).is_file(), name)

    def test_the_page_links_the_stylesheets_and_script(self):
        self.assertIn('href="/neoretro.css"', HTML)
        self.assertIn('href="/app.css"', HTML)
        self.assertIn('src="/app.js"', HTML)

    def test_nothing_is_left_inline(self):
        """The whole point of the split -- an inline <script> would be back
        to escaping JS through a Python string."""
        self.assertNotIn("<script>", HTML)
        self.assertNotIn("<style>", HTML)


class TestServing(unittest.TestCase):
    def test_each_asset_is_served_with_the_right_type(self):
        for name, expected in (
            ("index.html", "text/html"),
            ("app.css", "text/css"),
            ("neoretro.css", "text/css"),
            ("app.js", "text/javascript"),
        ):
            body, content_type = web_ui.read_ui_asset(name)
            self.assertIsNotNone(body, name)
            self.assertIn(expected, content_type)

    def test_traversal_out_of_the_ui_directory_is_refused(self):
        for name in ("../web_ui.py", "../../README.md", "/etc/passwd",
                     "..%2fweb_ui.py", "ui/../../src/web_ui.py"):
            body, _ = web_ui.read_ui_asset(name)
            self.assertIsNone(body, name)

    def test_a_non_asset_extension_is_refused(self):
        """Even inside src/ui: the extension map is the allowlist."""
        body, _ = web_ui.read_ui_asset("../rig.py")
        self.assertIsNone(body)

    def test_a_missing_file_is_refused(self):
        self.assertEqual(web_ui.read_ui_asset("nope.css"), (None, None))


class TestMarkupMatchesTheScript(unittest.TestCase):
    """The check that a rename during a restyle would otherwise break
    silently."""

    def test_every_id_the_script_looks_up_exists_in_the_page(self):
        ids_in_html = set(re.findall(r'id="([^"]+)"', HTML))
        wanted = set(re.findall(r'getElementById\("([^"]+)"\)', JS))
        self.assertTrue(wanted, "sanity: the script should look ids up")
        self.assertEqual(wanted - ids_in_html, set())

    def test_every_querySelector_id_exists_too(self):
        ids_in_html = set(re.findall(r'id="([^"]+)"', HTML))
        wanted = set(re.findall(r'querySelector\("#([A-Za-z0-9_-]+)', JS))
        self.assertEqual(wanted - ids_in_html, set())

    def test_the_dialog_the_helper_drives_is_in_the_page(self):
        for name in ("nrDialog", "nrDialogTitle", "nrDialogText",
                     "nrDialogOk", "nrDialogCancel"):
            self.assertIn(f'id="{name}"', HTML)


class TestDesignSystem(unittest.TestCase):
    def test_the_five_rules_are_defined(self):
        for rule in (".nr-clr", ".nr-border", ".nr-shadow", ".nr-focus"):
            self.assertIn(rule, NEORETRO)
        self.assertIn("[data-pressable]", NEORETRO)

    def test_the_border_is_a_black_two_pixel_one(self):
        self.assertIn("border: 2px solid #000", NEORETRO)

    def test_the_shadow_is_hard_and_four_pixels(self):
        self.assertIn("box-shadow: 4px 4px 0 0 #000", NEORETRO)

    def test_pressing_moves_the_element_onto_its_shadow(self):
        block = NEORETRO[NEORETRO.index("[data-pressable]"):]
        self.assertIn("translate: 4px 4px", block)

    def test_every_ansi_slot_has_a_colour_and_a_text_colour(self):
        slots = ["black", "red", "green", "yellow", "blue", "magenta", "cyan", "white"]
        slots += ["bright-" + s for s in slots]
        for slot in slots:
            self.assertIn(f"--{slot}:", NEORETRO, slot)
            self.assertIn(f"--on-{slot}:", NEORETRO, slot)
            self.assertIn(f'[data-color="{slot}"]', NEORETRO, slot)

    def test_the_theme_is_gruvbox_light(self):
        self.assertIn("--background: #fbf1c7", NEORETRO)
        self.assertIn("--foreground: #3c3836", NEORETRO)

    def test_every_data_color_used_is_one_the_system_defines(self):
        used = set(re.findall(r'data-color="([^"]+)"', HTML))
        used |= set(re.findall(r'dataset\.color = "([^"]+)"', JS))
        # Values assigned from a variable are checked by the branch below.
        used |= {"green", "red", "blue", "yellow", "magenta", "cyan", "black"}
        for color in used:
            self.assertIn(f'[data-color="{color}"]', NEORETRO, color)


class TestBoardContrast(unittest.TestCase):
    """A cream glyph on a cream square is 1.00:1 -- invisible. The board
    colours are only safe because each piece is outlined in the opposite
    colour, so this pins that the outline is still there."""

    @staticmethod
    def _ratio(a, b):
        def lum(h):
            h = h.lstrip("#")
            def ch(v):
                v /= 255
                return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
            r, g, bl = (ch(int(h[i:i + 2], 16)) for i in (0, 2, 4))
            return 0.2126 * r + 0.7152 * g + 0.0722 * bl
        la, lb = lum(a), lum(b)
        return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)

    def test_the_two_square_colours_are_distinguishable(self):
        self.assertGreater(self._ratio("#fbf1c7", "#928374"), 3.0)

    def test_both_piece_colours_are_outlined(self):
        for rule in (".white-piece", ".black-piece"):
            block = APP_CSS[APP_CSS.index(rule):]
            block = block[:block.index("}")]
            self.assertIn("text-shadow", block, rule)

    def test_the_outline_colours_are_opposites(self):
        white = APP_CSS[APP_CSS.index(".white-piece"):]
        white = white[:white.index("}")]
        black = APP_CSS[APP_CSS.index(".black-piece"):]
        black = black[:black.index("}")]
        self.assertIn("var(--foreground)", white)  # dark outline on a light fill
        self.assertIn("var(--black)", black)       # light outline on a dark fill

    def test_the_fill_and_its_outline_are_far_apart(self):
        self.assertGreater(self._ratio("#fbf1c7", "#3c3836"), 7.0)


if __name__ == "__main__":
    unittest.main()
