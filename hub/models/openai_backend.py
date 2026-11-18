"""OpenAI-compatible HTTP client (llama.cpp ``llama-server``, LM Studio, ...).

Implements the same surface as :class:`~hub.models.ollama.OllamaClient` so
``ModelManager`` can swap backends with ``ollama.backend``.  Stdlib only.

Differences from the Ollama wire format, all handled here:

* ``GET /v1/models`` instead of ``/api/tags``; there is no standard
  running-models endpoint, so ``running()`` returns ``[]``.
* Both ``generate`` and ``chat`` go to ``POST /v1/chat/completions``;
  Ollama-style messages (``images`` = bare base64) become OpenAI content
  parts with ``data:`` URIs.
* ``fmt`` becomes ``response_format`` (``json_object`` / ``json_schema``);
  llama.cpp and LM Studio both support grammar/schema-constrained output.
* Sampling options are mapped (``temperature``, ``top_p``, ``seed``,
  ``stop``, ``num_predict`` -> ``max_tokens``); Ollama-only keys such as
  ``num_ctx`` / ``top_k`` are dropped — those are server-startup settings
  on OpenAI-compatible servers.
* ``keep_alive`` is ignored (no such concept); ``load()`` is a 1-token
  completion, which triggers LM Studio's just-in-time load and is a no-op
  for an already-loaded llama.cpp model; ``unload()`` is a no-op.
* Optional bearer token (``ollama.api_key``) for servers started with
  ``--api-key`` (llama.cpp) or token auth enabled (LM Studio).

Errors are raised as :class:`OllamaError` — the shared model-transport error
type (name kept for compatibility); ``ModelManager`` catches it for every
backend.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from ..logging_util import get_logger
from .ollama import OllamaError

log = get_logger("openai")

# Ollama option key -> OpenAI chat-completions key.  Anything not listed is
# dropped: unknown fields make some servers reject the whole request.
_OPTION_MAP = {
    "temperature": "temperature",
    "top_p": "top_p",
    "seed": "seed",
    "stop": "stop",
    "num_predict": "max_tokens",
    "max_tokens": "max_tokens",
}


def _image_mime(b64: str) -> str:
    """Sniff the image type from the base64 magic bytes (Ollama sends none)."""
    if b64.startswith("/9j/"):
        return "image/jpeg"
    if b64.startswith("R0lGOD"):
        return "image/gif"
    if b64.startswith("UklGR"):
        return "image/webp"
    return "image/png"  # iVBOR... and anything unknown


def _content_parts(text: str, images: list[str] | None) -> Any:
    if not images:
        return text
    parts: list[dict] = [{"type": "text", "text": text}]
    for b64 in images:
        parts.append(
            {"type": "image_url", "image_url": {"url": f"data:{_image_mime(b64)};base64,{b64}"}}
        )
    return parts


class OpenAICompatClient:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 1234,
        timeout: float = 240,
        api_key: str = "",
    ):
        self.base_url = f"http://{host}:{port}"
        self.timeout = timeout
        self.api_key = api_key or ""

    # ------------------------------------------------------------------
    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _request(self, method: str, path: str, body: dict | None = None, timeout: float | None = None) -> dict:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method, headers=self._headers())
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            try:
                error = json.loads(detail).get("error", detail)
                detail = error.get("message", error) if isinstance(error, dict) else error
            except (json.JSONDecodeError, AttributeError):
                pass
            raise OllamaError(f"model server HTTP {exc.code}: {detail}", status=exc.code) from exc
        except urllib.error.URLError as exc:
            raise OllamaError(
                f"cannot reach model server at {self.base_url} ({exc.reason})",
                code="model_unavailable",
            ) from exc
        except (TimeoutError, json.JSONDecodeError) as exc:
            raise OllamaError(f"model server request failed: {exc}") from exc

    def _stream(self, path: str, body: dict, on_token=None, timeout: float | None = None) -> str:
        """POST with ``stream: true`` and re-assemble the SSE response.

        OpenAI-compatible servers answer with Server-Sent Events: ``data:``
        lines carrying ``choices[0].delta.content`` fragments, terminated by
        ``data: [DONE]``.  Comment/keep-alive lines (``: ...``) are ignored.
        """
        url = f"{self.base_url}{path}"
        request = urllib.request.Request(
            url,
            data=json.dumps({**body, "stream": True}).encode("utf-8"),
            method="POST",
            headers=self._headers(),
        )
        pieces: list[str] = []
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                for raw in response:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line or line.startswith(":"):
                        continue
                    if not line.startswith("data:"):
                        continue
                    payload = line[len("data:"):].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        frame = json.loads(payload)
                    except json.JSONDecodeError:
                        log.debug("ignoring non-JSON stream frame: %s", payload[:120])
                        continue
                    if not isinstance(frame, dict):
                        continue
                    if frame.get("error"):
                        error = frame["error"]
                        message = error.get("message", error) if isinstance(error, dict) else error
                        raise OllamaError(f"model server stream error: {message}")
                    choices = frame.get("choices") or []
                    if not choices:
                        continue
                    fragment = (choices[0].get("delta") or {}).get("content")
                    if fragment:
                        pieces.append(fragment)
                        if on_token is not None:
                            on_token(fragment)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise OllamaError(f"model server HTTP {exc.code}: {detail}", status=exc.code) from exc
        except urllib.error.URLError as exc:
            raise OllamaError(
                f"cannot reach model server at {self.base_url} ({exc.reason})",
                code="model_unavailable",
            ) from exc
        except (TimeoutError, OSError) as exc:
            raise OllamaError(f"model server stream failed: {exc}") from exc
        return "".join(pieces)

    # ------------------------------------------------------------------
    def is_alive(self, timeout: float = 2.0) -> bool:
        try:
            self._request("GET", "/v1/models", timeout=timeout)
            return True
        except OllamaError:
            return False

    def version(self) -> str:
        # The OpenAI API has no version endpoint; report the protocol family.
        return "openai-compatible"

    def tags(self) -> list[dict]:
        data = self._request("GET", "/v1/models", timeout=10).get("data", []) or []
        models = []
        for entry in data:
            if isinstance(entry, dict) and entry.get("id"):
                models.append({**entry, "name": entry["id"]})
        return models

    def running(self) -> list[dict]:
        # No standard endpoint exposes loaded models; the manager's own
        # usage tracking (loaded_models) still works.
        return []

    # ------------------------------------------------------------------
    def _body(
        self,
        model: str,
        messages: list[dict],
        *,
        fmt: Any = None,
        options: dict | None = None,
    ) -> dict:
        body: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
        for key, value in (options or {}).items():
            mapped = _OPTION_MAP.get(key)
            if mapped is not None:
                body[mapped] = value
        if fmt == "json":
            body["response_format"] = {"type": "json_object"}
        elif isinstance(fmt, dict):
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "schema": fmt},
            }
        return body

    @staticmethod
    def _text(response: dict) -> str:
        choices = response.get("choices") or []
        if not choices:
            raise OllamaError("model server returned no choices")
        return (choices[0].get("message") or {}).get("content") or ""

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
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": _content_parts(prompt, images)})
        body = self._body(model, messages, fmt=fmt, options=options)
        if stream:
            text = self._stream("/v1/chat/completions", body, on_token=on_token)
        else:
            text = self._text(self._request("POST", "/v1/chat/completions", body))
        return {"response": text, "model": model, "done": True}

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
        converted = [
            {
                "role": message.get("role", "user"),
                "content": _content_parts(str(message.get("content", "")), message.get("images")),
            }
            for message in messages
        ]
        body = self._body(model, converted, fmt=fmt, options=options)
        if stream:
            text = self._stream("/v1/chat/completions", body, on_token=on_token)
        else:
            text = self._text(self._request("POST", "/v1/chat/completions", body))
        return {"message": {"role": "assistant", "content": text}, "model": model, "done": True}

    def load(self, model: str, *, keep_alive: Any = "5m", timeout: float | None = None) -> dict:
        """Warm-up: a 1-token completion forces the weights into memory.

        LM Studio loads just-in-time on the first request; llama.cpp already
        has the model loaded at startup, so this is a cheap no-op there.
        """
        return self._request(
            "POST",
            "/v1/chat/completions",
            {
                "model": model,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
                "stream": False,
            },
            timeout=timeout,
        )

    def unload(self, model: str) -> None:
        # No standard unload endpoint; the server manages its own memory.
        log.debug("unload %s ignored (openai-compatible backend)", model)
