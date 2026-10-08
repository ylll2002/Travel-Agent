import copy
import json
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent import resolve_intent


TODAY = date(2026, 10, 6)
PREVIOUS = {"destination": "杭州", "origin": "上海", "start_date": "2026-10-09", "duration_days": 3, "travelers": "2人", "total_budget": 5000}
VALID = {"action": "collect", "data": {"total_budget": 6000}}


def response(value):
    content = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


class QuestionResponseTests(unittest.TestCase):
    def setUp(self):
        today = patch("agent.local_today", return_value=TODAY)
        today.start()
        self.addCleanup(today.stop)
        self.payload = {"messages": [{"role": "user", "content": "总预算改成6000元"}], "trip_data": copy.deepcopy(PREVIOUS)}

    def client(self, values):
        client = MagicMock()
        client.chat.completions.create.side_effect = [value if isinstance(value, Exception) else response(value) for value in values]
        return client

    def test_normal_object_uses_one_bounded_call_and_preserves_previous_parameters(self):
        client = self.client([VALID])
        result = resolve_intent(self.payload, client)
        self.assertEqual(result["action"], "confirm_trip")
        self.assertEqual(result["data"]["total_budget"], 6000)
        self.assertEqual(result["data"]["destination"], PREVIOUS["destination"])
        self.assertEqual(client.chat.completions.create.call_count, 1)
        self.assertEqual(client.chat.completions.create.call_args.kwargs["timeout"], 30)
        self.assertEqual(client.max_retries, 0)
        self.assertEqual(self.payload["trip_data"], PREVIOUS)

    def test_unique_object_array_is_unwrapped_without_retry(self):
        client = self.client([[VALID]])
        result = resolve_intent(self.payload, client)
        self.assertEqual(result["data"]["total_budget"], 6000)
        self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_json_fence_is_unwrapped_without_retry(self):
        client = self.client(["```json\n" + json.dumps(VALID) + "\n```"])
        result = resolve_intent(self.payload, client)
        self.assertEqual(result["action"], "confirm_trip")
        self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_invalid_json_receives_one_explicit_schema_correction(self):
        client = self.client(["{broken", VALID])
        result = resolve_intent(self.payload, client)
        self.assertEqual(result["data"]["total_budget"], 6000)
        self.assertEqual(client.chat.completions.create.call_count, 2)
        calls = client.chat.completions.create.call_args_list
        self.assertEqual([call.kwargs["timeout"] for call in calls], [30, 30])
        correction = calls[1].kwargs["messages"]
        self.assertIn("一个JSON对象", correction[-1]["content"])
        self.assertIn("不要补造", correction[-1]["content"])
        self.assertEqual(json.loads(correction[1]["content"])["trip_data"], PREVIOUS)

    def test_empty_multi_item_and_non_object_arrays_are_not_guessed(self):
        for malformed in ([], [VALID, {"action": "collect", "data": {"destination": "成都"}}], ["not an object"]):
            with self.subTest(response=malformed):
                client = self.client([malformed, VALID])
                result = resolve_intent(self.payload, client)
                self.assertEqual(client.chat.completions.create.call_count, 2)
                self.assertEqual(result["data"]["destination"], PREVIOUS["destination"])
                self.assertEqual(result["data"]["total_budget"], 6000)

    def test_unknown_action_is_corrected_once(self):
        client = self.client([{"action": "book_everything", "data": {"destination": "成都"}}, VALID])
        result = resolve_intent(self.payload, client)
        self.assertEqual(client.chat.completions.create.call_count, 2)
        self.assertEqual(result["data"]["destination"], PREVIOUS["destination"])

    def test_non_object_data_is_corrected_once(self):
        for invalid in (None, [], "杭州", 42):
            with self.subTest(data=invalid):
                client = self.client([{"action": "collect", "data": invalid}, VALID])
                result = resolve_intent(self.payload, client)
                self.assertEqual(client.chat.completions.create.call_count, 2)
                self.assertEqual(result["data"]["total_budget"], 6000)

    def test_second_bad_response_returns_original_state_without_a_default_trip(self):
        client = self.client([[], {"action": "collect", "data": []}])
        result = resolve_intent(self.payload, client)
        self.assertIn("error", result)
        self.assertEqual(result["data"], PREVIOUS)
        self.assertNotIn("action", result)
        self.assertEqual(client.chat.completions.create.call_count, 2)
        self.assertEqual(self.payload["trip_data"], PREVIOUS)

    def test_missing_response_choice_is_corrected_once(self):
        client = MagicMock()
        client.chat.completions.create.side_effect = [SimpleNamespace(choices=[]), response(VALID)]
        result = resolve_intent(self.payload, client)
        self.assertEqual(result["data"]["total_budget"], 6000)
        self.assertEqual(client.chat.completions.create.call_count, 2)

    def test_invalid_modify_schema_is_corrected_without_collecting_new_trip(self):
        valid = {"action": "modify", "mode": "block", "targets": [], "instruction": "餐厅近一点"}
        client = self.client([{"action": "modify", "instruction": None}, valid])
        result = resolve_intent({**self.payload, "has_plan": True}, client)
        self.assertEqual(result, valid)
        self.assertEqual(client.chat.completions.create.call_count, 2)

    def test_model_cannot_clear_previous_trip_with_string_new_trip_flag(self):
        client = self.client([{"action": "collect", "new_trip": "true", "data": {"total_budget": 6000}}, VALID])
        result = resolve_intent(self.payload, client)
        self.assertEqual(result["data"]["destination"], PREVIOUS["destination"])
        self.assertEqual(client.chat.completions.create.call_count, 2)

    def test_transport_timeout_is_friendly_and_not_schema_retried(self):
        client = self.client([TimeoutError("internal timeout details")])
        result = resolve_intent(self.payload, client)
        self.assertIn("超时", result["error"])
        self.assertEqual(result["data"], PREVIOUS)
        self.assertEqual(client.chat.completions.create.call_count, 1)
        self.assertNotIn("internal", result["error"])

    def test_transport_error_does_not_expose_exception_or_retry(self):
        client = self.client([RuntimeError("secret-token internal endpoint")])
        result = resolve_intent(self.payload, client)
        self.assertIn("error", result)
        self.assertEqual(result["data"], PREVIOUS)
        self.assertEqual(client.chat.completions.create.call_count, 1)
        self.assertNotIn("secret-token", result["error"])

    def test_real_client_constructor_disables_sdk_retries(self):
        client = self.client([VALID])
        with patch.dict("agent.os.environ", {"OPENAI_API_KEY": "local-test-only"}), patch("agent.OpenAI", return_value=client) as constructor:
            result = resolve_intent(self.payload)
        self.assertEqual(constructor.call_args.kwargs["max_retries"], 0)
        self.assertEqual(result["action"], "confirm_trip")

    def test_missing_api_key_requests_configuration_and_preserves_trip(self):
        for key in (None, "", "   ", "sk-your-key-here", "sk-your-qwen-key-here"):
            with self.subTest(key=key), patch.dict("agent.os.environ", {} if key is None else {"OPENAI_API_KEY": key}, clear=True), patch("agent.OpenAI") as constructor:
                result = resolve_intent(self.payload)
            self.assertIn("未配置模型 API Key", result["error"])
            self.assertIn("请先", result["error"])
            self.assertEqual(result["data"], PREVIOUS)
            constructor.assert_not_called()

    def test_empty_user_input_keeps_state_and_does_not_call_model(self):
        client = self.client([])
        result = resolve_intent({"messages": [], "trip_data": PREVIOUS}, client)
        self.assertEqual(result["data"], PREVIOUS)
        client.chat.completions.create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
