#!/usr/bin/env python3
"""Local AI Hub doctor — diagnose an installation in one command.

    python3 scripts/doctor.py
    python3 scripts/doctor.py --task hello.python
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hub import paths, protocol  # noqa: E402
from hub.config import load_config  # noqa: E402
from hub.engine import Engine  # noqa: E402
from hub.models import find_binary  # noqa: E402

GREEN, RED, YELLOW, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[0m"


def line(status: str, label: str, detail: str = "") -> None:
    colour = {"ok": GREEN, "warn": YELLOW, "fail": RED}.get(status, "")
    mark = {"ok": "✓", "warn": "!", "fail": "✗"}.get(status, "·")
    print(f"  {colour}{mark}{RESET} {label}{f' — {detail}' if detail else ''}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", help="run a plugin as a final check")
    parser.add_argument("--input", default="{}", help="task input JSON")
    parser.add_argument("--mock", action="store_true")
    args = parser.parse_args()

    problems = 0
    config = load_config({"ollama": {"mock": True}} if args.mock else None)

    print("Local AI Hub doctor\n")
    print("Paths")
    line("ok", "install root", str(paths.root_dir()))
    line("ok", "state dir", str(paths.data_dir()))
    line("ok", "log dir", str(paths.log_dir()))
    for source in config.sources:
        line("ok", "config", source)

    print("\nRuntimes")
    python_bin = shutil.which(config.get("runtime.python", "python3"))
    if python_bin and sys.version_info >= (3, 9):
        line("ok", "python", f"{python_bin} ({sys.version.split()[0]})")
    else:
        line("fail", "python", "Python 3.9+ required")
        problems += 1
    node_bin = shutil.which(config.get("runtime.node", "node"))
    if node_bin:
        line("ok", "node", node_bin)
    else:
        line("warn", "node", "not found — JavaScript plugins disabled")

    print("\nNative messaging")
    manifest = paths.native_manifest_dir() / "local_ai_hub.json"
    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text())
        except json.JSONDecodeError as exc:
            data = {}
            line("fail", "manifest", f"invalid JSON: {exc}")
            problems += 1
        target = Path(data.get("path", ""))
        if target.is_file() and os.access(target, os.X_OK):
            line("ok", "manifest", str(manifest))
            line("ok", "host executable", str(target))
        else:
            line("fail", "host executable", f"missing or not executable: {target}")
            problems += 1
        if "local-ai-hub@localaihub.dev" in (data.get("allowed_extensions") or []):
            line("ok", "allowed_extensions", "extension id matches")
        else:
            line("fail", "allowed_extensions", str(data.get("allowed_extensions")))
            problems += 1
    else:
        line("fail", "manifest", f"not found at {manifest} — run ./install.sh")
        problems += 1

    print("\nModels")
    binary = find_binary(config.get("ollama.binary", "auto"))
    engine = Engine(config)
    try:
        if config.get("ollama.mock"):
            line("warn", "backend", "mock (offline test mode)")
        elif binary:
            line("ok", "ollama binary", binary)
            alive = engine.models.client.is_alive()
            line("ok" if alive else "warn", "server", "running" if alive else "stopped (starts on demand)")
            if alive:
                models = engine.models.installed_models(refresh=True)
                line("ok" if models else "warn", "installed models", ", ".join(models) or "none")
                wanted = engine.models.default_model
                line("ok" if wanted in models else "warn", "default model", wanted if wanted in models else f"{wanted} missing → ollama pull {wanted}")
        else:
            line("warn", "ollama binary", "not found — install it or use --mock")

        print("\nPlugins")
        for plugin in engine.plugins.list():
            line("ok", plugin.id, f"{plugin.runtime['type']} · v{plugin.version}")
        for error in engine.plugins.errors:
            line("fail", "manifest error", f"{error['directory']}: {error['error']}")
            problems += 1

        print("\nDomains")
        domains = engine.domains.list_domains()
        if domains:
            for entry in domains:
                line("ok", entry["domain"], ", ".join(entry["features"]) or "no features")
        else:
            line("warn", "domains", "no domain configs found")

        if args.task:
            print(f"\nTask: {args.task}")
            response = engine.handle(
                {
                    "type": "task",
                    "id": protocol.new_id("doctor"),
                    "plugin": args.task,
                    "input": json.loads(args.input),
                    "context": {"interface": "doctor"},
                }
            )
            if response.get("ok"):
                line("ok", "task", f"{response['meta']['duration_ms']} ms")
                print(json.dumps(response.get("output"), indent=2, ensure_ascii=False)[:1500])
            else:
                line("fail", "task", json.dumps(response.get("error")))
                problems += 1
    finally:
        engine.shutdown()

    print()
    if problems:
        print(f"{RED}{problems} problem(s) found{RESET}")
        return 1
    print(f"{GREEN}installation looks healthy{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
