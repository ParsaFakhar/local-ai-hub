"""Model warm-up policy: preload weights before the first task.

Covers `ModelManager.warmup` (targets, skip/failure handling, async state),
the `models.warmup` control type, engine start-up warm-up, the HTTP/CLI wiring
and `OllamaClient.load` (empty prompt = load, no generation).
"""

import contextlib
import io
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from hub import api, protocol
from hub.config import load_config
from hub.engine import Engine
from hub.models.manager import ModelManager
from hub.models.ollama import OllamaClient, OllamaError
from scripts import run_task


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class FakeClient:
    """Stands in for OllamaClient: records loads, can fail or block."""

    def __init__(self, fail=(), delay=0.0, names=("m1", "m2")):
        self.calls = []
        self.fail = set(fail)
        self.delay = delay
        self.names = list(names)
        self.unloaded = []

    def is_alive(self, timeout=2.0):
        return True

    def tags(self):
        return [{"name": name} for name in self.names]

    def running(self):
        return []

    def load(self, model, *, keep_alive="5m", timeout=None):
        self.calls.append({"model": model, "keep_alive": keep_alive, "timeout": timeout})
        if self.delay:
            time.sleep(self.delay)
        if model in self.fail:
            raise OllamaError("cannot load " + model, "model_error")
        return {"done_reason": "load"}

    def unload(self, model):
        self.unloaded.append(model)


def make_manager(events=None, **ollama):
    settings = {"mock": True}
    settings.update(ollama)
    sink = (lambda name, data: events.append((name, data))) if events is not None else None
    return ModelManager(load_config({"ollama": settings}), on_event=sink)


def make_real_manager(client, **ollama):
    settings = {"mock": False, "auto_start": False, "default_model": "m1", "idle_unload_seconds": 0}
    settings.update(ollama)
    manager = ModelManager(load_config({"ollama": settings}))
    manager.client = client
    return manager


class WarmupTargetTests(unittest.TestCase):
    def test_default_target_is_the_default_model(self):
        manager = make_manager(default_model="qwen3.5:4b")
        self.assertEqual(manager.warmup_targets(), ["qwen3.5:4b"])

    def test_vision_model_is_optional(self):
        manager = make_manager(default_model="a", vision_model="v")
        self.assertEqual(manager.warmup_targets(), ["a"])
        manager = make_manager(default_model="a", vision_model="v", warmup_vision=True)
        self.assertEqual(manager.warmup_targets(), ["a", "v"])

    def test_extra_models_and_dedupe(self):
        manager = make_manager(default_model="a", vision_model="a", warmup_vision=True,
                               warmup_models=["b", "a", "c"])
        self.assertEqual(manager.warmup_targets(), ["a", "b", "c"])

    def test_extra_models_accepts_a_single_string(self):
        manager = make_manager(default_model="a", warmup_models="b")
        self.assertEqual(manager.warmup_targets(), ["a", "b"])

    def test_initial_state_is_idle(self):
        state = make_manager().warmup_status()
        self.assertEqual(state["state"], "idle")
        self.assertEqual(state["models"], [])
        self.assertIsNone(state["error"])


class MockWarmupTests(unittest.TestCase):
    def test_blocking_warmup_marks_the_model_warm(self):
        manager = make_manager(default_model="mock-model")
        report = manager.warmup(block=True)
        self.assertEqual(report["state"], "ready")
        self.assertEqual(report["loaded"], ["mock-model"])
        self.assertTrue(report["mock"])
        self.assertIn("mock-model", manager.status()["loaded_models"])

    def test_events_are_emitted(self):
        events = []
        manager = make_manager(events, default_model="mock-model")
        manager.warmup(block=True)
        names = [name for name, _ in events]
        self.assertEqual(names, ["model.warming", "model.warmed", "model.warmup_done"])
        self.assertEqual(events[0][1]["model"], "mock-model")
        self.assertEqual(events[-1][1]["state"], "ready")

    def test_second_warmup_skips_loaded_models(self):
        manager = make_manager(default_model="mock-model")
        manager.warmup(block=True)
        report = manager.warmup(block=True)
        self.assertEqual(report["loaded"], [])
        self.assertEqual(report["skipped"], ["mock-model"])
        self.assertEqual(report["state"], "ready")

    def test_explicit_models_override_the_policy(self):
        manager = make_manager(default_model="a", warmup_models=["b"])
        report = manager.warmup(["x", "y", "x"], block=True)
        self.assertEqual(report["models"], ["x", "y"])
        self.assertEqual(report["loaded"], ["x", "y"])

    def test_explicit_models_accepts_a_string(self):
        self.assertEqual(make_manager().warmup("solo", block=True)["loaded"], ["solo"])

    def test_blank_names_are_dropped(self):
        report = make_manager().warmup(["", "   "], block=True)
        self.assertEqual(report["models"], [])
        self.assertEqual(report["state"], "ready")

    def test_async_warmup_reports_running_then_ready(self):
        manager = make_manager(default_model="mock-model")
        first = manager.warmup()
        self.assertEqual(first["state"], "running")
        self.assertTrue(wait_for(lambda: manager.warmup_status()["state"] == "ready"))
        self.assertEqual(manager.warmup_status()["loaded"], ["mock-model"])

    def test_status_carries_the_warmup_report(self):
        manager = make_manager()
        manager.warmup(block=True)
        self.assertEqual(manager.status()["warmup"]["state"], "ready")

    def test_mock_accepts_any_explicit_tag(self):
        manager = make_manager(default_model="a", vision_model="v")
        self.assertEqual(manager.resolve_model("other:tag"), "other:tag")
        result = manager.generate("hi", model="other:tag")
        self.assertEqual(result["model"], "other:tag")
        self.assertTrue(result["mock"])


class RealBackendWarmupTests(unittest.TestCase):
    def test_load_uses_configured_keep_alive_and_timeout(self):
        client = FakeClient()
        manager = make_real_manager(client, keep_alive="10m", warmup_timeout_seconds=45)
        report = manager.warmup(block=True)
        self.assertEqual(report["state"], "ready")
        self.assertEqual(client.calls, [{"model": "m1", "keep_alive": "10m", "timeout": 45.0}])

    def test_failure_is_reported_not_raised(self):
        events = []
        client = FakeClient(fail=["m1"])
        manager = make_real_manager(client)
        manager.on_event = lambda name, data: events.append((name, data))
        report = manager.warmup(block=True)
        self.assertEqual(report["state"], "failed")
        self.assertEqual(report["failed"], ["m1"])
        self.assertIn("cannot load m1", report["error"])
        self.assertIn("model.warmup_failed", [name for name, _ in events])

    def test_partial_success(self):
        client = FakeClient(fail=["m2"])
        manager = make_real_manager(client, warmup_models=["m2"])
        report = manager.warmup(block=True)
        self.assertEqual(report["state"], "partial")
        self.assertEqual(report["loaded"], ["m1"])
        self.assertEqual(report["failed"], ["m2"])

    def test_uninstalled_model_fails_with_a_helpful_error(self):
        manager = make_real_manager(FakeClient(), default_model="not-installed", model_fallbacks=[])
        report = manager.warmup(block=True)
        self.assertEqual(report["state"], "failed")
        self.assertIn("ollama pull", report["error"])

    def test_already_used_model_is_skipped(self):
        client = FakeClient()
        manager = make_real_manager(client)
        manager._loaded["m1"] = time.monotonic()
        report = manager.warmup(block=True)
        self.assertEqual(report["skipped"], ["m1"])
        self.assertEqual(client.calls, [])

    def test_second_call_while_running_is_a_noop(self):
        client = FakeClient(delay=0.2)
        manager = make_real_manager(client)
        self.assertEqual(manager.warmup()["state"], "running")
        self.assertEqual(manager.warmup(["m2"])["state"], "running")
        self.assertTrue(wait_for(lambda: manager.warmup_status()["state"] == "ready"))
        self.assertEqual([call["model"] for call in client.calls], ["m1"])

    def test_shutdown_stops_the_warmup_loop(self):
        client = FakeClient()
        manager = make_real_manager(client, warmup_models=["m2"])
        manager.shutdown()
        report = manager.warmup(block=True)
        self.assertEqual(report["state"], "cancelled")
        self.assertEqual(client.calls, [])

    def test_warmup_does_not_pin_the_model(self):
        client = FakeClient()
        manager = make_real_manager(client)
        manager.warmup(block=True)
        self.assertEqual(manager.unload_all(), ["m1"])
        self.assertEqual(client.unloaded, ["m1"])


class OllamaClientLoadTests(unittest.TestCase):
    def test_load_sends_an_empty_prompt_and_keep_alive(self):
        client = OllamaClient()
        seen = {}

        def fake_request(method, path, body=None, timeout=None):
            seen.update({"method": method, "path": path, "body": body, "timeout": timeout})
            return {"done_reason": "load"}

        client._request = fake_request
        client.load("m1", keep_alive="7m", timeout=33)
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["path"], "/api/generate")
        self.assertEqual(seen["body"], {"model": "m1", "prompt": "", "keep_alive": "7m"})
        self.assertEqual(seen["timeout"], 33)
        self.assertNotIn("options", seen["body"])


class EngineWarmupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="lah-warmup-")
        cls._old_data = os.environ.get("LOCAL_AI_HUB_DATA")
        os.environ["LOCAL_AI_HUB_DATA"] = cls._tmp

    @classmethod
    def tearDownClass(cls):
        if cls._old_data is None:
            os.environ.pop("LOCAL_AI_HUB_DATA", None)
        else:
            os.environ["LOCAL_AI_HUB_DATA"] = cls._old_data
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def make_engine(self, **ollama):
        settings = {"mock": True}
        settings.update(ollama)
        return Engine(load_config({"ollama": settings, "tasks": {"audit_log": False}}))

    def test_control_type_is_registered_but_not_inline(self):
        self.assertIn("models.warmup", protocol.CONTROL_TYPES)
        self.assertNotIn("models.warmup", protocol.INLINE_CONTROL_TYPES)

    def test_models_warmup_control_returns_a_report(self):
        engine = self.make_engine(default_model="mock-model")
        try:
            result = engine.handle({"type": "models.warmup"})
            self.assertTrue(result["ok"], result)
            self.assertIn(result["output"]["warmup"]["state"], ("running", "ready"))
            self.assertTrue(wait_for(lambda: engine.models.warmup_status()["state"] == "ready"))
            self.assertEqual(engine.models.warmup_status()["loaded"], ["mock-model"])
        finally:
            engine.shutdown()

    def test_models_warmup_accepts_explicit_models(self):
        engine = self.make_engine()
        try:
            engine.handle({"type": "models.warmup", "models": ["one", "two"]})
            self.assertTrue(wait_for(lambda: engine.models.warmup_status()["state"] == "ready"))
            self.assertEqual(engine.models.warmup_status()["loaded"], ["one", "two"])
        finally:
            engine.shutdown()

    def test_models_warmup_rejects_a_bad_models_argument(self):
        engine = self.make_engine()
        try:
            for bad in ("m1", [""], [1], {"a": 1}):
                result = engine.handle({"type": "models.warmup", "models": bad})
                self.assertFalse(result["ok"], bad)
                self.assertEqual(result["error"]["code"], "bad_request")
        finally:
            engine.shutdown()

    def test_warmup_is_off_by_default(self):
        engine = self.make_engine()
        try:
            self.assertEqual(engine.models.warmup_status()["state"], "idle")
        finally:
            engine.shutdown()

    def test_engine_warms_on_start_when_enabled(self):
        engine = self.make_engine(warmup=True, default_model="mock-model")
        try:
            self.assertTrue(wait_for(lambda: engine.models.warmup_status()["state"] == "ready"))
            status = engine.handle({"type": "status"})["output"]
            self.assertEqual(status["models"]["warmup"]["loaded"], ["mock-model"])
        finally:
            engine.shutdown()

    def test_warmup_on_start_can_be_disabled(self):
        engine = self.make_engine(warmup=True, warmup_on_start=False)
        try:
            self.assertEqual(engine.models.warmup_status()["state"], "idle")
            self.assertTrue(engine.handle({"type": "models.warmup"})["ok"])
        finally:
            engine.shutdown()

    def test_http_and_openapi_expose_the_route(self):
        self.assertEqual(api.POST_ROUTES["/v1/models/warmup"], "models.warmup")
        self.assertIn("/v1/models/warmup", api.openapi_document()["paths"])


class HTTPWarmupTests(unittest.TestCase):
    """The real HTTP route must forward `models` (not drop the body)."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="lah-warmup-http-")
        cls._old_data = os.environ.get("LOCAL_AI_HUB_DATA")
        os.environ["LOCAL_AI_HUB_DATA"] = cls._tmp
        config = load_config({"ollama": {"mock": True}, "tasks": {"audit_log": False},
                              "api": {"enabled": True, "host": "127.0.0.1", "port": 0}})
        cls.token = api.ensure_token(config)
        cls.server = api.make_server(config)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        if cls._old_data is None:
            os.environ.pop("LOCAL_AI_HUB_DATA", None)
        else:
            os.environ["LOCAL_AI_HUB_DATA"] = cls._old_data
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def call(self, method, route, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{route}", data=data, method=method,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def test_post_warmup_forwards_explicit_models(self):
        status, envelope = self.call("POST", "/v1/models/warmup", {"models": ["http-a", "http-b"]})
        self.assertEqual(status, 200, envelope)
        self.assertTrue(envelope["ok"], envelope)
        self.assertEqual(envelope["output"]["warmup"]["models"], ["http-a", "http-b"])
        self.assertTrue(wait_for(lambda: self.call("GET", "/v1/models")[1]["output"]["warmup"]["state"] == "ready"))
        report = self.call("GET", "/v1/models")[1]["output"]["warmup"]
        self.assertEqual(sorted(report["loaded"] + report["skipped"]), ["http-a", "http-b"])

    def test_post_warmup_without_body_uses_the_policy(self):
        status, envelope = self.call("POST", "/v1/models/warmup", {})
        self.assertEqual(status, 200, envelope)
        self.assertTrue(envelope["ok"], envelope)
        self.assertTrue(envelope["output"]["warmup"]["models"])

    def test_post_warmup_rejects_a_bad_models_argument(self):
        status, envelope = self.call("POST", "/v1/models/warmup", {"models": "not-a-list"})
        self.assertEqual(status, 400, envelope)
        self.assertEqual(envelope["error"]["code"], "bad_request")

    def test_status_reports_warmup(self):
        status, envelope = self.call("GET", "/v1/status")
        self.assertEqual(status, 200)
        self.assertIn("warmup", envelope["output"]["models"])


class CLIWarmupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="lah-warmup-cli-")
        cls._old_data = os.environ.get("LOCAL_AI_HUB_DATA")
        os.environ["LOCAL_AI_HUB_DATA"] = cls._tmp

    @classmethod
    def tearDownClass(cls):
        if cls._old_data is None:
            os.environ.pop("LOCAL_AI_HUB_DATA", None)
        else:
            os.environ["LOCAL_AI_HUB_DATA"] = cls._old_data
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def run_cli(self, argv):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = run_task.main(argv)
        return code, json.loads(buffer.getvalue())

    def test_warmup_command(self):
        code, envelope = self.run_cli(["--mock", "--compact", "warmup"])
        self.assertEqual(code, 0)
        self.assertTrue(envelope["ok"], envelope)
        self.assertIn("warmup", envelope["output"])

    def test_warmup_command_with_models(self):
        code, envelope = self.run_cli(["--mock", "--compact", "warmup", "--models", "a, b ,a"])
        self.assertEqual(code, 0)
        self.assertEqual(envelope["output"]["warmup"]["models"], ["a", "b"])

    def test_request_builder_omits_models_when_unset(self):
        parser_args = run_task.build_parser().parse_args(["warmup"])
        self.assertNotIn("models", run_task._local_request(parser_args))


if __name__ == "__main__":
    unittest.main()
