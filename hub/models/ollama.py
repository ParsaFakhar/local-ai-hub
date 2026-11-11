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
        return self._request("POST", "/api/generate", body)

    def chat(
        self,
        model: str,
        messages: list[dict],
        *,
        fmt: Any = None,
        options: dict | None = None,
        keep_alive: Any = "5m",
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
        return self._request("POST", "/api/chat", body)

    def unload(self, model: str) -> None:
        """Free the model from memory (keep_alive=0 with an empty prompt)."""
        try:
            self._request("POST", "/api/generate", {"model": model, "prompt": "", "keep_alive": 0}, timeout=30)
        except OllamaError as exc:
            log.debug("unload %s failed: %s", model, exc)
