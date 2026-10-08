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

    def test_deepseek_flash_disables_thinking_for_json_and_schema_correction(self):
        client = self.client(["{broken", VALID])
        with patch.dict("agent.os.environ", {
            "OPENAI_MODEL": "deepseek-v4.1-flash",
            "OPENAI_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        }):
            result = resolve_intent(self.payload, client)
        self.assertEqual(result["action"], "confirm_trip")
        self.assertEqual(result["data"]["destination"], PREVIOUS["destination"])
        self.assertEqual(client.chat.completions.create.call_count, 2)
        for call in client.chat.completions.create.call_args_list:
            self.assertEqual(call.kwargs["model"], "deepseek-v4.1-flash")
            self.assertEqual(call.kwargs["extra_body"], {"enable_thinking": False})
            self.assertEqual(call.kwargs["response_format"], {"type": "json_object"})
        self.assertEqual(self.payload["trip_data"], PREVIOUS)

    def test_qwen_still_disables_thinking_for_json(self):
        client = self.client([VALID])
        with patch.dict("agent.os.environ", {"OPENAI_MODEL": "qwen3.8-max"}):
            result = resolve_intent(self.payload, client)
        self.assertEqual(result["action"], "confirm_trip")
        kwargs = client.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "qwen3.8-max")
        self.assertEqual(kwargs["extra_body"], {"enable_thinking": False})
        self.assertEqual(kwargs["response_format"], {"type": "json_object"})

    def test_deepseek_flash_missing_key_preserves_trip_without_model_call(self):
        with patch.dict("agent.os.environ", {
            "OPENAI_MODEL": "deepseek-v4.1-flash", "OPENAI_API_KEY": "",
        }, clear=True), patch("agent.OpenAI") as constructor:
            result = resolve_intent(self.payload)
        self.assertIn("未配置模型 API Key", result["error"])
        self.assertEqual(result["data"], PREVIOUS)
        self.assertEqual(self.payload["trip_data"], PREVIOUS)
        constructor.assert_not_called()

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

    def test_new_poi_returns_add_query_without_an_existing_block_target(self):
        client = self.client([{"action": "add", "query": " 灵隐寺 "}])
        payload = {**self.payload, "has_plan": True, "messages": [{"role": "user", "content": "我想去灵隐寺"}]}
        result = resolve_intent(payload, client)
        self.assertEqual(result, {"action": "add", "query": "灵隐寺"})
        self.assertNotIn("targets", result)
        self.assertNotIn("day", result)
        self.assertEqual(payload["trip_data"], PREVIOUS)
        self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_new_poi_can_carry_an_explicit_day_for_the_add_question(self):
        expected = {"action": "add", "query": "雷峰塔", "day": 2}
        client = self.client([expected])
        payload = {**self.payload, "has_plan": True, "messages": [{"role": "user", "content": "第二天再加一个雷峰塔"}]}
        self.assertEqual(resolve_intent(payload, client), expected)
        self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_invalid_add_query_is_corrected_before_returning_an_add_action(self):
        valid = {"action": "add", "query": "灵隐寺"}
        for query in (None, "", "   ", [], 42):
            with self.subTest(query=query):
                client = self.client([{"action": "add", "query": query}, valid])
                result = resolve_intent({**self.payload, "has_plan": True}, client)
                self.assertEqual(result, valid)
                self.assertEqual(client.chat.completions.create.call_count, 2)

    def test_invalid_add_day_is_not_coerced_to_a_chosen_day(self):
        valid = {"action": "add", "query": "灵隐寺"}
        for day in (0, -1, True, 1.5, "2", None):
            with self.subTest(day=day):
                client = self.client([{"action": "add", "query": "灵隐寺", "day": day}, valid])
                result = resolve_intent({**self.payload, "has_plan": True}, client)
                self.assertEqual(result, valid)
                self.assertEqual(client.chat.completions.create.call_count, 2)

    def test_add_without_a_plan_is_corrected_to_trip_information_collection(self):
        client = self.client([
            {"action": "add", "query": "灵隐寺"},
            {"action": "collect", "data": {"requested_pois": ["灵隐寺"]}},
        ])
        payload = {**self.payload, "has_plan": False, "messages": [{"role": "user", "content": "我想去灵隐寺"}]}
        result = resolve_intent(payload, client)
        self.assertEqual(result["action"], "confirm_trip")
        self.assertEqual(result["data"]["requested_pois"], ["灵隐寺"])
        self.assertEqual(result["data"]["destination"], PREVIOUS["destination"])
        self.assertEqual(client.chat.completions.create.call_count, 2)

    def test_add_schema_correction_keeps_the_original_plan_context(self):
        client = self.client([{"action": "add", "query": []}, {"action": "add", "query": "灵隐寺"}])
        payload = {**self.payload, "has_plan": True, "messages": [{"role": "user", "content": "我想去灵隐寺"}]}
        self.assertEqual(resolve_intent(payload, client), {"action": "add", "query": "灵隐寺"})
        retry = client.chat.completions.create.call_args_list[1].kwargs["messages"]
        original = json.loads(retry[1]["content"])
        self.assertTrue(original["has_plan"])
        self.assertEqual(original["messages"], payload["messages"])
        self.assertEqual(original["trip_data"], PREVIOUS)
        self.assertIn("explain/add/confirm/modify", retry[-1]["content"])

    def test_replacement_delete_and_explanation_keep_their_existing_actions(self):
        cases = [
            ("把西湖换成灵隐寺", {"action": "confirm", "mode": "block", "targets": ["西湖"], "instruction": "把西湖换成灵隐寺"}),
            ("删除灵隐寺", {"action": "confirm", "mode": "block", "targets": ["灵隐寺"], "instruction": "删除灵隐寺"}),
            ("灵隐寺怎么样", {"action": "explain", "answer": "灵隐寺是一座位于杭州的寺院。", "query": "灵隐寺"}),
        ]
        for content, expected in cases:
            with self.subTest(content=content):
                client = self.client([expected])
                payload = {**self.payload, "has_plan": True, "messages": [{"role": "user", "content": content}]}
                self.assertEqual(resolve_intent(payload, client), expected)
                self.assertEqual(client.chat.completions.create.call_count, 1)

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
