import os
import shutil
import tempfile
import unittest

from hub.config import load_config
from hub.engine import Engine

TINY_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="lah-tests-")
        cls._old_data = os.environ.get("LOCAL_AI_HUB_DATA")
        os.environ["LOCAL_AI_HUB_DATA"] = cls._tmp
        cls.engine = Engine(load_config({"ollama": {"mock": True}}))

    @classmethod
    def tearDownClass(cls):
        cls.engine.shutdown()
        if cls._old_data is None:
            os.environ.pop("LOCAL_AI_HUB_DATA", None)
        else:
            os.environ["LOCAL_AI_HUB_DATA"] = cls._old_data
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def task(self, plugin, payload, **extra):
        message = {"type": "task", "plugin": plugin, "input": payload}
        message.update(extra)
        return self.engine.handle(message)

    # ------------------------------------------------------------------
    def test_ping_and_status(self):
        pong = self.engine.handle({"type": "ping"})
        self.assertTrue(pong["ok"])
        status = self.engine.handle({"type": "status"})
        self.assertTrue(status["ok"])
        self.assertEqual(status["output"]["models"]["backend"], "mock")
        self.assertGreaterEqual(status["output"]["plugins"]["count"], 4)

    def test_unknown_message_type(self):
        result = self.engine.handle({"type": "does.not.exist"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "unknown_type")

    def test_python_plugin(self):
        result = self.task("hello.python", {"name": "Unit"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["output"]["greeting"], "Hello, Unit!")
        self.assertEqual(result["output"]["runtime"], "python")

    def test_schema_defaults_reach_plugin(self):
        result = self.task("hello.python", {})
        self.assertEqual(result["output"]["received"]["name"], "world")

    def test_javascript_plugin(self):
        if not shutil.which("node"):
            self.skipTest("node not installed")
        result = self.task("hello.javascript", {"name": "Node"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["output"]["runtime"], "javascript")

    def test_llm_bridge_roundtrip(self):
        result = self.task("hello.python", {"name": "Model", "use_llm": True})
        self.assertTrue(result["ok"], result)
        self.assertIn("model_text", result["output"])
        self.assertTrue(result["meta"]["models"])

    def test_comments_classification(self):
        result = self.task(
            "comments.classify",
            {"comments": [{"id": "c0", "text": "hello"}, {"id": "c1", "text": "buy now"}]},
            context={"domain": "example-board.com", "interface": "test"},
        )
        self.assertTrue(result["ok"], result)
        results = result["output"]["results"]
        self.assertEqual([entry["id"] for entry in results], ["c0", "c1"])
        allowed = set(result["output"]["summary"]["labels"]) | {"unknown"}
        self.assertTrue(all(entry["label"] in allowed for entry in results))

    def test_comments_empty_input(self):
        result = self.task("comments.classify", {"comments": []})
        self.assertTrue(result["ok"])
        self.assertEqual(result["output"]["results"], [])

    def test_images_tagging_and_missing_pixels(self):
        result = self.task(
            "images.tag",
            {"images": [{"id": "i0", "data_base64": TINY_PNG}, {"id": "i1"}]},
        )
        self.assertTrue(result["ok"], result)
        entries = {entry["id"]: entry for entry in result["output"]["results"]}
        self.assertTrue(entries["i0"]["tags"])
        self.assertIn("no_image_data", entries["i1"]["error"])

    def test_invalid_input_rejected(self):
        result = self.task("comments.classify", {"comments": "not-an-array"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid_input")

    def test_unknown_plugin(self):
        result = self.task("nope.nope", {})
        self.assertEqual(result["error"]["code"], "unknown_plugin")

    def test_domain_disable_blocks_task(self):
        result = self.task(
            "hello.python",
            {"name": "x"},
            context={
                "domain": "example-board.com",
                "domain_config": {"features": {"hello.python": {"enabled": False}}},
            },
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "plugin_disabled")

    def test_domain_options_merge(self):
        result = self.task(
            "hello.python",
            {"name": "Cfg"},
            context={
                "domain": "example-board.com",
                "domain_config": {"features": {"hello.python": {"enabled": True, "options": {"greeting_template": "Yo {name}"}}}},
            },
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["output"]["greeting"], "Yo Cfg")

    def test_request_options_override_domain(self):
        result = self.task(
            "hello.python",
            {"name": "Cfg"},
            options={"greeting_template": "Hi {name}!"},
            context={
                "domain": "example-board.com",
                "domain_config": {"features": {"hello.python": {"options": {"greeting_template": "Yo {name}"}}}},
            },
        )
        self.assertEqual(result["output"]["greeting"], "Hi Cfg!")

    def test_oversized_input_rejected(self):
        engine = Engine(load_config({"ollama": {"mock": True}, "security": {"max_input_bytes": 512}}))
        try:
            result = engine.handle(
                {"type": "task", "plugin": "hello.python", "input": {"name": "x" * 2000}}
            )
            self.assertEqual(result["error"]["code"], "input_too_large")
        finally:
            engine.shutdown()

    def test_empty_labels_fall_back_to_config(self):
        result = self.task("comments.classify", {"comments": [{"id": "c", "text": "hi"}], "labels": []})
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["output"]["summary"]["labels"])

    def _temp_plugin(self, directory, body, **manifest_extra):
        import json
        from pathlib import Path

        path = Path(directory) / "test.plugin"
        path.mkdir(parents=True, exist_ok=True)
        manifest = {
            "id": "test.plugin",
            "name": "Test plugin",
            "version": "1.0.0",
            "runtime": {"type": "python", "entry": "plugin.py"},
        }
        manifest.update(manifest_extra)
        (path / "manifest.json").write_text(json.dumps(manifest))
        (path / "plugin.py").write_text(body)
        return path.parent

    def test_plugin_crash_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._temp_plugin(tmp, "raise RuntimeError('boom')\n")
            engine = Engine(load_config({"ollama": {"mock": True}, "plugins": {"paths": [str(root)]}}))
            try:
                result = engine.handle({"type": "task", "plugin": "test.plugin", "input": {}})
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "plugin_failed")
            finally:
                engine.shutdown()

    def test_plugin_timeout_is_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._temp_plugin(tmp, "import time\ntime.sleep(30)\n", timeout_seconds=1)
            engine = Engine(load_config({"ollama": {"mock": True}, "plugins": {"paths": [str(root)]}}))
            try:
                result = engine.handle({"type": "task", "plugin": "test.plugin", "input": {}})
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "plugin_timeout")
            finally:
                engine.shutdown()


if __name__ == "__main__":
    unittest.main()
