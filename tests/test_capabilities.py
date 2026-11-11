"""Capability enforcement, configuration authority and unknown-capability reporting."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from hub import capabilities
from hub.config import load_config
from hub.engine import Engine

TINY_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

# A plugin that asks the engine for one model call and reports what happened.
PROBE = """
import json, sys
invoke = json.loads(sys.stdin.readline())
params = json.loads(%r)
print(json.dumps({"type": "llm.request", "id": "1",
                  "method": params.pop("__method", "generate"),
                  "params": params}), flush=True)
reply = json.loads(sys.stdin.readline())
print(json.dumps({"type": "result", "ok": True, "output": {
    "ok": bool(reply.get("ok")),
    "code": (reply.get("error") or {}).get("code"),
    "capabilities": invoke["plugin"].get("capabilities"),
}}), flush=True)
"""


def write_plugin(root: Path, plugin_id: str, caps: list[str], params: dict) -> None:
    directory = root / plugin_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "id": plugin_id,
                "name": plugin_id,
                "version": "1.0.0",
                "runtime": {"type": "python", "entry": "plugin.py"},
                "capabilities": caps,
                "input": {"type": "object"},
                "output": {"type": "object"},
            }
        ),
        encoding="utf-8",
    )
    (directory / "plugin.py").write_text(PROBE % json.dumps(params), encoding="utf-8")


class DenialRuleTests(unittest.TestCase):
    """The pure decision function."""

    def test_no_capabilities_denies_everything(self):
        self.assertIsNotNone(capabilities.denial([], "generate", {"prompt": "x"}))

    def test_generate_allowed(self):
        self.assertIsNone(capabilities.denial(["llm.generate"], "generate", {"prompt": "x"}))

    def test_chat_allowed_by_chat_capability(self):
        self.assertIsNone(capabilities.denial(["llm.chat"], "chat", {"messages": []}))

    def test_images_require_vision(self):
        reason = capabilities.denial(["llm.generate"], "generate", {"images": [TINY_PNG]})
        self.assertIn("llm.vision", reason)

    def test_vision_implies_model_access(self):
        self.assertIsNone(
            capabilities.denial(["llm.vision"], "generate", {"images": [TINY_PNG]})
        )

    def test_format_requires_structured(self):
        reason = capabilities.denial(["llm.generate"], "generate", {"format": "json"})
        self.assertIn("llm.structured", reason)

    def test_structured_allowed(self):
        self.assertIsNone(
            capabilities.denial(
                ["llm.generate", "llm.structured"], "generate", {"format": {"type": "object"}}
            )
        )

    def test_chat_images_require_vision(self):
        reason = capabilities.denial(
            ["llm.chat"], "chat", {"messages": [{"role": "user", "images": [TINY_PNG]}]}
        )
        self.assertIn("llm.vision", reason)

    def test_unknown_names_reported(self):
        self.assertEqual(
            capabilities.unknown(["llm.generate", "net.fetch", "fs.write"]),
            ["fs.write", "net.fetch"],
        )


class EnforcementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="lah-caps-")
        cls._old_data = os.environ.get("LOCAL_AI_HUB_DATA")
        os.environ["LOCAL_AI_HUB_DATA"] = cls._tmp
        cls.root = Path(cls._tmp) / "plugins"
        write_plugin(cls.root, "silent.plugin", [], {"prompt": "hi"})
        write_plugin(cls.root, "text.plugin", ["llm.generate"], {"prompt": "hi"})
        write_plugin(
            cls.root, "sneaky.vision", ["llm.generate"], {"prompt": "hi", "images": [TINY_PNG]}
        )
        write_plugin(
            cls.root, "sneaky.schema", ["llm.generate"], {"prompt": "hi", "format": "json"}
        )
        cls.config = {
            "ollama": {"mock": True},
            "plugins": {"paths": [str(cls.root)]},
            "tasks": {"audit_log": False},
        }
        cls.engine = Engine(load_config(cls.config))

    @classmethod
    def tearDownClass(cls):
        cls.engine.shutdown()
        if cls._old_data is None:
            os.environ.pop("LOCAL_AI_HUB_DATA", None)
        else:
            os.environ["LOCAL_AI_HUB_DATA"] = cls._old_data
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def probe(self, plugin_id):
        result = self.engine.handle({"type": "task", "plugin": plugin_id, "input": {}})
        self.assertTrue(result["ok"], result)
        return result["output"]

    def test_undeclared_capability_is_denied_at_runtime(self):
        output = self.probe("silent.plugin")
        self.assertFalse(output["ok"])
        self.assertEqual(output["code"], "capability_denied")

    def test_declared_capability_is_allowed(self):
        output = self.probe("text.plugin")
        self.assertTrue(output["ok"])
        self.assertEqual(output["capabilities"], ["llm.generate"])

    def test_images_denied_without_vision(self):
        self.assertEqual(self.probe("sneaky.vision")["code"], "capability_denied")

    def test_structured_output_denied_without_capability(self):
        self.assertEqual(self.probe("sneaky.schema")["code"], "capability_denied")

    def test_enforcement_can_be_disabled(self):
        config = dict(self.config)
        config["security"] = {"enforce_capabilities": False}
        engine = Engine(load_config(config))
        try:
            result = engine.handle({"type": "task", "plugin": "silent.plugin", "input": {}})
            self.assertTrue(result["output"]["ok"])
        finally:
            engine.shutdown()

    def test_bundled_plugin_capabilities_still_sufficient(self):
        engine = Engine(load_config({"ollama": {"mock": True}, "tasks": {"audit_log": False}}))
        try:
            comments = json.loads(
                (Path(__file__).resolve().parent.parent / "examples" / "comments.json").read_text()
            )
            result = engine.handle(
                {"type": "task", "plugin": "comments.classify", "input": comments}
            )
            self.assertTrue(result["ok"], result)
            images = json.loads(
                (Path(__file__).resolve().parent.parent / "examples" / "images.json").read_text()
            )
            result = engine.handle({"type": "task", "plugin": "images.tag", "input": images})
            self.assertTrue(result["ok"], result)
        finally:
            engine.shutdown()


class ConfigAuthorityTests(unittest.TestCase):
    """security.allow_page_supplied_config governs page-facing interfaces."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="lah-authority-")
        cls._old_data = os.environ.get("LOCAL_AI_HUB_DATA")
        os.environ["LOCAL_AI_HUB_DATA"] = cls._tmp

    @classmethod
    def tearDownClass(cls):
        if cls._old_data is None:
            os.environ.pop("LOCAL_AI_HUB_DATA", None)
        else:
            os.environ["LOCAL_AI_HUB_DATA"] = cls._old_data
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def engine(self, **security):
        return Engine(
            load_config(
                {
                    "ollama": {"mock": True},
                    "tasks": {"audit_log": False},
                    "security": security,
                }
            )
        )

    def run_hello(self, engine, interface, overrides=True):
        context = {"domain": "example-board.com", "interface": interface}
        if overrides:
            context["domain_config"] = {
                "features": {
                    "hello.python": {"options": {"greeting_template": "PWNED {name}"}}
                }
            }
        return engine.handle(
            {
                "type": "task",
                "plugin": "hello.python",
                "input": {"name": "x"},
                "options": {"greeting_template": "OPTIONS {name}"} if overrides else {},
                "context": context,
            }
        )

    def test_page_interface_cannot_inject_config(self):
        engine = self.engine()
        try:
            result = self.run_hello(engine, "tampermonkey")
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["output"]["greeting"], "Hello, x!")
        finally:
            engine.shutdown()

    def test_page_interface_cannot_disable_or_enable_features(self):
        engine = self.engine()
        try:
            result = engine.handle(
                {
                    "type": "task",
                    "plugin": "hello.python",
                    "input": {"name": "x"},
                    "context": {
                        "domain": "example-board.com",
                        "interface": "userscript",
                        "domain_config": {
                            "features": {"hello.python": {"enabled": False}}
                        },
                    },
                }
            )
            # The page must not be able to flip the engine's feature gate either way.
            self.assertTrue(result["ok"], result)
        finally:
            engine.shutdown()

    def test_local_interface_keeps_overrides(self):
        engine = self.engine()
        try:
            result = self.run_hello(engine, "firefox")
            self.assertEqual(result["output"]["greeting"], "OPTIONS x")
        finally:
            engine.shutdown()

    def test_opt_in_allows_page_config(self):
        engine = self.engine(allow_page_supplied_config=True)
        try:
            result = self.run_hello(engine, "tampermonkey")
            self.assertEqual(result["output"]["greeting"], "OPTIONS x")
        finally:
            engine.shutdown()

    def test_status_reports_the_settings(self):
        engine = self.engine()
        try:
            status = engine.handle({"type": "status"})["output"]
            self.assertTrue(status["security"]["enforce_capabilities"])
            self.assertFalse(status["security"]["allow_page_supplied_config"])
        finally:
            engine.shutdown()


if __name__ == "__main__":
    unittest.main()