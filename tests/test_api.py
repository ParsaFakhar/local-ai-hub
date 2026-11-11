"""Local HTTP API: routes, auth, guards, SSE, OpenAPI and the unix transport.

Every test drives a real server over a real socket — no handler mocking — so
the framing, status codes and headers are exercised exactly as a local client
would see them.
"""

import contextlib
import io
import json
import os
import shutil
import socket
import stat
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from hub import api, paths, protocol
from hub.client import APIError, LocalClient
from hub.config import load_config


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class ServerTestCase(unittest.TestCase):
    """One engine + one TCP server per class, isolated state directory."""

    extra_config: dict = {}

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="lah-api-")
        cls._old_data = os.environ.get("LOCAL_AI_HUB_DATA")
        os.environ["LOCAL_AI_HUB_DATA"] = cls._tmp
        cls.port = free_port()
        settings = {
            "ollama": {"mock": True},
            "api": {"enabled": True, "port": cls.port, "token_file": "test-token"},
        }
        for key, value in cls.extra_config.items():
            settings.setdefault(key, {}).update(value)
        cls.config = load_config(settings)
        cls.token = api.ensure_token(cls.config)
        cls.server = api.make_server(cls.config)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server.hub_engine.shutdown()
        cls.thread.join(timeout=5)
        if cls._old_data is None:
            os.environ.pop("LOCAL_AI_HUB_DATA", None)
        else:
            os.environ["LOCAL_AI_HUB_DATA"] = cls._old_data
        shutil.rmtree(cls._tmp, ignore_errors=True)

    # -- helpers --------------------------------------------------------
    def request(self, method, route, body=None, headers=None, token="__default__", raw=False):
        merged = {}
        if token == "__default__":
            merged["Authorization"] = f"Bearer {self.token}"
        elif token:
            merged["Authorization"] = f"Bearer {token}"
        merged.update(headers or {})
        data = None
        if body is not None:
            data = body if raw else json.dumps(body).encode("utf-8")
            merged.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(self.base + route, data=data, headers=merged, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = response.read().decode("utf-8")
                return response.status, dict(response.headers), payload
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read().decode("utf-8")

    def call(self, method, route, body=None, **kwargs):
        status, _, payload = self.request(method, route, body, **kwargs)
        try:
            return status, json.loads(payload)
        except json.JSONDecodeError:
            return status, payload


class TokenTests(ServerTestCase):
    def test_token_file_is_created_with_owner_only_permissions(self):
        path = paths.data_dir() / "test-token"
        self.assertTrue(path.is_file())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(path.read_text().strip(), self.token)

    def test_token_is_stable_across_calls(self):
        self.assertEqual(api.ensure_token(self.config), self.token)

    def test_missing_token_is_unauthorized(self):
        status, payload = self.call("GET", "/v1/status", token=None)
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], protocol.ErrorCode.UNAUTHORIZED)

    def test_wrong_token_is_unauthorized(self):
        self.assertEqual(self.call("GET", "/v1/status", token="nope")[0], 401)

    def test_non_bearer_scheme_is_unauthorized(self):
        status, _ = self.call("GET", "/v1/status", token=None, headers={"Authorization": self.token})
        self.assertEqual(status, 401)

    def test_query_token_only_works_for_events(self):
        status, _ = self.call("GET", f"/v1/status?access_token={self.token}", token=None)
        self.assertEqual(status, 401)


class GuardTests(ServerTestCase):
    def test_browser_origin_is_refused(self):
        status, payload = self.call("GET", "/v1/status", headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], protocol.ErrorCode.FORBIDDEN)

    def test_no_cors_headers_are_sent(self):
        _, headers, _ = self.request("GET", "/v1/ping")
        self.assertFalse([name for name in headers if name.lower().startswith("access-control")])
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(headers.get("Cache-Control"), "no-store")

    def test_non_loopback_host_header_is_refused(self):
        status, payload = self.call("GET", "/v1/status", headers={"Host": "attacker.example"})
        self.assertEqual(status, 403)
        self.assertIn("Host", payload["error"]["message"])

    def test_loopback_host_header_is_accepted(self):
        self.assertEqual(self.call("GET", "/v1/ping", headers={"Host": f"localhost:{self.port}"} )[0], 200)


class RouteTests(ServerTestCase):
    def test_ping(self):
        status, payload = self.call("GET", "/v1/ping")
        self.assertEqual(status, 200)
        self.assertTrue(payload["output"]["pong"])
        self.assertEqual(payload["protocol"], protocol.PROTOCOL_VERSION)

    def test_index_lists_routes(self):
        status, payload = self.call("GET", "/")
        self.assertEqual(status, 200)
        for route in ("/v1/task", "/v1/tasks", "/v1/events", "/v1/openapi.json"):
            self.assertIn(route, payload["routes"])

    def test_trailing_slash_is_tolerated(self):
        self.assertEqual(self.call("GET", "/v1/status/")[0], 200)

    def test_control_routes_answer_envelopes(self):
        for route in ("/v1/status", "/v1/plugins", "/v1/domains", "/v1/models", "/v1/config", "/v1/tasks"):
            status, payload = self.call("GET", route)
            self.assertEqual(status, 200, route)
            self.assertTrue(payload["ok"], route)
            self.assertEqual(payload["type"], "result")

    def test_unknown_route_is_404(self):
        status, payload = self.call("GET", "/v1/nope")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], protocol.ErrorCode.NOT_FOUND)

    def test_domain_route(self):
        status, payload = self.call("GET", "/v1/domains/example-board.com")
        self.assertEqual(status, 200)
        self.assertIn("comments.classify", payload["output"]["features"])

    def test_trailing_slash_on_a_collection_lists_it(self):
        status, payload = self.call("GET", "/v1/domains/")
        self.assertEqual(status, 200)
        self.assertIn("domains", payload["output"])

    def test_task_round_trip(self):
        status, payload = self.call(
            "POST", "/v1/task", {"plugin": "hello.python", "input": {"name": "HTTP"}, "id": "http-task-1"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["id"], "http-task-1")
        self.assertEqual(payload["output"]["greeting"], "Hello, HTTP!")
        self.assertEqual(payload["meta"]["domain"], "")

    def test_task_defaults_interface_to_http(self):
        _, payload = self.call("POST", "/v1/task", {"plugin": "hello.python", "input": {}})
        self.assertEqual(payload["output"]["context"]["interface"], "http")

    def test_invalid_task_input_is_400(self):
        status, payload = self.call("POST", "/v1/task", {"plugin": "hello.python", "input": {"name": 42}})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], protocol.ErrorCode.INVALID_INPUT)

    def test_unknown_plugin_is_400_with_protocol_code(self):
        status, payload = self.call("POST", "/v1/task", {"plugin": "no.such.plugin", "input": {}})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], protocol.ErrorCode.UNKNOWN_PLUGIN)

    def test_malformed_json_is_400(self):
        status, payload = self.call("POST", "/v1/task", b"{not json", raw=True)
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], protocol.ErrorCode.BAD_REQUEST)

    def test_non_object_body_is_400(self):
        self.assertEqual(self.call("POST", "/v1/task", b"[1,2]", raw=True)[0], 400)

    def test_non_object_context_is_400(self):
        status, payload = self.call(
            "POST", "/v1/task", {"plugin": "hello.python", "input": {}, "context": "nope"}
        )
        self.assertEqual(status, 400)
        self.assertIn("context", payload["error"]["message"])

    def test_oversized_body_is_413(self):
        status, _, payload = self.request(
            "POST",
            "/v1/task",
            b"{}",
            raw=True,
            headers={"Content-Length": str(64 * 1024 * 1024)},
        )
        self.assertEqual(status, 413)
        self.assertEqual(json.loads(payload)["error"]["code"], protocol.ErrorCode.INPUT_TOO_LARGE)

    def test_invalid_content_length_is_400(self):
        status, _ = self.call(
            "POST", "/v1/task", b"{}", raw=True, headers={"Content-Length": "banana"}
        )
        self.assertEqual(status, 400)

    def test_task_history_and_single_task(self):
        self.call("POST", "/v1/task", {"plugin": "hello.python", "input": {"name": "H"}, "id": "hist-1"})
        status, listing = self.call("GET", "/v1/tasks?limit=5")
        self.assertEqual(status, 200)
        self.assertIn("hist-1", [entry["id"] for entry in listing["output"]["history"]])
        status, single = self.call("GET", "/v1/tasks/hist-1")
        self.assertEqual(status, 200)
        self.assertEqual(single["output"]["task"]["id"], "hist-1")
        self.assertEqual(single["output"]["task"]["state"], "finished")

    def test_unknown_task_is_404(self):
        status, payload = self.call("GET", "/v1/tasks/never-existed")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], protocol.ErrorCode.UNKNOWN_TASK)

    def test_trailing_slash_on_tasks_lists_the_collection(self):
        status, payload = self.call("GET", "/v1/tasks/")
        self.assertEqual(status, 200)
        self.assertIn("history", payload["output"])

    def test_cancel_requires_a_target(self):
        status, payload = self.call("POST", "/v1/task/cancel", {})
        self.assertEqual(status, 400)
        self.assertIn("task_id", payload["error"]["message"])

    def test_cancel_all(self):
        status, payload = self.call("POST", "/v1/task/cancel", {"task_id": "*"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["output"]["all"])

    def test_models_unload_and_cache_clear(self):
        self.assertEqual(self.call("POST", "/v1/models/unload", {})[0], 200)
        status, payload = self.call("POST", "/v1/cache/clear", {})
        self.assertEqual(status, 200)
        self.assertIn("cache", payload["output"])

    def test_post_to_unknown_route_is_404(self):
        self.assertEqual(self.call("POST", "/v1/nope", {})[0], 404)

    def test_get_on_a_post_route_is_404(self):
        self.assertEqual(self.call("GET", "/v1/cache/clear")[0], 404)


class OpenAPITests(ServerTestCase):
    def test_document_describes_every_route(self):
        status, document = self.call("GET", "/v1/openapi.json")
        self.assertEqual(status, 200)
        self.assertTrue(document["openapi"].startswith("3."))
        for route in api.GET_ROUTES:
            self.assertIn(route, document["paths"])
        for route in api.POST_ROUTES:
            self.assertIn(route, document["paths"])
        for route in ("/v1/task", "/v1/events", "/v1/tasks/{id}", "/v1/domains/{domain}"):
            self.assertIn(route, document["paths"])

    def test_document_declares_bearer_auth(self):
        _, document = self.call("GET", "/v1/openapi.json")
        schemes = document["components"]["securitySchemes"]
        self.assertEqual(schemes["bearer"], {"type": "http", "scheme": "bearer"})
        self.assertEqual(document["security"], [{"bearer": []}])

    def test_tasks_route_documents_the_limit_parameter(self):
        _, document = self.call("GET", "/v1/openapi.json")
        parameters = document["paths"]["/v1/tasks"]["get"]["parameters"]
        self.assertIn("limit", [parameter["name"] for parameter in parameters])

    def test_events_route_documents_the_query_token(self):
        _, document = self.call("GET", "/v1/openapi.json")
        parameters = document["paths"]["/v1/events"]["get"]["parameters"]
        self.assertIn("access_token", [parameter["name"] for parameter in parameters])


class EventStreamTests(ServerTestCase):
    def test_sse_delivers_ready_then_task_events(self):
        url = f"{self.base}/v1/events?access_token={self.token}"
        request = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
        seen = []
        with urllib.request.urlopen(request, timeout=30) as stream:
            self.assertEqual(stream.headers.get("Content-Type"), "text/event-stream; charset=utf-8")
            # Read the initial 'ready' frame.
            name = stream.readline().decode().strip()
            self.assertEqual(name, "event: ready")
            stream.readline()  # data: {...}
            stream.readline()  # blank separator

            def run_task():
                self.call("POST", "/v1/task", {"plugin": "hello.python", "input": {"name": "SSE"}, "id": "sse-1"})

            worker = threading.Thread(target=run_task, daemon=True)
            worker.start()
            while len(seen) < 2:
                line = stream.readline().decode().strip()
                if line.startswith("event: "):
                    seen.append(line[len("event: "):])
            worker.join(timeout=10)
        self.assertIn("task.started", seen)

    def test_sse_rejects_a_bad_token(self):
        request = urllib.request.Request(f"{self.base}/v1/events?access_token=wrong")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(caught.exception.code, 401)

    def test_event_bus_fan_out_and_limit(self):
        bus = api.EventBus(max_subscribers=2, queue_size=4)
        first = bus.subscribe()
        second = bus.subscribe()
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertIsNone(bus.subscribe())
        self.assertEqual(bus.count(), 2)
        bus.publish({"event": "progress", "data": {"value": 0.5}})
        self.assertEqual(first.get(timeout=2)["event"], "progress")
        self.assertEqual(second.get(timeout=2)["event"], "progress")
        bus.unsubscribe(first)
        self.assertEqual(bus.count(), 1)
        self.assertIsNotNone(bus.subscribe())

    def test_event_bus_drops_when_a_subscriber_stalls(self):
        bus = api.EventBus(max_subscribers=1, queue_size=1)
        sink = bus.subscribe()
        for index in range(10):
            bus.publish({"event": "progress", "data": {"value": index}})
        self.assertEqual(sink.qsize(), 1)


class ClientTests(ServerTestCase):
    def client(self):
        return LocalClient(self.token, port=self.port)

    def test_discover_reads_the_token_file(self):
        config = load_config({"api": {"port": self.port, "token_file": "test-token"}})
        client = LocalClient.discover(config)
        self.assertEqual(client.token, self.token)
        self.assertTrue(client.available())

    def test_client_run_and_status(self):
        client = self.client()
        result = client.run("hello.python", {"name": "Client"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["output"]["greeting"], "Hello, Client!")
        self.assertIn("models", client.status())

    def test_client_task_status_and_cancel(self):
        client = self.client()
        client.run("hello.python", {"name": "T"}, task_id="client-task")
        self.assertEqual(client.task_status("client-task")["id"], "client-task")
        self.assertTrue(client.cancel("*")["all"])

    def test_client_tasks_and_cache(self):
        client = self.client()
        self.assertIn("history", client.tasks(5))
        self.assertIn("cache", client.cache_clear())

    def test_client_raises_apierror_with_envelope(self):
        client = self.client()
        with self.assertRaises(APIError) as caught:
            client.task_status("missing-task")
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(caught.exception.envelope["error"]["code"], protocol.ErrorCode.UNKNOWN_TASK)

    def test_client_with_a_bad_token_is_not_available(self):
        self.assertFalse(LocalClient("wrong", port=self.port).available())

    def test_unreachable_port_is_not_available(self):
        self.assertFalse(LocalClient(self.token, port=free_port()).available())


class UnixSocketTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="lah-unix-")
        cls._old_data = os.environ.get("LOCAL_AI_HUB_DATA")
        os.environ["LOCAL_AI_HUB_DATA"] = cls._tmp
        cls.socket_path = str(Path(cls._tmp) / "hub.sock")
        cls.config = load_config(
            {
                "ollama": {"mock": True},
                "api": {"enabled": True, "port": 0, "unix_socket": cls.socket_path, "token_file": "unix-token"},
            }
        )
        cls.token = api.ensure_token(cls.config)
        cls.server = api.make_unix_server(cls.config)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server.hub_engine.shutdown()
        cls.thread.join(timeout=5)
        if cls._old_data is None:
            os.environ.pop("LOCAL_AI_HUB_DATA", None)
        else:
            os.environ["LOCAL_AI_HUB_DATA"] = cls._old_data
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def test_socket_exists_with_owner_only_permissions(self):
        mode = os.stat(self.socket_path).st_mode
        self.assertTrue(stat.S_ISSOCK(mode))
        self.assertEqual(stat.S_IMODE(mode), 0o600)

    def test_client_over_unix_socket(self):
        client = LocalClient(self.token, unix_socket=self.socket_path)
        self.assertTrue(client.available())
        result = client.run("hello.python", {"name": "Unix"})
        self.assertEqual(result["output"]["greeting"], "Hello, Unix!")

    def test_unix_socket_still_requires_the_token(self):
        client = LocalClient("wrong", unix_socket=self.socket_path)
        self.assertFalse(client.available())

    def test_relative_socket_path_resolves_into_the_state_dir(self):
        config = load_config({"api": {"unix_socket": "relative.sock"}})
        server = api.make_unix_server(config)
        try:
            expected = paths.data_dir() / "relative.sock"
            self.assertTrue(expected.is_socket())
            self.assertEqual(server.server_address, str(expected))
        finally:
            server.server_close()
        self.assertFalse(expected.exists())

    def test_missing_socket_configuration_is_an_error(self):
        with self.assertRaises(ValueError):
            api.make_unix_server(load_config({"api": {"unix_socket": ""}}))

    def test_stale_socket_is_replaced(self):
        path = Path(self._tmp) / "stale.sock"
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.bind(str(path))
        probe.close()
        self.assertTrue(path.exists())
        server = api.make_unix_server(self.config, path=str(path))
        try:
            self.assertTrue(path.is_socket())
        finally:
            server.server_close()

    def test_non_socket_file_is_not_clobbered(self):
        path = Path(self._tmp) / "not-a-socket"
        path.write_text("keep me")
        with self.assertRaises(OSError):
            api.make_unix_server(self.config, path=str(path))
        self.assertEqual(path.read_text(), "keep me")


class DisabledAPITests(unittest.TestCase):
    def test_main_refuses_to_start_when_disabled(self):
        tmp = tempfile.mkdtemp(prefix="lah-api-off-")
        old = os.environ.get("LOCAL_AI_HUB_DATA")
        os.environ["LOCAL_AI_HUB_DATA"] = tmp
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(api.main([]), 2)
            self.assertIn("disabled", out.getvalue())
        finally:
            if old is None:
                os.environ.pop("LOCAL_AI_HUB_DATA", None)
            else:
                os.environ["LOCAL_AI_HUB_DATA"] = old
            shutil.rmtree(tmp, ignore_errors=True)

    def test_nothing_to_serve_is_reported(self):
        tmp = tempfile.mkdtemp(prefix="lah-api-none-")
        old = os.environ.get("LOCAL_AI_HUB_DATA")
        os.environ["LOCAL_AI_HUB_DATA"] = tmp
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(api.main(["--force", "--port", "0"]), 2)
            self.assertIn("Nothing to serve", out.getvalue())
        finally:
            if old is None:
                os.environ.pop("LOCAL_AI_HUB_DATA", None)
            else:
                os.environ["LOCAL_AI_HUB_DATA"] = old
            shutil.rmtree(tmp, ignore_errors=True)


class HTTPStatusMappingTests(unittest.TestCase):
    def test_error_codes_map_to_sensible_statuses(self):
        expected = {
            protocol.ErrorCode.BAD_REQUEST: 400,
            protocol.ErrorCode.INVALID_INPUT: 400,
            protocol.ErrorCode.UNAUTHORIZED: 401,
            protocol.ErrorCode.FORBIDDEN: 403,
            protocol.ErrorCode.NOT_FOUND: 404,
            protocol.ErrorCode.UNKNOWN_TASK: 404,
            protocol.ErrorCode.UNKNOWN_PLUGIN: 404,
            protocol.ErrorCode.INPUT_TOO_LARGE: 413,
            protocol.ErrorCode.RATE_LIMITED: 429,
            protocol.ErrorCode.INTERNAL_ERROR: 500,
        }
        for code, status in expected.items():
            self.assertEqual(api._http_status(protocol.make_error("-", code, "x")), status, code)


if __name__ == "__main__":
    unittest.main()
