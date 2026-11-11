import unittest

from yepitran.scenario import ScenarioError, parse


class ScenarioReviewTests(unittest.TestCase):
    def test_quoted_hash_numbers_are_text_not_modifiers(self):
        for literal in ("#2", "#0"):
            with self.subTest(literal=literal):
                scenario = parse(f'type "{literal}" into "Name"\ntap "{literal}"\n')
                self.assertEqual(scenario.steps[0].text, literal)
                self.assertEqual(scenario.steps[1].target.value, literal)
                self.assertEqual(scenario.steps[1].target.nth, 1)
        step = parse('tap "#2" #3\n').steps[0]
        self.assertEqual((step.target.value, step.target.nth), ("#2", 3))
        self.assertEqual(parse(r'tap hash\ #2').steps[0].target.value, 'hash #2')

    def test_empty_app_header_is_rejected(self):
        with self.assertRaisesRegex(ScenarioError, "app needs"):
            parse("app:\nlaunch\n")

    def test_reverse_requires_at_least_one_valid_tcp_port(self):
        for header in ("reverse:\n", "reverse: 0\n", "reverse: 65536\n"):
            with self.subTest(header=header), self.assertRaises(ScenarioError):
                parse(header + "launch\n")

    def test_deeplink_requires_valid_nonempty_scheme_and_target(self):
        for url in ("://home", "myapp://"):
            with self.subTest(url=url), self.assertRaisesRegex(ScenarioError, "deeplink needs"):
                parse(f"deeplink {url}\n")

    def test_empty_quoted_selector_is_rejected(self):
        with self.assertRaisesRegex(ScenarioError, "empty selector"):
            parse('tap ""\n')


if __name__ == "__main__":
    unittest.main()
