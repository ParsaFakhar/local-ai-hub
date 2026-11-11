"""Tampermonkey / userscript bridge policy.

A userscript cannot use native messaging.  Instead it dispatches a
``CustomEvent`` on ``document``; the content script picks it up, but **only**
on domains the user explicitly opted in, and only for plugin ids the user
enabled there.  The page supplies *input data* — never configuration,
selectors, actions or code.

Two enforcement points, one policy:

* the content script (``extension/content/bridge.js``) mirrors this module so
  a rejected request never even reaches the native host;
* the engine re-checks every task whose ``context.interface`` is page-facing,
  so a buggy or bypassed content script still cannot open the door.

Domain configuration (written by the user, in ``config/domains/<host>.json``
or the extension's Settings UI)::

    {
      "domain": "example-board.com",
      "bridge": {
        "tampermonkey": true,          # opt this site in (required)
        "plugins": ["comments.classify"],   # optional allowlist
        "allow_apply": false,          # may the page ask to apply results?
        "max_requests_per_minute": 30
      },
      "features": { ... }
    }

Nothing in here is ever read from the page: ``DomainRegistry`` loads local
files and extension overrides only.
"""

from __future__ import annotations

from typing import Any

#: Hub-level defaults (see ``config.DEFAULTS['bridge']``).
DEFAULT_MAX_REQUESTS_PER_MINUTE = 30
DEFAULT_MAX_INPUT_BYTES = 256 * 1024


def _positive_int(value: Any, fallback: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return number if number > 0 else fallback


def policy(domain_config: dict | None, hub_config=None) -> dict:
    """Resolve the effective bridge policy for one domain.

    ``domain_config`` is the *effective* domain configuration (files +
    extension overrides).  ``hub_config`` is a :class:`hub.config.Config`.
    Pure function: no I/O, trivially unit-testable.
    """
    domain_config = domain_config or {}
    bridge = domain_config.get("bridge")
    bridge = bridge if isinstance(bridge, dict) else {}

    def hub_get(key: str, default: Any) -> Any:
        return hub_config.get(f"bridge.{key}", default) if hub_config else default

    master = bool(hub_get("enabled", True))
    opted_in = bool(bridge.get("tampermonkey", False))
    features = domain_config.get("features")
    features = features if isinstance(features, dict) else {}
    enabled_features = sorted(
        name
        for name, feature in features.items()
        if isinstance(feature, dict) and feature.get("enabled")
    )

    declared = bridge.get("plugins")
    if isinstance(declared, list) and declared:
        # An explicit list is the user's decision and wins on its own.
        allowed = sorted({str(name) for name in declared if str(name)})
    else:
        # No list: expose exactly what the user already enabled on this site.
        allowed = enabled_features

    return {
        "enabled": master and opted_in,
        "opted_in": opted_in,
        "master_switch": master,
        "domain": domain_config.get("domain", ""),
        "plugins": allowed,
        "allow_apply": bool(bridge.get("allow_apply", False)) and bool(hub_get("allow_apply", True)),
        "max_requests_per_minute": _positive_int(
            bridge.get("max_requests_per_minute"),
            _positive_int(hub_get("max_requests_per_minute", DEFAULT_MAX_REQUESTS_PER_MINUTE),
                          DEFAULT_MAX_REQUESTS_PER_MINUTE),
        ),
        "max_input_bytes": _positive_int(
            bridge.get("max_input_bytes"),
            _positive_int(hub_get("max_input_bytes", DEFAULT_MAX_INPUT_BYTES), DEFAULT_MAX_INPUT_BYTES),
        ),
    }


def denial(resolved_policy: dict, plugin_id: str, input_bytes: int = 0) -> tuple[str, str] | None:
    """Return ``(code, message)`` to reject a page request, or ``None``.

    Codes match :class:`hub.protocol.ErrorCode` so the caller can turn the
    result straight into an error envelope.
    """
    if not resolved_policy.get("enabled"):
        return (
            "forbidden",
            "the userscript bridge is not enabled for this domain; "
            "set bridge.tampermonkey = true in its domain config",
        )
    if plugin_id not in (resolved_policy.get("plugins") or []):
        return (
            "forbidden",
            f"plugin {plugin_id!r} is not exposed to the bridge on this domain",
        )
    limit = int(resolved_policy.get("max_input_bytes") or DEFAULT_MAX_INPUT_BYTES)
    if input_bytes > limit:
        return (
            "input_too_large",
            f"bridge input is {input_bytes} bytes, limit is {limit}",
        )
    return None
