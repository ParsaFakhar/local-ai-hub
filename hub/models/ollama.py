"""Minimal Ollama HTTP client (stdlib only, no third-party dependencies)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from ..logging_util import get_logger

log = get_logger("ollama")


class OllamaError(RuntimeError):
    def __init__(self, message: str, code: str = "model_error", status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


class OllamaClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 11434, timeout: float = 240):
        self.base_url = f"http://{host}:{port}"
        self.timeout = timeout

    # ------------------------------------------------------------------
    def _request(self, method: str, path: str, body: dict | None = None, timeout: float | None = None) -> dict:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            url, data=data, method=method, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            try:
                detail = json.loads(detail).get("error", detail)
            except (json.JSONDecodeError, AttributeError):
                pass
            raise OllamaError(f"Ollama HTTP {exc.code}: {detail}", status=exc.code) from exc
        except urllib.error.URLError as exc:
            raise OllamaError(
                f"cannot reach Ollama at {self.base_url} ({exc.reason})", code="model_unavailable"
            ) from exc
        except (TimeoutError, json.JSONDecodeError) as exc:
            raise OllamaError(f"Ollama request failed: {exc}") from exc

    def _stream(
        self,
        path: str,
        body: dict,
        on_token=None,
        timeout: float | None = None,
    ) -> dict:
        """POST with ``stream: true`` and re-assemble the response.

        Ollama answers with newline-delimited JSON frames; each carries a
        fragment (``response`` for /api/generate, ``message.content`` for
        /api/chat) and the last one has ``done: true`` plus timing counters.
        ``on_token`` is called with every non-empty fragment so callers can
        forward partial output while the model is still running.
        """
        url = f"{self.base_url}{path}"
        request = urllib.request.Request(
            url,
            data=json.dumps({**body, "stream": True}).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        pieces: list[str] = []
        final: dict = {}
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                for raw in response:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    try:
                        frame = json.loads(line)
                    except json.JSONDecodeError:
                        log.debug("ignoring non-JSON stream frame: %s", line[:120])
                        continue
                    if not isinstance(frame, dict):
                        continue
                    if frame.get("error"):
                        raise OllamaError(f"Ollama stream error: {frame['error']}")
                    fragment = frame.get("response")
                    if fragment is None:
                        fragment = (frame.get("message") or {}).get("content", "")
                    if fragment:
                        pieces.append(fragment)
                        if on_token is not None:
                            on_token(fragment)
                    if frame.get("done"):
                        final = frame
                        break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise OllamaError(f"Ollama HTTP {exc.code}: {detail}", status=exc.code) from exc
        except urllib.error.URLError as exc:
            raise OllamaError(
                f"cannot reach Ollama at {self.base_url} ({exc.reason})", code="model_unavailable"
            ) from exc
        except (TimeoutError, OSError) as exc:
            raise OllamaError(f"Ollama stream failed: {exc}") from exc

        text = "".join(pieces)
        if path.endswith("/chat"):
            merged = dict(final)
            merged["message"] = {**(final.get("message") or {}), "content": text}
            return merged
        merged = dict(final)
        merged["response"] = text
        return merged

    def is_alive(self, timeout: float = 2.0) -> bool:
        try:
            self._request("GET", "/api/version", timeout=timeout)
            return True
        except OllamaError:
            return False

    def version(self) -> str:
        return str(self._request("GET", "/api/version", timeout=5).get("version", "unknown"))

    def tags(self) -> list[dict]:
        return self._request("GET", "/api/tags", timeout=10).get("models", []) or []

    def running(self) -> list[dict]:
        return self._request("GET", "/api/ps", timeout=10).get("models", []) or []

    def generate(
        self,
        model: str,
        prompt: str,
        *,
        system: str | None = None,
        images: list[str] | None = None,
        fmt: Any = None,
        options: dict | None = None,
        keep_alive: Any = "5m",
        stream: bool = False,
        on_token=None,
    ) -> dict:
        body: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "keep_alive": keep_alive,
            "options": options or {},
        }
        if system:
            body["system"] = system
        if images:
            body["images"] = images
        if fmt is not None:
            body["format"] = fmt
        if stream:
            return self._stream("/api/generate", body, on_token=on_token)
        return self._request("POST", "/api/generate", body)

    def chat(
        self,
        model: str,
        messages: list[dict],
        *,
        fmt: Any = None,
        options: dict | None = None,
        keep_alive: Any = "5m",
        stream: bool = False,
        on_token=None,
    ) -> dict:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "keep_alive": keep_alive,
            "options": options or {},
        }
        if fmt is not None:
            body["format"] = fmt
        if stream:
            return self._stream("/api/chat", body, on_token=on_token)
        return self._request("POST", "/api/chat", body)

    def unload(self, model: str) -> None:
        """Free the model from memory (keep_alive=0 with an empty prompt)."""
        try:
            self._request("POST", "/api/generate", {"model": model, "prompt": "", "keep_alive": 0}, timeout=30)
        except OllamaError as exc:
            log.debug("unload %s failed: %s", model, exc)
