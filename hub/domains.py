"""Per-domain configuration registry.

A domain file describes which plugins are active on a site and how the
interface should extract inputs / apply outputs::

    {
      "domain": "example-board.com",
      "features": {
        "comments.classify": {
          "enabled": true,
          "root_selector": "div.comments",
          "item_selector": "p.comment",
          "fields": {"text": {"attr": "text"}},
          "input": {"comments": "@items"},
          "options": {"labels": ["ok", "spam", "toxic"]}
        }
      }
    }

Resolution order (later wins): ``*`` -> parent domains -> exact host.
Only the *extension UI* may override these values at runtime; a webpage
never can (see ``security.allow_page_supplied_config``).
"""

from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from typing import Any

from . import paths
from .config import deep_merge
from .logging_util import get_logger

log = get_logger("domains")


def normalize_domain(value: str | None) -> str:
    if not value:
        return ""
    domain = str(value).strip().lower()
    for prefix in ("https://", "http://"):
        if domain.startswith(prefix):
            domain = domain[len(prefix) :]
    domain = domain.split("/", 1)[0].split("?", 1)[0]
    domain = domain.split("@")[-1].split(":", 1)[0]
    return domain.rstrip(".")


def domain_candidates(domain: str) -> list[str]:
    """Least specific first: '*', '*.com', 'com', '*.example.com', 'example.com', ..."""
    domain = normalize_domain(domain)
    if not domain:
        return ["*"]
    parts = domain.split(".")
    chain: list[str] = ["*"]
    for index in range(len(parts) - 1, -1, -1):
        suffix = ".".join(parts[index:])
        chain.append(f"*.{suffix}")
        chain.append(suffix)
    return chain


class DomainRegistry:
    #: Replace-semantics sync from the extension Settings UI (in memory only).
    MAX_OVERRIDE_DOMAINS = 200
    MAX_OVERRIDE_BYTES = 256 * 1024

    def __init__(self, config):
        self.config = config
        self._files: dict[str, dict] = {}
        self._overrides: dict[str, dict] = {}
        self._overrides_lock = threading.Lock()
        self.reload()

    # ------------------------------------------------------------------
    def _search_dirs(self) -> list[Path]:
        dirs: list[Path] = []
        for entry in self.config.get("domains.paths", ["domains"]):
            path = Path(entry).expanduser()
            if not path.is_absolute():
                # bundled configs live in <install>/config/<entry>
                bundled = paths.root_dir() / "config" / entry
                user = paths.data_dir() / "config" / entry
                dirs.extend([bundled, user])
            else:
                dirs.append(path)
        return [d for d in dirs if d.is_dir()]

    def reload(self) -> None:
        files: dict[str, dict] = {}
        for directory in self._search_dirs():
            for path in sorted(directory.glob("*.json")):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    log.warning("skipping domain config %s: %s", path, exc)
                    continue
                if not isinstance(data, dict):
                    continue
                key = normalize_domain(data.get("domain") or path.stem)
                if not key:
                    continue
                files[key] = deep_merge(files.get(key, {}), data)
                files[key]["domain"] = key
        self._files = files
        log.debug("loaded %d domain configs", len(files))

    # ------------------------------------------------------------------
    def list_domains(self) -> list[dict]:
        entries = {
            key: {
                "domain": key,
                "features": sorted((value.get("features") or {}).keys()),
                "label": value.get("label", key),
            }
            for key, value in self._files.items()
        }
        with self._overrides_lock:
            extra = {k: v for k, v in self._overrides.items() if k not in entries}
        for key, value in extra.items():
            features = value.get("features") if isinstance(value.get("features"), dict) else {}
            entries[key] = {"domain": key, "features": sorted(features.keys()),
                            "label": value.get("label", key)}
        return [entries[key] for key in sorted(entries)]

    def set_overrides(self, overrides: dict) -> int:
        """Store the extension's FULL override map (replace semantics, in memory).

        Only the extension Settings UI may call this (the engine enforces the
        interface). Raises ValueError on malformed or oversized input.
        """
        if not isinstance(overrides, dict):
            raise ValueError("overrides must be an object mapping domain -> override")
        if len(overrides) > self.MAX_OVERRIDE_DOMAINS:
            raise ValueError(
                f"too many domain overrides ({len(overrides)} > {self.MAX_OVERRIDE_DOMAINS})"
            )
        cleaned: dict[str, dict] = {}
        for domain, value in overrides.items():
            key = normalize_domain(domain)
            if not key or not isinstance(value, dict):
                raise ValueError("overrides must map non-empty domain names to objects")
            cleaned[key] = copy.deepcopy(value)
        if len(json.dumps(cleaned, ensure_ascii=False).encode("utf-8")) > self.MAX_OVERRIDE_BYTES:
            raise ValueError(f"domain overrides exceed {self.MAX_OVERRIDE_BYTES} bytes")
        with self._overrides_lock:
            self._overrides = cleaned
        log.debug("stored %d extension domain override(s)", len(cleaned))
        return len(cleaned)

    def has_file(self, domain: str) -> bool:
        return any(candidate in self._files for candidate in domain_candidates(domain))

    def has_override(self, domain: str) -> bool:
        with self._overrides_lock:
            return normalize_domain(domain) in self._overrides

    def get(self, domain: str, overrides: dict | None = None) -> dict:
        merged: dict[str, Any] = {"domain": normalize_domain(domain), "features": {}}
        for candidate in domain_candidates(domain):
            if candidate in self._files:
                merged = deep_merge(merged, self._files[candidate])
        merged["domain"] = normalize_domain(domain)

        if self.config.get("domains.allow_extension_overrides", True):
            with self._overrides_lock:
                stored = self._overrides.get(normalize_domain(domain))
            if stored is not None:
                # Explicit per-request overrides (trusted local callers) win
                # over the stored extension overrides.
                overrides = deep_merge(stored, overrides) if isinstance(overrides, dict) else stored
            if isinstance(overrides, dict):
                if isinstance(overrides.get("features"), dict):
                    merged = deep_merge(merged, {"features": overrides["features"]})
                if isinstance(overrides.get("bridge"), dict):
                    merged = deep_merge(merged, {"bridge": overrides["bridge"]})
        return merged

    def feature(
        self, domain: str, plugin_id: str, overrides: dict | None = None
    ) -> dict | None:
        features = self.get(domain, overrides).get("features") or {}
        feature = features.get(plugin_id)
        return feature if isinstance(feature, dict) else None

    def enabled_plugins(self, domain: str, overrides: dict | None = None) -> list[str]:
        features = self.get(domain, overrides).get("features") or {}
        return sorted(
            plugin_id
            for plugin_id, feature in features.items()
            if isinstance(feature, dict) and feature.get("enabled")
        )
