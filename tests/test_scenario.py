import unittest

from yepitran.scenario import ScenarioError, Selector, Step, parse, parse_duration


def only(source):
    return parse(source).steps[0]


class HeaderTests(unittest.TestCase):
    def test_headers_are_read(self):
        sc = parse("app: com.x\npermissions: deny\nreverse: 3000 8080\ntimeout: 2m\nlaunch\n")
        self.assertEqual(sc.app, "com.x")
        self.assertEqual(sc.permissions, "deny")
        self.assertEqual(sc.reverse, [3000, 8080])
        self.assertEqual(sc.timeout, 120.0)

    def test_header_defaults(self):
        sc = parse("launch\n")
        self.assertIsNone(sc.app)
        self.assertEqual(sc.permissions, "allow")
        self.assertEqual(sc.reverse, [])
        self.assertEqual(sc.timeout, 20.0)


class StepKindTests(unittest.TestCase):
    def test_launch(self):
        self.assertEqual(only("launch"), Step(line=1, raw="launch", kind="launch"))

    def test_tap_text(self):
        self.assertEqual(
            only('tap "Sign in"'),
            Step(line=1, raw='tap "Sign in"', kind="tap", target=Selector("text", "Sign in")),
        )

    def test_tap_id(self):
        self.assertEqual(
            only("tap id=login"),
            Step(line=1, raw="tap id=login", kind="tap", target=Selector("id", "login")),
        )

    def test_tap_has(self):
        self.assertEqual(
            only('tap has="Sig"'),
            Step(line=1, raw='tap has="Sig"', kind="tap", target=Selector("has", "Sig")),
        )

    def test_type_into_target(self):
        self.assertEqual(
            only('type "hi there" into "Name"'),
            Step(
                line=1,
                raw='type "hi there" into "Name"',
                kind="type",
                text="hi there",
                target=Selector("text", "Name"),
            ),
        )

    def test_type_without_target(self):
        self.assertEqual(
            only('type "x"'),
            Step(line=1, raw='type "x"', kind="type", text="x", target=None),
        )

    def test_scroll_down_until(self):
        self.assertEqual(
            only('scroll down until "Item 40"'),
            Step(
                line=1,
                raw='scroll down until "Item 40"',
                kind="scroll",
                text="down",
                target=Selector("text", "Item 40"),
            ),
        )

    def test_scroll_up(self):
        self.assertEqual(
            only("scroll up"),
            Step(line=1, raw="scroll up", kind="scroll", text="up", target=None),
        )

    def test_deeplink(self):
        self.assertEqual(
            only("deeplink myapp://a/b"),
            Step(line=1, raw="deeplink myapp://a/b", kind="deeplink", text="myapp://a/b"),
        )

    def test_wait_with_timeout(self):
        self.assertEqual(
            only('wait "Home" 30s'),
            Step(
                line=1,
                raw='wait "Home" 30s',
                kind="wait",
                target=Selector("text", "Home"),
                timeout=30.0,
            ),
        )

    def test_wait_without_timeout(self):
        self.assertEqual(
            only('wait "Home"'),
            Step(line=1, raw='wait "Home"', kind="wait", target=Selector("text", "Home"), timeout=None),
        )

    def test_assert_visible(self):
        self.assertEqual(
            only('assert visible "Saved"'),
            Step(
                line=1,
                raw='assert visible "Saved"',
                kind="assert",
                target=Selector("text", "Saved"),
                timeout=None,
                negate=False,
            ),
        )

    def test_assert_gone_with_timeout(self):
        self.assertEqual(
            only('assert gone "Spinner" 5s'),
            Step(
                line=1,
                raw='assert gone "Spinner" 5s',
                kind="assert",
                target=Selector("text", "Spinner"),
                timeout=5.0,
                negate=True,
            ),
        )

    def test_screenshot(self):
        self.assertEqual(
            only("screenshot after-save"),
            Step(line=1, raw="screenshot after-save", kind="screenshot", text="after-save"),
        )


class PlatformTests(unittest.TestCase):
    SOURCE = 'launch\nios: tap "Done"\n'

    def test_prefix_sets_platform(self):
        self.assertIsNone(parse(self.SOURCE).steps[0].platform)
        self.assertEqual(
            parse(self.SOURCE).steps[1],
            Step(line=2, raw='tap "Done"', kind="tap", platform="ios", target=Selector("text", "Done")),
        )

    def test_for_platform_excludes_other_platform(self):
        kinds = [(s.kind, s.line) for s in parse(self.SOURCE).for_platform("android")]
        self.assertEqual(kinds, [("launch", 1)])

    def test_for_platform_includes_own_platform(self):
        kinds = [(s.kind, s.line) for s in parse(self.SOURCE).for_platform("ios")]
        self.assertEqual(kinds, [("launch", 1), ("tap", 2)])


class CommentAndLineTests(unittest.TestCase):
    def test_comments_and_blank_lines_skipped_and_line_numbers_kept(self):
        sc = parse('# intro\n\nlaunch\n   # indented comment\n\n   tap "Done"\n')
        self.assertEqual([s.line for s in sc.steps], [3, 6])
        self.assertEqual(sc.steps[1].raw, 'tap "Done"')


class ParseDurationTests(unittest.TestCase):
    def test_milliseconds(self):
        self.assertEqual(parse_duration("500ms"), 0.5)

    def test_seconds(self):
        self.assertEqual(parse_duration("20s"), 20.0)

    def test_minutes(self):
        self.assertEqual(parse_duration("2m"), 120.0)

    def test_invalid(self):
        self.assertIsNone(parse_duration("abc"))


class ErrorTests(unittest.TestCase):
    def assert_error(self, source, lineno, fragment):
        with self.assertRaises(ScenarioError) as cm:
            parse(source)
        msg = str(cm.exception)
        self.assertTrue(msg.startswith(f"line {lineno}:"), msg)
        self.assertIn(fragment, msg)

    def test_unknown_step(self):
        self.assert_error('launch\nclick "x"\n', 2, "unknown step 'click'")

    def test_header_after_step(self):
        self.assert_error("launch\napp: x\n", 2, "headers must come before")

    def test_bad_permissions(self):
        self.assert_error("# c\n\npermissions: maybe\nlaunch\n", 3, "permissions must be allow or deny")

    def test_bad_deeplink(self):
        self.assert_error("launch\ndeeplink notaurl\n", 2, "deeplink needs a URL")

    def test_bad_screenshot_name(self):
        self.assert_error("launch\nscreenshot bad/name\n", 2, "screenshot name")

    def test_bad_wait_timeout(self):
        self.assert_error('wait "x" soon\n', 1, "bad timeout 'soon'")

    def test_unbalanced_quote(self):
        self.assert_error('launch\ntap "x\n', 2, "No closing quotation")

    def test_empty_scenario(self):
        with self.assertRaises(ScenarioError) as cm:
            parse("# only a comment\n\n")
        self.assertIn("no steps", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
