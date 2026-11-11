"""Opt-in result cache.

Re-running a feature on an unchanged thread should not re-classify every
comment.  The cache key covers everything that can change the answer:

    plugin id + plugin version + resolved config + input + model names

so editing a domain config, changing a label set or switching model naturally
invalidates the entry.  Disabled by default (``cache.enabled``): a cache is a
surprise if you did not ask for one, and staleness matters for moderation.

In-memory only, LRU + TTL, thread-safe.  Nothing is written to disk — page
content should not accumulate in a second location.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from typing import Any

from .logging_util import get_logger

log = get_logger("cache")


def cache_key(
    plugin_id: str,
    plugin_version: str,
    config: dict,
    payload: Any,
    models: dict | None = None,
) -> str:
    material = {
        "plugin": plugin_id,
        "version": plugin_version,
        "config": config or {},
        "input": payload,
        "models": models or {},
    }
    blob = json.dumps(material, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class ResultCache:
    def __init__(self, config=None):
        self.config = config
        self.enabled = bool(config.get("cache.enabled", False)) if config else False
        self.ttl = float((config.get("cache.ttl_seconds", 900) if config else 900) or 0)
        self.max_entries = int((config.get("cache.max_entries", 256) if config else 256) or 0)
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, tuple[float, Any, dict]] = OrderedDict()
        self.hits = 0
        self.misses = 0

    # ------------------------------------------------------------------
    def get(self, key: str) -> tuple[Any, dict] | None:
        if not self.enabled:
            return None
        now = time.time()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self.misses += 1
                return None
            stored_at, output, meta = entry
            if self.ttl and now - stored_at > self.ttl:
                self._entries.pop(key, None)
                self.misses += 1
                return None
            self._entries.move_to_end(key)
            self.hits += 1
            age_ms = int((now - stored_at) * 1000)
        cached_meta = dict(meta)
        cached_meta["cache"] = {"hit": True, "age_ms": age_ms, "key": key[:16]}
        return output, cached_meta

    def put(self, key: str, output: Any, meta: dict) -> None:
        if not self.enabled or self.max_entries <= 0:
            return
        with self._lock:
            self._entries[key] = (time.time(), output, dict(meta))
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    def clear(self) -> int:
        with self._lock:
            count = len(self._entries)
            self._entries.clear()
            self.hits = 0
            self.misses = 0
        return count

    def stats(self) -> dict:
        with self._lock:
            return {
                "enabled": self.enabled,
                "entries": len(self._entries),
                "max_entries": self.max_entries,
                "ttl_seconds": self.ttl,
                "hits": self.hits,
                "misses": self.misses,
            }