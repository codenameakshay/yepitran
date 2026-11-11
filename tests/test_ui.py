import os
import tempfile
import unittest

# proc reads YEPITRAN_HOME at import; set it first so no test can touch the real device lock.
os.environ["YEPITRAN_HOME"] = tempfile.mkdtemp()

from yepitran.proc import valid_png  # noqa: E402
from yepitran.scenario import Selector  # noqa: E402
from yepitran.ui import Node, find, tap_point, text_landed  # noqa: E402

SCREEN = (402.0, 874.0)
PNG_HEAD = b"\x89PNG\r\n\x1a\n"
PNG_COMPLETE = PNG_HEAD + b"0" * 20 + b"IEND\xaeB`\x82"


class NodeMatchTests(unittest.TestCase):
    def test_text_selector_matches_a_whole_line_and_has_matches_a_substring(self):
        n = Node("Dart sheet\nsubtitle", "", "", "StaticText", 0, 0, 402, 60)
        self.assertTrue(n.matches(Selector("text", "Dart sheet")))
        self.assertTrue(n.matches(Selector("has", "subt")))
        self.assertFalse(n.matches(Selector("text", "sub")))

    def test_id_selector_matches_the_id_exactly(self):
        n = Node("", "login_button", "", "Button", 0, 0, 10, 10)
        self.assertTrue(n.matches(Selector("id", "login_button")))
        self.assertFalse(n.matches(Selector("id", "login")))


class OnScreenTests(unittest.TestCase):
    def test_zero_size_node_at_origin_is_off_screen(self):
        self.assertFalse(Node("Ghost", "", "", "Button", 0, 0, 0, 0).on_screen(SCREEN))

    def test_node_centred_below_the_screen_is_off_screen(self):
        self.assertFalse(Node("Ghost", "", "", "Button", 0, 880, 402, 40).on_screen(SCREEN))

    def test_node_inside_the_screen_is_on_screen(self):
        self.assertTrue(Node("Home", "", "", "Button", 0, 100, 402, 50).on_screen(SCREEN))


class FindTests(unittest.TestCase):
    def test_returns_the_top_most_match(self):
        nodes = [
            Node("Save", "", "", "StaticText", 0, 500, 402, 40),
            Node("Save", "", "", "StaticText", 0, 100, 402, 40),
        ]
        hit = find(nodes, Selector("text", "Save"), SCREEN)
        self.assertEqual(hit.y, 100)

    def test_tappable_prefers_a_button_over_a_higher_title(self):
        nodes = [
            Node("Add", "", "", "StaticText", 0, 100, 402, 40),
            Node("Add", "", "", "Button", 0, 500, 402, 40),
        ]
        hit = find(nodes, Selector("text", "Add"), SCREEN, tappable=True)
        self.assertEqual(hit.kind, "Button")
        self.assertEqual(hit.y, 500)


class TapPointTests(unittest.TestCase):
    def test_full_screen_scrim_is_tapped_away_from_the_sheet(self):
        scrim = Node("Dismiss", "", "", "Other", 0, 0, 402, 874, "", (0, 1))
        item0 = Node("Item 0", "", "", "Cell", 8, 402, 386, 48, "", (0, 0, 1))
        item1 = Node("Item 1", "", "", "Cell", 8, 450, 386, 400, "", (0, 0, 2))
        x, y = tap_point(scrim, [scrim, item0, item1])
        self.assertLess(y, 402)

    def test_button_inside_a_larger_labelled_row_gets_its_exact_centre(self):
        row = Node("Row", "", "", "Cell", 0, 0, 402, 200, "", (0, 1))
        button = Node("Buy", "", "", "Button", 100, 50, 100, 40, "", (0, 0, 0))
        self.assertEqual(tap_point(button, [row, button]), (150.0, 70.0))

    def test_unlabelled_overlapping_node_does_not_move_the_point(self):
        target = Node("Save", "", "", "Button", 0, 0, 100, 100, "", (0, 0))
        overlay = Node("", "", "", "Other", 0, 0, 402, 874, "", (0, 1))
        self.assertEqual(tap_point(target, [target, overlay]), (50.0, 50.0))

    def test_tall_row_is_tapped_above_a_tab_bar_covering_its_lower_part(self):
        row = Node("Row", "", "", "Cell", 0, 100, 402, 700, "", (0, 0))
        tabs = Node("Tabs", "", "", "Other", 0, 400, 402, 400, "", (0, 1))
        x, y = tap_point(row, [row, tabs])
        self.assertLess(y, 400)


class TextLandedTests(unittest.TestCase):
    def test_typed_characters_in_order_count_despite_reformatting(self):
        self.assertTrue(text_landed("Ship yepitran 42", "Ship yepitran 42"))
        self.assertTrue(text_landed("hello", "Hello"))
        self.assertTrue(text_landed("5551234", "+1 (555) 1234"))

    def test_dropped_character_is_detected(self):
        self.assertFalse(text_landed("hello", "helo"))
        self.assertFalse(text_landed("Ship yepitran 42", "Ship proof run 42"))
        self.assertFalse(text_landed("Cab", "Can"))
        self.assertTrue(text_landed("5551234", "(555) 123-4"))

    def test_keyboard_stutter_and_word_splits_are_detected(self):
        self.assertFalse(text_landed("Try yepitran", "TTryTry yepitran"))   # doubled input on a slow device
        self.assertFalse(text_landed("Try yepitran", "Try yean"))           # dropped input
        self.assertFalse(text_landed("yepitran", "yep it ran"))             # autocorrect split the word
        self.assertFalse(text_landed("Try yepitran", "Try yepitranx"))      # extra trailing letter
        self.assertTrue(text_landed("Try yepitran", "Try yepitran"))
        self.assertTrue(text_landed("New York", "new york"))


class ValidPngTests(unittest.TestCase):
    def test_complete_png_is_valid(self):
        self.assertTrue(valid_png(PNG_COMPLETE))

    def test_png_cut_before_iend_is_invalid(self):
        self.assertFalse(valid_png(PNG_COMPLETE[:len(PNG_HEAD) + 20]))

    def test_empty_bytes_are_invalid(self):
        self.assertFalse(valid_png(b""))


if __name__ == "__main__":
    unittest.main()
