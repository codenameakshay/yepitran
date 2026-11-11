import ast
import unittest
from pathlib import Path
from types import SimpleNamespace


class SoakSummaryTests(unittest.TestCase):
    def test_successful_prefix_is_not_a_completed_soak(self):
        source = Path(__file__).resolve().parents[1] / "scripts/soak.py"
        exit_call = ast.parse(source.read_text()).body[-1].value
        self.assertEqual(ast.unparse(exit_call.func), "sys.exit")
        predicate = compile(ast.Expression(exit_call.args[0]), "<isolated soak exit status>", "eval")
        complete = {"ios": [{"verdict": "PASS"}, {"verdict": "PASS"}]}
        partial = {"ios": [{"verdict": "PASS"}]}
        failed = {"ios": [{"verdict": "PASS"}, {"verdict": "INFRA FAIL"}]}
        for results, expected in ((partial, 1), (complete, 0), (failed, 1)):
            with self.subTest(results=results):
                self.assertEqual(eval(predicate, {"results": results, "a": SimpleNamespace(n=2)}), expected)
