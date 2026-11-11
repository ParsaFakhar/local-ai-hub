"""Configuration loading and merging.

Layered, lowest priority first:

1. ``DEFAULTS`` below
2. ``<install>/config/hub.json``
3. ``<state>/config/hub.json``   (user overrides, survives updates)
4. environment variables (``LOCAL_AI_HUB_*``) for a few operational knobs
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

from . import paths
from .protocol import PAGE_INTERFACES

DEFAULTS: dict[str, Any] = {
    "log": {
        "level": "info",
        "file": "hub.log",
        "max_bytes": 1_048_576,
        "backups": 3,
    },
    "plugins": {
        "paths": ["plugins"],
        "disabled": [],
        "timeout_seconds": 180,
        "max_llm_calls": 64,
        "max_output_bytes": 8 * 1024 * 1024,
    },
    "runtime": {
        "python": "python3",
        "node": "node",
    },
    "ollama": {
        "binary": "auto",
        "host": "127.0.0.1",
        "port": 11434,
        "auto_start": True,
        "stop_on_exit": False,
        "startup_timeout_seconds": 45,
        "request_timeout_seconds": 240,
        "keep_alive": "5m",
        "idle_unload_seconds": 300,
        "default_model": "qwen3.5:4b",
        "vision_model": "qwen3.5:4b",
        "model_fallbacks": ["qwen3:4b", "qwen2.5:4b", "llama3.2:3b"],
        "num_ctx": 8192,
        "temperature": 0.1,
        # Streaming is opt-in: a plugin may also request it per call
        # (params.stream / SDK on_token), which overrides this default.
        "stream": False,
        "mock": False,
    },
    "domains": {
        "paths": ["domains"],
        "allow_extension_overrides": True,
    },
    "api": {
        "enabled": False,
        "host": "127.0.0.1",
        "port": 8765,
        "token_file": "api-token",
        "allowed_origins": [],
        "allow_token_query": True,
        "sse_keepalive_seconds": 15,
        # Opt-in alternative transport for local programs: an AF_UNIX socket
        # with 0600 permissions instead of a TCP port.
        "unix_socket": "",
    },
    "tasks": {
        "history_size": 100,
        "audit_log": True,
        "audit_file": "tasks.jsonl",
    },
    "cache": {
        "enabled": False,
        "ttl_seconds": 900,
        "max_entries": 256,
    },
    "bridge": {
        # Master switch for the userscript <-> content-script bridge.  A domain
        # must *also* opt in with "bridge": {"tampermonkey": true}.
        "enabled": True,
        "require_domain_opt_in": True,
        "allow_apply": True,
        "max_requests_per_minute": 30,
        "max_input_bytes": 256 * 1024,
    },
    "security": {
        "max_input_bytes": 4 * 1024 * 1024,
        "allow_page_supplied_config": False,
        "enforce_capabilities": True,
        "page_interfaces": sorted(PAGE_INTERFACES),
    },
}

_ENV_MAP = {
    "LOCAL_AI_HUB_MODEL": ("ollama", "default_model"),
    "LOCAL_AI_HUB_VISION_MODEL": ("ollama", "vision_model"),
    "LOCAL_AI_HUB_OLLAMA_HOST": ("ollama", "host"),
    "LOCAL_AI_HUB_OLLAMA_PORT": ("ollama", "port"),
    "LOCAL_AI_HUB_LOG_LEVEL": ("log", "level"),
    "LOCAL_AI_HUB_PYTHON": ("runtime", "python"),
    "LOCAL_AI_HUB_NODE": ("runtime", "node"),
}


def deep_merge(base: dict, override: dict) -> dict:
    """Recursive dict merge; ``override`` wins.  Lists are replaced, not appended."""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


class Config:
    def __init__(self, data: dict[str, Any], sources: list[str] | None = None):
        self.data = data
        self.sources = sources or []

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, name: str) -> dict:
        value = self.get(name, {})
        return value if isinstance(value, dict) else {}

    def as_dict(self) -> dict:
        return copy.deepcopy(self.data)


def _read_json(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        # Config problems must never take the host down; report on stderr.
        import sys

        print(f"[local-ai-hub] ignoring config {path}: {exc}", file=sys.stderr)
        return {}


def load_config(extra: dict | None = None) -> Config:
    data = copy.deepcopy(DEFAULTS)
    sources: list[str] = []

    for directory in reversed(paths.config_dirs()):  # lowest priority first
        path = directory / "hub.json"
        if path.is_file():
            data = deep_merge(data, _read_json(path))
            sources.append(str(path))

    for env_name, dotted in _ENV_MAP.items():
        raw = os.environ.get(env_name)
        if raw is None:
            continue
        node = data
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        current = node.get(parts[-1])
        node[parts[-1]] = type(current)(raw) if isinstance(current, int) and raw.isdigit() else raw

    if os.environ.get("LOCAL_AI_HUB_MOCK_LLM") in ("1", "true", "yes"):
        data["ollama"]["mock"] = True

    if extra:
        data = deep_merge(data, extra)

    return Config(data, sources)
