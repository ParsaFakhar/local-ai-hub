#!/usr/bin/env python3
"""Local AI Hub CLI — the universal protocol without Firefox.

    ./bin/laihub status
    ./bin/laihub plugins
    ./bin/laihub task hello.python --input '{"name":"M1 Pro"}'
    ./bin/laihub task comments.classify --domain example-board.com --input @comments.json
    ./bin/laihub task page.summarize --input @page.json --stream
    ./bin/laihub tasks --limit 10          # a running hub (API) if there is one
    ./bin/laihub cancel '*'                # ask that hub to stop what it is doing
    ./bin/laihub cache --clear
    ./bin/laihub serve --port 8765         # start the local API
    ./bin/laihub doctor
    ./bin/laihub --version

Task execution always happens in-process unless ``--remote`` is given, so the
CLI works with no server running at all.  Commands about *other* processes'
tasks (``tasks``, ``cancel``) automatically use the local API when it is
reachable, and say so when it is not.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from hub import PROTOCOL_VERSION, __version__, protocol  # noqa: E402
from hub.client import APIError, LocalClient  # noqa: E402
from hub.config import load_config  # noqa: E402
from hub.engine import Engine  # noqa: E402

CONTROL_COMMANDS = {
    "ping": "ping",
    "status": "status",
    "plugins": "plugins.list",
    "domains": "domains.list",
    "domain": "domains.get",
    "models": "models.list",
    "unload": "models.unload",
    "config": "config.get",
    "tasks": "tasks.list",
    "cache": "cache.clear",
}


def read_json_arg(value: str | None):
    if value is None:
        return {}
    if value == "-":
        return json.loads(sys.stdin.read() or "{}")
    if value.startswith("@"):
        return json.loads(Path(value[1:]).expanduser().read_text(encoding="utf-8"))
    return json.loads(value)


#: Global flags accepted *before or after* the subcommand.  SUPPRESS keeps an
#: unspecified flag from overwriting the value parsed at the top level.
def _common_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--mock", action="store_true", default=argparse.SUPPRESS,
                        help="use the offline mock model backend")
    common.add_argument("--config", default=argparse.SUPPRESS, help="extra config JSON (inline, @file)")
    common.add_argument("--events", action="store_true", default=argparse.SUPPRESS,
                        help="print progress events to stderr")
    common.add_argument("--compact", action="store_true", default=argparse.SUPPRESS,
                        help="single-line JSON output")
    common.add_argument("--remote", action="store_true", default=argparse.SUPPRESS,
                        help="send the request to a running local API")
    return common


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="laihub", description="Local AI Hub command line interface")
    parser.add_argument("--mock", action="store_true", help="use the offline mock model backend")
    parser.add_argument("--config", help="extra config JSON (inline, @file)")
    parser.add_argument("--events", action="store_true", help="print progress events to stderr")
    parser.add_argument("--compact", action="store_true", help="single-line JSON output")
    parser.add_argument("--remote", action="store_true", help="send the request to a running local API")
    parser.add_argument("--version", action="store_true", help="print version information and exit")
    common = _common_parser()
    sub = parser.add_subparsers(dest="command", parser_class=argparse.ArgumentParser)

    for name in ("ping", "status", "plugins", "domains", "models", "unload", "config"):
        sub.add_parser(name, parents=[common])

    domain_parser = sub.add_parser("domain", parents=[common], help="effective configuration for one domain")
    domain_parser.add_argument("name")

    tasks = sub.add_parser("tasks", parents=[common], help="running tasks + recent history")
    tasks.add_argument("--limit", type=int, default=20)
    tasks.add_argument("--running", action="store_true", help="only what is running right now")
    tasks.add_argument("--id", help="one task by id (tasks.get)")

    cancel = sub.add_parser("cancel", parents=[common], help="cancel a running task ('*' for all)")
    cancel.add_argument("task_id", nargs="?", default="*")

    cache = sub.add_parser("cache", parents=[common], help="result-cache statistics")
    cache.add_argument("--clear", action="store_true", help="drop every cached result")

    task = sub.add_parser("task", parents=[common], help="run a plugin")
    task.add_argument("plugin")
    task.add_argument("--input", default="{}", help="JSON, @file or - for stdin")
    task.add_argument("--options", default="{}", help="plugin config overrides (JSON/@file)")
    task.add_argument("--domain", default="")
    task.add_argument("--url", default="")
    task.add_argument("--title", default="")
    task.add_argument("--interface", default="cli")
    task.add_argument("--id", help="caller-chosen task id")
    task.add_argument("--stream", action="store_true", help="stream model tokens to stderr")
    task.add_argument("--timeout", type=float, help="plugin timeout in seconds")

    serve = sub.add_parser("serve", parents=[common], help="run the local HTTP API (opt-in)")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--unix-socket")
    serve.add_argument("--force", action="store_true", help="run even if api.enabled is false")

    doctor = sub.add_parser("doctor", parents=[common], help="diagnose the installation")
    doctor.add_argument("--task")
    doctor.add_argument("--input", default="{}")

    return parser


def version_document() -> dict:
    manifest = _ROOT / "extension" / "manifest.json"
    extension_version = None
    if manifest.is_file():
        try:
            extension_version = json.loads(manifest.read_text(encoding="utf-8")).get("version")
        except (OSError, json.JSONDecodeError):
            extension_version = None
    return {
        "app": "Local AI Hub",
        "version": __version__,
        "protocol": PROTOCOL_VERSION,
        "extension": extension_version,
        "python": sys.version.split()[0],
        "root": str(_ROOT),
    }


def _remote_client(config, args) -> LocalClient | None:
    """A reachable local API, or None (with a note on stderr when it matters)."""
    client = LocalClient.discover(config)
    if client.available():
        return client
    if args.remote:
        raise APIError(
            "no local API is listening. Start one with ./bin/laihub serve "
            "(or ./install.sh --api), or drop --remote."
        )
    return None


def _run_remote(client: LocalClient, args) -> dict:
    if args.command == "task":
        return client.run(
            args.plugin,
            read_json_arg(args.input),
            options=read_json_arg(args.options),
            context={"domain": args.domain, "url": args.url, "title": args.title, "interface": args.interface},
            task_id=args.id,
        )
    if args.command == "tasks":
        if args.id:
            output: dict = {"task": client.task_status(args.id)}
        else:
            output = client.tasks(args.limit)
            if args.running:
                output = {"running": output.get("running", []), "counters": output.get("counters", {})}
        return protocol.make_result(protocol.new_id("cli"), output)
    if args.command == "cancel":
        return protocol.make_result(protocol.new_id("cli"), client.cancel(args.task_id))
    if args.command == "cache":
        output = client.cache_clear() if args.clear else client.status().get("cache", {})
        return protocol.make_result(protocol.new_id("cli"), output)
    if args.command == "unload":
        return protocol.make_result(protocol.new_id("cli"), client.unload())

    route = {
        "ping": "/v1/ping",
        "status": "/v1/status",
        "plugins": "/v1/plugins",
        "domains": "/v1/domains",
        "models": "/v1/models",
        "config": "/v1/config",
        "domain": f"/v1/domains/{args.name}",
    }[args.command]
    return client.call("GET", route)


def _local_request(args) -> dict:
    if args.command == "task":
        request = {
            "type": "task",
            "id": args.id or protocol.new_id("cli"),
            "plugin": args.plugin,
            "input": read_json_arg(args.input),
            "options": read_json_arg(args.options),
            "context": {
                "domain": args.domain,
                "url": args.url,
                "title": args.title,
                "interface": args.interface,
            },
        }
        if args.timeout:
            request["timeout_seconds"] = args.timeout
        return request
    request = {"type": CONTROL_COMMANDS[args.command], "id": protocol.new_id("cli")}
    if args.command == "domain":
        request["domain"] = args.name
    if args.command == "tasks":
        if args.id:
            request = {"type": "tasks.get", "id": protocol.new_id("cli"), "task_id": args.id}
        elif args.limit:
            request["limit"] = args.limit
    if args.command == "cache" and not args.clear:
        # Statistics only: read them out of the status report.
        request = {"type": "status", "id": protocol.new_id("cli")}
    return request


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(json.dumps(version_document(), indent=None if args.compact else 2))
        return 0
    if not args.command:
        parser.print_help()
        return 2

    extra = read_json_arg(args.config) if args.config else {}
    if args.mock:
        extra.setdefault("ollama", {})["mock"] = True
    if getattr(args, "stream", False):
        extra.setdefault("ollama", {})["stream"] = True
    config = load_config(extra)

    # ---- commands that are not protocol messages -------------------------
    if args.command == "serve":
        from hub.api import main as api_main

        forwarded = []
        if args.host:
            forwarded += ["--host", args.host]
        if args.port is not None:
            forwarded += ["--port", str(args.port)]
        if args.unix_socket:
            forwarded += ["--unix-socket", args.unix_socket]
        if args.force:
            forwarded.append("--force")
        if args.mock:
            forwarded.append("--mock")
        return api_main(forwarded)

    if args.command == "doctor":
        from scripts.doctor import main as doctor_main

        forwarded = ["--input", args.input]
        if args.task:
            forwarded += ["--task", args.task]
        if args.mock:
            forwarded.append("--mock")
        return doctor_main(forwarded)

    # ---- remote (a running hub) ------------------------------------------
    client = None
    if args.remote or args.command in ("tasks", "cancel"):
        try:
            client = _remote_client(config, args)
        except APIError as exc:
            print(json.dumps(protocol.make_error("-", "api_unavailable", str(exc)), indent=2))
            return 1
    if client is not None:
        try:
            response = _run_remote(client, args)
        except APIError as exc:
            response = exc.envelope or protocol.make_error("-", "api_error", str(exc))
        print(json.dumps(response, ensure_ascii=False, indent=None if args.compact else 2))
        return 0 if response.get("ok") else 1

    # ---- in-process engine ------------------------------------------------
    tokens_seen = [0]

    def emit(message: dict) -> None:
        if message.get("event") == "model.token":
            tokens_seen[0] += 1
            if getattr(args, "stream", False):
                sys.stderr.write((message.get("data") or {}).get("text", ""))
                sys.stderr.flush()
            elif args.events:
                print(json.dumps(message, ensure_ascii=False), file=sys.stderr)
            return
        if args.events:
            print(json.dumps(message, ensure_ascii=False), file=sys.stderr)

    engine = Engine(config, emit=emit)
    try:
        response = engine.handle(_local_request(args))
    finally:
        engine.shutdown()
        if tokens_seen[0] and getattr(args, "stream", False):
            sys.stderr.write("\n")

    if args.command == "cache" and not args.clear and response.get("ok"):
        response = protocol.make_result(response["id"], (response.get("output") or {}).get("cache", {}))
    if args.command == "tasks" and args.running and response.get("ok"):
        output = response.get("output") or {}
        response = protocol.make_result(
            response["id"], {"running": output.get("running", []), "counters": output.get("counters", {})}
        )
    if args.command in ("tasks", "cancel") and response.get("ok"):
        response.setdefault("meta", {})["source"] = "local engine (no API listening)"

    print(json.dumps(response, ensure_ascii=False, indent=None if args.compact else 2))
    return 0 if response.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
