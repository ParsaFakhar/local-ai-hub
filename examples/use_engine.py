#!/usr/bin/env python3
"""Use Local AI Hub from your own Python program — no Firefox, no HTTP.

    python3 examples/use_engine.py
    python3 examples/use_engine.py --real     # use Ollama instead of the mock
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hub.config import load_config  # noqa: E402
from hub.engine import Engine  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--real", action="store_true", help="use the real Ollama backend")
    args = parser.parse_args()

    config = load_config(None if args.real else {"ollama": {"mock": True}})
    engine = Engine(config, emit=lambda event: print(f"  event: {event['event']} {event.get('data', {})}"))

    try:
        print("Registered plugins:", [plugin.id for plugin in engine.plugins.list()])

        comments = json.loads((ROOT / "examples" / "comments.json").read_text())
        response = engine.handle(
            {
                "type": "task",
                "plugin": "comments.classify",
                "input": comments,
                "context": {"domain": "example-board.com", "interface": "python-example"},
            }
        )
        if not response["ok"]:
            print("failed:", response["error"])
            return 1

        print(f"\n{response['meta']['duration_ms']} ms, models: {response['meta']['models']}")
        for result in response["output"]["results"]:
            print(f"  {result['id']}: {result['label']:<10} {result['confidence']:.2f}  {result['reason'][:60]}")
        print("\nsummary:", response["output"]["summary"]["counts"])
    finally:
        engine.shutdown()   # unloads models, optionally stops a hub-started Ollama
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
