"""Tiny stdio-free client for the local HTTP API (stdlib only).

Used by the CLI for commands that only make sense against a *running* hub
(``laihub tasks``, ``laihub cancel``) and handy for your own programs::

    from hub.client import LocalClient

    client = LocalClient.discover()          # config + token file, TCP or socket
    print(client.status()["models"])
    print(client.task("hello.python", {"name": "M1 Pro"})["output"])

The API is opt-in, loopback-only and bearer-token authenticated; this client
never sends the token anywhere else and never follows redirects.
"""

from __future__ import annotations

import http.client
import json
import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterator

from . import paths, protocol
from .config import Config, load_config


class APIError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, envelope: dict | None = None):
        super().__init__(message)
        self.status = status
        self.envelope = envelope or {}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


class UnixHTTPConnection(http.client.HTTPConnection):
    """http.client over an AF_UNIX socket (``api.unix_socket``)."""

    def __init__(self, path: str, timeout: float = 30):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._path)
        self.sock = sock


class LocalClient:
    def __init__(
        self,
        token: str,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
        unix_socket: str | None = None,
        timeout: float = 60,
    ):
        self.token = token
        self.host = host
        self.port = int(port)
        self.unix_socket = str(unix_socket) if unix_socket else None
        self.timeout = timeout

    # ------------------------------------------------------------------
    @classmethod
    def discover(cls, config: Config | None = None, timeout: float = 60) -> "LocalClient":
        config = config or load_config()
        token_path = paths.data_dir() / str(config.get("api.token_file", "api-token"))
        token = token_path.read_text(encoding="utf-8").strip() if token_path.is_file() else ""
        socket_path = str(config.get("api.unix_socket") or "")
        resolved = None
        if socket_path:
            candidate = Path(socket_path).expanduser()
            resolved = str(candidate if candidate.is_absolute() else paths.data_dir() / candidate)
        return cls(
            token,
            host=str(config.get("api.host", "127.0.0.1")),
            port=int(config.get("api.port", 8765)),
            unix_socket=resolved,
            timeout=timeout,
        )

    @property
    def base_url(self) -> str:
        if self.unix_socket:
            return "http://localhost"
        return f"http://{self.host}:{self.port}"

    def available(self, timeout: float = 1.5) -> bool:
        """True when a hub answers ``GET /v1/ping`` with our token."""
        try:
            self.call("GET", "/v1/ping", timeout=timeout)
        except (APIError, OSError):
            return False
        return True

    # ------------------------------------------------------------------
    def call(
        self,
        method: str,
        route: str,
        body: dict | None = None,
        *,
        timeout: float | None = None,
    ) -> dict:
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        wait = timeout or self.timeout

        if self.unix_socket:
            connection = UnixHTTPConnection(self.unix_socket, timeout=wait)
            try:
                connection.request(method, route, body=payload, headers=headers)
                response = connection.getresponse()
                raw, status = response.read(), response.status
            finally:
                connection.close()
        else:
            request = urllib.request.Request(
                f"{self.base_url}{route}", data=payload, method=method, headers=headers
            )
            opener = urllib.request.build_opener(_NoRedirect)
            try:
                with opener.open(request, timeout=wait) as response:
                    raw, status = response.read(), response.status
            except urllib.error.HTTPError as exc:
                raw, status = exc.read(), exc.code
            except urllib.error.URLError as exc:
                raise APIError(f"cannot reach the local API at {self.base_url}: {exc.reason}") from exc

        try:
            envelope = json.loads(raw or b"{}")
        except json.JSONDecodeError as exc:
            raise APIError(f"API returned invalid JSON (HTTP {status})", status) from exc
        if status >= 400 or not envelope.get("ok", False):
            error = envelope.get("error") or {}
            raise APIError(
                error.get("message") or f"API request failed (HTTP {status})", status, envelope
            )
        return envelope

    # -- convenience ---------------------------------------------------
    def ping(self) -> dict:
        return self.call("GET", "/v1/ping")["output"]

    def status(self) -> dict:
        return self.call("GET", "/v1/status")["output"]

    def plugins(self) -> dict:
        return self.call("GET", "/v1/plugins")["output"]

    def tasks(self, limit: int | None = None) -> dict:
        route = "/v1/tasks" + (f"?limit={int(limit)}" if limit else "")
        return self.call("GET", route)["output"]

    def task_status(self, task_id: str) -> dict:
        return self.call("GET", f"/v1/tasks/{task_id}")["output"]["task"]

    def cancel(self, task_id: str = "*") -> dict:
        return self.call("POST", "/v1/task/cancel", {"task_id": task_id})["output"]

    def cache_clear(self) -> dict:
        return self.call("POST", "/v1/cache/clear")["output"]

    def unload(self) -> dict:
        return self.call("POST", "/v1/models/unload")["output"]

    def run(
        self,
        plugin: str,
        input_data: dict | None = None,
        *,
        options: dict | None = None,
        context: dict | None = None,
        task_id: str | None = None,
        timeout: float | None = None,
    ) -> dict:
        body = {
            "type": "task",
            "id": task_id or protocol.new_id("client"),
            "plugin": plugin,
            "input": input_data or {},
            "options": options or {},
            "context": {"interface": "http", **(context or {})},
        }
        return self.call("POST", "/v1/task", body, timeout=timeout)

    def events(self, max_events: int | None = None) -> Iterator[dict]:
        """Iterate Server-Sent Events (blocks; stop by closing the generator)."""
        if self.unix_socket:
            connection: Any = UnixHTTPConnection(self.unix_socket, timeout=self.timeout)
            connection.request(
                "GET", f"/v1/events?access_token={self.token}", headers={"Accept": "text/event-stream"}
            )
            response = connection.getresponse()
            stream = response
        else:
            request = urllib.request.Request(
                f"{self.base_url}/v1/events?access_token={self.token}",
                headers={"Accept": "text/event-stream"},
            )
            stream = urllib.request.urlopen(request, timeout=self.timeout)
        seen = 0
        try:
            for raw in stream:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    event = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                seen += 1
                yield event
                if max_events and seen >= max_events:
                    return
        finally:
            try:
                stream.close()
            except Exception:  # noqa: BLE001 - closing a live stream is best effort
                pass
