"""Local HTTP API (disabled by default).

Lets *other* local applications — Python scripts, Node programs, shortcuts,
editors — use the same universal task protocol without Firefox.

    ./install.sh --api                    # enable it and create a token
    ./bin/laihub-api                      # start it
    curl -s localhost:8765/v1/status -H "Authorization: Bearer $(cat …/api-token)"

Bound to 127.0.0.1 only, bearer-token authenticated, no CORS headers, and any
request carrying a browser ``Origin`` is refused unless explicitly allowlisted.
A webpage therefore cannot drive it.

Routes
------

======================================  =====================================
``GET  /v1/ping``                       liveness + version
``GET  /v1/status``                     engine status
``GET  /v1/plugins``                    manifest summaries
``GET  /v1/domains``                    configured domains
``GET  /v1/domains/<domain>``           effective config for one domain
``GET  /v1/models``                     model backend status
``GET  /v1/config``                     effective hub config
``GET  /v1/tasks``                      running tasks + recent history
``GET  /v1/events``                     Server-Sent Events stream
``GET  /v1/openapi.json``               machine-readable description
``POST /v1/task``                       run a task
``POST /v1/task/cancel``                cancel a running task
``POST /v1/models/unload``              free model memory
``POST /v1/cache/clear``                drop cached results
``POST /v1/shutdown``                   clean shutdown
======================================  =====================================
"""

from __future__ import annotations

import json
import queue
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import paths, protocol
from .config import load_config
from .engine import Engine
from .logging_util import get_logger, setup_logging

log = get_logger("api")

#: GET routes that map straight onto a control message.
GET_ROUTES = {
    "/v1/ping": "ping",
    "/v1/status": "status",
    "/v1/plugins": "plugins.list",
    "/v1/domains": "domains.list",
    "/v1/models": "models.list",
    "/v1/config": "config.get",
    "/v1/tasks": "tasks.list",
}

#: POST routes that map onto a control message (body becomes the request).
POST_ROUTES = {
    "/v1/task/cancel": "task.cancel",
    "/v1/models/unload": "models.unload",
    "/v1/cache/clear": "cache.clear",
    "/v1/shutdown": "shutdown",
}


def ensure_token(config) -> str:
    token_path = paths.data_dir() / str(config.get("api.token_file", "api-token"))
    if token_path.is_file():
        token = token_path.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(32)
    token_path.write_text(token, encoding="utf-8")
    try:
        token_path.chmod(0o600)
    except OSError:
        pass
    log.info("generated API token at %s", token_path)
    return token


def openapi_document(port: int = 8765) -> dict:
    """A small, honest OpenAPI 3.1 description of the local surface."""

    def envelope(summary: str) -> dict:
        return {
            "summary": summary,
            "responses": {
                "200": {
                    "description": "protocol result envelope",
                    "content": {"application/json": {"schema": {"type": "object"}}},
                },
                "401": {"description": "missing or invalid bearer token"},
            },
        }

    paths_doc: dict[str, dict] = {}
    for route, kind in GET_ROUTES.items():
        paths_doc[route] = {"get": envelope(f"{kind} control message")}
    paths_doc["/v1/domains/{domain}"] = {
        "get": {
            **envelope("domains.get for one domain"),
            "parameters": [
                {
                    "name": "domain",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string"},
                }
            ],
        }
    }
    paths_doc["/v1/task"] = {
        "post": {
            "summary": "run a universal task",
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "required": ["plugin"],
                            "properties": {
                                "plugin": {"type": "string"},
                                "input": {"type": "object"},
                                "options": {"type": "object"},
                                "context": {"type": "object"},
                                "id": {"type": "string"},
                            },
                        }
                    }
                },
            },
            "responses": {
                "200": {"description": "task result envelope"},
                "400": {"description": "task failed; error envelope"},
                "401": {"description": "missing or invalid bearer token"},
                "413": {"description": "body exceeds security.max_input_bytes"},
            },
        }
    }
    for route, kind in POST_ROUTES.items():
        paths_doc[route] = {"post": envelope(f"{kind} control message")}
    paths_doc["/v1/events"] = {
        "get": {
            "summary": "Server-Sent Events stream of engine events",
            "description": (
                "Each event is a protocol event envelope encoded as an SSE "
                "`data:` line. EventSource cannot set headers, so this route "
                "also accepts ?access_token=<token> when api.allow_token_query "
                "is true."
            ),
            "responses": {"200": {"description": "text/event-stream"}},
        }
    }
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Local AI Hub",
            "version": "1.0",
            "description": "Localhost-only, bearer-token authenticated task API.",
        },
        "servers": [{"url": f"http://127.0.0.1:{port}"}],
        "components": {
            "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}
        },
        "security": [{"bearer": []}],
        "paths": paths_doc,
    }


class EventBus:
    """Fan-out of engine events to any number of SSE subscribers."""

    def __init__(self, max_subscribers: int = 16, queue_size: int = 256):
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue] = []
        self.max_subscribers = max_subscribers
        self.queue_size = queue_size

    def subscribe(self) -> queue.Queue | None:
        with self._lock:
            if len(self._subscribers) >= self.max_subscribers:
                return None
            sink: queue.Queue = queue.Queue(maxsize=self.queue_size)
            self._subscribers.append(sink)
            return sink

    def unsubscribe(self, sink: queue.Queue) -> None:
        with self._lock:
            if sink in self._subscribers:
                self._subscribers.remove(sink)

    def publish(self, event: dict) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for sink in subscribers:
            try:
                sink.put_nowait(event)
            except queue.Full:
                # A slow reader must never block the engine; drop for that reader.
                pass

    def count(self) -> int:
        with self._lock:
            return len(self._subscribers)


def make_server(config=None) -> ThreadingHTTPServer:
    config = config or load_config()
    setup_logging(config)
    bus = EventBus()
    engine = Engine(config, emit=bus.publish)
    token = ensure_token(config)
    lock = threading.Lock()
    allowed_origins = {str(origin) for origin in (config.get("api.allowed_origins") or [])}
    allow_token_query = bool(config.get("api.allow_token_query", True))
    keepalive = float(config.get("api.sse_keepalive_seconds", 15))
    max_input = int(config.get("security.max_input_bytes", 4 * 1024 * 1024))
    started_at = time.time()

    class Handler(BaseHTTPRequestHandler):
        server_version = "LocalAIHub/1.0"
        protocol_version = "HTTP/1.1"

        # -- helpers ----------------------------------------------------
        def _reply(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            # Deliberately no Access-Control-Allow-* headers.
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _error(self, status: int, code: str, message: str) -> None:
            # We may not have drained the request body; do not reuse the socket.
            self.close_connection = True
            self._reply(status, protocol.make_error("-", code, message))

        def _token_from_request(self, parsed) -> str:
            header = self.headers.get("Authorization", "")
            if header.startswith("Bearer "):
                return header[7:].strip()
            if allow_token_query and parsed.path == "/v1/events":
                values = parse_qs(parsed.query).get("access_token") or []
                if values:
                    return values[0]
            return ""

        def _authorized(self, parsed) -> bool:
            supplied = self._token_from_request(parsed)
            return bool(supplied) and secrets.compare_digest(supplied, token)

        def _origin_ok(self) -> bool:
            """Refuse browser-originated requests (defence in depth, no CORS)."""
            origin = self.headers.get("Origin")
            if not origin:
                return True
            return origin in allowed_origins

        def _host_ok(self) -> bool:
            """Reject DNS-rebinding: only loopback Host values are accepted."""
            host = (self.headers.get("Host") or "").split(":")[0].strip("[]").lower()
            if not host:
                return True  # HTTP/1.0 clients
            return host in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}

        def _guard(self, parsed) -> bool:
            if not self._host_ok():
                self._error(403, protocol.ErrorCode.FORBIDDEN, "Host header is not loopback")
                return False
            if not self._origin_ok():
                self._error(
                    403,
                    protocol.ErrorCode.FORBIDDEN,
                    "browser Origin is not allowlisted; this API is for local programs",
                )
                return False
            if not self._authorized(parsed):
                self._error(
                    401,
                    protocol.ErrorCode.UNAUTHORIZED,
                    "missing or invalid bearer token",
                )
                return False
            return True

        def _read_body(self) -> dict | None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._error(400, protocol.ErrorCode.BAD_REQUEST, "invalid Content-Length")
                return None
            if length < 0:
                self._error(400, protocol.ErrorCode.BAD_REQUEST, "invalid Content-Length")
                return None
            if length > max_input:
                self._error(
                    413, protocol.ErrorCode.INPUT_TOO_LARGE, "request body too large"
                )
                return None
            raw = self.rfile.read(length) if length else b"{}"
            try:
                message = json.loads(raw or b"{}")
            except json.JSONDecodeError as exc:
                self._error(400, protocol.ErrorCode.BAD_REQUEST, str(exc))
                return None
            if not isinstance(message, dict):
                self._error(
                    400, protocol.ErrorCode.BAD_REQUEST, "body must be a JSON object"
                )
                return None
            return message

        def _control(self, kind: str, extra: dict | None = None) -> None:
            request = {"type": kind, "id": protocol.new_id("api")}
            request.update(extra or {})
            with lock:
                result = engine.handle(request)
            self._reply(200 if result.get("ok") else 500, result)

        # -- routes -----------------------------------------------------
        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            route = parsed.path.rstrip("/") or "/"
            if not self._guard(parsed):
                return

            if route == "/v1/events":
                return self._stream_events()
            if route == "/v1/openapi.json":
                return self._reply(200, openapi_document(self.server.server_address[1]))
            if route in ("/", "/v1"):
                return self._reply(
                    200,
                    {
                        "app": "Local AI Hub",
                        "api": "v1",
                        "uptime_seconds": int(time.time() - started_at),
                        "routes": sorted(
                            list(GET_ROUTES)
                            + list(POST_ROUTES)
                            + ["/v1/task", "/v1/events", "/v1/domains/{domain}", "/v1/openapi.json"]
                        ),
                    },
                )
            if route.startswith("/v1/domains/"):
                domain = unquote(route[len("/v1/domains/") :])
                if not domain:
                    return self._error(
                        400, protocol.ErrorCode.BAD_REQUEST, "domain is required"
                    )
                return self._control("domains.get", {"domain": domain})

            kind = GET_ROUTES.get(route)
            if kind is None:
                return self._error(
                    404, protocol.ErrorCode.NOT_FOUND, f"no route {parsed.path}"
                )
            extra: dict = {}
            if kind == "tasks.list":
                values = parse_qs(parsed.query).get("limit") or []
                if values and values[0].isdigit():
                    extra["limit"] = int(values[0])
            self._control(kind, extra)

        def do_POST(self):  # noqa: N802
            parsed = urlparse(self.path)
            route = parsed.path.rstrip("/") or "/"
            if not self._guard(parsed):
                return

            if route == "/v1/task":
                message = self._read_body()
                if message is None:
                    return
                message.setdefault("type", "task")
                message.setdefault("id", protocol.new_id("api"))
                context = message.setdefault("context", {})
                if not isinstance(context, dict):
                    return self._error(
                        400, protocol.ErrorCode.BAD_REQUEST, "'context' must be an object"
                    )
                context.setdefault("interface", "http")
                with lock:
                    result = engine.handle(message)
                return self._reply(200 if result.get("ok") else 400, result)

            kind = POST_ROUTES.get(route)
            if kind is None:
                return self._error(
                    404, protocol.ErrorCode.NOT_FOUND, f"no route {parsed.path}"
                )
            message = self._read_body()
            if message is None:
                return
            if route == "/v1/task/cancel":
                target = message.get("task_id") or message.get("id") or message.get("target")
                if not target:
                    return self._error(
                        400,
                        protocol.ErrorCode.BAD_REQUEST,
                        "'task_id' is required (use '*' to cancel every running task)",
                    )
                return self._control(kind, {"task_id": target})
            self._control(kind)
            if kind == "shutdown":
                threading.Thread(target=self.server.shutdown, daemon=True).start()

        # -- SSE --------------------------------------------------------
        def _stream_events(self):
            sink = bus.subscribe()
            if sink is None:
                return self._error(
                    503, protocol.ErrorCode.RATE_LIMITED, "too many event subscribers"
                )
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            def write(chunk: str) -> bool:
                try:
                    self.wfile.write(chunk.encode("utf-8"))
                    self.wfile.flush()
                    return True
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return False

            try:
                if not write(
                    "event: ready\ndata: "
                    + json.dumps({"app": "Local AI Hub", "time_ms": protocol.now_ms()})
                    + "\n\n"
                ):
                    return
                while True:
                    try:
                        event = sink.get(timeout=keepalive)
                    except queue.Empty:
                        if not write(": keepalive\n\n"):
                            return
                        continue
                    name = str(event.get("event") or "message")
                    if not write(
                        f"event: {name}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                    ):
                        return
            finally:
                bus.unsubscribe(sink)
                self.close_connection = True

        def log_message(self, fmt, *args):  # silence default stderr spam
            log.debug("%s - %s", self.address_string(), fmt % args)

    host = str(config.get("api.host", "127.0.0.1"))
    port = int(config.get("api.port", 8765))
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    httpd.hub_engine = engine  # for tests / embedding
    httpd.hub_events = bus
    log.info("local API listening on http://%s:%d (token required)", host, port)
    return httpd


def main() -> int:
    config = load_config()
    if not config.get("api.enabled", False):
        print(
            "The local API is disabled. Set api.enabled = true in config/hub.json "
            "(or run ./install.sh --api)."
        )
        return 2
    server = make_server(config)
    print(f"Local AI Hub API on http://{server.server_address[0]}:{server.server_address[1]}")
    print(f"Token file: {paths.data_dir() / str(config.get('api.token_file', 'api-token'))}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.server_close()
        server.hub_engine.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())