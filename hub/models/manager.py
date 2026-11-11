"""Model runtime lifecycle: on-demand Ollama startup, loading and unloading.

Responsibilities
----------------
* find the ``ollama`` binary (PATH, Homebrew, /usr/local, Ollama.app)
* start ``ollama serve`` only when an AI task actually needs it
* resolve a requested model tag against the models actually installed
* unload idle models to give RAM back to the machine (M1 Pro friendly)
* optionally stop the server again on shutdown if the hub started it
* provide an offline ``mock`` backend so the platform can be tested
  end-to-end without any model installed
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .. import paths, schema
from ..logging_util import get_logger
from .ollama import OllamaClient, OllamaError

log = get_logger("models")

_BINARY_CANDIDATES = (
    "/opt/homebrew/bin/ollama",
    "/usr/local/bin/ollama",
    "/usr/bin/ollama",
    "/Applications/Ollama.app/Contents/Resources/ollama",
)


class ModelError(RuntimeError):
    def __init__(self, message: str, code: str = "model_error"):
        super().__init__(message)
        self.code = code


def find_binary(configured: str = "auto") -> str | None:
    if configured and configured != "auto":
        return shutil.which(configured) or (configured if Path(configured).is_file() else None)
    found = shutil.which("ollama")
    if found:
        return found
    for candidate in _BINARY_CANDIDATES:
        if Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


# --------------------------------------------------------------------------
# Offline mock backend (tests / development without a GPU in the loop)
# --------------------------------------------------------------------------


def _mock_fill(value: Any, key: str = "") -> Any:
    """Make schema samples readable in offline demos (test backend only)."""
    if isinstance(value, dict):
        return {k: _mock_fill(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_mock_fill(item, key) for item in value]
    if isinstance(value, str) and not value:
        return f"[mock] {key or 'value'}"
    if isinstance(value, (int, float)) and not isinstance(value, bool) and key == "confidence":
        return 0.5
    return value


def _mock_payload(prompt: str, fmt: Any, messages: list[dict] | None = None) -> str:
    text = prompt or "\n".join(str(m.get("content", "")) for m in (messages or []))
    if isinstance(fmt, dict):
        value = _mock_fill(schema.example_from_schema(fmt))
        # Make id-bearing result arrays line up with the ids present in the prompt.
        ids = re.findall(r'"id"\s*:\s*"([^"]{1,64})"', text)
        if ids:
            for key, sub in (fmt.get("properties") or {}).items():
                if sub.get("type") == "array" and "id" in ((sub.get("items") or {}).get("properties") or {}):
                    template = _mock_fill(schema.example_from_schema(sub["items"]))
                    value[key] = [dict(template, id=identifier) for identifier in ids]
        return json.dumps(value, ensure_ascii=False)
    if fmt == "json":
        return json.dumps({"mock": True, "text": "offline mock response"})
    return "[mock] Local AI Hub offline response."


def _mock_tokens(text: str):
    """Split a mock response into word-ish fragments (offline streaming)."""
    pieces = re.findall(r"\S+\s*", text)
    return pieces or ([text] if text else [])


class ModelManager:
    def __init__(self, config, on_event=None):
        self.config = config
        self.on_event = on_event
        self.client = OllamaClient(
            host=config.get("ollama.host", "127.0.0.1"),
            port=int(config.get("ollama.port", 11434)),
            timeout=float(config.get("ollama.request_timeout_seconds", 240)),
        )
        self.mock = bool(config.get("ollama.mock", False))
        self.binary = find_binary(config.get("ollama.binary", "auto"))
        self._process: subprocess.Popen | None = None
        self.started_by_hub = False
        self._lock = threading.RLock()
        self._loaded: dict[str, float] = {}
        self._tags_cache: tuple[float, list[str]] = (0.0, [])
        self._last_used = 0.0
        self._reaper: threading.Thread | None = None
        self._stopping = threading.Event()

    # ------------------------------------------------------------------
    def _emit(self, name: str, data: dict) -> None:
        if self.on_event:
            try:
                self.on_event(name, data)
            except Exception:  # noqa: BLE001 - events are best effort
                log.debug("event sink failed for %s", name, exc_info=True)

    @property
    def default_model(self) -> str:
        return str(self.config.get("ollama.default_model", "qwen3.5:4b"))

    @property
    def vision_model(self) -> str:
        return str(self.config.get("ollama.vision_model") or self.default_model)

    # ------------------------------------------------------------------
    def ensure_server(self) -> None:
        if self.mock:
            return
        if self.client.is_alive():
            return
        with self._lock:
            if self.client.is_alive():
                return
            if not self.config.get("ollama.auto_start", True):
                raise ModelError(
                    "Ollama is not running and auto_start is disabled", "model_unavailable"
                )
            if not self.binary:
                raise ModelError(
                    "Ollama binary not found. Install it from https://ollama.com/download "
                    "or set ollama.binary in config/hub.json",
                    "model_unavailable",
                )
            self._emit("model.server_starting", {"binary": self.binary})
            log.info("starting ollama serve (%s)", self.binary)
            log_path = paths.log_dir() / "ollama.log"
            with log_path.open("ab") as sink:
                self._process = subprocess.Popen(  # noqa: S603 - fixed argv, local trusted binary
                    [self.binary, "serve"],
                    stdout=sink,
                    stderr=sink,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                    env={**os.environ, "OLLAMA_HOST": f"{self.config.get('ollama.host')}:{self.config.get('ollama.port')}"},
                )
            self.started_by_hub = True
            deadline = time.monotonic() + float(self.config.get("ollama.startup_timeout_seconds", 45))
            while time.monotonic() < deadline:
                if self._process.poll() is not None:
                    raise ModelError(
                        f"ollama serve exited immediately (code {self._process.returncode}); see {log_path}",
                        "model_unavailable",
                    )
                if self.client.is_alive(timeout=1.5):
                    log.info("ollama is up (version %s)", self.client.version())
                    self._emit("model.server_ready", {"started_by_hub": True})
                    self._start_reaper()
                    return
                time.sleep(0.4)
            raise ModelError("timed out waiting for ollama serve", "model_unavailable")

    def installed_models(self, refresh: bool = False) -> list[str]:
        if self.mock:
            return [self.default_model, self.vision_model]
        cached_at, names = self._tags_cache
        if not refresh and time.monotonic() - cached_at < 30 and names:
            return names
        try:
            self.ensure_server()
            names = [str(entry.get("name")) for entry in self.client.tags() if entry.get("name")]
        except (ModelError, OllamaError) as exc:
            log.warning("cannot list models: %s", exc)
            return names
        self._tags_cache = (time.monotonic(), names)
        return names

    def resolve_model(self, requested: str | None) -> str:
        wanted = requested or self.default_model
        available = self.installed_models()
        if not available or wanted in available:
            return wanted
        base = wanted.split(":")[0]
        for name in available:
            if name.split(":")[0] == base:
                log.info("model %s not installed, using %s", wanted, name)
                return name
        for fallback in self.config.get("ollama.model_fallbacks", []) or []:
            if fallback in available:
                log.info("model %s not installed, falling back to %s", wanted, fallback)
                return fallback
        raise ModelError(
            f"model {wanted!r} is not installed. Run: ollama pull {wanted} "
            f"(installed: {', '.join(available) or 'none'})",
            "model_unavailable",
        )

    # ------------------------------------------------------------------
    def _streaming(self, requested: bool | None, on_token) -> bool:
        """Effective streaming decision for one call.

        A caller that supplies ``on_token`` wants streaming; otherwise the
        per-call flag wins, then ``ollama.stream`` from the config.
        """
        if on_token is not None:
            return True
        if requested is not None:
            return bool(requested)
        return bool(self.config.get("ollama.stream", False))

    def _options(self, overrides: dict | None) -> dict:
        options = {
            "temperature": float(self.config.get("ollama.temperature", 0.1)),
            "num_ctx": int(self.config.get("ollama.num_ctx", 8192)),
        }
        options.update(overrides or {})
        return options

    def generate(
        self,
        prompt: str,
        *,
        model: str | None = None,
        system: str | None = None,
        images: list[str] | None = None,
        fmt: Any = None,
        options: dict | None = None,
        stream: bool | None = None,
        on_token=None,
    ) -> dict:
        resolved = self.resolve_model(model or (self.vision_model if images else self.default_model))
        keep_alive = self.config.get("ollama.keep_alive", "5m")
        streaming = self._streaming(stream, on_token)
        started = time.monotonic()
        self._emit("model.generating", {"model": resolved, "images": len(images or []), "stream": streaming})

        tokens = 0
        sink = None
        if streaming and on_token is not None:
            def sink(fragment: str) -> None:
                nonlocal tokens
                tokens += 1
                on_token(fragment)

        if self.mock:
            text = _mock_payload(prompt, fmt)
            if sink is not None:
                for fragment in _mock_tokens(text):
                    sink(fragment)
        else:
            self.ensure_server()
            try:
                response = self.client.generate(
                    resolved,
                    prompt,
                    system=system,
                    images=images,
                    fmt=fmt,
                    options=self._options(options),
                    keep_alive=keep_alive,
                    stream=streaming,
                    on_token=sink,
                )
            except OllamaError as exc:
                raise ModelError(str(exc), exc.code) from exc
            text = response.get("response", "")

        self._touch(resolved)
        return {
            "text": text,
            "model": resolved,
            "mock": self.mock,
            "streamed": bool(streaming),
            "tokens": tokens,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }

    def chat(
        self,
        messages: list[dict],
        *,
        model: str | None = None,
        fmt: Any = None,
        options: dict | None = None,
        stream: bool | None = None,
        on_token=None,
    ) -> dict:
        has_images = any(message.get("images") for message in messages)
        resolved = self.resolve_model(model or (self.vision_model if has_images else self.default_model))
        streaming = self._streaming(stream, on_token)
        started = time.monotonic()
        self._emit("model.generating", {"model": resolved, "chat": True, "stream": streaming})

        tokens = 0
        sink = None
        if streaming and on_token is not None:
            def sink(fragment: str) -> None:
                nonlocal tokens
                tokens += 1
                on_token(fragment)

        if self.mock:
            text = _mock_payload("", fmt, messages)
            if sink is not None:
                for fragment in _mock_tokens(text):
                    sink(fragment)
        else:
            self.ensure_server()
            try:
                response = self.client.chat(
                    resolved,
                    messages,
                    fmt=fmt,
                    options=self._options(options),
                    keep_alive=self.config.get("ollama.keep_alive", "5m"),
                    stream=streaming,
                    on_token=sink,
                )
            except OllamaError as exc:
                raise ModelError(str(exc), exc.code) from exc
            text = (response.get("message") or {}).get("content", "")

        self._touch(resolved)
        return {
            "text": text,
            "model": resolved,
            "mock": self.mock,
            "streamed": bool(streaming),
            "tokens": tokens,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }

    # ------------------------------------------------------------------
    def _touch(self, model: str) -> None:
        with self._lock:
            self._loaded[model] = time.monotonic()
            self._last_used = time.monotonic()
        self._start_reaper()

    def _start_reaper(self) -> None:
        if self.mock or self._reaper is not None:
            return
        idle = float(self.config.get("ollama.idle_unload_seconds", 300))
        if idle <= 0:
            return

        def loop() -> None:
            while not self._stopping.wait(min(idle, 30)):
                with self._lock:
                    now = time.monotonic()
                    stale = [name for name, used in self._loaded.items() if now - used >= idle]
                    for name in stale:
                        self._loaded.pop(name, None)
                for name in stale:
                    log.info("unloading idle model %s", name)
                    self.client.unload(name)
                    self._emit("model.unloaded", {"model": name, "reason": "idle"})

        self._reaper = threading.Thread(target=loop, name="ollama-idle-reaper", daemon=True)
        self._reaper.start()

    def unload_all(self) -> list[str]:
        with self._lock:
            names = list(self._loaded)
            self._loaded.clear()
        if not self.mock:
            for name in names:
                self.client.unload(name)
                self._emit("model.unloaded", {"model": name, "reason": "requested"})
        return names

    def status(self) -> dict:
        alive = True if self.mock else self.client.is_alive()
        running: list[str] = []
        if alive and not self.mock:
            try:
                running = [str(entry.get("name")) for entry in self.client.running()]
            except OllamaError:
                running = []
        return {
            "backend": "mock" if self.mock else "ollama",
            "binary": self.binary,
            "base_url": self.client.base_url,
            "running": alive,
            "started_by_hub": self.started_by_hub,
            "default_model": self.default_model,
            "vision_model": self.vision_model,
            "loaded_models": sorted(set(self._loaded) | set(running)),
            "installed_models": self.installed_models() if alive else [],
            "idle_unload_seconds": self.config.get("ollama.idle_unload_seconds"),
            "stream": bool(self.config.get("ollama.stream", False)),
            "last_used_ms_ago": int((time.monotonic() - self._last_used) * 1000) if self._last_used else None,
        }

    def shutdown(self) -> None:
        self._stopping.set()
        self.unload_all()
        if self.started_by_hub and self._process and self.config.get("ollama.stop_on_exit", False):
            log.info("stopping ollama serve started by the hub")
            self._process.terminate()
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()
        self._process = None
