"""Regression checks. Run: python -m unittest discover -s tests -v"""
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_import_data = tempfile.TemporaryDirectory(prefix="robo-test-")
os.environ["LINGSHU_DATA_DIR"] = _import_data.name
os.environ["DEEPSEEK_API_KEY"] = ""

from fastapi.testclient import TestClient
import api_server as server
import skills
import vita_crew


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="robo-case-")
        root = Path(self.temp.name)
        self.patches = [
            patch.object(server, "PUBLIC_MODE", True),
            patch.object(server, "PERSONA_FILE", str(root / "persona.txt")),
            patch.object(server, "HISTORY_FILE", str(root / "history.md")),
            patch.object(server, "_maybe_auto_sync"),
            patch.object(server, "_maybe_summarize_history"),
            patch.object(server, "robo_analyse", return_value=None),
            patch.object(server, "dispatch_response_emoji"),
            patch.dict(os.environ, {"LINGSHU_PUBLIC_MODE": "1"}),
        ]
        for item in self.patches:
            item.start()
        server._public_sessions.clear()
        server._public_requests.clear()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_persona_empty_and_reset_use_original_default(self):
        with patch.object(server, "PUBLIC_MODE", False), TestClient(server.app) as client:
            self.assertTrue(client.get("/persona").json()["default"])
            self.assertTrue(client.post("/persona", json={"text": "custom"}).json()["ok"])
            self.assertEqual(client.get("/persona").json()["text"], "custom")
            client.post("/persona", json={"text": "  "})
            self.assertEqual(client.get("/persona").json()["text"], server.DEFAULT_PERSONA)
            Path(server.PERSONA_FILE).write_text("", encoding="utf-8")
            self.assertEqual(server.get_persona(), server.DEFAULT_PERSONA)

    def test_websocket_heartbeat_does_not_call_model_or_write_history(self):
        with TestClient(server.app) as client, patch.object(server, "run_chat_logic") as model:
            with client.websocket_connect("/ws") as ws:
                ws.send_text("ping")
                self.assertEqual(ws.receive_json(), {"type": "pong"})
                ws.send_json({"type": "ping"})
                self.assertEqual(ws.receive_json(), {"type": "pong"})
                ws.send_json([])
                self.assertEqual(ws.receive_json()["type"], "error")
            model.assert_not_called()
            self.assertFalse(Path(server.HISTORY_FILE).exists())

    def test_public_visitors_have_separate_context_and_can_reset(self):
        seen = []
        def model(text, messages):
            seen.append([item["content"] for item in messages])
            return "reply:" + text
        with patch.object(server, "fast_ollama_response", side_effect=model), \
                TestClient(server.app) as first, TestClient(server.app) as second:
            result = first.post("/chat", json={"message": "first-private"})
            self.assertIn("HttpOnly", result.headers["set-cookie"])
            second.post("/chat", json={"message": "second-private"})
            first.post("/chat", json={"message": "follow-up"})
            self.assertEqual(seen[0], [])
            self.assertEqual(seen[1], [])
            self.assertIn("first-private", seen[2])
            self.assertNotIn("second-private", seen[2])
            first.post("/chat/reset")
            first.post("/chat", json={"message": "new-conversation"})
            self.assertEqual(seen[3], [])
            self.assertFalse(Path(server.HISTORY_FILE).exists())

    def test_public_mode_blocks_management_routes_and_model_hardware_tools(self):
        with TestClient(server.app) as client:
            for method, path, body in [
                ("GET", "/sysinfo", None), ("GET", "/memory/status", None),
                ("POST", "/persona", {"text": "overwrite"}),
                ("POST", "/terminal/exec", {"command": "pwd"}),
                ("POST", "/robot/connect", None),
            ]:
                self.assertEqual(client.request(method, path, json=body).status_code, 403, path)
        with patch.object(vita_crew, "execute_tool") as execute, \
                patch.object(vita_crew, "ALLOW_RISKY_TOOLS", True):
            vita_crew.execute_tool_safely("robot_connect", {})
            vita_crew.execute_tool_safely("sysinfo", {})
            execute.assert_not_called()

    def test_deep_mode_uses_reasoning_pipeline(self):
        result = SimpleNamespace(raw="deep reply", thinking_log="done")
        with patch.object(server, "create_vita_crew", return_value=result) as deep, \
                TestClient(server.app) as client:
            response = client.post("/chat", json={"message": "hello", "mode": "deep"})
            self.assertEqual(response.json()["response"], "deep reply")
            self.assertTrue(deep.call_args.kwargs["force_deep"])

    def test_health_remains_responsive_during_model_request(self):
        entered, release = threading.Event(), threading.Event()
        def model(*args, **kwargs):
            entered.set()
            release.wait(3)
            return "reply"
        with patch.object(server, "fast_ollama_response", side_effect=model), TestClient(server.app) as client, \
                ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(client.post, "/chat", json={"message": "hello"})
            self.assertTrue(entered.wait(2))
            try:
                started = time.monotonic()
                self.assertEqual(client.get("/health").status_code, 200)
                self.assertLess(time.monotonic() - started, 0.75)
            finally:
                release.set()
            self.assertEqual(future.result(timeout=2).status_code, 200)

    def test_validation_and_public_rate_limit(self):
        with TestClient(server.app) as client:
            self.assertEqual(client.post("/chat", json={"message": " "}).status_code, 400)
            self.assertEqual(client.post("/chat", json={"message": "a" * 8001}).status_code, 400)
            self.assertEqual(client.post("/chat", json={"message": "a", "mode": "invalid"}).status_code, 422)
            self.assertEqual(client.post("/speak", json={"text": "a" * 1501}).status_code, 400)
            for _ in range(30):
                client.get("/speak")
            response = client.get("/speak")
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response.headers["retry-after"], "60")

    def test_summary_preserves_turns_appended_during_model_call(self):
        history = Path(server.HISTORY_FILE)
        history.write_text("x" * 6000, encoding="utf-8")
        def summarize(*args, **kwargs):
            with history.open("a", encoding="utf-8") as stream:
                stream.write("\nNEW_TURN_MUST_SURVIVE")
            return "This is a valid summary of older conversation."
        with patch.object(server, "fast_ollama_response", side_effect=summarize):
            server._do_summarize(history)
        self.assertIn("NEW_TURN_MUST_SURVIVE", history.read_text(encoding="utf-8"))

    def test_calculator_allows_math_and_rejects_python_access(self):
        self.assertEqual(skills.calculator("12345 * 6789"), str(12345 * 6789))
        self.assertEqual(skills.calculator("sqrt(81) + sum([1,2,3])"), "15.0")
        for expression in ["().__class__.__bases__", "__import__('os')", "9 ** 999999999", "factorial(100000)"]:
            self.assertIn("计算失败", skills.calculator(expression))


if __name__ == "__main__":
    unittest.main()
