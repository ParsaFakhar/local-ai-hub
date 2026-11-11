# Changelog

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
