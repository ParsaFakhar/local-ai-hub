"""Result cache: key material, TTL, LRU, stats and engine integration."""

import os
import shutil
import tempfile
import time
import unittest

from hub.cache import ResultCache, cache_key
from hub.config import load_config
from hub.engine import Engine


class FakeConfig:
    """Minimal stand-in for hub.config.Config (only .get is used)."""

    def __init__(self, values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)


def cache_config(**overrides):
    values = {"cache.enabled": True, "cache.ttl_seconds": 900, "cache.max_entries": 256}
    values.update({f"cache.{k}": v for k, v in overrides.items()})
    return FakeConfig(values)


class CacheKeyTests(unittest.TestCase):
    def test_key_is_stable_for_equal_material(self):
        a = cache_key("p", "1.0", {"labels": ["ok", "spam"]}, {"x": 1}, {"default": "m"})
        b = cache_key("p", "1.0", {"labels": ["ok", "spam"]}, {"x": 1}, {"default": "m"})
        self.assertEqual(a, b)
        self.assertEqual(len(a), 64)

    def test_key_ignores_dict_order(self):
        self.assertEqual(
            cache_key("p", "1.0", {"a": 1, "b": 2}, {"x": 1, "y": 2}),
            cache_key("p", "1.0", {"b": 2, "a": 1}, {"y": 2, "x": 1}),
        )

    def test_every_component_invalidates(self):
        base = cache_key("p", "1.0", {"a": 1}, {"x": 1}, {"default": "m"})
        variants = [
            cache_key("other", "1.0", {"a": 1}, {"x": 1}, {"default": "m"}),
            cache_key("p", "2.0", {"a": 1}, {"x": 1}, {"default": "m"}),
            cache_key("p", "1.0", {"a": 2}, {"x": 1}, {"default": "m"}),
            cache_key("p", "1.0", {"a": 1}, {"x": 2}, {"default": "m"}),
            cache_key("p", "1.0", {"a": 1}, {"x": 1}, {"default": "other"}),
        ]
        for variant in variants:
            self.assertNotEqual(base, variant)

    def test_list_order_matters(self):
        self.assertNotEqual(
            cache_key("p", "1", {}, {"items": [1, 2]}),
            cache_key("p", "1", {}, {"items": [2, 1]}),
        )


class ResultCacheTests(unittest.TestCase):
    def test_disabled_by_default(self):
        cache = ResultCache(load_config())
        self.assertFalse(cache.enabled)
        cache.put("k", {"v": 1}, {})
        self.assertIsNone(cache.get("k"))
        self.assertEqual(cache.stats()["entries"], 0)

    def test_round_trip_marks_meta_as_hit(self):
        cache = ResultCache(cache_config())
        cache.put("k", {"v": 1}, {"plugin": "p", "models": ["m"]})
        output, meta = cache.get("k")
        self.assertEqual(output, {"v": 1})
        self.assertTrue(meta["cache"]["hit"])
        self.assertGreaterEqual(meta["cache"]["age_ms"], 0)
        self.assertEqual(meta["cache"]["key"], "k"[:16])
        # The stored meta must not be mutated by a read.
        self.assertEqual(cache.get("k")[1]["plugin"], "p")

    def test_meta_is_copied_not_shared(self):
        cache = ResultCache(cache_config())
        meta = {"plugin": "p"}
        cache.put("k", {"v": 1}, meta)
        meta["plugin"] = "changed"
        self.assertEqual(cache.get("k")[1]["plugin"], "p")

    def test_misses_and_hits_are_counted(self):
        cache = ResultCache(cache_config())
        self.assertIsNone(cache.get("absent"))
        cache.put("k", 1, {})
        cache.get("k")
        cache.get("k")
        stats = cache.stats()
        self.assertEqual((stats["hits"], stats["misses"]), (2, 1))

    def test_ttl_expiry(self):
        cache = ResultCache(cache_config(ttl_seconds=0.05))
        cache.put("k", "v", {})
        self.assertIsNotNone(cache.get("k"))
        time.sleep(0.08)
        self.assertIsNone(cache.get("k"))
        self.assertEqual(cache.stats()["entries"], 0)

    def test_zero_ttl_never_expires(self):
        cache = ResultCache(cache_config(ttl_seconds=0))
        cache.put("k", "v", {})
        self.assertIsNotNone(cache.get("k"))

    def test_lru_eviction_keeps_recent_entries(self):
        cache = ResultCache(cache_config(max_entries=2))
        cache.put("a", 1, {})
        cache.put("b", 2, {})
        cache.get("a")  # 'a' becomes the most recently used
        cache.put("c", 3, {})
        self.assertIsNotNone(cache.get("a"))
        self.assertIsNone(cache.get("b"))
        self.assertIsNotNone(cache.get("c"))
        self.assertEqual(cache.stats()["entries"], 2)

    def test_zero_max_entries_stores_nothing(self):
        cache = ResultCache(cache_config(max_entries=0))
        cache.put("k", 1, {})
        self.assertIsNone(cache.get("k"))

    def test_clear_resets_counters(self):
        cache = ResultCache(cache_config())
        cache.put("a", 1, {})
        cache.put("b", 2, {})
        cache.get("a")
        self.assertEqual(cache.clear(), 2)
        stats = cache.stats()
        self.assertEqual((stats["entries"], stats["hits"], stats["misses"]), (0, 0, 0))

    def test_overwrite_does_not_grow(self):
        cache = ResultCache(cache_config())
        cache.put("k", 1, {})
        cache.put("k", 2, {})
        self.assertEqual(cache.stats()["entries"], 1)
        self.assertEqual(cache.get("k")[0], 2)


class EngineCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="lah-cache-")
        cls._old_data = os.environ.get("LOCAL_AI_HUB_DATA")
        os.environ["LOCAL_AI_HUB_DATA"] = cls._tmp

    @classmethod
    def tearDownClass(cls):
        if cls._old_data is None:
            os.environ.pop("LOCAL_AI_HUB_DATA", None)
        else:
            os.environ["LOCAL_AI_HUB_DATA"] = cls._old_data
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def engine(self, **cache):
        settings = {"enabled": True}
        settings.update(cache)
        return Engine(load_config({"ollama": {"mock": True}, "cache": settings}))

    def test_second_identical_task_is_served_from_cache(self):
        engine = self.engine()
        try:
            first = engine.handle({"type": "task", "id": "c1", "plugin": "hello.python", "input": {"name": "Cache"}})
            second = engine.handle({"type": "task", "id": "c2", "plugin": "hello.python", "input": {"name": "Cache"}})
            self.assertTrue(first["ok"], first)
            self.assertTrue(second["ok"], second)
            self.assertNotIn("cache", first["meta"])
            self.assertTrue(second["meta"]["cache"]["hit"])
            self.assertEqual(first["output"], second["output"])
            self.assertEqual(engine.cache.stats()["hits"], 1)
            history = engine.handle({"type": "tasks.list"})["output"]["history"]
            self.assertTrue(any(entry["id"] == "c2" and entry["cached"] for entry in history))
        finally:
            engine.shutdown()

    def test_different_input_is_not_cached(self):
        engine = self.engine()
        try:
            engine.handle({"type": "task", "plugin": "hello.python", "input": {"name": "A"}})
            other = engine.handle({"type": "task", "plugin": "hello.python", "input": {"name": "B"}})
            self.assertNotIn("cache", other["meta"])
        finally:
            engine.shutdown()

    def test_option_change_invalidates(self):
        engine = self.engine()
        try:
            payload = {"type": "task", "plugin": "hello.python", "input": {"name": "A"}}
            engine.handle(dict(payload, options={"greeting_template": "Hi {name}"}))
            changed = engine.handle(dict(payload, options={"greeting_template": "Yo {name}"}))
            self.assertNotIn("cache", changed["meta"])
            self.assertEqual(changed["output"]["greeting"], "Yo A")
        finally:
            engine.shutdown()

    def test_cache_clear_control_message(self):
        engine = self.engine()
        try:
            engine.handle({"type": "task", "plugin": "hello.python", "input": {"name": "A"}})
            cleared = engine.handle({"type": "cache.clear"})
            self.assertTrue(cleared["ok"])
            self.assertEqual(cleared["output"]["cleared"], 1)
            self.assertEqual(cleared["output"]["cache"]["entries"], 0)
            again = engine.handle({"type": "task", "plugin": "hello.python", "input": {"name": "A"}})
            self.assertNotIn("cache", again["meta"])
        finally:
            engine.shutdown()

    def test_disabled_cache_never_hits(self):
        engine = self.engine(enabled=False)
        try:
            engine.handle({"type": "task", "plugin": "hello.python", "input": {"name": "A"}})
            second = engine.handle({"type": "task", "plugin": "hello.python", "input": {"name": "A"}})
            self.assertNotIn("cache", second["meta"])
        finally:
            engine.shutdown()


if __name__ == "__main__":
    unittest.main()
