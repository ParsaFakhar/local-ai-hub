#!/usr/bin/env python3
"""Local AI Hub CLI — use the engine from any Python program or shell script.

    ./bin/laihub plugins
    ./bin/laihub status
    ./bin/laihub task hello.python --input '{"name":"M1 Pro"}'
    ./bin/laihub task comments.classify --domain example-board.com --input @comments.json
    ./bin/laihub task hello.javascript --mock          # offline, no model needed

This is the same universal protocol Firefox uses, minus the transport.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from hub import protocol  # noqa: E402
from hub.config import load_config  # noqa: E402
from hub.engine import Engine  # noqa: E402


def read_json_arg(value: str | None):
    if value is None:
        return {}
    if value == "-":
        return json.loads(sys.stdin.read() or "{}")
    if value.startswith("@"):
        return json.loads(Path(value[1:]).expanduser().read_text(encoding="utf-8"))
    return json.loads(value)


def main() -> int:
    parser = argparse.ArgumentParser(prog="laihub", description="Local AI Hub command line interface")
    parser.add_argument("--mock", action="store_true", help="use the offline mock model backend")
    parser.add_argument("--config", help="extra config JSON (inline, @file)")
    parser.add_argument("--events", action="store_true", help="print progress events to stderr")
    parser.add_argument("--compact", action="store_true", help="single-line JSON output")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("ping")
    sub.add_parser("status")
    sub.add_parser("plugins")
    sub.add_parser("domains")
    domain_parser = sub.add_parser("domain")
    domain_parser.add_argument("name")
    sub.add_parser("models")
    sub.add_parser("unload")

    task = sub.add_parser("task")
    task.add_argument("plugin")
    task.add_argument("--input", default="{}", help="JSON, @file or - for stdin")
    task.add_argument("--options", default="{}", help="plugin config overrides (JSON/@file)")
    task.add_argument("--domain", default="")
    task.add_argument("--url", default="")
    task.add_argument("--interface", default="cli")

    args = parser.parse_args()

    extra = read_json_arg(args.config) if args.config else {}
    if args.mock:
        extra.setdefault("ollama", {})["mock"] = True
    config = load_config(extra)

    def emit(message: dict) -> None:
        if args.events:
            print(json.dumps(message, ensure_ascii=False), file=sys.stderr)

    engine = Engine(config, emit=emit)

    if args.command == "task":
        request = {
            "type": "task",
            "id": protocol.new_id("cli"),
            "plugin": args.plugin,
            "input": read_json_arg(args.input),
            "options": read_json_arg(args.options),
            "context": {
                "domain": args.domain,
                "url": args.url,
                "interface": args.interface,
            },
        }
    else:
        mapping = {
            "ping": "ping",
            "status": "status",
            "plugins": "plugins.list",
            "domains": "domains.list",
            "domain": "domains.get",
            "models": "models.list",
            "unload": "models.unload",
        }
        request = {"type": mapping[args.command], "id": protocol.new_id("cli")}
        if args.command == "domain":
            request["domain"] = args.name

    try:
        response = engine.handle(request)
    finally:
        engine.shutdown()

    indent = None if args.compact else 2
    print(json.dumps(response, ensure_ascii=False, indent=indent))
    return 0 if response.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
