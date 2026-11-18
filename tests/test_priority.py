"""Task priorities: the intake queue, registry bookkeeping and control types.

Covers `hub/tasks.py:TaskQueue` (ordering, bounds, cancellation of waiting
tasks), the `priority` field end to end through the engine, and the native
host's worker loop pulling from the priority queue.
"""

import importlib.util
import json
import os
import queue
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from hub.config import load_config
from hub.engine import Engine
from hub.tasks import (
    PRIORITY_DEFAULT,
    TaskQueue,
    TaskRegistry,
    normalize_priority,
)

ROOT = Path(__file__).resolve().parent.parent


def load_host_module():
    """Import native/local_ai_hub_host.py without running its main()."""
    os.environ.setdefault("LOCAL_AI_HUB_HOME", str(ROOT))
    spec = importlib.util.spec_from_file_location(
        "lah_native_host", ROOT / "native" / "local_ai_hub_host.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NormalizePriorityTests(unittest.TestCase):
    def test_names(self):
        self.assertEqual(normalize_priority("low"), 0)
        self.assertEqual(normalize_priority("normal"), 5)
        self.assertEqual(normalize_priority("HIGH"), 9)
        self.assertEqual(normalize_priority("  High  "), 9)

    def test_numbers_and_numeric_strings(self):
        self.assertEqual(normalize_priority(0), 0)
        self.assertEqual(normalize_priority(9), 9)
        self.assertEqual(normalize_priority(7.9), 7)
        self.assertEqual(normalize_priority("3"), 3)
        self.assertEqual(normalize_priority("2.5"), 2)

    def test_clamped_to_range(self):
        self.assertEqual(normalize_priority(-4), 0)
        self.assertEqual(normalize_priority(1000), 9)

    def test_garbage_falls_back_to_default(self):
        for value in (None, True, False, "urgent", "", [], {}, object()):
            self.assertEqual(normalize_priority(value), PRIORITY_DEFAULT, repr(value))

    def test_custom_default_is_used_and_clamped(self):
        self.assertEqual(normalize_priority("nonsense", 2), 2)
        self.assertEqual(normalize_priority(None, 42), 9)
        self.assertEqual(normalize_priority(None, -3), 0)


class TaskQueueTests(unittest.TestCase):
    def test_higher_priority_runs_first(self):
        q = TaskQueue()
        q.put({"id": "a", "plugin": "p", "priority": "low"})
        q.put({"id": "b", "plugin": "p"})
        q.put({"id": "c", "plugin": "p", "priority": 9})
        self.assertEqual([q.get(timeout=1)["id"] for _ in range(3)], ["c", "b", "a"])

    def test_fifo_within_one_priority(self):
        q = TaskQueue()
        for index in range(5):
            q.put({"id": f"t{index}", "plugin": "p", "priority": 5})
        q.put({"id": "urgent", "plugin": "p", "priority": 6})
        self.assertEqual(q.get(timeout=1)["id"], "urgent")
        self.assertEqual([q.get(timeout=1)["id"] for _ in range(5)], ["t0", "t1", "t2", "t3", "t4"])

    def test_default_priority_from_constructor(self):
        q = TaskQueue(default_priority="low")
        self.assertEqual(q.default_priority, 0)
        q.put({"id": "a", "plugin": "p"})
        q.put({"id": "b", "plugin": "p", "priority": 1})
        self.assertEqual(q.snapshot()[0]["id"], "b")
        self.assertEqual(q.snapshot()[1]["priority"], 0)

    def test_put_assigns_missing_id(self):
        q = TaskQueue()
        request_id = q.put({"type": "task", "plugin": "p"})
        self.assertTrue(request_id.startswith("task-"))
        self.assertEqual(q.get(timeout=1)["id"], request_id)

    def test_snapshot_is_run_order_without_bodies(self):
        q = TaskQueue()
        q.put({"id": "low", "plugin": "a", "priority": 1, "input": {"secret": "x"}})
        q.put({"id": "high", "plugin": "b", "priority": 8})
        snapshot = q.snapshot()
        self.assertEqual([entry["id"] for entry in snapshot], ["high", "low"])
        self.assertEqual([entry["position"] for entry in snapshot], [1, 2])
        self.assertEqual(snapshot[0]["plugin"], "b")
        self.assertGreaterEqual(snapshot[0]["queued_ms"], 0)
        self.assertNotIn("input", json.dumps(snapshot))

    def test_remove_drops_a_waiting_task(self):
        q = TaskQueue()
        q.put({"id": "a", "plugin": "p", "priority": 9})
        q.put({"id": "b", "plugin": "p"})
        removed = q.remove("a")
        self.assertEqual(removed["id"], "a")
        self.assertIsNone(q.remove("a"))
        self.assertEqual(q.qsize(), 1)
        self.assertEqual([entry["id"] for entry in q.snapshot()], ["b"])
        self.assertEqual(q.get(timeout=1)["id"], "b")

    def test_clear_returns_every_waiting_message(self):
        q = TaskQueue()
        q.put({"id": "a", "plugin": "p"})
        q.put({"id": "b", "plugin": "p"})
        self.assertEqual(sorted(m["id"] for m in q.clear()), ["a", "b"])
        self.assertEqual(q.qsize(), 0)
        self.assertEqual(q.snapshot(), [])
        with self.assertRaises(queue.Empty):
            q.get(timeout=0.01)

    def test_get_times_out_when_empty(self):
        with self.assertRaises(queue.Empty):
            TaskQueue().get(timeout=0.01)

    def test_put_blocks_when_full_then_resumes(self):
        q = TaskQueue(maxsize=1)
        q.put({"id": "a", "plugin": "p"})
        with self.assertRaises(queue.Full):
            q.put({"id": "b", "plugin": "p"}, timeout=0.02)
        self.assertEqual(q.get(timeout=1)["id"], "a")
        q.put({"id": "b", "plugin": "p"}, timeout=1)
        self.assertEqual(q.qsize(), 1)

    def test_remove_frees_a_slot_for_a_blocked_producer(self):
        q = TaskQueue(maxsize=1)
        q.put({"id": "a", "plugin": "p"})
        done = []

        def producer():
            q.put({"id": "b", "plugin": "p"}, timeout=2)
            done.append(True)

        thread = threading.Thread(target=producer, daemon=True)
        thread.start()
        q.remove("a")
        thread.join(timeout=3)
        self.assertEqual(done, [True])
        self.assertEqual(q.get(timeout=1)["id"], "b")

    def test_non_envelope_messages_stay_orderable(self):
        q = TaskQueue()
        q.put(["not", "an", "envelope"])
        self.assertEqual(q.qsize(), 1)
        self.assertEqual(q.get(timeout=1), ["not", "an", "envelope"])

    def test_task_done_and_join(self):
        q = TaskQueue()
        q.put({"id": "a", "plugin": "p"})
        q.get(timeout=1)
        q.join(timeout=0.01)  # still unfinished
        q.task_done()
        q.join(timeout=1)
        self.assertEqual(q.qsize(), 0)

    def test_two_workers_see_every_task_exactly_once(self):
        q = TaskQueue(maxsize=8)
        seen = []
        lock = threading.Lock()
        stop = threading.Event()

        def worker():
            while not stop.is_set():
                try:
                    message = q.get(timeout=0.05)
                except queue.Empty:
                    continue
                with lock:
                    seen.append(message["id"])
                q.task_done()

        workers = [threading.Thread(target=worker, daemon=True) for _ in range(2)]
        for thread in workers:
            thread.start()
        for index in range(40):
            q.put({"id": f"t{index}", "plugin": "p", "priority": index % 10}, timeout=2)
        q.join(timeout=5)
        stop.set()
        for thread in workers:
            thread.join(timeout=2)
        self.assertEqual(sorted(seen), sorted(f"t{index}" for index in range(40)))


class RegistryPriorityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="lah-prio-")
        cls._old_data = os.environ.get("LOCAL_AI_HUB_DATA")
        os.environ["LOCAL_AI_HUB_DATA"] = cls._tmp

    @classmethod
    def tearDownClass(cls):
        if cls._old_data is None:
            os.environ.pop("LOCAL_AI_HUB_DATA", None)
        else:
            os.environ["LOCAL_AI_HUB_DATA"] = cls._old_data
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def test_priority_in_running_get_and_history(self):
        registry = TaskRegistry()
        registry.register("t1", plugin="p", priority=9)
        self.assertEqual(registry.running()[0]["priority"], 9)
        self.assertEqual(registry.get("t1")["priority"], 9)
        registry.finish("t1", ok=True, duration_ms=1)
        self.assertEqual(registry.history()[0]["priority"], 9)
        self.assertEqual(registry.get("t1")["priority"], 9)

    def test_priority_defaults_and_is_normalised(self):
        registry = TaskRegistry()
        registry.register("t1", plugin="p")
        registry.register("t2", plugin="p", priority="high")
        self.assertEqual(registry.get("t1")["priority"], PRIORITY_DEFAULT)
        self.assertEqual(registry.get("t2")["priority"], 9)

    def test_cancel_before_start_is_audited_as_cancelled(self):
        config = load_config({"tasks": {"audit_log": True, "audit_file": "prio-audit.jsonl"}})
        registry = TaskRegistry(config)
        record = registry.cancel_before_start("q1", plugin="p", domain="example.com", priority="high")
        self.assertTrue(record["cancelled"])
        self.assertFalse(record["ok"])
        self.assertEqual(record["priority"], 9)
        self.assertEqual(record["duration_ms"], 0)
        self.assertEqual(registry.counters()["cancelled"], 1)
        self.assertEqual(registry.counters()["failed"], 0)
        self.assertEqual(registry.get("q1")["state"], "cancelled")
        from hub import paths

        lines = (paths.log_dir() / "prio-audit.jsonl").read_text().strip().splitlines()
        self.assertEqual(json.loads(lines[-1])["id"], "q1")


class EnginePriorityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="lah-prio-engine-")
        cls._old_data = os.environ.get("LOCAL_AI_HUB_DATA")
        os.environ["LOCAL_AI_HUB_DATA"] = cls._tmp

    @classmethod
    def tearDownClass(cls):
        if cls._old_data is None:
            os.environ.pop("LOCAL_AI_HUB_DATA", None)
        else:
            os.environ["LOCAL_AI_HUB_DATA"] = cls._old_data
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def make_engine(self, **extra):
        config = {"ollama": {"mock": True}, "tasks": {"audit_log": False}}
        config.update(extra)
        return Engine(load_config(config))

    def task(self, engine, **extra):
        message = {"type": "task", "plugin": "hello.python", "input": {"name": "Prio"}}
        message.update(extra)
        return engine.handle(message)

    def test_result_meta_carries_the_default_priority(self):
        engine = self.make_engine()
        try:
            result = self.task(engine, id="p-default")
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["meta"]["priority"], PRIORITY_DEFAULT)
            listed = engine.handle({"type": "tasks.get", "task_id": "p-default"})
            self.assertEqual(listed["output"]["task"]["priority"], PRIORITY_DEFAULT)
        finally:
            engine.shutdown()

    def test_named_and_numeric_priority_reach_meta_and_history(self):
        engine = self.make_engine()
        try:
            self.assertEqual(self.task(engine, id="p-high", priority="high")["meta"]["priority"], 9)
            self.assertEqual(self.task(engine, id="p-two", priority=2)["meta"]["priority"], 2)
            history = engine.handle({"type": "tasks.list"})["output"]["history"]
            by_id = {entry["id"]: entry["priority"] for entry in history}
            self.assertEqual(by_id["p-high"], 9)
            self.assertEqual(by_id["p-two"], 2)
        finally:
            engine.shutdown()

    def test_invalid_priority_does_not_fail_the_task(self):
        engine = self.make_engine()
        try:
            result = self.task(engine, priority={"nope": True})
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["meta"]["priority"], PRIORITY_DEFAULT)
        finally:
            engine.shutdown()

    def test_config_default_priority_is_honoured(self):
        engine = self.make_engine(tasks={"audit_log": False, "default_priority": "low", "queue_size": 4})
        try:
            self.assertEqual(engine.queue.default_priority, 0)
            self.assertEqual(engine.queue.maxsize, 4)
            self.assertEqual(self.task(engine)["meta"]["priority"], 0)
            self.assertEqual(self.task(engine, priority=7)["meta"]["priority"], 7)
        finally:
            engine.shutdown()

    def test_cached_result_reports_priority(self):
        engine = self.make_engine(cache={"enabled": True, "ttl_seconds": 60, "max_entries": 8})
        try:
            first = self.task(engine, id="c1", priority="high")
            second = self.task(engine, id="c2", priority="low")
            self.assertTrue(first["ok"] and second["ok"])
            self.assertTrue(second["meta"]["cache"]["hit"])
            self.assertEqual(second["meta"]["priority"], 0)
        finally:
            engine.shutdown()

    def test_tasks_list_reports_the_waiting_queue(self):
        engine = self.make_engine()
        try:
            engine.queue.put({"id": "wait-low", "plugin": "hello.python", "priority": 1})
            engine.queue.put({"id": "wait-high", "plugin": "hello.python", "priority": 9})
            output = engine.handle({"type": "tasks.list"})["output"]
            self.assertEqual([entry["id"] for entry in output["queued"]], ["wait-high", "wait-low"])
            status = engine.handle({"type": "status"})["output"]
            self.assertEqual(status["tasks"]["queued"], 2)
        finally:
            engine.shutdown()

    def test_cancel_a_queued_task(self):
        engine = self.make_engine()
        events = []
        engine.emit = events.append
        try:
            engine.queue.put({"id": "wait-1", "plugin": "hello.python", "priority": "high",
                              "context": {"domain": "Example.COM", "interface": "firefox"}})
            result = engine.handle({"type": "task.cancel", "task_id": "wait-1"})
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["output"], {"cancelled": ["wait-1"], "queued": True})
            self.assertEqual(engine.queue.qsize(), 0)
            record = engine.handle({"type": "tasks.get", "task_id": "wait-1"})["output"]["task"]
            self.assertEqual(record["state"], "cancelled")
            self.assertEqual(record["domain"], "example.com")
            self.assertEqual(record["priority"], 9)
            self.assertEqual(engine.tasks.counters()["cancelled"], 1)
            self.assertTrue(any(e.get("event") == "task.cancelled" for e in events))
        finally:
            engine.shutdown()

    def test_cancel_all_drops_running_and_queued(self):
        engine = self.make_engine()
        try:
            engine.tasks.register("run-1", plugin="hello.python")
            engine.queue.put({"id": "wait-1", "plugin": "hello.python"})
            engine.queue.put({"id": "wait-2", "plugin": "hello.python"})
            output = engine.handle({"type": "task.cancel", "task_id": "*"})["output"]
            self.assertTrue(output["all"])
            self.assertEqual(sorted(output["cancelled"]), ["run-1", "wait-1", "wait-2"])
            self.assertEqual(engine.queue.qsize(), 0)
            self.assertEqual(engine.tasks.counters()["cancelled"], 2)
        finally:
            engine.shutdown()

    def test_cancel_unknown_task_is_unchanged(self):
        engine = self.make_engine()
        try:
            output = engine.handle({"type": "task.cancel", "task_id": "ghost"})["output"]
            self.assertEqual(output, {"cancelled": [], "unknown_task": "ghost"})
        finally:
            engine.shutdown()

    def test_shutdown_releases_the_queue(self):
        engine = self.make_engine()
        engine.queue.put({"id": "wait-1", "plugin": "hello.python"})
        engine.shutdown()
        self.assertEqual(engine.queue.qsize(), 0)


class NativeHostQueueTests(unittest.TestCase):
    """The host's worker loop must consume the engine's priority queue."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="lah-prio-host-")
        cls._old_data = os.environ.get("LOCAL_AI_HUB_DATA")
        os.environ["LOCAL_AI_HUB_DATA"] = cls._tmp
        cls.host_module = load_host_module()

    @classmethod
    def tearDownClass(cls):
        if cls._old_data is None:
            os.environ.pop("LOCAL_AI_HUB_DATA", None)
        else:
            os.environ["LOCAL_AI_HUB_DATA"] = cls._old_data
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def test_worker_runs_high_priority_first(self):
        engine = Engine(load_config({"ollama": {"mock": True}, "tasks": {"audit_log": False}}))
        host = self.host_module.NativeHost.__new__(self.host_module.NativeHost)
        host._shutdown = threading.Event()
        host.engine = engine
        processed = []
        host._process = processed.append  # instance attribute shadows the method
        for request_id, priority in (("low", 1), ("mid", 5), ("high", 9)):
            engine.queue.put({"type": "task", "id": request_id, "plugin": "hello.python",
                              "priority": priority})
        worker = threading.Thread(target=host._worker, args=(engine.queue,), daemon=True)
        worker.start()
        try:
            engine.queue.join(timeout=5)
        finally:
            host._shutdown.set()
            worker.join(timeout=2)
            engine.shutdown()
        self.assertEqual(processed[0]["id"], "high")
        self.assertEqual(sorted(m["id"] for m in processed), ["high", "low", "mid"])

    def test_enqueue_full_queue_answers_rate_limited(self):
        engine = Engine(load_config({"ollama": {"mock": True},
                                     "tasks": {"audit_log": False, "queue_size": 1}}))
        host = self.host_module.NativeHost.__new__(self.host_module.NativeHost)
        host._shutdown = threading.Event()
        host.engine = engine
        sent = []
        host.send = sent.append
        engine.queue.put({"type": "task", "id": "a", "plugin": "hello.python"})
        host._enqueue(engine.queue, {"type": "task", "id": "b", "plugin": "hello.python"})
        self.assertEqual(engine.queue.qsize(), 1)
        self.assertEqual(len(sent), 1)
        self.assertFalse(sent[0]["ok"])
        self.assertEqual(sent[0]["error"]["code"], "rate_limited")
        self.assertEqual(sent[0]["id"], "b")
        engine.shutdown()


if __name__ == "__main__":
    unittest.main()
