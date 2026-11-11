#!/usr/bin/env python3
"""End-to-end smoke test over the *real* native messaging transport.

Spawns bin/local-ai-hub-host exactly like Firefox does, speaks the 4-byte
length-prefixed JSON protocol and exercises every subsystem.  Uses the
offline mock model backend so it runs anywhere, with no model download.

    python3 scripts/smoke_test.py            # verbose
    python3 scripts/smoke_test.py --quiet
    python3 scripts/smoke_test.py --real     # use the real Ollama runtime
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hub import protocol  # noqa: E402

TINY_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class HostSession:
    def __init__(self, mock: bool = True, data_dir: str | None = None):
        env = dict(os.environ)
        env["LOCAL_AI_HUB_HOME"] = str(ROOT)
        env["LOCAL_AI_HUB_DATA"] = data_dir or tempfile.mkdtemp(prefix="lah-smoke-")
        env["PYTHONPATH"] = str(ROOT)
        if mock:
            env["LOCAL_AI_HUB_MOCK_LLM"] = "1"
        self.process = subprocess.Popen(
            [str(ROOT / "bin" / "local-ai-hub-host")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=str(ROOT),
        )
        self.events: list[dict] = []

    def send(self, message: dict) -> None:
        assert self.process.stdin
        self.process.stdin.write(protocol.encode_native_message(message))
        self.process.stdin.flush()

    def read(self, timeout: float = 60.0) -> dict:
        assert self.process.stdout
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            header = self.process.stdout.read(4)
            if not header or len(header) < 4:
                stderr = self.process.stderr.read().decode("utf-8", "replace") if self.process.stderr else ""
                raise RuntimeError(f"host closed the pipe. stderr:\n{stderr[-3000:]}")
            (length,) = struct.unpack("<I", header)
            payload = self.process.stdout.read(length)
            return json.loads(payload.decode("utf-8"))
        raise TimeoutError("no message from host")

    def request(self, message: dict, timeout: float = 90.0) -> dict:
        message.setdefault("id", protocol.new_id("smoke"))
        message.setdefault("protocol", "1.0")
        self.send(message)
        while True:
            reply = self.read(timeout)
            if reply.get("type") == "event":
                self.events.append(reply)
                continue
            if reply.get("id") == message["id"]:
                return reply
            raise RuntimeError(f"unexpected reply {reply}")

    def close(self) -> str:
        try:
            self.request({"type": "shutdown"}, timeout=15)
        except Exception:  # noqa: BLE001
            pass
        try:
            if self.process.stdin:
                self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
        stderr = self.process.stderr.read().decode("utf-8", "replace") if self.process.stderr else ""
        return stderr


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--real", action="store_true", help="use the real Ollama backend")
    args = parser.parse_args()

    verbose = not args.quiet
    results: list[tuple[str, bool, str]] = []
    session = HostSession(mock=not args.real)

    def check(name: str, condition: bool, detail: str = "") -> bool:
        results.append((name, bool(condition), detail))
        if verbose:
            mark = "\033[32mPASS\033[0m" if condition else "\033[31mFAIL\033[0m"
            print(f"  [{mark}] {name}{f' — {detail}' if detail else ''}")
        return bool(condition)

    try:
        if verbose:
            print(f"Local AI Hub smoke test ({'real model' if args.real else 'mock model'})\n")

        ready = session.read(timeout=30)
        check("host.ready event", ready.get("event") == "host.ready", json.dumps(ready.get("data", {})))

        ping = session.request({"type": "ping", "input": {"hello": "firefox"}})
        check("ping", ping.get("ok") and ping["output"]["pong"], f"protocol {ping.get('protocol')}")

        plugins = session.request({"type": "plugins.list"})
        ids = sorted(plugin["id"] for plugin in plugins.get("output", {}).get("plugins", []))
        expected = ["comments.classify", "hello.javascript", "hello.python", "images.tag"]
        check("plugin discovery", all(name in ids for name in expected), ", ".join(ids))
        check("no manifest errors", not plugins.get("output", {}).get("errors"), str(plugins.get("output", {}).get("errors")))

        py = session.request(
            {"type": "task", "plugin": "hello.python", "input": {"name": "M1 Pro"}}
        )
        check("python plugin", py.get("ok") and py["output"]["greeting"] == "Hello, M1 Pro!", py.get("output", {}).get("python_version", ""))

        if shutil.which("node"):
            js = session.request({"type": "task", "plugin": "hello.javascript", "input": {"name": "Nightly"}})
            check("javascript plugin", js.get("ok") and js["output"]["greeting"] == "Hello, Nightly!", js.get("output", {}).get("node_version", ""))
        else:
            check("javascript plugin", True, "skipped (node not installed)")

        domain = session.request({"type": "domains.get", "domain": "example-board.com"})
        features = domain.get("output", {}).get("features", {})
        check("domain config", "comments.classify" in features and "images.tag" in features, ", ".join(sorted(features)))

        comments = session.request(
            {
                "type": "task",
                "plugin": "comments.classify",
                "input": {
                    "comments": [
                        {"id": "c0", "text": "cheap watches at spam.example"},
                        {"id": "c1", "text": "thanks for the detailed write-up"},
                        {"id": "c2", "text": "nobody asked for your opinion, idiot"},
                    ]
                },
                "context": {"domain": "example-board.com", "url": "https://example-board.com/t/1", "interface": "smoke"},
            }
        )
        output = comments.get("output", {})
        check(
            "comment classification",
            comments.get("ok") and len(output.get("results", [])) == 3,
            f"{output.get('summary', {}).get('counts')} in {comments.get('meta', {}).get('duration_ms')} ms",
        )
        check(
            "labels restricted to allowlist",
            all(
                result["label"] in (output.get("summary", {}).get("labels", []) + ["unknown"])
                for result in output.get("results", [])
            ),
        )

        images = session.request(
            {
                "type": "task",
                "plugin": "images.tag",
                "input": {"images": [{"id": "i0", "data_base64": TINY_PNG_BASE64, "alt": "test pixel"}]},
                "context": {"domain": "example-board.com", "interface": "smoke"},
            }
        )
        image_results = images.get("output", {}).get("results", [])
        check("image tagging", images.get("ok") and len(image_results) == 1, json.dumps(image_results)[:120])

        invalid = session.request({"type": "task", "plugin": "comments.classify", "input": {"comments": "not-an-array"}})
        check("input validation rejects bad shape", not invalid.get("ok") and invalid["error"]["code"] == "invalid_input", invalid.get("error", {}).get("code", ""))

        unknown = session.request({"type": "task", "plugin": "does.not.exist", "input": {}})
        check("unknown plugin rejected", not unknown.get("ok") and unknown["error"]["code"] == "unknown_plugin")

        disabled = session.request(
            {
                "type": "task",
                "plugin": "hello.python",
                "input": {"name": "x"},
                "context": {"domain": "example-board.com", "domain_config": {"features": {"hello.python": {"enabled": False}}}},
            }
        )
        check("disabled feature blocked", not disabled.get("ok") and disabled["error"]["code"] == "plugin_disabled")

        status = session.request({"type": "status"})
        check("status report", status.get("ok") and "models" in status.get("output", {}), status.get("output", {}).get("models", {}).get("backend", ""))

        check("progress events received", any(event.get("event") == "progress" for event in session.events), f"{len(session.events)} events")
    finally:
        stderr = session.close()

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\n{passed}/{total} checks passed")
    if passed != total:
        print("\nhost stderr (tail):\n" + stderr[-2500:], file=sys.stderr)
        return 1
    if verbose and stderr.strip():
        print("\nhost log (tail):")
        print("\n".join(stderr.strip().splitlines()[-8:]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
