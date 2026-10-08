"""Offline tests for standalone loop context binding and bounded repair."""
import unittest
import io
import json
from unittest.mock import patch
from ValidateAgent import run


def issue(severity="high", actionable=True):
    return {"severity": severity, "type": "时间", "detail": "时间冲突", "suggestion": "调整时间", "actionable": actionable}


class RunLoopTests(unittest.TestCase):
    def setUp(self):
        self.context = {"profile": {"city": "福州"}, "preferences": {"culture": True},
                        "recent_trips": [{"destination": "上海"}], "search": {"poi": [{"name": "博物馆"}]},
                        "basic": {"total_budget": 4000}}
        self.plan = {"revision": 1, "blocks": []}

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

    def test_explicit_edit_reviews_once_without_reapplying_instruction(self):
        original = {"revision": 4, "blocks": [], "audit": {"passed": True}}
        context = {**self.context, "plan": original, "modify": {"instruction": "换一家餐厅"}}
        raw = {"passed": False, "issues": [issue()]}
        with patch.object(run, "generate_plan", return_value=self.plan) as generate, \
             patch.object(run.validate, "validate_plan", return_value=raw) as validate:
            result = run.run_loop(context)
        generate.assert_called_once()
        validate.assert_called_once()
        self.assertEqual(result["plan"]["revision"], 5)
        self.assertEqual(result["audit"]["plan_revision"], 5)
        self.assertNotIn("audit", result["plan"])
        self.assertFalse(result["passed"])
        self.assertEqual(original["revision"], 4)

    def test_old_review_revision_is_not_relabelled_as_new(self):
        raw = {"passed": True, "issues": [], "plan_revision": 0}
        result, generate, _ = self.execute([raw])
        self.assertEqual(result["audit"]["status"], "error")
        self.assertFalse(result["passed"])
        generate.assert_called_once()

    def test_edit_without_repeated_basic_keeps_saved_hard_budget(self):
        context = {"plan": {"revision": 4, "basic": {"total_budget": 100}}, "modify": {"instruction": "换一个"}}
        with patch.object(run, "generate_plan", return_value=self.plan), \
             patch.object(run.validate, "validate_plan", return_value={"passed": True, "issues": []}) as validate:
            run.run_loop(context)
        self.assertEqual(validate.call_args.kwargs["basic"], {"total_budget": 100})
        self.assertNotIn("basic", context)

    def test_global_edit_reviews_the_resolved_new_budget(self):
        changed = {**self.plan, "basic": {"total_budget": 200}}
        context = {"plan": {"revision": 4}, "basic": {"total_budget": 100},
                   "modify": {"mode": "global", "instruction": "预算改成200"}}
        with patch.object(run, "generate_plan", return_value=changed), \
             patch.object(run.validate, "validate_plan", return_value={"passed": True, "issues": []}) as validate:
            run.run_loop(context)
        self.assertEqual(validate.call_args.kwargs["basic"], {"total_budget": 200})
        self.assertEqual(context["basic"], {"total_budget": 100})


    def test_four_passed_suggestions_do_not_trigger_another_plan(self):
        suggestions = [{**issue("medium"), "detail": detail} for detail in
                       ("报价待核实", "优化杭帮菜匹配", "雨天可准备室内备选", "部分来源信息待核实")]
        result, generate, validate = self.execute([{"passed": True, "issues": suggestions}])
        self.assertEqual(result["audit"]["status"], "warning")
        self.assertEqual(len(result["audit"]["issues"]), 4)
        generate.assert_called_once()
        validate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
