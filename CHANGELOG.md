# Changelog

## 0.2.0 — from prototype to platform

Engine

- **Per-plugin capability enforcement.** Manifest `capabilities` are checked
  before every model call: `llm.generate`, `llm.chat`, `llm.vision`,
  `llm.structured`. A plugin cannot use a capability it never declared
  (`capability_denied`); unknown names stay informational so newer plugins still
  load on older engines.
- **Task registry**: running tasks, bounded history (`tasks.history_size`) and an
  append-only JSONL **audit log** of input *hashes*, models and durations
  (`tasks.audit_log`, `tasks.audit_file`). Cancellation of one task or all.
- **Task priorities**: an optional `priority` (`0`-`9`, higher first, or
  `low`/`normal`/`high`) orders the native host's intake queue, FIFO within a
  level. `tasks.list` reports the waiting queue, `status` its depth, and
  `task.cancel` can drop a task before it starts (recorded as cancelled in the
  history and audit log). New keys `tasks.queue_size` and
  `tasks.default_priority`; CLI flag `laihub task --priority`.
- **Model warm-up** (opt-in, `ollama.warmup`): preload the default model — and
  optionally the vision model plus `ollama.warmup_models` — before the first
  task, on a background thread, using an empty-prompt load. New control type
  `models.warmup`, `POST /v1/models/warmup`, `laihub warmup [--models …]`,
  `model.warming` / `model.warmed` / `model.warmup_failed` / `model.warmup_done`
  events and a `warmup` report in `status` / `models.list`. Failures are
  per-model and never break a task; the idle reaper still unloads warm models.
- **Second inference backend**: `ollama.backend: "openai"` routes every model
  call to any OpenAI-compatible server (llama.cpp `llama-server`, LM Studio;
  aliases `llama.cpp`, `lmstudio`) instead of Ollama. Same interface, plugins
  unchanged: `/v1/chat/completions` with SSE streaming, `fmt` mapped to
  `response_format` (`json_object`/`json_schema`), base64 images converted to
  `data:` URI content parts, sampling options mapped (`num_predict` →
  `max_tokens`), optional bearer token (`ollama.api_key`). Warm-up becomes a
  1-token completion (LM Studio JIT load); auto-start, `keep_alive` and idle
  unload do not apply — the server manages its own lifecycle.
- **Result cache** with TTL and an entry cap (`cache.enabled`,
  `cache.ttl_seconds`, `cache.max_entries`), off by default, plus `cache.clear`.
- **Streaming inference** end to end: Ollama streaming and deterministic mock
  fragments, `on_token` in the Python and JavaScript SDKs, `llm.token` /
  `model.token` events, `--events` on the CLI.
- New control types `tasks.list`, `tasks.get`, `task.cancel`, `cache.clear`.
  Latency-sensitive controls (`ping`, `status`, `shutdown`, `task.cancel`,
  `tasks.list`, `tasks.get`) are answered inline instead of queueing behind a
  long task.
- **Userscript-bridge policy** (`hub/bridge.py`), re-checked by the engine for
  every page-facing interface (`tampermonkey`, `userscript`, `page`, `web`).
  Page-supplied `options` and `domain_config` are dropped, never merged.
- Native-pipe size guards (1 MiB outgoing, 64 MiB incoming) answer
  `message_too_large` instead of letting the browser drop the port. Error
  vocabulary gained `capability_denied`, `task_cancelled`, `unknown_task`,
  `rate_limited`, `unauthorized`, `forbidden`, `not_found`.

Command line

- `laihub tasks` (`--running`, `--id`, `--limit`), `cancel [<id>|*]`, `cache`,
  `serve`, `doctor`, `--version`, alongside the existing `ping`, `status`,
  `plugins`, `domains`, `domain`, `models`, `unload`, `config`, `task`.
- `--remote` sends the request to a running local API instead of starting an
  engine in-process.

Local HTTP API (still opt-in, localhost only, bearer token, no CORS)

- `GET /v1/ping`, `/v1/config`, `/v1/domains/<domain>`, `/v1/tasks`,
  `/v1/tasks/<id>`, `/v1/events` (Server-Sent Events), `/v1/openapi.json`.
- `POST /v1/task/cancel`, `/v1/cache/clear`, `/v1/models/unload`, `/v1/shutdown`.
- Optional **AF_UNIX socket** transport (`api.unix_socket`), origin allowlist
  (`api.allowed_origins`) and token-in-query auth (`api.allow_token_query`) for
  clients such as `EventSource` that cannot set headers.
- **Rate limiting** (`api.max_requests_per_minute`, default 120, `0` disables):
  one sliding-window quota for all authenticated requests, mirroring the
  userscript bridge policy; exhaustion answers `429` with a `rate_limited`
  envelope and a `Retry-After` header. Unauthenticated requests never consume
  quota. Documented in `/v1/openapi.json`.

Firefox extension

- **Tampermonkey / Violentmonkey bridge**: opt-in per domain, plugin allowlist,
  input data only, rate and size limits, results applied with the user's own
  action spec. Silent on sites that did not opt in.
- **Dry-run mode** (plan the changes, apply nothing), **per-item result review**,
  **selector picker**, context-menu "send selection to Local AI Hub" and
  per-feature keyboard commands.
- Options UI: feature templates for all ten plugins and a per-domain bridge
  opt-in.

Manifest V3

- `scripts/build_mv3.py` builds and validates `dist/mv3-firefox` (event page) and
  `dist/mv3-chrome` (service worker) from the unchanged MV2 sources; `make mv3`
  and a step in `scripts/check.sh`.
- `lib/browser.js` compatibility shim, `OffscreenCanvas` image re-encoding for
  workers, and a 30 s `lah-keepalive` alarm that reconnects the native port and
  sweeps deadlines lost to a worker restart.

Plugins

- Six new plugins: `page.summarize`, `text.translate`, `links.check`,
  `forms.fill`, `images.dedupe` (pure-Python perceptual hashing),
  `moderation.queue`. Ten bundled plugins in total.
- Fixes: `images.dedupe` hashes are no longer disturbed by floating-point
  rounding noise; `links.check` classifies anchors correctly, accepts bare-domain
  allowlist entries and honours `input.allowlist_domains`.

Config, docs, tests

- `config/hub.json` carries the `tasks`, `cache`, `api`, `bridge` and `security`
  sections the engine reads; every version string unified at 0.2.0.
- New docs: [TAMPERMONKEY.md](docs/TAMPERMONKEY.md) and [MV3.md](docs/MV3.md).
- Example inputs for every plugin and a ready-to-edit Tampermonkey userscript.
- **376 Python unit tests, 28 extension logic checks, 18 end-to-end smoke
  checks**, including new suites for the API, cache, CLI, bridge, capabilities,
  streaming, task registry, priorities, warm-up and the six new plugins;
  pure-Python PNG/JPEG fixtures.
- Repository hygiene: `.gitignore`, no tracked `.DS_Store` or scratch artifacts.

Known gaps: real imageboard selectors, a signed persistent Firefox installation,
MV3 exercised in a real browser, and an OS-level sandbox (plugins remain trusted
local programs).

## 0.1.0 — initial prototype

Platform

- Universal task protocol v1.0 (request / result / error / event envelopes).
- Native messaging host with 4-byte framing, worker threads, clean shutdown,
  protocol-only stdout and rotating file logs.
- Core engine: task router, plugin manager, domain registry, schema validation,
  config layering (defaults → bundled → user → env).
- Plugin execution over stdio JSON RPC for Python and Node.js, with timeouts,
  output caps and per-task model-call caps.
- Ollama lifecycle: binary discovery, on-demand `serve`, model resolution with
  fallbacks, structured JSON output, idle unload, optional stop-on-exit.
- Offline mock model backend for tests and development without any model.
- CLI (`bin/laihub`) and experimental token-authenticated localhost HTTP API.

Firefox extension (MV2)

- Persistent background page owning the native port, reconnect with backoff,
  request correlation, event fan-out, badge status.
- Generic selector-driven extraction (`root_selector`, `item_selector`,
  `fields`, `input` mapping) with stable `data-lah-id` result matching.
- Allowlisted result applier: badge / class / attribute / hide / show,
  opt-in text replacement. No innerHTML, no code execution.
- Shadow-DOM control panel with per-feature run buttons, progress and logs.
- Popup status + quick actions; options page with domain editor, feature
  templates, plugin test-run, settings, diagnostics, config export/import.
- Keyboard commands: Alt+Shift+L (run all), Alt+Shift+H (toggle panel).
- Background image fetching with downscaling (plugins receive pixels, not URLs).

Plugins

- `comments.classify` — batched contextual classification, schema-enforced
  labels, confidence, reasons, allowlist coercion, per-batch failure isolation.
- `images.tag` — local vision descriptions and normalized tags, per-image
  failure isolation, optional safety flags.
- `hello.python`, `hello.javascript` — reference plugins for both runtimes.
- Plugin template and Python/JavaScript SDKs (stdlib only).

Config, docs, tests

- Domain configs: `example-board.com` (spec example), `news.ycombinator.com`
  (working demo), `*` defaults.
- Docs: architecture, protocol, plugin API, domain config, macOS install,
  security, roadmap.
- 45 Python unit tests, 12 extension logic tests, 15 end-to-end smoke checks.
- `install.sh` / `uninstall.sh`, `doctor.py`, `make_icons.py`, `package.py`.

Known gaps: Tampermonkey bridge, real imageboard selectors, signed persistent
Firefox installation, capability sandboxing.
