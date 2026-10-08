import json
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import search as search_routes


class PoiKeywordRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app = FastAPI()
        app.include_router(search_routes.router, prefix="/api")
        cls.client = TestClient(app)

    def test_city_keyword_search_returns_provider_candidate(self):
        candidate = {
            "name": "灵隐寺",
            "longitude": 120.101,
            "latitude": 30.243,
            "url": "https://example.test/poi",
            "district": "西湖区",
        }
        proc = SimpleNamespace(returncode=0, stdout=json.dumps({"poi": [candidate]}), stderr="")
        with patch.object(search_routes.subprocess, "run", return_value=proc) as run:
            response = self.client.post("/api/search/poi", json={"destination": " 杭州 ", "keyword": " 灵隐寺 "})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"poi": [candidate]})
        command = run.call_args.args[0]
        self.assertEqual(command[-4:], ["--poi-keyword", "灵隐寺", "--city", "杭州"])
        self.assertEqual(run.call_args.kwargs["timeout"], 60)

    def test_empty_search_is_valid_result(self):
        proc = SimpleNamespace(returncode=0, stdout='{"poi": []}', stderr="")
        with patch.object(search_routes.subprocess, "run", return_value=proc):
            response = self.client.post("/api/search/poi", json={"destination": "杭州", "keyword": "不存在的地点"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"poi": []})

    def test_blank_city_or_keyword_does_not_start_search(self):
        with patch.object(search_routes.subprocess, "run") as run:
            for data in ({"destination": "  ", "keyword": "灵隐寺"}, {"destination": "杭州", "keyword": "  "}):
                with self.subTest(data=data):
                    response = self.client.post("/api/search/poi", json=data)
                    self.assertEqual(response.status_code, 400)
                    self.assertIn("目的地和景点名称", response.json()["detail"])
        run.assert_not_called()

    def test_provider_failure_or_bad_output_is_safe(self):
        cases = [
            SimpleNamespace(returncode=1, stdout="", stderr="secret-token"),
            SimpleNamespace(returncode=0, stdout='{"error":"secret-token"}', stderr=""),
            SimpleNamespace(returncode=0, stdout="secret-token", stderr=""),
            SimpleNamespace(returncode=0, stdout='{"poi": {"name":"灵隐寺"}}', stderr=""),
        ]
        for proc in cases:
            with self.subTest(proc=proc), patch.object(search_routes.subprocess, "run", return_value=proc):
                response = self.client.post("/api/search/poi", json={"destination": "杭州", "keyword": "灵隐寺"})
                self.assertEqual(response.status_code, 502)
                self.assertNotIn("secret-token", response.text)

    def test_timeout_is_retryable(self):
        with patch.object(search_routes.subprocess, "run", side_effect=subprocess.TimeoutExpired("search", 60)):
            response = self.client.post("/api/search/poi", json={"destination": "杭州", "keyword": "灵隐寺"})
        self.assertEqual(response.status_code, 504)
        self.assertIn("超时", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
