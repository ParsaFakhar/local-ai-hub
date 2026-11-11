#!/usr/bin/env python3
"""Use Local AI Hub over the experimental local HTTP API.

    ./install.sh --api          # enable it once
    ./bin/laihub-api            # run the server (separate terminal)
    python3 examples/use_http_api.py

Stdlib only. The same client shape works from Node, Shortcuts, or any app.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hub import paths  # noqa: E402

BASE = "http://127.0.0.1:8765"


def token() -> str:
    path = paths.data_dir() / "api-token"
    if not path.is_file():
        raise SystemExit(f"No API token at {path}. Run ./install.sh --api first.")
    return path.read_text().strip()


def call(method: str, route: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(
        f"{BASE}{route}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token()}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read())
    except urllib.error.URLError as exc:
        raise SystemExit(f"Cannot reach {BASE}: {exc.reason}. Is ./bin/laihub-api running?")


def main() -> int:
    status = call("GET", "/v1/status")
    print("status:", json.dumps(status.get("output", {}).get("models", {}), indent=2))

    plugins = call("GET", "/v1/plugins")
    print("plugins:", [plugin["id"] for plugin in plugins.get("output", {}).get("plugins", [])])

    payload = json.loads((ROOT / "examples" / "comments.json").read_text())
    result = call(
        "POST",
        "/v1/task",
        {
            "plugin": "comments.classify",
            "input": payload,
            "context": {"domain": "example-board.com", "interface": "http-example"},
        },
    )
    print(json.dumps(result, indent=2)[:2000])
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
