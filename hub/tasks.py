"""Running-task registry, history and audit log.

The engine can execute several tasks at once (the native host runs two worker
threads, the HTTP API is threaded).  This module keeps that bookkeeping in one
thread-safe place so the engine itself stays readable.

* ``register``  hands out a :class:`threading.Event` used to cancel a task
* ``finish``    moves a task from *running* to *history* and writes an audit line
* ``cancel``    sets the event; the plugin runner notices and reaps the child

The audit log is append-only JSONL containing **hashes** of the input, never
the input itself: it answers "what ran, when, with which model, how long"
without copying page content into a second place on disk.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import deque
from typing import Any

from . import paths
from .logging_util import get_logger

log = get_logger("tasks")


def input_hash(value: Any) -> str:
    """Stable hash of a task input (canonical JSON, keys sorted)."""
    try:
        blob = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        blob = repr(value)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class TaskRegistry:
    def __init__(self, config=None):
        self.config = config
        self._lock = threading.Lock()
        self._running: dict[str, dict] = {}
        history_size = int((config.get("tasks.history_size", 100) if config else 100) or 0)
        self._history: deque[dict] = deque(maxlen=max(1, history_size))
        self.completed = 0
        self.failed = 0
        self.cancelled = 0

    # ------------------------------------------------------------------
    def register(
        self,
        request_id: str,
        *,
        plugin: str,
        domain: str = "",
        interface: str = "unknown",
        digest: str = "",
    ) -> threading.Event:
        cancel_event = threading.Event()
        with self._lock:
            self._running[request_id] = {
                "id": request_id,
                "plugin": plugin,
                "domain": domain,
                "interface": interface,
                "input_hash": digest,
                "started_at": time.time(),
                "cancel": cancel_event,
            }
        return cancel_event

    def is_cancelled(self, request_id: str) -> bool:
        with self._lock:
            entry = self._running.get(request_id)
            return bool(entry and entry["cancel"].is_set())

    def finish(
        self,
        request_id: str,
        *,
        ok: bool,
        duration_ms: int = 0,
        models: list[str] | None = None,
        error: str | None = None,
        cached: bool = False,
    ) -> dict:
        with self._lock:
            entry = self._running.pop(request_id, None) or {
                "id": request_id,
                "plugin": "?",
                "domain": "",
                "interface": "unknown",
                "input_hash": "",
                "started_at": time.time(),
            }
            cancelled = bool(entry.get("cancel") and entry["cancel"].is_set())
            record = {
                "id": request_id,
                "plugin": entry["plugin"],
                "domain": entry["domain"],
                "interface": entry["interface"],
                "input_hash": entry["input_hash"],
                "started_at": entry["started_at"],
                "finished_at": time.time(),
                "duration_ms": duration_ms,
                "ok": bool(ok),
                "cancelled": cancelled,
                "cached": bool(cached),
                "models": list(models or []),
                "error": error,
            }
            self._history.append(record)
            if cancelled:
                self.cancelled += 1
            elif ok:
                self.completed += 1
            else:
                self.failed += 1
        self._audit(record)
        return record

    def cancel(self, request_id: str) -> bool:
        with self._lock:
            entry = self._running.get(request_id)
            if entry is None:
                return False
            entry["cancel"].set()
            return True

    def cancel_all(self) -> list[str]:
        with self._lock:
            ids = list(self._running)
            for entry in self._running.values():
                entry["cancel"].set()
        return ids

    # ------------------------------------------------------------------
    def running(self) -> list[dict]:
        now = time.time()
        with self._lock:
            return [
                {
                    "id": entry["id"],
                    "plugin": entry["plugin"],
                    "domain": entry["domain"],
                    "interface": entry["interface"],
                    "running_ms": int((now - entry["started_at"]) * 1000),
                    "cancelling": entry["cancel"].is_set(),
                }
                for entry in self._running.values()
            ]

    def history(self, limit: int | None = None) -> list[dict]:
        with self._lock:
            items = list(self._history)
        items.reverse()  # newest first
        return items[:limit] if limit else items

    def get(self, request_id: str) -> dict | None:
        """One task by id: running entry or finished history record.

        Returns ``None`` when the id is unknown (never ran, or fell out of the
        bounded history).  The cancel event is never exposed.
        """
        now = time.time()
        with self._lock:
            entry = self._running.get(request_id)
            if entry is not None:
                return {
                    "id": entry["id"],
                    "plugin": entry["plugin"],
                    "domain": entry["domain"],
                    "interface": entry["interface"],
                    "input_hash": entry["input_hash"],
                    "started_at": entry["started_at"],
                    "state": "running",
                    "running_ms": int((now - entry["started_at"]) * 1000),
                    "cancelling": entry["cancel"].is_set(),
                }
            for record in reversed(self._history):
                if record["id"] == request_id:
                    return {**record, "state": "cancelled" if record.get("cancelled") else (
                        "finished" if record.get("ok") else "failed"
                    )}
        return None

    def counters(self) -> dict:
        with self._lock:
            return {
                "completed": self.completed,
                "failed": self.failed,
                "cancelled": self.cancelled,
                "running": len(self._running),
            }

    # ------------------------------------------------------------------
    def _audit(self, record: dict) -> None:
        if not self.config or not self.config.get("tasks.audit_log", True):
            return
        name = str(self.config.get("tasks.audit_file", "tasks.jsonl"))
        try:
            path = paths.log_dir() / name
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:  # never let auditing break a task
            log.debug("cannot write audit log: %s", exc)