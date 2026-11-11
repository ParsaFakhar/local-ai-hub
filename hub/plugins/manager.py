"""Plugin discovery and manifest validation.

A plugin is an *independent local program* plus a ``manifest.json``.
The engine never imports plugin code: it launches the declared runtime
(python / node) and talks to it over stdio.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import paths
from ..logging_util import get_logger

log = get_logger("plugins")

PLUGIN_ID_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
SUPPORTED_RUNTIMES = {"python", "javascript"}


class ManifestError(ValueError):
    pass


@dataclass
class Plugin:
    id: str
    name: str
    version: str
    description: str
    runtime: dict
    directory: Path
    input_schema: dict = field(default_factory=dict)
    output_schema: dict = field(default_factory=dict)
    config_schema: dict = field(default_factory=dict)
    default_config: dict = field(default_factory=dict)
    models: dict = field(default_factory=dict)
    capabilities: list[str] = field(default_factory=list)
    timeout_seconds: float | None = None
    raw: dict = field(default_factory=dict)

    @property
    def entry(self) -> Path:
        return self.directory / self.runtime["entry"]

    def summary(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "runtime": self.runtime.get("type"),
            "entry": self.runtime.get("entry"),
            "capabilities": self.capabilities,
            "models": self.models,
            "default_config": self.default_config,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "config_schema": self.config_schema,
            "directory": str(self.directory),
        }


def _validate_manifest(data: dict, directory: Path) -> None:
    plugin_id = data.get("id")
    if not isinstance(plugin_id, str) or not PLUGIN_ID_RE.match(plugin_id):
        raise ManifestError(f"invalid plugin id {plugin_id!r}")
    if not isinstance(data.get("version"), str):
        raise ManifestError("manifest requires a string 'version'")

    runtime = data.get("runtime")
    if not isinstance(runtime, dict):
        raise ManifestError("manifest requires a 'runtime' object")
    if runtime.get("type") not in SUPPORTED_RUNTIMES:
        raise ManifestError(f"unsupported runtime {runtime.get('type')!r}")
    entry = runtime.get("entry")
    if not isinstance(entry, str) or not entry:
        raise ManifestError("runtime requires an 'entry' file")

    entry_path = (directory / entry).resolve()
    try:
        entry_path.relative_to(directory.resolve())
    except ValueError as exc:
        raise ManifestError("runtime entry escapes the plugin directory") from exc
    if not entry_path.is_file():
        raise ManifestError(f"runtime entry not found: {entry}")


def load_plugin(directory: Path) -> Plugin:
    manifest_path = directory / "manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ManifestError("manifest must be a JSON object")
    _validate_manifest(data, directory)
    return Plugin(
        id=data["id"],
        name=data.get("name", data["id"]),
        version=data["version"],
        description=data.get("description", ""),
        runtime=data["runtime"],
        directory=directory.resolve(),
        input_schema=data.get("input", {}) or {},
        output_schema=data.get("output", {}) or {},
        config_schema=data.get("config_schema", {}) or {},
        default_config=data.get("default_config", {}) or {},
        models=data.get("models", {}) or {},
        capabilities=data.get("capabilities", []) or [],
        timeout_seconds=data.get("timeout_seconds"),
        raw=data,
    )


class PluginManager:
    def __init__(self, config):
        self.config = config
        self._plugins: dict[str, Plugin] = {}
        self.errors: list[dict] = []
        self.discover()

    def _search_dirs(self) -> list[Path]:
        result: list[Path] = []
        for entry in self.config.get("plugins.paths", ["plugins"]):
            path = paths.resolve_relative(entry)
            if path.is_dir():
                result.append(path)
        return result

    def discover(self) -> None:
        plugins: dict[str, Plugin] = {}
        errors: list[dict] = []
        disabled = set(self.config.get("plugins.disabled", []) or [])

        for root in self._search_dirs():
            for directory in sorted(p for p in root.iterdir() if p.is_dir()):
                if not (directory / "manifest.json").is_file():
                    continue
                try:
                    plugin = load_plugin(directory)
                except (ManifestError, OSError, json.JSONDecodeError) as exc:
                    errors.append({"directory": str(directory), "error": str(exc)})
                    log.warning("plugin %s rejected: %s", directory.name, exc)
                    continue
                if plugin.id in disabled:
                    log.info("plugin %s disabled by configuration", plugin.id)
                    continue
                if plugin.id in plugins:
                    errors.append(
                        {"directory": str(directory), "error": f"duplicate plugin id {plugin.id}"}
                    )
                    continue
                plugins[plugin.id] = plugin

        self._plugins = plugins
        self.errors = errors
        log.info("discovered %d plugin(s): %s", len(plugins), ", ".join(sorted(plugins)) or "-")

    def get(self, plugin_id: str) -> Plugin | None:
        return self._plugins.get(plugin_id)

    def list(self) -> list[Plugin]:
        return [self._plugins[key] for key in sorted(self._plugins)]

    def summaries(self) -> list[dict]:
        return [plugin.summary() for plugin in self.list()]
