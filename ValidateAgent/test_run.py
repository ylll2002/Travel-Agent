"""Offline tests for standalone loop context binding and bounded repair."""
import unittest
import io
import json
from unittest.mock import patch
import run


def issue(severity="high", actionable=True):
    return {"severity": severity, "type": "时间", "detail": "时间冲突", "suggestion": "调整时间", "actionable": actionable}


class RunLoopTests(unittest.TestCase):
    def setUp(self):
        self.context = {"profile": {"city": "福州"}, "preferences": {"culture": True},
                        "recent_trips": [{"destination": "上海"}], "search": {"poi": [{"name": "博物馆"}]},
                        "basic": {"total_budget": 4000}}
        self.plan = {"revision": 2, "blocks": []}

    def execute(self, audits):
        with patch.object(run, "generate_plan", return_value=self.plan) as generate, \
             patch.object(run.validate, "validate_plan", side_effect=audits) as validate:
            result = run.run_loop(self.context)
        return result, generate, validate

    def test_all_context_uses_correct_keyword_parameters(self):
        result, generate, validate = self.execute([{"passed": True, "issues": []}])
        self.assertEqual(validate.call_args.args, ())
        self.assertEqual(validate.call_args.kwargs, {"plan": self.plan, **self.context})
        self.assertEqual(result["audit"]["status"], "passed")
        self.assertEqual(generate.call_count, 1)

    def test_only_actionable_high_triggers_one_repair(self):
        result, generate, validate = self.execute([
            {"passed": False, "issues": [issue(), {**issue("low"), "detail": "可选优化"}]},
            {"passed": True, "issues": []}])
        self.assertEqual(generate.call_count, 2)
        feedback = generate.call_args.args[1]
        self.assertIn("时间冲突", feedback)
        self.assertNotIn("可选优化", feedback)
        self.assertEqual(validate.call_count, 2)
        self.assertEqual([e["status"] for e in result["history"]], ["blocked", "passed"])

    def test_service_error_warning_and_non_actionable_do_not_regenerate(self):
        for audit in ({"passed": False, "issues": [], "error": "timeout"},
                      {"passed": True, "issues": [issue("medium", False)]},
                      {"passed": False, "issues": [issue("high", False)]},
                      {"passed": False, "issues": [issue("medium")]},
                      {"passed": "true", "issues": []}):
            with self.subTest(audit=audit):
                result, generate, validate = self.execute([audit])
                self.assertEqual(generate.call_count, 1)
                self.assertEqual(validate.call_count, 1)
                self.assertIs(result["passed"], audit.get("passed") is True)
                self.assertEqual(len(result["history"]), 1)

    def test_unresolved_problem_stops_at_two_rounds(self):
        audit = {"passed": False, "issues": [issue()], "feedback": "仍然有冲突"}
        result, generate, _ = self.execute([audit, audit])
        self.assertEqual(generate.call_count, 2)
        self.assertFalse(result["passed"])
        self.assertEqual(result["audit"]["status"], "blocked")
        self.assertEqual(result["final_feedback"], "仍然有冲突")

    def test_generation_failure_does_not_call_validator(self):
        for value in ({"error": "failed"}, []):
            with self.subTest(value=value), patch.object(run, "generate_plan", return_value=value), \
                 patch.object(run.validate, "validate_plan") as validate:
                result = run.run_loop(self.context)
                validate.assert_not_called()
                self.assertEqual(result["audit"]["status"], "error")
        with patch.object(run, "generate_plan", side_effect=OSError()), patch.object(run.validate, "validate_plan") as validate:
            self.assertEqual(run.run_loop(self.context)["audit"]["status"], "error")
            validate.assert_not_called()

    def test_cli_invalid_or_empty_input_has_structured_error(self):
        for raw in ("", "not JSON", "[]"):
            with self.subTest(raw=raw), patch.object(run.sys, "stdin", io.StringIO(raw)), \
                 patch("builtins.print") as output, patch.object(run, "generate_plan") as generate:
                run.main()
                result = json.loads(output.call_args.args[0])
                self.assertFalse(result["passed"])
                self.assertEqual(result["audit"]["status"], "error")
                self.assertEqual(result["history"], [])
                generate.assert_not_called()

    def test_iteration_limit_is_bounded(self):
        for limit in (0, 3, True):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                run.run_loop(self.context, limit)


if __name__ == "__main__":
    unittest.main()
