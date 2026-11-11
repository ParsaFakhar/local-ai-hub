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
| `background.js` | Single native port, request/response correlation, reconnect with backoff, settings storage, image fetching, event fan-out |
| `content/extractors.js` | Generic selector-driven extraction (`root_selector`, `item_selector`, `fields`, `input` mapping) |
| `content/appliers.js` | Allowlisted result application (badge / class / attribute / hide / show / opt-in text) |
| `content/content.js` | Orchestration + shadow-DOM control panel, progress, keyboard commands |
| `ui/popup.*` | Runtime status, quick actions |
| `ui/options.*` | Domain editor, plugin list + test run, settings, diagnostics |

MV2 is deliberate: a persistent background page keeps the native port alive and
makes long-running tasks and unsolicited events trivial. Firefox continues to
support MV2; an MV3 port is listed in the roadmap.

The content script is loaded on all pages but stays inert until the domain has
enabled features. Webpages get **no** API surface.

### 2. Native messaging host (`native/local_ai_hub_host.py`)

* Framing: 4-byte little-endian length + UTF-8 JSON (Firefox native messaging).
* `stdout` is protocol-only. All logging goes to `stderr` + a rotating file.
* A reader loop plus two worker threads: `ping`/`status` stay responsive while a
  long task runs.
* Outgoing frames are capped at 1 MiB (Firefox limit) with a clean error instead
  of a broken pipe.
* EOF on stdin (browser closed) triggers model cleanup and exit.

### 3. Core engine (`hub/`)

| Module | Responsibility |
| ------ | -------------- |
| `engine.py` | Universal router: `task`, `ping`, `status`, `plugins.list`, `domains.*`, `models.*`, `config.get`, `shutdown` |
| `protocol.py` | Envelopes, validation, error codes, native framing |
| `config.py` | Layered config: defaults → bundled → user → env |
| `domains.py` | Domain registry, specificity chain (`*` → parent → exact), extension overrides |
| `plugins/manager.py` | Discovery, manifest validation (id, runtime, entry containment) |
| `runtime/runner.py` | Subprocess execution, stdio RPC, timeouts, output caps, LLM call caps |
| `models/manager.py` | Ollama discovery, on-demand start, model resolution, idle unload, mock backend |
| `models/ollama.py` | Stdlib HTTP client (`/api/generate`, `/api/chat`, `/api/tags`, `/api/ps`) |
| `schema.py` | JSON-Schema subset validation, defaults, sample generation |
| `api.py` | Experimental token-authenticated localhost HTTP API |

### 4. Plugin system (`plugins/`, `sdk/`)

A plugin folder contains `manifest.json` plus an entry program. The engine
launches `python3 -u plugin.py` or `node plugin.js` with:

* `cwd` = plugin directory
* a minimal environment plus `PYTHONPATH`/`NODE_PATH` pointing at the SDK
* one `invoke` frame on stdin
* a bidirectional line-delimited JSON channel for `llm.request` / `llm.response`,
  `log`, `progress` and the final `result`

Because the engine brokers every model call, plugins cannot bypass model
lifecycle, limits or logging — and a plugin never needs to know where inference
comes from.

## Task data flow

```
page ──selectors──▶ items[{id, fields}] ──input map──▶ task.input
                                                        │
background ──native frame──▶ host ──▶ Engine.handle ─────┤
                                                        ▼
                                          PluginManager.get(id)
                                          DomainRegistry.feature(domain, id)
                                          schema.validate(input)
                                                        ▼
                                          run_plugin(subprocess)
                                            ├── llm.request → ModelManager → Ollama
                                            ├── progress/log → events → Firefox
                                            └── result{output}
                                                        ▼
content ◀── result frame ◀── host ◀── Engine (meta: models, duration)
   └── appliers: match by id → badge / class / attribute / hide
```

Every item carries a `data-lah-id`, which is how results find their way back to
the exact DOM node they came from — no fragile index arithmetic.

## Non-Firefox interfaces

The engine is transport-agnostic. Already available:

* **CLI** — `scripts/run_task.py` (`./bin/laihub …`)
* **Python** — `from hub.engine import Engine; Engine().handle(request)`
* **HTTP** — `hub/api.py`, `POST /v1/task` with a bearer token (opt-in)

Planned: Tampermonkey bridge and a stable public API (see ROADMAP).

## Failure handling

| Failure | Behaviour |
| ------- | --------- |
| Host not installed | Extension shows an actionable notice; requests fail fast with instructions |
| Host dies mid-task | Pending promises reject, port reconnects with exponential backoff (max 30 s) |
| Ollama missing/stopped | Started on demand; clear `model_unavailable` error with install/pull hint |
| Model tag missing | Fallback list, then an explicit `ollama pull <tag>` message |
| Plugin crashes | `plugin_failed` + stderr tail in `error.details` |
| Plugin hangs | `plugin_timeout`, process terminated then killed |
| Plugin floods stdout | Output cap → `plugin_failed` |
| Too many model calls | Per-task cap (`plugins.max_llm_calls`, default 64) |
| Oversized result | `message_too_large` instead of a broken pipe |
| Invalid input/output | Schema errors returned/logged, never silently coerced |
