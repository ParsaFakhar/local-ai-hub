"""Running-task registry, history and audit log.

The engine can execute several tasks at once (the native host runs two worker
threads, the HTTP API is threaded).  This module keeps that bookkeeping in one
thread-safe place so the engine itself stays readable.

* ``TaskQueue`` orders *waiting* tasks: higher priority first, FIFO inside one
  priority level.  The native host's two workers pull from it.
* ``register``  hands out a :class:`threading.Event` used to cancel a task
* ``finish``    moves a task from *running* to *history* and writes an audit line
* ``cancel``    sets the event; the plugin runner notices and reaps the child

The audit log is append-only JSONL containing **hashes** of the input, never
the input itself: it answers "what ran, when, with which model, how long"
without copying page content into a second place on disk.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import queue
import threading
import time
from collections import deque
from typing import Any

from . import paths
from .logging_util import get_logger
from .protocol import new_id

log = get_logger("tasks")


def input_hash(value: Any) -> str:
    """Stable hash of a task input (canonical JSON, keys sorted)."""
    try:
        blob = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        blob = repr(value)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


#: Task priority is an integer 0-9, **higher runs first**.  ``normal`` (5) is
#: the default; the names below are accepted wherever a number is.
PRIORITY_MIN = 0
PRIORITY_MAX = 9
PRIORITY_DEFAULT = 5
PRIORITY_NAMES = {"low": 0, "normal": 5, "high": 9}


def normalize_priority(value: Any, default: int = PRIORITY_DEFAULT) -> int:
    """Coerce a caller-supplied priority into 0-9.

    Accepts numbers, numeric strings and the names ``low`` / ``normal`` /
    ``high``.  Anything unusable falls back to *default* — a bad priority must
    never fail a task.
    """
    fallback = max(PRIORITY_MIN, min(PRIORITY_MAX, int(default)))
    if value is None or isinstance(value, bool):
        return fallback
    if isinstance(value, (int, float)):
        number = int(value)
    elif isinstance(value, str):
        text = value.strip().lower()
        if text in PRIORITY_NAMES:
            return PRIORITY_NAMES[text]
        try:
            number = int(float(text))
        except ValueError:
            return fallback
    else:
        return fallback
    return max(PRIORITY_MIN, min(PRIORITY_MAX, number))


class TaskQueue:
    """Bounded, thread-safe priority queue for tasks that have not started yet.

    The native host reads frames on one thread and executes them on two workers;
    this is the hand-off buffer.  It offers the slice of :class:`queue.Queue`
    the host uses (``put``, ``get(timeout)``, ``task_done``, ``qsize``) plus the
    introspection the control types need:

    * ``snapshot()`` — waiting tasks in the order they will run (``tasks.list``)
    * ``remove(id)`` / ``clear()`` — drop waiting tasks (``task.cancel``)

    Ordering: highest ``priority`` first, then arrival order (FIFO) for ties.
    Only the intake queue is ordered — once a worker picks a task up it runs to
    completion, and the HTTP API executes requests in its own threads without
    queueing.
    """

    def __init__(self, maxsize: int = 64, *, default_priority: int = PRIORITY_DEFAULT):
        self.maxsize = max(1, int(maxsize or 0))
        self.default_priority = normalize_priority(default_priority)
        self._cond = threading.Condition()
        self._heap: list[tuple[int, int, dict]] = []
        self._entries: dict[str, dict] = {}
        self._seq = 0
        self._unfinished = 0

    # -- producer / consumer -------------------------------------------
    def put(self, message: Any, *, timeout: float | None = None) -> str:
        """Queue a request; block while the queue is full.  Returns its id."""
        with self._cond:
            if not self._cond.wait_for(lambda: len(self._entries) < self.maxsize, timeout):
                raise queue.Full
            self._seq += 1
            seq = self._seq
            if isinstance(message, dict):
                request_id = message.get("id")
                if not isinstance(request_id, str) or not request_id:
                    request_id = new_id("task")
                    message["id"] = request_id
                plugin = message.get("plugin") or "?"
                priority = normalize_priority(message.get("priority"), self.default_priority)
            else:
                # Not an envelope: the engine will reject it, keep it orderable.
                request_id = f"anon-{seq}"
                plugin, priority = "?", self.default_priority
            entry = {
                "key": request_id,
                "id": request_id,
                "plugin": plugin,
                "priority": priority,
                "queued_at": time.time(),
                "message": message,
                "removed": False,
            }
            self._entries[request_id] = entry
            heapq.heappush(self._heap, (-priority, seq, entry))
            self._unfinished += 1
            self._cond.notify_all()
            return request_id

    def get(self, timeout: float | None = None) -> Any:
        """Next message to run.  Raises :class:`queue.Empty` on timeout."""
        with self._cond:
            if not self._cond.wait_for(lambda: bool(self._entries), timeout):
                raise queue.Empty
            while self._heap:
                _, _, entry = heapq.heappop(self._heap)
                if entry["removed"]:
                    continue
                self._entries.pop(entry["key"], None)
                self._cond.notify_all()  # a slot freed up for a blocked producer
                return entry["message"]
            raise queue.Empty  # pragma: no cover - heap and index agree

    def task_done(self) -> None:
        with self._cond:
            self._unfinished = max(0, self._unfinished - 1)
            self._cond.notify_all()

    def join(self, timeout: float | None = None) -> None:
        with self._cond:
            self._cond.wait_for(lambda: self._unfinished == 0, timeout)

    def qsize(self) -> int:
        with self._cond:
            return len(self._entries)

    # -- introspection / cancellation ----------------------------------
    def snapshot(self) -> list[dict]:
        """Waiting tasks, next-to-run first (never exposes the message body)."""
        now = time.time()
        with self._cond:
            ordered = sorted(
                (item for item in self._heap if not item[2]["removed"]),
                key=lambda item: (item[0], item[1]),
            )
            return [
                {
                    "id": entry["id"],
                    "plugin": entry["plugin"],
                    "priority": entry["priority"],
                    "queued_ms": int((now - entry["queued_at"]) * 1000),
                    "position": index + 1,
                }
                for index, (_, _, entry) in enumerate(ordered)
            ]

    def remove(self, request_id: str) -> Any | None:
        """Drop one waiting task; returns its message, or ``None`` if absent."""
        with self._cond:
            entry = self._entries.pop(request_id, None)
            if entry is None:
                return None
            entry["removed"] = True
            self._unfinished = max(0, self._unfinished - 1)
            self._cond.notify_all()
            return entry["message"]

    def clear(self) -> list[Any]:
        """Drop every waiting task and return their messages."""
        with self._cond:
            messages = [entry["message"] for entry in self._entries.values()]
            for entry in self._entries.values():
                entry["removed"] = True
            self._entries.clear()
            self._unfinished = 0
            self._cond.notify_all()
            return messages


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
        priority: int = PRIORITY_DEFAULT,
    ) -> threading.Event:
        cancel_event = threading.Event()
        with self._lock:
            self._running[request_id] = {
                "id": request_id,
                "plugin": plugin,
                "domain": domain,
                "interface": interface,
                "input_hash": digest,
                "priority": normalize_priority(priority),
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
                "priority": PRIORITY_DEFAULT,
                "started_at": time.time(),
            }
            cancelled = bool(entry.get("cancel") and entry["cancel"].is_set())
            record = {
                "id": request_id,
                "plugin": entry["plugin"],
                "domain": entry["domain"],
                "interface": entry["interface"],
                "input_hash": entry["input_hash"],
                "priority": entry.get("priority", PRIORITY_DEFAULT),
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

    def cancel_before_start(
        self,
        request_id: str,
        *,
        plugin: str = "?",
        domain: str = "",
        interface: str = "unknown",
        priority: int = PRIORITY_DEFAULT,
    ) -> dict:
        """Record a task cancelled while it was still queued (it never ran)."""
        now = time.time()
        record = {
            "id": request_id,
            "plugin": plugin,
            "domain": domain,
            "interface": interface,
            "input_hash": "",
            "priority": normalize_priority(priority),
            "started_at": now,
            "finished_at": now,
            "duration_ms": 0,
            "ok": False,
            "cancelled": True,
            "cached": False,
            "models": [],
            "error": "cancelled before start",
        }
        with self._lock:
            self._history.append(record)
            self.cancelled += 1
        self._audit(record)
        return record

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
                    "priority": entry.get("priority", PRIORITY_DEFAULT),
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
                    "priority": entry.get("priority", PRIORITY_DEFAULT),
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