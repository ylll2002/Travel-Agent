"""仅验证缺少模型 key 的 CLI 反馈，以及确定性行程更新保持可用。"""

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import plan


class MissingAPIKeyTests(unittest.TestCase):
    def run_cli(self, payload):
        output = io.StringIO()
        with patch("plan.sys.stdin", io.StringIO(json.dumps(payload))), redirect_stdout(output):
            plan.main()
        return json.loads(output.getvalue())

    def test_missing_key_blocks_model_operations_with_configuration_message(self):
        payloads = [
            {"search": {}, "basic": {}},
            {"classify": True, "instruction": "换一个景点", "blocks": []},
            {"blocks": [], "instruction": "换一家餐厅"},
            {"audit_repair": True, "plan": {}},
        ]
        for key in (None, "", "   ", "sk-your-key-here"):
            for payload in payloads:
                with self.subTest(key=key, payload=payload), patch.dict("plan.os.environ", {} if key is None else {"OPENAI_API_KEY": key}, clear=True), patch("plan.OpenAI") as client:
                    result = self.run_cli(payload)
                self.assertIn("未配置模型 API Key", result["error"])
                client.assert_not_called()

    def test_finalization_does_not_require_a_model_key(self):
        snapshot = {"blocks": [], "revision": 3}
        with patch.dict("plan.os.environ", {"OPENAI_API_KEY": ""}), patch("plan.finalize_plan", return_value=snapshot) as finalize:
            result = self.run_cli({"finalize": True, "plan": snapshot})
        self.assertEqual(result, snapshot)
        finalize.assert_called_once()


if __name__ == "__main__":
    unittest.main()
