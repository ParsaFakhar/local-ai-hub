"""Local AI Hub — Python plugin SDK (stdlib only).

A plugin is a normal Python program::

    from local_ai_hub import Plugin

    def main(task, hub):
        return {"greeting": f"Hello, {task.input.get('name', 'world')}!"}

    Plugin(main).run()

``task``  : input, config, context, plugin id
``hub``   : log(), progress(), generate(), chat(), json()  (model access)

The engine owns the model runtime; a plugin never talks to Ollama directly
and never executes code produced by a model or by a webpage.
"""

from __future__ import annotations

import json
import re
import sys
import traceback
from typing import Any, Callable

__all__ = ["Plugin", "Task", "Hub", "LLMError", "PluginError"]

PROTOCOL = "1.0"


class LLMError(RuntimeError):
    """A model call failed or returned unusable output."""


class PluginError(RuntimeError):
    """Raise from a plugin to report a clean, typed failure."""

    def __init__(self, message: str, code: str = "plugin_failed", details: Any = None):
        super().__init__(message)
        self.code = code
        self.details = details


class Task:
    def __init__(self, payload: dict):
        self.payload = payload
        self.id = payload.get("task_id", "-")
        self.input = payload.get("input") or {}
        self.config = payload.get("config") or {}
        self.context = payload.get("context") or {}
        self.models = payload.get("models") or {}
        self.limits = payload.get("limits") or {}
        plugin = payload.get("plugin") or {}
        self.plugin_id = plugin.get("id", "unknown")
        self.plugin_version = plugin.get("version", "0")

    def option(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)


class Hub:
    """Line-delimited JSON RPC channel back to the engine."""

    def __init__(self, wire_out=None, wire_in=None):
        self._out = wire_out if wire_out is not None else sys.stdout
        self._in = wire_in if wire_in is not None else sys.stdin
        self._counter = 0
        self.last_model: str | None = None
        self.mock = False

    # ------------------------------------------------------------------
    def _send(self, message: dict) -> None:
        self._out.write(json.dumps(message, ensure_ascii=False) + "\n")
        self._out.flush()

    def log(self, message: str, level: str = "info") -> None:
        self._send({"type": "log", "level": level, "message": str(message)})

    def progress(self, value: float, message: str | None = None) -> None:
        self._send({"type": "progress", "value": value, "message": message})

    # ------------------------------------------------------------------
    def _call(self, method: str, params: dict) -> dict:
        self._counter += 1
        call_id = str(self._counter)
        self._send({"type": "llm.request", "id": call_id, "method": method, "params": params})
        while True:
            line = self._in.readline()
            if not line:
                raise LLMError("engine closed the plugin channel")
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("type") != "llm.response" or message.get("id") != call_id:
                continue
            if not message.get("ok"):
                error = message.get("error") or {}
                raise LLMError(error.get("message", "model call failed"))
            result = message.get("result") or {}
            self.last_model = result.get("model")
            self.mock = bool(result.get("mock"))
            return result

    def generate(
        self,
        prompt: str,
        *,
        model: str | None = None,
        system: str | None = None,
        images: list[str] | None = None,
        options: dict | None = None,
    ) -> str:
        params = {"prompt": prompt, "model": model, "system": system, "options": options}
        if images:
            params["images"] = images
        return self._call("generate", params).get("text", "")

    def chat(self, messages: list[dict], *, model: str | None = None, options: dict | None = None) -> str:
        return self._call("chat", {"messages": messages, "model": model, "options": options}).get("text", "")

    def json(
        self,
        prompt: str,
        *,
        schema: dict | None = None,
        model: str | None = None,
        system: str | None = None,
        images: list[str] | None = None,
        options: dict | None = None,
    ) -> Any:
        """Structured output.  ``schema`` is enforced by the model runtime."""
        params = {
            "prompt": prompt,
            "model": model,
            "system": system,
            "format": schema if schema else "json",
            "options": options,
        }
        if images:
            params["images"] = images
        text = self._call("generate", params).get("text", "")
        return parse_json(text)


def parse_json(text: str) -> Any:
    """Tolerant JSON parsing: handles code fences and surrounding prose."""
    text = (text or "").strip()
    if not text:
        raise LLMError("model returned an empty response")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1).strip())
        except json.JSONDecodeError:
            pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise LLMError(f"model did not return valid JSON: {text[:200]!r}")


class Plugin:
    """Wrap a handler ``fn(task, hub) -> dict`` and run the stdio protocol."""

    def __init__(self, handler: Callable[[Task, Hub], Any]):
        self.handler = handler

    def run(self) -> None:
        wire_out, wire_in = sys.stdout, sys.stdin
        # Stray print() calls must never corrupt the protocol stream.
        sys.stdout = sys.stderr
        hub = Hub(wire_out=wire_out, wire_in=wire_in)
        try:
            line = wire_in.readline()
            if not line:
                return
            payload = json.loads(line)
            if payload.get("type") != "invoke":
                raise PluginError(f"expected an invoke frame, got {payload.get('type')!r}")
            task = Task(payload)
            output = self.handler(task, hub)
            hub._send({"type": "result", "ok": True, "output": output if output is not None else {}})
        except PluginError as exc:
            hub._send(
                {
                    "type": "result",
                    "ok": False,
                    "error": {"code": exc.code, "message": str(exc), "details": exc.details},
                }
            )
        except Exception as exc:  # noqa: BLE001 - report, never crash silently
            hub._send(
                {
                    "type": "result",
                    "ok": False,
                    "error": {
                        "code": "plugin_failed",
                        "message": f"{type(exc).__name__}: {exc}",
                        "details": traceback.format_exc(limit=6),
                    },
                }
            )
        finally:
            try:
                wire_out.flush()
            except (OSError, ValueError):
                pass
