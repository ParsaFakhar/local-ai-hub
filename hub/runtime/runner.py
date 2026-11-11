"""Subprocess execution of plugins.

The engine and a plugin exchange newline-delimited JSON on stdio:

    engine -> plugin : {"type":"invoke", ...}
    plugin -> engine : {"type":"llm.request","id":"1","method":"generate","params":{...}}
    engine -> plugin : {"type":"llm.response","id":"1","ok":true,"result":{...}}
    plugin -> engine : {"type":"log"|"progress", ...}
    plugin -> engine : {"type":"result","ok":true,"output":{...}}

Only the engine talks to Ollama, so model lifecycle, limits and auditing
stay in one place.  Plugins are trusted local programs; nothing here ever
executes strings coming from a webpage or from a model response.
"""

from __future__ import annotations

import codecs
import json
import os
import selectors
import shutil
import subprocess
import time
from pathlib import Path

from .. import paths
from ..logging_util import get_logger
from ..plugins.manager import Plugin

log = get_logger("runner")


class PluginRuntimeError(RuntimeError):
    def __init__(self, code: str, message: str, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def _interpreter(plugin: Plugin, config) -> list[str]:
    runtime_type = plugin.runtime["type"]
    if runtime_type == "python":
        configured = config.get("runtime.python", "python3")
        binary = shutil.which(configured) or configured
        return [binary, "-u", str(plugin.entry)]
    if runtime_type == "javascript":
        configured = config.get("runtime.node", "node")
        binary = shutil.which(configured)
        if not binary:
            raise PluginRuntimeError(
                "runtime_missing",
                f"Node.js not found ({configured!r}); install Node.js or set runtime.node",
            )
        return [binary, str(plugin.entry)]
    raise PluginRuntimeError("runtime_missing", f"unsupported runtime {runtime_type!r}")


def _child_env(plugin: Plugin) -> dict:
    root = paths.root_dir()
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"),
        "HOME": os.environ.get("HOME", str(Path.home())),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "LOCAL_AI_HUB_HOME": str(root),
        "LOCAL_AI_HUB_PLUGIN_DIR": str(plugin.directory),
        "LOCAL_AI_HUB_PLUGIN_ID": plugin.id,
        "LOCAL_AI_HUB_PROTOCOL": "1.0",
    }
    sdk_python = root / "sdk" / "python"
    sdk_js = root / "sdk" / "js"
    env["PYTHONPATH"] = os.pathsep.join(
        [str(sdk_python), str(plugin.directory), os.environ.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    env["NODE_PATH"] = os.pathsep.join(
        [str(sdk_js), str(plugin.directory), os.environ.get("NODE_PATH", "")]
    ).rstrip(os.pathsep)
    for passthrough in ("LOCAL_AI_HUB_MOCK_LLM", "LOCAL_AI_HUB_DATA", "OLLAMA_HOST"):
        if passthrough in os.environ:
            env[passthrough] = os.environ[passthrough]
    return env


class _LineReader:
    """Incremental UTF-8 line splitter for a non-blocking pipe."""

    def __init__(self, fd: int):
        self.fd = fd
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.buffer = ""
        self.eof = False
        self.total = 0

    def read_lines(self, limit: int) -> list[str]:
        try:
            chunk = os.read(self.fd, 65536)
        except (BlockingIOError, InterruptedError):
            return []
        if not chunk:
            self.eof = True
            tail, self.buffer = self.buffer, ""
            return [tail] if tail.strip() else []
        self.total += len(chunk)
        if self.total > limit:
            raise PluginRuntimeError("plugin_failed", "plugin produced too much output")
        self.buffer += self.decoder.decode(chunk)
        lines = self.buffer.split("\n")
        self.buffer = lines.pop()
        return [line for line in lines if line.strip()]


def run_plugin(
    plugin: Plugin,
    payload: dict,
    *,
    config,
    llm_handler,
    on_event=None,
    timeout: float | None = None,
) -> dict:
    """Run one plugin invocation and return its ``output`` dict."""
    command = _interpreter(plugin, config)
    env = _child_env(plugin)
    timeout = float(timeout or plugin.timeout_seconds or config.get("plugins.timeout_seconds", 180))
    max_output = int(config.get("plugins.max_output_bytes", 8 * 1024 * 1024))
    max_llm_calls = int(config.get("plugins.max_llm_calls", 64))
    task_id = payload.get("task_id", "-")

    log.debug("exec %s (cwd=%s)", " ".join(command), plugin.directory)
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell, trusted local plugin
            command,
            cwd=str(plugin.directory),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
        )
    except FileNotFoundError as exc:
        raise PluginRuntimeError("runtime_missing", f"cannot start {command[0]}: {exc}") from exc

    stdout = _LineReader(process.stdout.fileno())
    stderr = _LineReader(process.stderr.fileno())
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, stdout)
    selector.register(process.stderr, selectors.EVENT_READ, stderr)

    deadline = time.monotonic() + timeout
    llm_calls = 0
    result: dict | None = None
    stderr_tail: list[str] = []

    def send(message: dict) -> None:
        assert process.stdin is not None
        process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        process.stdin.flush()

    # stdin stays open for the whole invocation: it carries llm.response frames back
    # to the plugin.  It is closed in the finally block below.
    try:
        send(payload)
    except (BrokenPipeError, OSError) as exc:
        raise PluginRuntimeError(
            "plugin_failed", f"plugin {plugin.id} closed its input stream: {exc}"
        ) from exc

    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PluginRuntimeError(
                    "plugin_timeout", f"plugin {plugin.id} exceeded {timeout:.0f}s"
                )
            if result is not None:
                break

            for key, _ in selector.select(min(remaining, 0.25)):
                reader: _LineReader = key.data
                for line in reader.read_lines(max_output):
                    if reader is stderr:
                        stderr_tail.append(line)
                        del stderr_tail[:-50]
                        log.debug("[%s stderr] %s", plugin.id, line)
                        continue
                    try:
                        message = json.loads(line)
                    except json.JSONDecodeError:
                        log.warning("[%s] non-JSON stdout line ignored: %s", plugin.id, line[:200])
                        continue
                    if not isinstance(message, dict):
                        continue

                    kind = message.get("type")
                    if kind == "result":
                        result = message
                    elif kind == "log":
                        level = str(message.get("level", "info")).lower()
                        getattr(log, level if level in ("debug", "info", "warning", "error") else "info")(
                            "[%s] %s", plugin.id, message.get("message", "")
                        )
                        if on_event:
                            on_event("plugin.log", {"level": level, "message": message.get("message")})
                    elif kind == "progress":
                        if on_event:
                            on_event(
                                "progress",
                                {
                                    "value": message.get("value"),
                                    "message": message.get("message"),
                                    "plugin": plugin.id,
                                },
                            )
                    elif kind == "llm.request":
                        llm_calls += 1
                        if llm_calls > max_llm_calls:
                            send(
                                {
                                    "type": "llm.response",
                                    "id": message.get("id"),
                                    "ok": False,
                                    "error": {
                                        "code": "limit_exceeded",
                                        "message": f"more than {max_llm_calls} model calls in one task",
                                    },
                                }
                            )
                            continue
                        try:
                            llm_result = llm_handler(message.get("method", "generate"), message.get("params") or {})
                            send(
                                {
                                    "type": "llm.response",
                                    "id": message.get("id"),
                                    "ok": True,
                                    "result": llm_result,
                                }
                            )
                        except Exception as exc:  # noqa: BLE001 - reported back to the plugin
                            log.warning("[%s] llm call failed: %s", plugin.id, exc)
                            send(
                                {
                                    "type": "llm.response",
                                    "id": message.get("id"),
                                    "ok": False,
                                    "error": {
                                        "code": getattr(exc, "code", "model_error"),
                                        "message": str(exc),
                                    },
                                }
                            )
                    else:
                        log.debug("[%s] unknown message type %r", plugin.id, kind)

            if stdout.eof and result is None:
                if process.poll() is not None:
                    raise PluginRuntimeError(
                        "plugin_failed",
                        f"plugin {plugin.id} exited without a result (code {process.returncode})",
                        {"stderr": stderr_tail[-10:]},
                    )
    finally:
        selector.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        for pipe in (process.stdin, process.stdout, process.stderr):
            try:
                if pipe and not pipe.closed:
                    pipe.close()
            except OSError:
                pass

    if result is None:
        raise PluginRuntimeError("plugin_failed", f"plugin {plugin.id} returned no result")
    if not result.get("ok", False):
        error = result.get("error") or {}
        raise PluginRuntimeError(
            error.get("code") or "plugin_failed",
            error.get("message") or "plugin reported failure",
            error.get("details"),
        )

    output = result.get("output")
    if not isinstance(output, (dict, list)):
        raise PluginRuntimeError("plugin_failed", "plugin output must be an object or array")
    return output
