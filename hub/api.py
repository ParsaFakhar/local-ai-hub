"""Experimental local HTTP API (disabled by default).

Lets *other* local applications — Python scripts, Node programs, shortcuts,
editors — use the same universal task protocol without Firefox.

    ./bin/laihub-api                      # start it (config api.enabled must be true)
    curl -s localhost:8765/v1/status -H "Authorization: Bearer $(cat …/api-token)"

Bound to 127.0.0.1 only, bearer-token authenticated, no CORS: a webpage
cannot call it from the browser.  This is the seed of the future public API.
"""

from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import paths, protocol
from .config import load_config
from .engine import Engine
from .logging_util import get_logger, setup_logging

log = get_logger("api")


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


def make_server(config=None) -> ThreadingHTTPServer:
    config = config or load_config()
    setup_logging(config)
    engine = Engine(config)
    token = ensure_token(config)
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "LocalAIHub/0.1"

        def _reply(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            header = self.headers.get("Authorization", "")
            return header.startswith("Bearer ") and secrets.compare_digest(header[7:].strip(), token)

        def do_GET(self):  # noqa: N802
            if not self._authorized():
                return self._reply(401, protocol.make_error("-", "unauthorized", "missing or invalid bearer token"))
            routes = {
                "/v1/status": "status",
                "/v1/plugins": "plugins.list",
                "/v1/domains": "domains.list",
                "/v1/models": "models.list",
            }
            kind = routes.get(self.path.split("?")[0])
            if kind is None:
                return self._reply(404, protocol.make_error("-", "not_found", f"no route {self.path}"))
            with lock:
                result = engine.handle({"type": kind, "id": protocol.new_id("api")})
            return self._reply(200 if result.get("ok") else 500, result)

        def do_POST(self):  # noqa: N802
            if not self._authorized():
                return self._reply(401, protocol.make_error("-", "unauthorized", "missing or invalid bearer token"))
            if self.path.split("?")[0] != "/v1/task":
                return self._reply(404, protocol.make_error("-", "not_found", f"no route {self.path}"))
            length = int(self.headers.get("Content-Length") or 0)
            if length > int(config.get("security.max_input_bytes", 4 * 1024 * 1024)):
                return self._reply(413, protocol.make_error("-", "input_too_large", "request body too large"))
            try:
                message = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError as exc:
                return self._reply(400, protocol.make_error("-", "bad_request", str(exc)))
            if not isinstance(message, dict):
                return self._reply(400, protocol.make_error("-", "bad_request", "body must be a JSON object"))
            message.setdefault("type", "task")
            message.setdefault("id", protocol.new_id("api"))
            context = message.setdefault("context", {})
            context.setdefault("interface", "http")
            with lock:
                result = engine.handle(message)
            return self._reply(200 if result.get("ok") else 400, result)

        def log_message(self, fmt, *args):  # silence default stderr spam
            log.debug("%s - %s", self.address_string(), fmt % args)

    host = str(config.get("api.host", "127.0.0.1"))
    port = int(config.get("api.port", 8765))
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    log.info("local API listening on http://%s:%d (token required)", host, port)
    return httpd


def main() -> int:
    config = load_config()
    if not config.get("api.enabled", False):
        print("The local API is disabled. Set api.enabled = true in config/hub.json (or run ./install.sh --api).")
        return 2
    server = make_server(config)
    print(f"Local AI Hub API on http://{server.server_address[0]}:{server.server_address[1]}")
    print(f"Token file: {paths.data_dir() / str(config.get('api.token_file', 'api-token'))}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
