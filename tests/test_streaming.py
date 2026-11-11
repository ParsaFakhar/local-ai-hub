"""Streaming inference, end to end.

Covers every hop of the token path:

    Ollama NDJSON  ->  OllamaClient._stream  ->  ModelManager  ->  runner
    (llm.token frames)  ->  SDK on_token / onToken  ->  engine model.token events

The Ollama wire format is exercised against a real local HTTP server that
speaks newline-delimited JSON, so no model download is needed.
"""

import json
import os
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from hub.config import load_config
from hub.engine import Engine
from hub.models.manager import ModelManager
from hub.models.ollama import OllamaClient, OllamaError

ROOT = Path(__file__).resolve().parent.parent


class FakeOllama:
    """A tiny stand-in for Ollama's /api/generate and /api/chat streaming."""

    def __init__(self, frames, chat=False):
        self.frames = frames
        self.chat = chat
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                outer.requests.append(json.loads(self.rfile.read(length) or b"{}"))
                body = b"".join(
                    (json.dumps(frame) + "\n").encode("utf-8") for frame in outer.frames
                )
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def client(self):
        return OllamaClient(port=self.port, timeout=10)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def generate_frames(text_pieces):
    frames = [{"model": "test:1b", "response": piece, "done": False} for piece in text_pieces]
    frames.append({"model": "test:1b", "response": "", "done": True, "eval_count": len(text_pieces)})
    return frames


def chat_frames(text_pieces):
    frames = [
        {"model": "test:1b", "message": {"role": "assistant", "content": piece}, "done": False}
        for piece in text_pieces
    ]
    frames.append({"model": "test:1b", "message": {"role": "assistant", "content": ""}, "done": True})
    return frames


class OllamaStreamTests(unittest.TestCase):
    def test_generate_stream_reassembles_and_forwards(self):
        server = FakeOllama(generate_frames(["Hello", " ", "stream", "ing", " world"]))
        try:
            seen = []
            result = server.client().generate(
                "test:1b", "hi", stream=True, on_token=seen.append
            )
            self.assertEqual(result["response"], "Hello streaming world")
            self.assertEqual(seen, ["Hello", " ", "stream", "ing", " world"])
            self.assertTrue(result["done"])
            self.assertEqual(result["eval_count"], 5)
            self.assertTrue(server.requests[0]["stream"])
        finally:
            server.close()

    def test_chat_stream_reassembles_message_content(self):
        server = FakeOllama(chat_frames(["a", "b", "c"]), chat=True)
        try:
            seen = []
            result = server.client().chat(
                "test:1b", [{"role": "user", "content": "hi"}], stream=True, on_token=seen.append
            )
            self.assertEqual(result["message"]["content"], "abc")
            self.assertEqual(seen, ["a", "b", "c"])
        finally:
            server.close()

    def test_blank_and_non_json_frames_are_ignored(self):
        frames = [
            {"response": "ok", "done": False},
            "not a json frame",
            {"response": "", "done": False},
            {"response": "!", "done": True},
        ]
        server = FakeOllama(frames)
        try:
            seen = []
            result = server.client().generate("test:1b", "hi", stream=True, on_token=seen.append)
            self.assertEqual(result["response"], "ok!")
            self.assertEqual(seen, ["ok", "!"])
        finally:
            server.close()

    def test_error_frame_raises(self):
        server = FakeOllama([{"response": "partial", "done": False}, {"error": "model exploded"}])
        try:
            with self.assertRaises(OllamaError) as caught:
                server.client().generate("test:1b", "hi", stream=True)
            self.assertIn("model exploded", str(caught.exception))
        finally:
            server.close()

    def test_stream_stops_at_done(self):
        frames = generate_frames(["a", "b"]) + [{"response": "never read", "done": False}]
        server = FakeOllama(frames)
        try:
            seen = []
            result = server.client().generate("test:1b", "hi", stream=True, on_token=seen.append)
            self.assertEqual(result["response"], "ab")
            self.assertNotIn("never read", seen)
        finally:
            server.close()

    def test_unreachable_server_is_model_unavailable(self):
        client = OllamaClient(port=1, timeout=2)
        with self.assertRaises(OllamaError) as caught:
            client.generate("test:1b", "hi", stream=True)
        self.assertEqual(caught.exception.code, "model_unavailable")

    def test_non_streaming_request_still_works(self):
        server = FakeOllama([{"response": "whole answer", "done": True}])
        try:
            result = server.client().generate("test:1b", "hi")
            self.assertEqual(result["response"], "whole answer")
            self.assertFalse(server.requests[0]["stream"])
        finally:
            server.close()


class ModelManagerStreamingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="lah-stream-")
        cls._old_data = os.environ.get("LOCAL_AI_HUB_DATA")
        os.environ["LOCAL_AI_HUB_DATA"] = cls._tmp

    @classmethod
    def tearDownClass(cls):
        if cls._old_data is None:
            os.environ.pop("LOCAL_AI_HUB_DATA", None)
        else:
            os.environ["LOCAL_AI_HUB_DATA"] = cls._old_data
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def manager(self, **ollama):
        settings = {"mock": True}
        settings.update(ollama)
        return ModelManager(load_config({"ollama": settings}))

    def test_on_token_implies_streaming(self):
        manager = self.manager()
        seen = []
        result = manager.generate("hello", on_token=seen.append)
        self.assertTrue(result["streamed"])
        self.assertGreater(result["tokens"], 1)
        self.assertEqual("".join(seen), result["text"])

    def test_no_streaming_by_default(self):
        manager = self.manager()
        result = manager.generate("hello")
        self.assertFalse(result["streamed"])
        self.assertEqual(result["tokens"], 0)

    def test_config_default_enables_streaming(self):
        manager = self.manager(stream=True)
        seen = []
        result = manager.generate("hello", on_token=seen.append)
        self.assertTrue(result["streamed"])
        self.assertTrue(seen)

    def test_explicit_false_overrides_the_config_default(self):
        manager = self.manager(stream=True)
        result = manager.generate("hello", stream=False)
        self.assertFalse(result["streamed"])

    def test_chat_streams_too(self):
        manager = self.manager()
        seen = []
        result = manager.chat([{"role": "user", "content": "hi"}], on_token=seen.append)
        self.assertTrue(result["streamed"])
        self.assertEqual("".join(seen), result["text"])

    def test_structured_output_streams_raw_json(self):
        manager = self.manager()
        seen = []
        result = manager.generate(
            "give me json", fmt={"type": "object", "properties": {"answer": {"type": "string"}}},
            on_token=seen.append,
        )
        self.assertEqual(json.loads(result["text"])["answer"], "[mock] answer")
        self.assertEqual("".join(seen), result["text"])

    def test_status_reports_the_stream_default(self):
        self.assertTrue(self.manager(stream=True).status()["stream"])
        self.assertFalse(self.manager().status()["stream"])


STREAM_PLUGIN_PY = """
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sdk" / "python"))
from local_ai_hub import Plugin

plugin = Plugin()

def main(task, hub):
    fragments = []
    text = hub.generate("say something", on_token=fragments.append)
    return {"text": text, "fragments": fragments, "joined": "".join(fragments)}

plugin.run(main)
"""

STREAM_PLUGIN_JS = """
'use strict';
const path = require('path');
const { Plugin } = require(path.resolve(__dirname, '..', '..', 'sdk', 'js', 'local_ai_hub.js'));
const plugin = new Plugin();
plugin.run(async (task, hub) => {
  const fragments = [];
  const text = await hub.generate('say something', { onToken: (t) => fragments.push(t) });
  return { text, fragments, joined: fragments.join('') };
});
"""

BROKEN_CALLBACK_PLUGIN = """
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sdk" / "python"))
from local_ai_hub import Plugin

plugin = Plugin()

def main(task, hub):
    def boom(fragment):
        raise RuntimeError("plugin callback bug")
    text = hub.generate("say something", on_token=boom)
    return {"text": text}

plugin.run(main)
"""


class EngineStreamingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="lah-stream-engine-")
        cls._old_data = os.environ.get("LOCAL_AI_HUB_DATA")
        os.environ["LOCAL_AI_HUB_DATA"] = cls._tmp
        cls.plugins = Path(cls._tmp) / "plugins"
        for name, source, entry in (
            ("stream.python", STREAM_PLUGIN_PY, "plugin.py"),
            ("stream.javascript", STREAM_PLUGIN_JS, "plugin.js"),
            ("stream.broken", BROKEN_CALLBACK_PLUGIN, "plugin.py"),
        ):
            directory = cls.plugins / name
            directory.mkdir(parents=True)
            runtime = "javascript" if entry.endswith(".js") else "python"
            (directory / "manifest.json").write_text(
                json.dumps(
                    {
                        "id": name,
                        "version": "1.0.0",
                        "runtime": {"type": runtime, "entry": entry},
                        "capabilities": ["llm.generate"],
                        "input": {"type": "object", "properties": {}},
                        "output": {"type": "object"},
                    }
                )
            )
            (directory / entry).write_text(source)

    @classmethod
    def tearDownClass(cls):
        if cls._old_data is None:
            os.environ.pop("LOCAL_AI_HUB_DATA", None)
        else:
            os.environ["LOCAL_AI_HUB_DATA"] = cls._old_data
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def engine(self, **ollama):
        events = []
        settings = {"mock": True}
        settings.update(ollama)
        engine = Engine(
            load_config(
                {"ollama": settings, "plugins": {"paths": [str(self.plugins)]}}
            ),
            emit=events.append,
        )
        return engine, events

    def test_python_sdk_receives_tokens(self):
        engine, events = self.engine()
        try:
            result = engine.handle({"type": "task", "id": "s-py", "plugin": "stream.python", "input": {}})
            self.assertTrue(result["ok"], result)
            output = result["output"]
            self.assertGreater(len(output["fragments"]), 1)
            self.assertEqual(output["joined"], output["text"])
            tokens = [event for event in events if event.get("event") == "model.token"]
            self.assertTrue(tokens)
            self.assertTrue(all(event["id"] == "s-py" for event in tokens))
            self.assertEqual("".join(event["data"]["text"] for event in tokens), output["text"])
        finally:
            engine.shutdown()

    def test_javascript_sdk_receives_tokens(self):
        engine, events = self.engine()
        try:
            result = engine.handle({"type": "task", "id": "s-js", "plugin": "stream.javascript", "input": {}})
            self.assertTrue(result["ok"], result)
            self.assertGreater(len(result["output"]["fragments"]), 1)
            self.assertEqual(result["output"]["joined"], result["output"]["text"])
            self.assertTrue([event for event in events if event.get("event") == "model.token"])
        finally:
            engine.shutdown()

    def test_config_stream_flag_emits_events_without_sdk_callback(self):
        engine, events = self.engine(stream=True)
        try:
            result = engine.handle(
                {"type": "task", "id": "s-cfg", "plugin": "hello.python", "input": {"use_llm": True}}
            )
            self.assertTrue(result["ok"], result)
            tokens = [event for event in events if event.get("event") == "model.token"]
            self.assertTrue(tokens)
            self.assertEqual(
                "".join(event["data"]["text"] for event in tokens), result["output"]["model_text"]
            )
        finally:
            engine.shutdown()

    def test_no_token_events_when_streaming_is_off(self):
        engine, events = self.engine()
        try:
            engine.handle({"type": "task", "plugin": "hello.python", "input": {"use_llm": True}})
            self.assertFalse([event for event in events if event.get("event") == "model.token"])
        finally:
            engine.shutdown()

    def test_a_broken_plugin_callback_does_not_fail_the_task(self):
        engine, _ = self.engine()
        try:
            result = engine.handle({"type": "task", "plugin": "stream.broken", "input": {}})
            self.assertTrue(result["ok"], result)
            self.assertIn("[mock]", result["output"]["text"])
        finally:
            engine.shutdown()

    def test_token_events_are_coalesced(self):
        from hub.engine import TOKEN_FLUSH_CHARS, _TokenForwarder

        emitted = []
        forwarder = _TokenForwarder(emitted.append)
        for _ in range(200):
            forwarder("x")
        forwarder.flush()
        self.assertLess(len(emitted), 200)
        self.assertEqual("".join(emitted), "x" * 200)
        self.assertTrue(all(len(text) >= TOKEN_FLUSH_CHARS for text in emitted[:-1]))

    def test_token_forwarder_ignores_empty_fragments(self):
        from hub.engine import _TokenForwarder

        emitted = []
        forwarder = _TokenForwarder(emitted.append)
        forwarder("")
        forwarder.flush()
        self.assertEqual(emitted, [])
        self.assertEqual(forwarder.tokens, 0)

    def test_token_forwarder_has_a_hard_event_cap(self):
        from hub.engine import MAX_TOKEN_EVENTS, _TokenForwarder

        emitted = []
        forwarder = _TokenForwarder(emitted.append)
        for _ in range(MAX_TOKEN_EVENTS + 50):
            forwarder("y" * 64)
        forwarder.flush()
        self.assertEqual(len(emitted), MAX_TOKEN_EVENTS)


if __name__ == "__main__":
    unittest.main()
