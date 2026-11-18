"""Second inference backend: OpenAI-compatible servers (llama.cpp, LM Studio).

Exercises ``OpenAICompatClient`` against a real local HTTP server that speaks
the OpenAI wire format (JSON + SSE), and the ``ModelManager`` integration
behind the ``ollama.backend`` switch — no model download needed.
"""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from hub.config import load_config
from hub.models.manager import ModelError, ModelManager
from hub.models.ollama import OllamaClient, OllamaError
from hub.models.openai_backend import OpenAICompatClient, _image_mime

PNG = "iVBORw0KGgoAAAANSUhEUg=="
JPEG = "/9j/4AAQSkZJRg=="
GIF = "R0lGODlhAQABAAAAACw="
WEBP = "UklGRgAAAAA="


class FakeOpenAI:
    """A tiny stand-in for /v1/models and /v1/chat/completions."""

    def __init__(self):
        self.requests = []
        self.mode = "ok"
        self.reply = "Hello world"
        self.fragments = ["Hel", "lo ", "world"]
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _send(self, code, payload, ctype="application/json"):
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                outer.requests.append(("GET", self.path, None, dict(self.headers)))
                if self.path == "/v1/models" and outer.mode != "http_error":
                    self._send(200, {"object": "list", "data": [
                        {"id": "test-model", "object": "model"},
                        {"id": "vision-model", "object": "model"},
                        {"object": "model"},  # no id: must be filtered out
                    ]})
                else:
                    self._send(500 if outer.mode == "http_error" else 404,
                               {"error": {"message": "boom"}})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                outer.requests.append(("POST", self.path, body, dict(self.headers)))
                if self.path != "/v1/chat/completions":
                    self._send(404, {"error": {"message": "not found"}})
                    return
                if outer.mode == "http_error":
                    self._send(500, {"error": {"message": "boom"}})
                    return
                if outer.mode == "empty_choices":
                    self._send(200, {"choices": []})
                    return
                if body.get("stream"):
                    lines = [b": keep-alive\n\n", b"data: not-json\n\n"]
                    if outer.mode == "stream_error":
                        lines.append(b'data: {"error": {"message": "bad"}}\n\n')
                    else:
                        for fragment in outer.fragments:
                            frame = {"choices": [{"delta": {"content": fragment}}]}
                            lines.append(b"data: " + json.dumps(frame).encode() + b"\n\n")
                        lines.append(b"data: [DONE]\n\n")
                        # Anything after [DONE] must be ignored.
                        lines.append(b'data: {"choices": [{"delta": {"content": "IGNORED"}}]}\n\n')
                    self._send(200, b"".join(lines), ctype="text/event-stream")
                    return
                self._send(200, {
                    "id": "cmpl-1",
                    "model": body.get("model"),
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": outer.reply}}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 2},
                })

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def reset(self):
        self.requests = []
        self.mode = "ok"
        self.reply = "Hello world"
        self.fragments = ["Hel", "lo ", "world"]

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def posts(self):
        return [body for method, path, body, _ in self.requests
                if method == "POST" and path == "/v1/chat/completions"]


class ClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = FakeOpenAI()

    @classmethod
    def tearDownClass(cls):
        cls.fake.stop()

    def setUp(self):
        self.fake.reset()
        self.client = OpenAICompatClient(port=self.fake.port, timeout=10)

    def test_is_alive(self):
        self.assertTrue(self.client.is_alive())
        self.assertFalse(OpenAICompatClient(port=1, timeout=2).is_alive())

    def test_version_label(self):
        self.assertEqual(self.client.version(), "openai-compatible")

    def test_tags_maps_id_to_name_and_filters(self):
        names = [entry["name"] for entry in self.client.tags()]
        self.assertEqual(names, ["test-model", "vision-model"])

    def test_running_is_empty(self):
        self.assertEqual(self.client.running(), [])

    def test_generate_non_stream(self):
        result = self.client.generate("test-model", "hi")
        self.assertEqual(result["response"], "Hello world")
        body = self.fake.posts()[0]
        self.assertEqual(body["model"], "test-model")
        self.assertFalse(body["stream"])
        self.assertEqual(body["messages"], [{"role": "user", "content": "hi"}])
        self.assertNotIn("response_format", body)

    def test_generate_system_prompt(self):
        self.client.generate("test-model", "hi", system="be brief")
        self.assertEqual(self.fake.posts()[0]["messages"][0],
                         {"role": "system", "content": "be brief"})

    def test_generate_images_become_data_uri_parts(self):
        self.client.generate("test-model", "what is this?", images=[PNG, JPEG])
        content = self.fake.posts()[0]["messages"][-1]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "what is this?"})
        self.assertEqual(content[1]["image_url"]["url"], f"data:image/png;base64,{PNG}")
        self.assertEqual(content[2]["image_url"]["url"], f"data:image/jpeg;base64,{JPEG}")

    def test_image_mime_sniffing(self):
        self.assertEqual(_image_mime(PNG), "image/png")
        self.assertEqual(_image_mime(JPEG), "image/jpeg")
        self.assertEqual(_image_mime(GIF), "image/gif")
        self.assertEqual(_image_mime(WEBP), "image/webp")
        self.assertEqual(_image_mime("????"), "image/png")

    def test_fmt_json_maps_to_json_object(self):
        self.client.generate("test-model", "hi", fmt="json")
        self.assertEqual(self.fake.posts()[0]["response_format"], {"type": "json_object"})

    def test_fmt_schema_maps_to_json_schema(self):
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
        self.client.generate("test-model", "hi", fmt=schema)
        fmt = self.fake.posts()[0]["response_format"]
        self.assertEqual(fmt["type"], "json_schema")
        self.assertEqual(fmt["json_schema"]["schema"], schema)

    def test_option_mapping(self):
        self.client.generate("test-model", "hi", options={
            "temperature": 0.2, "top_p": 0.9, "seed": 7, "num_predict": 64,
            "stop": ["X"], "num_ctx": 4096, "top_k": 40,
        })
        body = self.fake.posts()[0]
        self.assertEqual(body["temperature"], 0.2)
        self.assertEqual(body["top_p"], 0.9)
        self.assertEqual(body["seed"], 7)
        self.assertEqual(body["max_tokens"], 64)
        self.assertEqual(body["stop"], ["X"])
        for dropped in ("num_ctx", "top_k", "num_predict"):
            self.assertNotIn(dropped, body)

    def test_api_key_header(self):
        self.client.generate("test-model", "hi")
        self.assertNotIn("Authorization", self.fake.requests[-1][3])
        keyed = OpenAICompatClient(port=self.fake.port, timeout=10, api_key="secret")
        keyed.generate("test-model", "hi")
        self.assertEqual(self.fake.requests[-1][3].get("Authorization"), "Bearer secret")

    def test_chat_converts_messages_and_images(self):
        result = self.client.chat("test-model", [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "hi", "images": [PNG]},
            {"role": "assistant", "content": "hello"},
        ])
        self.assertEqual(result["message"]["content"], "Hello world")
        messages = self.fake.posts()[0]["messages"]
        self.assertEqual(messages[0], {"role": "system", "content": "s"})
        self.assertEqual(messages[1]["content"][1]["image_url"]["url"],
                         f"data:image/png;base64,{PNG}")
        self.assertEqual(messages[2], {"role": "assistant", "content": "hello"})

    def test_streaming_tokens_and_reassembly(self):
        tokens = []
        result = self.client.generate("test-model", "hi", stream=True, on_token=tokens.append)
        self.assertEqual(tokens, ["Hel", "lo ", "world"])
        self.assertEqual(result["response"], "Hello world")
        self.assertTrue(self.fake.posts()[0]["stream"])

    def test_streaming_chat(self):
        tokens = []
        result = self.client.chat("test-model", [{"role": "user", "content": "hi"}],
                                stream=True, on_token=tokens.append)
        self.assertEqual(result["message"]["content"], "Hello world")
        self.assertEqual(len(tokens), 3)

    def test_stream_error_frame(self):
        self.fake.mode = "stream_error"
        with self.assertRaises(OllamaError) as caught:
            self.client.generate("test-model", "hi", stream=True)
        self.assertIn("bad", str(caught.exception))

    def test_http_error_carries_status_and_message(self):
        self.fake.mode = "http_error"
        with self.assertRaises(OllamaError) as caught:
            self.client.generate("test-model", "hi")
        self.assertEqual(caught.exception.status, 500)
        self.assertIn("boom", str(caught.exception))

    def test_unreachable_is_model_unavailable(self):
        with self.assertRaises(OllamaError) as caught:
            OpenAICompatClient(port=1, timeout=2).generate("test-model", "hi")
        self.assertEqual(caught.exception.code, "model_unavailable")

    def test_empty_choices_is_an_error(self):
        self.fake.mode = "empty_choices"
        with self.assertRaises(OllamaError):
            self.client.generate("test-model", "hi")

    def test_load_is_one_token_completion(self):
        self.client.load("test-model", timeout=5)
        body = self.fake.posts()[0]
        self.assertEqual(body["max_tokens"], 1)
        self.assertFalse(body["stream"])
        self.assertEqual(body["messages"], [{"role": "user", "content": "ping"}])

    def test_unload_is_a_noop(self):
        self.client.unload("test-model")
        self.assertEqual(self.fake.posts(), [])


class ManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = FakeOpenAI()

    @classmethod
    def tearDownClass(cls):
        cls.fake.stop()

    def setUp(self):
        self.fake.reset()

    def manager(self, **overrides):
        settings = {
            "backend": "openai", "host": "127.0.0.1", "port": self.fake.port,
            "mock": False, "default_model": "test-model", "vision_model": "vision-model",
            "idle_unload_seconds": 0, "request_timeout_seconds": 10,
        }
        settings.update(overrides)
        return ModelManager(load_config({"ollama": settings}))

    def test_backend_switch_selects_client(self):
        manager = self.manager()
        self.assertEqual(manager.backend, "openai")
        self.assertIsInstance(manager.client, OpenAICompatClient)
        self.assertIsNone(manager.binary)

    def test_default_backend_is_ollama(self):
        manager = ModelManager(load_config({}))
        self.assertEqual(manager.backend, "ollama")
        self.assertIsInstance(manager.client, OllamaClient)

    def test_backend_aliases(self):
        for alias in ("lmstudio", "lm-studio", "llama.cpp", "llamacpp", "OpenAI", "openai-compatible"):
            self.assertEqual(self.manager(backend=alias).backend, "openai", alias)

    def test_unknown_backend_rejected(self):
        with self.assertRaises(ModelError) as caught:
            self.manager(backend="nope")
        self.assertEqual(caught.exception.code, "config_error")

    def test_status_reports_backend(self):
        status = self.manager().status()
        self.assertEqual(status["backend"], "openai")
        self.assertEqual(status["base_url"], f"http://127.0.0.1:{self.fake.port}")
        self.assertTrue(status["running"])
        self.assertEqual(status["installed_models"], ["test-model", "vision-model"])

    def test_mock_still_wins_over_backend(self):
        manager = self.manager(mock=True)
        self.assertEqual(manager.status()["backend"], "mock")
        self.assertIn("[mock]", manager.generate("hi")["text"])

    def test_generate_end_to_end(self):
        result = self.manager().generate("hi")
        self.assertEqual(result["text"], "Hello world")
        self.assertEqual(result["model"], "test-model")
        self.assertFalse(result["mock"])

    def test_chat_streaming_end_to_end(self):
        tokens = []
        result = self.manager().chat([{"role": "user", "content": "hi"}], on_token=tokens.append)
        self.assertEqual(result["text"], "Hello world")
        self.assertTrue(result["streamed"])
        self.assertEqual(result["tokens"], 3)
        self.assertEqual(tokens, ["Hel", "lo ", "world"])

    def test_resolve_model_base_name_fallback(self):
        self.assertEqual(self.manager().resolve_model("test-model:q4"), "test-model")

    def test_ensure_server_never_autostarts_external_backend(self):
        manager = self.manager(port=1, auto_start=True)
        with self.assertRaises(ModelError) as caught:
            manager.generate("hi")
        self.assertEqual(caught.exception.code, "model_unavailable")
        self.assertIn("openai-compatible", str(caught.exception))
        self.assertIsNone(manager._process)
        self.assertFalse(manager.started_by_hub)

    def test_warmup_uses_one_token_load(self):
        manager = self.manager()
        report = manager.warmup(["test-model"], block=True)
        self.assertEqual(report["state"], "ready")
        self.assertEqual(report["loaded"], ["test-model"])
        self.assertEqual(self.fake.posts()[0]["max_tokens"], 1)
        self.assertEqual(manager.warmup_status()["state"], "ready")

    def test_warmup_failure_reported(self):
        self.fake.mode = "http_error"
        report = self.manager().warmup(["test-model"], block=True)
        self.assertEqual(report["state"], "failed")
        self.assertEqual(report["failed"], ["test-model"])

    def test_unload_all_is_safe(self):
        manager = self.manager()
        manager.generate("hi")
        before = len(self.fake.posts())
        self.assertEqual(manager.unload_all(), ["test-model"])
        self.assertEqual(len(self.fake.posts()), before)  # no unload request exists

    def test_api_key_from_config(self):
        manager = self.manager(api_key="secret")
        manager.generate("hi")
        self.assertEqual(self.fake.requests[-1][3].get("Authorization"), "Bearer secret")


if __name__ == "__main__":
    unittest.main()
