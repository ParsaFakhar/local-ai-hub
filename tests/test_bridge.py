"""Userscript-bridge policy and engine-side enforcement.

The content script mirrors these rules; the engine re-checks them so a
bypassed or buggy content script still cannot open the door.
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from hub import bridge
from hub.config import load_config
from hub.engine import Engine


class PolicyTests(unittest.TestCase):
    def config(self, **bridge_options):
        return load_config({"bridge": bridge_options}) if bridge_options else load_config()

    def test_no_opt_in_means_disabled(self):
        resolved = bridge.policy({"domain": "x.test", "features": {"a.b": {"enabled": True}}})
        self.assertFalse(resolved["enabled"])
        self.assertEqual(bridge.denial(resolved, "a.b")[0], "forbidden")

    def test_opt_in_exposes_enabled_features(self):
        resolved = bridge.policy(
            {
                "domain": "x.test",
                "bridge": {"tampermonkey": True},
                "features": {"a.b": {"enabled": True}, "c.d": {"enabled": False}},
            }
        )
        self.assertTrue(resolved["enabled"])
        self.assertEqual(resolved["plugins"], ["a.b"])
        self.assertIsNone(bridge.denial(resolved, "a.b"))
        self.assertEqual(bridge.denial(resolved, "c.d")[0], "forbidden")

    def test_explicit_plugin_list_wins(self):
        resolved = bridge.policy(
            {
                "bridge": {"tampermonkey": True, "plugins": ["hello.python"]},
                "features": {"a.b": {"enabled": True}},
            }
        )
        self.assertEqual(resolved["plugins"], ["hello.python"])

    def test_master_switch_overrides_the_domain(self):
        resolved = bridge.policy(
            {"bridge": {"tampermonkey": True}, "features": {"a.b": {"enabled": True}}},
            self.config(enabled=False),
        )
        self.assertTrue(resolved["opted_in"])
        self.assertFalse(resolved["enabled"])

    def test_apply_requires_both_levels(self):
        domain = {"bridge": {"tampermonkey": True, "allow_apply": True}, "features": {}}
        self.assertTrue(bridge.policy(domain, self.config())["allow_apply"])
        self.assertFalse(bridge.policy(domain, self.config(allow_apply=False))["allow_apply"])
        self.assertFalse(bridge.policy({"bridge": {"tampermonkey": True}})["allow_apply"])

    def test_limits_fall_back_to_hub_defaults(self):
        resolved = bridge.policy({"bridge": {"tampermonkey": True}}, self.config(max_requests_per_minute=7))
        self.assertEqual(resolved["max_requests_per_minute"], 7)
        resolved = bridge.policy(
            {
                "bridge": {
                    "tampermonkey": True,
                    "plugins": ["a.b"],
                    "max_requests_per_minute": 3,
                    "max_input_bytes": 10,
                }
            },
            self.config(max_requests_per_minute=7),
        )
        self.assertEqual(resolved["max_requests_per_minute"], 3)
        self.assertEqual(resolved["max_input_bytes"], 10)
        self.assertEqual(bridge.denial(resolved, "a.b", 20)[0], "input_too_large")
        self.assertIsNone(bridge.denial(resolved, "a.b", 10))

    def test_denial_precedence_matches_the_content_script(self):
        """enabled -> plugin allowlist -> input size (same order as bridge.js)."""
        domain = {"bridge": {"tampermonkey": True, "plugins": ["a.b"], "max_input_bytes": 10}}
        resolved = bridge.policy(domain)
        self.assertEqual(bridge.denial(resolved, "other.plugin", 999)[0], "forbidden")
        self.assertEqual(bridge.denial(resolved, "a.b", 999)[0], "input_too_large")
        closed = bridge.policy({"bridge": {"tampermonkey": False, "plugins": ["a.b"]}})
        self.assertEqual(bridge.denial(closed, "a.b", 999)[0], "forbidden")

    def test_nonsense_limits_do_not_disable_the_cap(self):
        resolved = bridge.policy(
            {"bridge": {"tampermonkey": True, "max_input_bytes": "lots", "max_requests_per_minute": -5}}
        )
        self.assertEqual(resolved["max_input_bytes"], bridge.DEFAULT_MAX_INPUT_BYTES)
        self.assertEqual(resolved["max_requests_per_minute"], bridge.DEFAULT_MAX_REQUESTS_PER_MINUTE)

    def test_malformed_domain_config_is_simply_closed(self):
        for value in (None, {}, {"bridge": "yes"}, {"bridge": {"tampermonkey": "yes"}}):
            resolved = bridge.policy(value)
            # "yes" is truthy but the domain file is the user's own config; the
            # important part is that nothing crashes and no plugin is exposed.
            self.assertEqual(resolved["plugins"], [])


class EngineBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="lah-bridge-")
        cls._old_data = os.environ.get("LOCAL_AI_HUB_DATA")
        os.environ["LOCAL_AI_HUB_DATA"] = cls._tmp
        domains = Path(cls._tmp) / "config" / "domains"
        domains.mkdir(parents=True, exist_ok=True)
        (domains / "open.test.json").write_text(
            json.dumps(
                {
                    "domain": "open.test",
                    "bridge": {"tampermonkey": True, "plugins": ["hello.python"], "max_input_bytes": 512},
                    "features": {"hello.python": {"enabled": True}},
                }
            ),
            encoding="utf-8",
        )
        (domains / "closed.test.json").write_text(
            json.dumps({"domain": "closed.test", "features": {"hello.python": {"enabled": True}}}),
            encoding="utf-8",
        )

    @classmethod
    def tearDownClass(cls):
        if cls._old_data is None:
            os.environ.pop("LOCAL_AI_HUB_DATA", None)
        else:
            os.environ["LOCAL_AI_HUB_DATA"] = cls._old_data
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def engine(self, **extra):
        config = {"ollama": {"mock": True}, "tasks": {"audit_log": False}}
        config.update(extra)
        return Engine(load_config(config))

    def run_task(self, engine, domain, interface="tampermonkey", **extra):
        message = {
            "type": "task",
            "plugin": "hello.python",
            "input": {"name": "bridge"},
            "context": {"domain": domain, "interface": interface},
        }
        message.update(extra)
        return engine.handle(message)

    def test_opted_in_domain_accepts_page_tasks(self):
        engine = self.engine()
        try:
            result = self.run_task(engine, "open.test")
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["output"]["greeting"], "Hello, bridge!")
        finally:
            engine.shutdown()

    def test_domain_without_opt_in_is_refused(self):
        engine = self.engine()
        try:
            result = self.run_task(engine, "closed.test")
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "forbidden")
        finally:
            engine.shutdown()

    def test_plugin_outside_the_allowlist_is_refused(self):
        engine = self.engine()
        try:
            result = self.run_task(engine, "open.test", plugin="comments.classify", input={"comments": []})
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "forbidden")
        finally:
            engine.shutdown()

    def test_page_cannot_opt_itself_in(self):
        engine = self.engine()
        try:
            result = self.run_task(
                engine,
                "closed.test",
                context={
                    "domain": "closed.test",
                    "interface": "tampermonkey",
                    "domain_config": {"bridge": {"tampermonkey": True, "plugins": ["hello.python"]}},
                },
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "forbidden")
        finally:
            engine.shutdown()

    def test_bridge_input_cap_is_enforced(self):
        engine = self.engine()
        try:
            result = self.run_task(engine, "open.test", input={"name": "x" * 2000})
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "input_too_large")
        finally:
            engine.shutdown()

    def test_local_interfaces_are_unaffected(self):
        engine = self.engine()
        try:
            for interface in ("firefox", "cli", "http"):
                result = self.run_task(engine, "closed.test", interface=interface)
                self.assertTrue(result["ok"], (interface, result))
        finally:
            engine.shutdown()

    def test_domains_get_publishes_the_resolved_policy(self):
        engine = self.engine()
        try:
            output = engine.handle({"type": "domains.get", "domain": "open.test"})["output"]
            self.assertTrue(output["bridge_policy"]["enabled"])
            self.assertEqual(output["bridge_policy"]["plugins"], ["hello.python"])
            closed = engine.handle({"type": "domains.get", "domain": "closed.test"})["output"]
            self.assertFalse(closed["bridge_policy"]["enabled"])
        finally:
            engine.shutdown()

    def test_status_lists_bridge_domains(self):
        engine = self.engine()
        try:
            status = engine.handle({"type": "status"})["output"]
            self.assertIn("open.test", status["bridge"]["domains"])
            self.assertNotIn("closed.test", status["bridge"]["domains"])
        finally:
            engine.shutdown()


if __name__ == "__main__":
    unittest.main()
