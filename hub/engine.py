"""The core engine: universal task router.

Interfaces (Firefox native host, CLI, HTTP API) call :meth:`Engine.handle`
with a protocol envelope and get a result envelope back.  The engine is the
only component that knows about plugins *and* models.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable

from . import PROTOCOL_VERSION, __version__, bridge, capabilities, protocol, schema
from .cache import ResultCache, cache_key
from .config import Config, deep_merge, load_config
from .domains import DomainRegistry, normalize_domain
from .logging_util import get_logger, setup_logging
from .models import ModelError, ModelManager
from .plugins import PluginManager
from .runtime import PluginRuntimeError, run_plugin
from .tasks import TaskRegistry, input_hash

log = get_logger("engine")

Emit = Callable[[dict], None]


def _is_error_envelope(value: Any) -> bool:
    """True for a complete ``{"type": "result", "ok": false, ...}`` envelope."""
    return (
        isinstance(value, dict)
        and value.get("type") == "result"
        and value.get("ok") is False
        and isinstance(value.get("error"), dict)
    )


class Engine:
    def __init__(self, config: Config | None = None, emit: Emit | None = None):
        self.config = config or load_config()
        self.logger = setup_logging(self.config)
        self.emit = emit
        self.plugins = PluginManager(self.config)
        self.domains = DomainRegistry(self.config)
        self.models = ModelManager(self.config, on_event=self._runtime_event)
        self.tasks = TaskRegistry(self.config)
        self.cache = ResultCache(self.config)
        self.started_at = time.time()
        self.task_count = 0
        self.error_count = 0
        self._counters_lock = threading.Lock()

    # ------------------------------------------------------------------
    def _runtime_event(self, name: str, data: dict) -> None:
        if self.emit:
            self.emit(protocol.make_event(name, data))

    def _task_event(self, request_id: str, name: str, data: dict) -> None:
        if self.emit:
            self.emit(protocol.make_event(name, data, request_id))

    def _bump(self, *, ok: bool) -> None:
        with self._counters_lock:
            if ok:
                self.task_count += 1
            else:
                self.error_count += 1

    # ------------------------------------------------------------------
    def handle(self, message: Any) -> dict:
        try:
            request = protocol.validate_request(message)
        except protocol.ProtocolError as exc:
            request_id = message.get("id") if isinstance(message, dict) else None
            return protocol.make_error(request_id or "-", protocol.ErrorCode.BAD_REQUEST, str(exc))

        request_id = request["id"]
        kind = request["type"]
        started = time.monotonic()
        try:
            if kind == "task":
                return self._handle_task(request)
            handler = getattr(self, f"_handle_{kind.replace('.', '_')}", None)
            if handler is None:
                return protocol.make_error(
                    request_id, protocol.ErrorCode.UNKNOWN_TYPE, f"unknown message type {kind!r}"
                )
            output = handler(request)
            # A control handler may return a ready-made error envelope (bad
            # arguments it validated itself).  Pass it through untouched instead
            # of wrapping an error inside a successful result.
            if _is_error_envelope(output):
                self._bump(ok=False)
                return output
            return protocol.make_result(
                request_id, output, {"duration_ms": int((time.monotonic() - started) * 1000)}
            )
        except Exception as exc:  # noqa: BLE001 - the host must survive any failure
            self._bump(ok=False)
            log.exception("error handling %s (%s)", kind, request_id)
            return protocol.make_error(
                request_id, protocol.ErrorCode.INTERNAL_ERROR, f"{type(exc).__name__}: {exc}"
            )

    # ------------------------------------------------------------------
    # Control handlers
    # ------------------------------------------------------------------
    def _handle_ping(self, request: dict) -> dict:
        return {
            "pong": True,
            "app": "Local AI Hub",
            "version": __version__,
            "protocol": PROTOCOL_VERSION,
            "time_ms": protocol.now_ms(),
            "echo": request.get("input"),
        }

    def _handle_status(self, request: dict) -> dict:
        return {
            "app": "Local AI Hub",
            "version": __version__,
            "protocol": PROTOCOL_VERSION,
            "uptime_seconds": int(time.time() - self.started_at),
            "plugins": {"count": len(self.plugins.list()), "errors": self.plugins.errors},
            "domains": {"count": len(self.domains.list_domains())},
            "tasks": self.tasks.counters(),
            "cache": self.cache.stats(),
            "models": self.models.status(),
            "security": {
                "enforce_capabilities": bool(self.config.get("security.enforce_capabilities", True)),
                "allow_page_supplied_config": bool(
                    self.config.get("security.allow_page_supplied_config", False)
                ),
            },
            "bridge": {
                "enabled": bool(self.config.get("bridge.enabled", True)),
                "require_domain_opt_in": bool(self.config.get("bridge.require_domain_opt_in", True)),
                "domains": [
                    entry["domain"]
                    for entry in self.domains.list_domains()
                    if bridge.policy(self.domains.get(entry["domain"]), self.config)["enabled"]
                ],
            },
            "config_sources": self.config.sources,
        }

    def _handle_tasks_list(self, request: dict) -> dict:
        limit = request.get("limit")
        limit = int(limit) if isinstance(limit, (int, float)) and limit > 0 else None
        return {
            "running": self.tasks.running(),
            "history": self.tasks.history(limit),
            "counters": self.tasks.counters(),
        }

    def _handle_task_cancel(self, request: dict) -> dict:
        target = request.get("task_id") or request.get("target")
        if target in (None, "", "*"):
            cancelled = self.tasks.cancel_all()
            log.info("cancelled %d running task(s)", len(cancelled))
            return {"cancelled": cancelled, "all": True}
        if not isinstance(target, str):
            return protocol.make_error(
                request["id"], protocol.ErrorCode.BAD_REQUEST, "'task_id' must be a string"
            )
        found = self.tasks.cancel(target)
        if not found:
            return {"cancelled": [], "unknown_task": target}
        log.info("cancellation requested for task %s", target)
        self._task_event(target, "task.cancelling", {"id": target})
        return {"cancelled": [target]}

    def _handle_cache_clear(self, request: dict) -> dict:
        return {"cleared": self.cache.clear(), "cache": self.cache.stats()}

    def _handle_plugins_list(self, request: dict) -> dict:
        return {"plugins": self.plugins.summaries(), "errors": self.plugins.errors}

    def _handle_domains_list(self, request: dict) -> dict:
        return {"domains": self.domains.list_domains()}

    def _handle_domains_get(self, request: dict) -> dict:
        domain = normalize_domain(
            request.get("domain") or (request.get("context") or {}).get("domain")
        )
        overrides = request.get("overrides") if isinstance(request.get("overrides"), dict) else None
        config = self.domains.get(domain, overrides)
        config["available_plugins"] = [plugin.id for plugin in self.plugins.list()]
        # Resolved userscript-bridge policy: the content script uses this instead
        # of re-deriving the rules, so both sides always agree.
        config["bridge_policy"] = bridge.policy(config, self.config)
        return config

    def _handle_models_list(self, request: dict) -> dict:
        return self.models.status()

    def _handle_models_unload(self, request: dict) -> dict:
        return {"unloaded": self.models.unload_all()}

    def _handle_config_get(self, request: dict) -> dict:
        data = self.config.as_dict()
        return {"config": data, "sources": self.config.sources}

    def _handle_shutdown(self, request: dict) -> dict:
        self.models.shutdown()
        return {"shutdown": True}

    # ------------------------------------------------------------------
    # Task execution
    # ------------------------------------------------------------------
    def _is_page_interface(self, interface: str) -> bool:
        """True for interfaces that relay requests originating inside a webpage."""
        configured = self.config.get("security.page_interfaces") or sorted(protocol.PAGE_INTERFACES)
        return str(interface or "").lower() in {str(name).lower() for name in configured}

    def _resolve_plugin_config(self, plugin, request: dict) -> tuple[dict, dict | None]:
        context = request.get("context") or {}
        domain = normalize_domain(context.get("domain"))
        overrides = context.get("domain_config") if isinstance(context.get("domain_config"), dict) else None

        # Configuration authority (docs/SECURITY.md §4): a page-facing interface
        # may submit *input*, never configuration.  Local interfaces (extension
        # background, CLI, HTTP, your own program) are the user's own code.
        if overrides and self._is_page_interface(context.get("interface")):
            if not self.config.get("security.allow_page_supplied_config", False):
                log.warning(
                    "dropping page-supplied domain_config from interface %r (task %s)",
                    context.get("interface"),
                    request.get("id"),
                )
                overrides = None

        feature = self.domains.feature(domain, plugin.id, overrides) if domain else None

        if feature is not None and feature.get("enabled") is False:
            raise PluginRuntimeError(
                protocol.ErrorCode.PLUGIN_DISABLED,
                f"plugin {plugin.id} is disabled for {domain}",
            )

        merged = deep_merge(plugin.default_config, (feature or {}).get("options") or {})
        request_options = request.get("options") or {}
        if request_options and self._is_page_interface(context.get("interface")):
            if not self.config.get("security.allow_page_supplied_config", False):
                log.warning(
                    "dropping page-supplied options from interface %r (task %s)",
                    context.get("interface"),
                    request.get("id"),
                )
                request_options = {}
        merged = deep_merge(merged, request_options)
        return merged, feature

    def _handle_task(self, request: dict) -> dict:
        request_id = request["id"]
        plugin_id = request["plugin"]
        started = time.monotonic()

        plugin = self.plugins.get(plugin_id)
        if plugin is None:
            return protocol.make_error(
                request_id,
                protocol.ErrorCode.UNKNOWN_PLUGIN,
                f"unknown plugin {plugin_id!r}",
                {"available": [p.id for p in self.plugins.list()]},
            )

        raw_input = request.get("input", {})
        encoded_size = len(json.dumps(raw_input, ensure_ascii=False))
        limit = int(self.config.get("security.max_input_bytes", 4 * 1024 * 1024))
        if encoded_size > limit:
            return protocol.make_error(
                request_id,
                protocol.ErrorCode.INPUT_TOO_LARGE,
                f"input is {encoded_size} bytes, limit is {limit}; reduce the batch size",
            )

        # Page-facing interfaces (userscript bridge) need an explicit, locally
        # configured opt-in for this domain *and* this plugin.  The policy is
        # read from local config only — never from the request.
        context = request.get("context") or {}
        if self._is_page_interface(context.get("interface")) and self.config.get(
            "bridge.require_domain_opt_in", True
        ):
            domain = normalize_domain(context.get("domain"))
            resolved = bridge.policy(self.domains.get(domain), self.config)
            rejected = bridge.denial(resolved, plugin_id, encoded_size)
            if rejected:
                code, message = rejected
                log.warning("bridge denied %s for %s: %s", plugin_id, domain or "-", message)
                return protocol.make_error(request_id, code, message)

        try:
            plugin_config, feature = self._resolve_plugin_config(plugin, request)
        except PluginRuntimeError as exc:
            return protocol.make_error(request_id, exc.code, exc.message, exc.details)

        payload_input = schema.apply_defaults(raw_input, plugin.input_schema)
        errors = schema.validate(payload_input, plugin.input_schema)
        if errors:
            return protocol.make_error(
                request_id, protocol.ErrorCode.INVALID_INPUT, "input does not match the plugin schema", errors
            )

        context = request.get("context") or {}
        payload = {
            "type": "invoke",
            "protocol": PROTOCOL_VERSION,
            "task_id": request_id,
            "plugin": {
                "id": plugin.id,
                "version": plugin.version,
                "dir": str(plugin.directory),
                "capabilities": list(plugin.capabilities),
            },
            "input": payload_input,
            "config": plugin_config,
            "context": {
                "domain": normalize_domain(context.get("domain")),
                "url": context.get("url"),
                "title": context.get("title"),
                "interface": context.get("interface", "unknown"),
                "tab_id": context.get("tab_id"),
            },
            "models": {
                "default": self.models.default_model,
                "vision": self.models.vision_model,
                "requested": plugin.models,
            },
            "limits": {
                "max_llm_calls": int(self.config.get("plugins.max_llm_calls", 64)),
                "timeout_seconds": plugin.timeout_seconds
                or self.config.get("plugins.timeout_seconds", 180),
            },
        }

        used_models: list[str] = []
        enforce_capabilities = bool(self.config.get("security.enforce_capabilities", True))

        def llm_handler(method: str, params: dict) -> dict:
            if method not in ("generate", "chat"):
                raise ModelError(f"unsupported model method {method!r}")
            if enforce_capabilities:
                reason = capabilities.denial(plugin.capabilities, method, params)
                if reason:
                    log.warning(
                        "capability denied for %s: %s (declared: %s)",
                        plugin.id,
                        reason,
                        ", ".join(plugin.capabilities) or "none",
                    )
                    raise ModelError(
                        f"capability denied for plugin {plugin.id}: {reason}",
                        protocol.ErrorCode.CAPABILITY_DENIED,
                    )
            if method == "generate":
                result = self.models.generate(
                    params.get("prompt", ""),
                    model=params.get("model"),
                    system=params.get("system"),
                    images=params.get("images"),
                    fmt=params.get("format"),
                    options=params.get("options"),
                )
            else:
                result = self.models.chat(
                    params.get("messages") or [],
                    model=params.get("model"),
                    fmt=params.get("format"),
                    options=params.get("options"),
                )
            if result.get("model") and result["model"] not in used_models:
                used_models.append(result["model"])
            return result

        # ---- result cache -------------------------------------------------
        digest = input_hash(payload_input)
        key = cache_key(
            plugin.id,
            plugin.version,
            plugin_config,
            payload_input,
            {"default": self.models.default_model, "vision": self.models.vision_model},
        )
        cached = self.cache.get(key)
        if cached is not None:
            output, cached_meta = cached
            cached_meta = dict(cached_meta)
            cached_meta["duration_ms"] = int((time.monotonic() - started) * 1000)
            self._bump(ok=True)
            self.tasks.register(
                request_id,
                plugin=plugin.id,
                domain=payload["context"]["domain"],
                interface=payload["context"]["interface"],
                digest=digest,
            )
            self.tasks.finish(
                request_id,
                ok=True,
                duration_ms=cached_meta["duration_ms"],
                models=cached_meta.get("models") or [],
                cached=True,
            )
            log.info("task %s -> %s served from cache", request_id, plugin.id)
            self._task_event(request_id, "task.finished", {"plugin": plugin.id, "cached": True})
            return protocol.make_result(request_id, output, cached_meta)

        cancel_event = self.tasks.register(
            request_id,
            plugin=plugin.id,
            domain=payload["context"]["domain"],
            interface=payload["context"]["interface"],
            digest=digest,
        )

        self._task_event(request_id, "task.started", {"plugin": plugin.id})
        log.info("task %s -> %s (%s)", request_id, plugin.id, payload["context"]["domain"] or "no domain")

        try:
            output = run_plugin(
                plugin,
                payload,
                config=self.config,
                llm_handler=llm_handler,
                on_event=lambda name, data: self._task_event(request_id, name, data),
                cancel_event=cancel_event,
            )
        except PluginRuntimeError as exc:
            self._bump(ok=False)
            duration_ms = int((time.monotonic() - started) * 1000)
            self.tasks.finish(
                request_id, ok=False, duration_ms=duration_ms, error=exc.message
            )
            log.warning("task %s failed: %s", request_id, exc.message)
            self._task_event(request_id, "task.failed", {"plugin": plugin.id, "error": exc.message})
            return protocol.make_error(request_id, exc.code, exc.message, exc.details)
        except ModelError as exc:
            self._bump(ok=False)
            duration_ms = int((time.monotonic() - started) * 1000)
            self.tasks.finish(request_id, ok=False, duration_ms=duration_ms, error=str(exc))
            self._task_event(request_id, "task.failed", {"plugin": plugin.id, "error": str(exc)})
            return protocol.make_error(request_id, exc.code, str(exc))

        output_errors = schema.validate(output, plugin.output_schema)
        if output_errors:
            log.warning("plugin %s output violates its schema: %s", plugin.id, output_errors)

        self._bump(ok=True)
        duration_ms = int((time.monotonic() - started) * 1000)
        meta = {
            "plugin": plugin.id,
            "plugin_version": plugin.version,
            "models": used_models,
            "mock_model": self.models.mock,
            "duration_ms": duration_ms,
            "domain": payload["context"]["domain"],
            "output_schema_errors": output_errors,
            "input_hash": digest,
        }
        self.tasks.finish(
            request_id, ok=True, duration_ms=duration_ms, models=used_models
        )
        self.cache.put(key, output, meta)
        self._task_event(request_id, "task.finished", {"plugin": plugin.id, "duration_ms": duration_ms})
        log.info("task %s done in %d ms", request_id, duration_ms)
        return protocol.make_result(request_id, output, meta)

    # ------------------------------------------------------------------
    def shutdown(self) -> None:
        self.tasks.cancel_all()
        self.models.shutdown()
