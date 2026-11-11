"""Task registry: cancellation, history, counters and the audit log."""

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from hub.config import load_config
from hub.engine import Engine
from hub.tasks import TaskRegistry, input_hash

SLOW_PLUGIN = """
import json, sys, time
json.loads(sys.stdin.readline())
print(json.dumps({"type": "progress", "value": 0.1, "message": "started"}), flush=True)
time.sleep(30)
print(json.dumps({"type": "result", "ok": True, "output": {"finished": True}}), flush=True)
"""

FAST_PLUGIN = """
import json, sys
json.loads(sys.stdin.readline())
print(json.dumps({"type": "result", "ok": True, "output": {"finished": True}}), flush=True)
"""


def write_plugin(root: Path, plugin_id: str, source: str, timeout: int = 60) -> None:
    directory = root / plugin_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "id": plugin_id,
                "version": "1.0.0",
                "runtime": {"type": "python", "entry": "plugin.py"},
                "capabilities": [],
                "timeout_seconds": timeout,
                "input": {"type": "object"},
                "output": {"type": "object"},
            }
        ),
        encoding="utf-8",
    )
    (directory / "plugin.py").write_text(source, encoding="utf-8")


class RegistryTests(unittest.TestCase):
    def test_hash_is_stable_and_order_independent(self):
        self.assertEqual(input_hash({"a": 1, "b": 2}), input_hash({"b": 2, "a": 1}))
        self.assertNotEqual(input_hash({"a": 1}), input_hash({"a": 2}))

    def test_register_finish_and_counters(self):
        registry = TaskRegistry()
        registry.register("t1", plugin="p", domain="d", interface="cli", digest="abc")
        self.assertEqual(len(registry.running()), 1)
        registry.finish("t1", ok=True, duration_ms=5, models=["m"])
        self.assertEqual(registry.running(), [])
        self.assertEqual(registry.counters()["completed"], 1)
        history = registry.history()
        self.assertEqual(history[0]["id"], "t1")
        self.assertEqual(history[0]["models"], ["m"])
        self.assertEqual(history[0]["input_hash"], "abc")

    def test_cancel_unknown_task(self):
        self.assertFalse(TaskRegistry().cancel("nope"))

    def test_cancel_sets_event(self):
        registry = TaskRegistry()
        event = registry.register("t1", plugin="p")
        self.assertFalse(event.is_set())
        self.assertTrue(registry.cancel("t1"))
        self.assertTrue(event.is_set())
        self.assertTrue(registry.running()[0]["cancelling"])
        registry.finish("t1", ok=False)
        self.assertEqual(registry.counters()["cancelled"], 1)

    def test_cancel_all(self):
        registry = TaskRegistry()
        registry.register("a", plugin="p")
        registry.register("b", plugin="p")
        self.assertEqual(sorted(registry.cancel_all()), ["a", "b"])

    def test_history_is_bounded_newest_first(self):
        registry = TaskRegistry(load_config({"tasks": {"history_size": 3, "audit_log": False}}))
        for index in range(6):
            registry.register(f"t{index}", plugin="p")
            registry.finish(f"t{index}", ok=True)
        history = registry.history()
        self.assertEqual(len(history), 3)
        self.assertEqual([entry["id"] for entry in history], ["t5", "t4", "t3"])


class EngineTaskTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="lah-tasks-")
        cls._old_data = os.environ.get("LOCAL_AI_HUB_DATA")
        os.environ["LOCAL_AI_HUB_DATA"] = cls._tmp
        cls.root = Path(cls._tmp) / "plugins"
        write_plugin(cls.root, "slow.plugin", SLOW_PLUGIN)
        write_plugin(cls.root, "fast.plugin", FAST_PLUGIN)

    @classmethod
    def tearDownClass(cls):
        if cls._old_data is None:
            os.environ.pop("LOCAL_AI_HUB_DATA", None)
        else:
            os.environ["LOCAL_AI_HUB_DATA"] = cls._old_data
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def make_engine(self, **extra):
        config = {
            "ollama": {"mock": True},
            "plugins": {"paths": [str(self.root)]},
            "tasks": {"audit_log": True, "audit_file": "test-audit.jsonl"},
        }
        config.update(extra)
        return Engine(load_config(config))

    def test_history_records_a_completed_task(self):
        engine = self.make_engine()
        try:
            result = engine.handle(
                {"type": "task", "id": "task-hist", "plugin": "fast.plugin", "input": {}}
            )
            self.assertTrue(result["ok"], result)
            listing = engine.handle({"type": "tasks.list"})["output"]
            self.assertEqual(listing["running"], [])
            self.assertEqual(listing["history"][0]["id"], "task-hist")
            self.assertTrue(listing["history"][0]["ok"])
            self.assertIn("input_hash", result["meta"])
        finally:
            engine.shutdown()

    def test_audit_log_is_written_with_hashes_not_content(self):
        engine = self.make_engine()
        try:
            engine.handle(
                {
                    "type": "task",
                    "id": "task-audit",
                    "plugin": "fast.plugin",
                    "input": {"secret": "do-not-log-me"},
                }
            )
            path = Path(self._tmp) / "logs" / "test-audit.jsonl"
            self.assertTrue(path.is_file())
            lines = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
            record = [entry for entry in lines if entry["id"] == "task-audit"][-1]
            self.assertEqual(record["plugin"], "fast.plugin")
            self.assertTrue(record["ok"])
            self.assertEqual(len(record["input_hash"]), 64)
            self.assertNotIn("do-not-log-me", path.read_text())
        finally:
            engine.shutdown()

    def test_running_task_can_be_cancelled(self):
        engine = self.make_engine()
        outcome = {}

        def run():
            outcome["result"] = engine.handle(
                {"type": "task", "id": "task-cancel-me", "plugin": "slow.plugin", "input": {}}
            )

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        try:
            deadline = time.time() + 10
            while time.time() < deadline:
                if engine.handle({"type": "tasks.list"})["output"]["running"]:
                    break
                time.sleep(0.05)
            else:
                self.fail("task never appeared in the running list")

            cancelled = engine.handle({"type": "task.cancel", "task_id": "task-cancel-me"})
            self.assertTrue(cancelled["ok"], cancelled)
            self.assertEqual(cancelled["output"]["cancelled"], ["task-cancel-me"])

            worker.join(timeout=15)
            self.assertFalse(worker.is_alive(), "cancelled task did not return")
            result = outcome["result"]
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "task_cancelled")
            self.assertEqual(engine.tasks.counters()["cancelled"], 1)
        finally:
            engine.tasks.cancel_all()
            worker.join(timeout=10)
            engine.shutdown()

    def test_cancel_unknown_task_reports_it(self):
        engine = self.make_engine()
        try:
            result = engine.handle({"type": "task.cancel", "task_id": "ghost"})
            self.assertTrue(result["ok"])
            self.assertEqual(result["output"]["cancelled"], [])
            self.assertEqual(result["output"]["unknown_task"], "ghost")
        finally:
            engine.shutdown()

    def test_cancel_requires_a_string_id(self):
        engine = self.make_engine()
        try:
            result = engine.handle({"type": "task.cancel", "task_id": 42})
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "bad_request")
        finally:
            engine.shutdown()

    def test_status_exposes_task_counters(self):
        engine = self.make_engine()
        try:
            engine.handle({"type": "task", "plugin": "fast.plugin", "input": {}})
            status = engine.handle({"type": "status"})["output"]
            self.assertGreaterEqual(status["tasks"]["completed"], 1)
            self.assertEqual(status["tasks"]["running"], 0)
        finally:
            engine.shutdown()


if __name__ == "__main__":
    unittest.main()