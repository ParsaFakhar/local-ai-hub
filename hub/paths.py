"""Filesystem locations used by the hub.

``LOCAL_AI_HUB_HOME``  -> installation directory (code, bundled config, plugins)
``LOCAL_AI_HUB_DATA``  -> writable state directory (user config, logs, tokens)

On macOS the writable state directory defaults to
``~/Library/Application Support/LocalAIHub``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def root_dir() -> Path:
    env = os.environ.get("LOCAL_AI_HUB_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    env = os.environ.get("LOCAL_AI_HUB_DATA")
    if env:
        path = Path(env).expanduser()
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / "LocalAIHub"
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        path = Path(base) / "local-ai-hub"
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_dir() -> Path:
    path = data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_dirs() -> list[Path]:
    """Config search path, highest priority first."""
    dirs = [data_dir() / "config", root_dir() / "config"]
    return [d for d in dirs if d.is_dir()]


def resolve_relative(value: str) -> Path:
    """Resolve a config path relative to the installation root."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = root_dir() / path
    return path


def native_manifest_dir() -> Path:
    """Per-user Firefox native messaging manifest directory."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Mozilla" / "NativeMessagingHosts"
    return Path.home() / ".mozilla" / "native-messaging-hosts"
