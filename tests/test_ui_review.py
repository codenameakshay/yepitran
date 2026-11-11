import unittest

from yepitran.scenario import Selector, parse
from yepitran.ui import Node, contains, find, signature, tap_point


class UIReviewTests(unittest.TestCase):
    def test_explicit_nth_tap_match_uses_top_to_bottom_order(self):
        title = Node("Save", "", "", "StaticText", 0, 100, 100, 40, path=(0,))
        button = Node("Save", "", "", "Button", 0, 500, 100, 40, path=(1,))
        first = parse('tap "Save" #1\n').steps[0].target
        second = parse('tap "Save" #2\n').steps[0].target
        self.assertIs(find([title, button], first, (402, 874), tappable=True), title)
        self.assertIs(find([title, button], second, (402, 874), tappable=True), button)

    def test_tap_point_stays_inside_screen_for_clipped_node(self):
        target = Node("Continue", "", "", "Button", 0, 80, 200, 100, path=(0,))
        cover = Node("Overlay", "", "", "Other", 70, 95, 60, 15, path=(1,))
        x, y = tap_point(target, [target, cover], screen=(100, 100))
        self.assertGreaterEqual(x, 0)
        self.assertLessEqual(x, 100)
        self.assertGreaterEqual(y, 0)
        self.assertLessEqual(y, 100)
        self.assertTrue(contains(target, x, y))
        self.assertFalse(contains(cover, x, y))

    def test_signature_distinguishes_id_kind_and_tree_position(self):
        base = Node("Continue", "", "", "Button", 0, 0, 100, 50, path=(0,))
        by_id = Node("Continue", "continue", "", "Button", 0, 0, 100, 50, path=(0,))
        by_kind = Node("Continue", "", "", "StaticText", 0, 0, 100, 50, path=(0,))
        by_path = Node("Continue", "", "", "Button", 0, 0, 100, 50, path=(1,))
        self.assertNotEqual(signature([base]), signature([by_id]))
        self.assertNotEqual(signature([base]), signature([by_kind]))
        self.assertNotEqual(signature([base]), signature([by_path]))


if __name__ == "__main__":
    unittest.main()
