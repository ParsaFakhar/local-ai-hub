"""CLI (`bin/laihub` / scripts/run_task.py): every subcommand, both modes.

The CLI is exercised in-process (fast, covers argument parsing) and once
through the real `bin/laihub` shell wrapper (covers the launcher itself).
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from hub import __version__, api, protocol
from hub.client import LocalClient
from hub.config import load_config
from scripts import run_task

ROOT = Path(__file__).resolve().parent.parent


class CLITestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="lah-cli-")
        cls._old_data = os.environ.get("LOCAL_AI_HUB_DATA")
        os.environ["LOCAL_AI_HUB_DATA"] = cls._tmp

    @classmethod
    def tearDownClass(cls):
        if cls._old_data is None:
            os.environ.pop("LOCAL_AI_HUB_DATA", None)
        else:
            os.environ["LOCAL_AI_HUB_DATA"] = cls._old_data
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def run_cli(self, *argv, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        old_stdin = sys.stdin
        if stdin is not None:
            sys.stdin = io.StringIO(stdin)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = run_task.main(list(argv))
        finally:
            sys.stdin = old_stdin
        return code, out.getvalue(), err.getvalue()

    def run_json(self, *argv, stdin=None):
        code, out, err = self.run_cli(*argv, stdin=stdin)
        try:
            return code, json.loads(out), err
        except json.JSONDecodeError:
            self.fail(f"CLI did not print JSON (exit {code}): {out!r} / {err!r}")


class ParserTests(CLITestCase):
    def test_no_command_prints_help(self):
        code, out, _ = self.run_cli()
        self.assertEqual(code, 2)
        self.assertIn("usage:", out)

    def test_version_document(self):
        code, payload, _ = self.run_json("--version")
        self.assertEqual(code, 0)
        self.assertEqual(payload["version"], __version__)
        self.assertEqual(payload["protocol"], protocol.PROTOCOL_VERSION)
        self.assertEqual(payload["extension"], __version__)
        self.assertEqual(payload["python"], sys.version.split()[0])

    def test_compact_version_is_one_line(self):
        _, out, _ = self.run_cli("--version", "--compact")
        self.assertEqual(len(out.strip().splitlines()), 1)

    def test_global_flags_are_accepted_after_the_subcommand(self):
        code, payload, _ = self.run_json("task", "hello.python", "--input", '{"name":"Post"}', "--mock")
        self.assertTrue(payload["ok"], payload)
        self.assertTrue(payload["meta"]["mock_model"])

    def test_unknown_plugin_exits_nonzero(self):
        code, payload, _ = self.run_json("--mock", "task", "no.such.plugin")
        self.assertEqual(code, 1)
        self.assertEqual(payload["error"]["code"], protocol.ErrorCode.UNKNOWN_PLUGIN)


class InputTests(CLITestCase):
    def test_inline_json(self):
        _, payload, _ = self.run_json("--mock", "task", "hello.python", "--input", '{"name":"Inline"}')
        self.assertEqual(payload["output"]["greeting"], "Hello, Inline!")

    def test_file_input(self):
        path = Path(self._tmp) / "input.json"
        path.write_text(json.dumps({"name": "File"}))
        _, payload, _ = self.run_json("--mock", "task", "hello.python", "--input", f"@{path}")
        self.assertEqual(payload["output"]["greeting"], "Hello, File!")

    def test_stdin_input(self):
        _, payload, _ = self.run_json(
            "--mock", "task", "hello.python", "--input", "-", stdin='{"name":"Stdin"}'
        )
        self.assertEqual(payload["output"]["greeting"], "Hello, Stdin!")

    def test_empty_stdin_is_an_empty_object(self):
        _, payload, _ = self.run_json("--mock", "task", "hello.python", "--input", "-", stdin="")
        self.assertEqual(payload["output"]["greeting"], "Hello, world!")

    def test_options_override_config(self):
        _, payload, _ = self.run_json(
            "--mock", "task", "hello.python",
            "--input", '{"name":"Opt"}',
            "--options", '{"greeting_template":"Howdy {name}"}',
        )
        self.assertEqual(payload["output"]["greeting"], "Howdy Opt")

    def test_bundled_example_input(self):
        _, payload, _ = self.run_json(
            "--mock", "task", "comments.classify",
            "--domain", "example-board.com",
            "--input", f"@{ROOT / 'examples' / 'comments.json'}",
        )
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(len(payload["output"]["results"]), 5)

    def test_context_flags_reach_the_plugin(self):
        _, payload, _ = self.run_json(
            "--mock", "task", "hello.python",
            "--domain", "Example.COM", "--url", "https://example.com/x", "--title", "T",
        )
        self.assertEqual(payload["output"]["context"]["domain"], "example.com")
        self.assertEqual(payload["meta"]["domain"], "example.com")

    def test_caller_chosen_id_is_echoed(self):
        _, payload, _ = self.run_json("--mock", "task", "hello.python", "--id", "cli-fixed")
        self.assertEqual(payload["id"], "cli-fixed")

    def test_timeout_flag_is_forwarded(self):
        _, payload, _ = self.run_json(
            "--mock", "task", "hello.python", "--timeout", "30", "--compact"
        )
        self.assertTrue(payload["ok"])


class ControlCommandTests(CLITestCase):
    def test_ping(self):
        _, payload, _ = self.run_json("--mock", "ping")
        self.assertTrue(payload["output"]["pong"])

    def test_status(self):
        _, payload, _ = self.run_json("--mock", "status")
        self.assertEqual(payload["output"]["models"]["backend"], "mock")

    def test_plugins(self):
        _, payload, _ = self.run_json("--mock", "plugins")
        ids = [entry["id"] for entry in payload["output"]["plugins"]]
        for expected in ("hello.python", "comments.classify", "page.summarize", "images.dedupe"):
            self.assertIn(expected, ids)
        self.assertEqual(payload["output"]["errors"], [])

    def test_domains_and_domain(self):
        _, listing, _ = self.run_json("--mock", "domains")
        self.assertIn("example-board.com", [entry["domain"] for entry in listing["output"]["domains"]])
        _, one, _ = self.run_json("--mock", "domain", "example-board.com")
        self.assertIn("comments.classify", one["output"]["features"])

    def test_models_and_unload(self):
        _, models, _ = self.run_json("--mock", "models")
        self.assertEqual(models["output"]["backend"], "mock")
        _, unloaded, _ = self.run_json("--mock", "unload")
        self.assertIn("unloaded", unloaded["output"])

    def test_config(self):
        _, payload, _ = self.run_json("--mock", "config")
        self.assertIn("config", payload["output"])

    def test_tasks_local_engine_is_labelled(self):
        _, payload, _ = self.run_json("--mock", "tasks")
        self.assertTrue(payload["ok"])
        self.assertIn("history", payload["output"])
        self.assertIn("local engine", payload["meta"]["source"])

    def test_tasks_running_filter(self):
        _, payload, _ = self.run_json("--mock", "tasks", "--running")
        self.assertEqual(set(payload["output"]), {"running", "counters"})

    def test_tasks_by_id_of_an_unknown_task(self):
        code, payload, _ = self.run_json("--mock", "tasks", "--id", "never-ran")
        self.assertEqual(code, 1)
        self.assertEqual(payload["error"]["code"], protocol.ErrorCode.UNKNOWN_TASK)

    def test_cancel_defaults_to_all(self):
        _, payload, _ = self.run_json("--mock", "cancel")
        self.assertTrue(payload["output"]["all"])

    def test_cancel_one_task(self):
        _, payload, _ = self.run_json("--mock", "cancel", "some-id")
        self.assertIn("cancelled", payload["output"])

    def test_cache_stats(self):
        _, payload, _ = self.run_json("--mock", "cache")
        self.assertIn("entries", payload["output"])
        self.assertFalse(payload["output"]["enabled"])

    def test_cache_clear(self):
        _, payload, _ = self.run_json("--mock", "cache", "--clear")
        self.assertEqual(payload["output"]["cleared"], 0)

    def test_extra_config_file_is_merged(self):
        path = Path(self._tmp) / "extra.json"
        path.write_text(json.dumps({"ollama": {"default_model": "test:1b"}}))
        _, payload, _ = self.run_json("--config", f"@{path}", "--mock", "models")
        self.assertEqual(payload["output"]["default_model"], "test:1b")


class EventAndStreamTests(CLITestCase):
    def test_events_go_to_stderr_not_stdout(self):
        code, out, err = self.run_cli(
            "--mock", "--events", "task", "comments.classify",
            "--input", f"@{ROOT / 'examples' / 'comments.json'}", "--compact",
        )
        self.assertEqual(code, 0)
        json.loads(out)  # stdout stays a single JSON document
        self.assertIn('"event"', err)

    def test_stream_prints_tokens_to_stderr(self):
        code, out, err = self.run_cli(
            "--mock", "task", "hello.python",
            "--input", '{"name":"Stream","use_llm":true}', "--stream", "--compact",
        )
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertIn("[mock]", err)
        self.assertEqual(payload["output"]["model_text"].strip(), err.strip())

    def test_stream_flag_enables_streaming_in_config(self):
        _, payload, _ = self.run_json(
            "--mock", "task", "hello.python", "--input", '{"use_llm":true}', "--stream", "--compact"
        )
        self.assertTrue(payload["ok"])


class ForwardingTests(CLITestCase):
    def test_serve_forwards_flags_to_the_api(self):
        with mock.patch("hub.api.main", return_value=0) as target:
            code, _, _ = self.run_cli(
                "serve", "--host", "127.0.0.1", "--port", "9999",
                "--unix-socket", "/tmp/x.sock", "--force", "--mock",
            )
        self.assertEqual(code, 0)
        forwarded = target.call_args[0][0]
        for expected in ("--host", "127.0.0.1", "--port", "9999", "--unix-socket", "/tmp/x.sock", "--force", "--mock"):
            self.assertIn(expected, forwarded)

    def test_doctor_forwards_flags(self):
        with mock.patch("scripts.doctor.main", return_value=0) as target:
            code, _, _ = self.run_cli("doctor", "--task", "hello.python", "--input", "{}", "--mock")
        self.assertEqual(code, 0)
        forwarded = target.call_args[0][0]
        self.assertIn("--task", forwarded)
        self.assertIn("--mock", forwarded)

    def test_doctor_runs_for_real(self):
        code, out, _ = self.run_cli("--mock", "doctor")
        self.assertIn("Plugins", out)
        self.assertIn("hello.python", out)
        self.assertIn(code, (0, 1))  # 1 when the native manifest is not installed


class RemoteTests(CLITestCase):
    def test_remote_without_a_server_explains_itself(self):
        code, payload, _ = self.run_json("--remote", "--mock", "status")
        self.assertEqual(code, 1)
        self.assertEqual(payload["error"]["code"], "api_unavailable")
        self.assertIn("laihub serve", payload["error"]["message"])

    def test_tasks_falls_back_to_the_local_engine(self):
        _, payload, _ = self.run_json("--mock", "tasks")
        self.assertIn("source", payload["meta"])


class RemoteWithServerTests(CLITestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import socket

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            cls.port = probe.getsockname()[1]
        cls.config = load_config(
            {
                "ollama": {"mock": True},
                "api": {"enabled": True, "port": cls.port, "token_file": "cli-token"},
            }
        )
        api.ensure_token(cls.config)
        cls.server = api.make_server(cls.config)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server.hub_engine.shutdown()
        cls.thread.join(timeout=5)
        super().tearDownClass()

    def cli_config(self):
        return json.dumps({"api": {"port": self.port, "token_file": "cli-token"}})

    def test_remote_status_uses_the_api(self):
        code, out, _ = self.run_cli("--remote", "--config", self.cli_config(), "--mock", "status")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertNotIn("source", payload.get("meta", {}))

    def test_remote_task_runs_on_the_server(self):
        code, out, _ = self.run_cli(
            "--remote", "--config", self.cli_config(), "task", "hello.python",
            "--input", '{"name":"Remote"}', "--id", "remote-cli-1",
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["output"]["greeting"], "Hello, Remote!")
        client = LocalClient.discover(self.config)
        self.assertEqual(client.task_status("remote-cli-1")["id"], "remote-cli-1")

    def test_tasks_are_read_from_the_running_hub(self):
        client = LocalClient.discover(self.config)
        client.run("hello.python", {"name": "X"}, task_id="remote-list-1")
        code, out, _ = self.run_cli("--config", self.cli_config(), "tasks", "--limit", "10")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertIn("remote-list-1", [entry["id"] for entry in payload["output"]["history"]])

    def test_cancel_uses_the_running_hub(self):
        code, out, _ = self.run_cli("--config", self.cli_config(), "cancel", "*")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["output"]["all"])

    def test_cache_clear_uses_the_running_hub(self):
        code, out, _ = self.run_cli("--config", self.cli_config(), "cache", "--clear")
        self.assertEqual(code, 0)
        self.assertIn("cleared", json.loads(out)["output"])


class LauncherTests(CLITestCase):
    def test_shell_wrapper_runs(self):
        result = subprocess.run(
            [str(ROOT / "bin" / "laihub"), "--version", "--compact"],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "LOCAL_AI_HUB_DATA": self._tmp},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["version"], __version__)

    def test_shell_wrapper_runs_a_task(self):
        result = subprocess.run(
            [str(ROOT / "bin" / "laihub"), "--mock", "--compact", "task", "hello.python",
             "--input", '{"name":"Wrapper"}'],
            capture_output=True, text=True, timeout=120,
            env={**os.environ, "LOCAL_AI_HUB_DATA": self._tmp},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["output"]["greeting"], "Hello, Wrapper!")


if __name__ == "__main__":
    unittest.main()
