# Architecture

## Design principle

> Local AI Hub is a general-purpose local AI execution platform. Firefox is one
> interface. Every capability is a plugin, never an architectural change.

Three consequences drive the code:

1. **The transport is dumb.** The native host only frames JSON and forwards it.
2. **The engine is the only smart middle.** It validates, routes, merges config,
   runs plugins and owns the model runtime.
3. **Plugins are independent programs.** Separate processes, own manifests, own
   runtime, own dependencies. The engine never imports them.

## Components

### 1. Firefox extension (`extension/`)

| File | Responsibility |
| ---- | -------------- |
| `manifest.json` | MV2, persistent background page, `nativeMessaging`, gecko id `local-ai-hub@localaihub.dev` |
| `background.js` | Single native port, request/response correlation, reconnect with backoff, settings storage, image fetching, event fan-out, context menus, keep-alive alarm |
| `lib/protocol.js` | Envelope builders + message kinds shared by background and content |
| `lib/browser.js` | `browser.*` / `chrome.*` compatibility shim (used by the MV3 builds) |
| `content/extractors.js` | Generic selector-driven extraction (`root_selector`, `item_selector`, `fields`, `input` mapping) |
| `content/appliers.js` | Allowlisted result application (badge / class / attribute / hide / show / opt-in text) |
| `content/bridge.js` | Opt-in userscript bridge: policy mirror, input whitelist, rate limit, JSON-string events |
| `content/content.js` | Orchestration + shadow-DOM control panel, dry run, result review, selector picker, progress, commands |
| `ui/popup.*` | Runtime status, quick actions |
| `ui/options.*` | Domain editor (incl. bridge opt-in), feature templates for all ten plugins, plugin test run, settings, diagnostics, config export/import |

MV2 remains the reference implementation: a persistent background page keeps the
native port alive and makes long-running tasks and unsolicited events trivial.
**MV3 variants are built from the same sources** by `scripts/build_mv3.py`
(`dist/mv3-firefox` event page, `dist/mv3-chrome` service worker); only the
manifest and the background bootstrap differ. See [MV3.md](MV3.md).

The content script is loaded on all pages but stays inert until the domain has
enabled features. Webpages get **no** API surface — the only exception is the
userscript bridge on domains the user explicitly opted in, and it carries input
data only ([TAMPERMONKEY.md](TAMPERMONKEY.md)).

### 2. Native messaging host (`native/local_ai_hub_host.py`)

* Framing: 4-byte little-endian length + UTF-8 JSON (Firefox native messaging).
* `stdout` is protocol-only. All logging goes to `stderr` + a rotating file.
* A reader loop plus worker threads. `INLINE_CONTROL_TYPES` (`ping`, `status`,
  `shutdown`, `task.cancel`, `tasks.list`, `tasks.get`) are answered inline so
  they stay responsive while a long task runs.
* Outgoing frames are capped at 1 MiB and incoming frames at 64 MiB, with a clean
  `message_too_large` error instead of a broken pipe.
* EOF on stdin (browser closed) triggers model cleanup and exit.

### 3. Core engine (`hub/`)

| Module | Responsibility |
| ------ | -------------- |
| `engine.py` | Universal router: `task`, `ping`, `status`, `plugins.list`, `domains.*`, `models.*`, `config.get`, `tasks.*`, `task.cancel`, `cache.clear`, `shutdown` |
| `protocol.py` | Envelopes, validation, error codes, control types, native framing + size caps |
| `config.py` | Layered config: defaults → bundled → user → env |
| `domains.py` | Domain registry, specificity chain (`*` → parent → exact), extension overrides |
| `plugins/manager.py` | Discovery, manifest validation (id, runtime, entry containment) |
| `runtime/runner.py` | Subprocess execution, stdio RPC, timeouts, output caps, LLM call caps, `llm.token` streaming frames |
| `models/manager.py` | Ollama discovery, on-demand start, model resolution, idle unload, streaming, mock backend |
| `models/ollama.py` | Stdlib HTTP client (`/api/generate`, `/api/chat`, `/api/tags`, `/api/ps`) |
| `capabilities.py` | Per-plugin `llm.*` capability checks before every model call |
| `tasks.py` | Running-task registry, cancellation, bounded history, JSONL audit log (input hashes) |
| `cache.py` | Result cache: sha256 cache key, TTL, LRU entry cap, hit metadata |
| `bridge.py` | Userscript-bridge policy resolution + denial codes (pure functions) |
| `schema.py` | JSON-Schema subset validation, defaults, sample generation |
| `api.py` | Opt-in token-authenticated localhost HTTP API, SSE events, optional Unix socket |
| `client.py` | Client used by the CLI to talk to a running API (`--remote`) |

### 4. Plugin system (`plugins/`, `sdk/`)

A plugin folder contains `manifest.json` plus an entry program. The engine
launches `python3 -u plugin.py` or `node plugin.js` with:

* `cwd` = plugin directory
* a minimal environment plus `PYTHONPATH`/`NODE_PATH` pointing at the SDK
* one `invoke` frame on stdin
* a bidirectional line-delimited JSON channel for `llm.request` / `llm.response`,
  optional `llm.token` fragments, `log`, `progress` and the final `result`

Because the engine brokers every model call, plugins cannot bypass model
lifecycle, limits, capability checks or logging — and a plugin never needs to
know where inference comes from.

## Task data flow

```
page ──selectors──▶ items[{id, fields}] ──input map──▶ task.input
                                                        │
background ──native frame──▶ host ──▶ Engine.handle ─────┤
                                                        ▼
                                          PluginManager.get(id)
                                          DomainRegistry.feature(domain, id)
                                          bridge policy (page interfaces only)
                                          schema.validate(input)
                                          cache lookup (if enabled)
                                                        ▼
                                          run_plugin(subprocess)
                                            ├── llm.request → capabilities → ModelManager → Ollama
                                            ├── llm.token → SDK on_token; model.token → interface
                                            ├── progress/log → events → Firefox
                                            └── result{output}
                                                        ▼
                                          tasks.finish (history + audit) · cache.put
                                                        ▼
content ◀── result frame ◀── host ◀── Engine (meta: models, duration, cache)
   └── appliers: match by id → badge / class / attribute / hide
```

Every item carries a `data-lah-id`, which is how results find their way back to
the exact DOM node they came from — no fragile index arithmetic.

## Non-Firefox interfaces

The engine is transport-agnostic. Available today:

* **CLI** — `scripts/run_task.py` (`./bin/laihub …`), including `tasks`, `cancel`,
  `cache`, `serve`, `doctor` and `--remote`
* **Python** — `from hub.engine import Engine; Engine().handle(request)`
* **HTTP** — `hub/api.py`: `POST /v1/task`, control routes, `GET /v1/events`
  (Server-Sent Events), optional AF_UNIX transport, bearer token (opt-in)
* **Userscript** — Tampermonkey/Violentmonkey via the opt-in content-script
  bridge, which relays to the same engine over the native port

Still open: API stability guarantees and published client libraries (see
[ROADMAP.md](ROADMAP.md)).

## Failure handling

| Failure | Behaviour |
| ------- | --------- |
| Host not installed | Extension shows an actionable notice; requests fail fast with instructions |
| Host dies mid-task | Pending promises reject, port reconnects with exponential backoff (max 30 s) |
| Worker/event page suspended (MV3) | 30 s keep-alive alarm reconnects and sweeps lost deadlines; in-flight requests fail cleanly |
| Ollama missing/stopped | Started on demand; clear `model_unavailable` error with install/pull hint |
| Model tag missing | Fallback list, then an explicit `ollama pull <tag>` message |
| Plugin crashes | `plugin_failed` + stderr tail in `error.details` |
| Plugin hangs | `plugin_timeout`, process terminated then killed |
| Plugin floods stdout | Output cap → `plugin_failed` |
| Too many model calls | Per-task cap (`plugins.max_llm_calls`, default 64) |
| Undeclared capability | `capability_denied` before the model is touched |
| Page request without opt-in | `forbidden` from the bridge policy, logged |
| Task cancelled | `task_cancelled`; `task.cancelling` event first |
| Oversized result | `message_too_large` instead of a broken pipe |
| Invalid input/output | Schema errors returned/logged, never silently coerced |
