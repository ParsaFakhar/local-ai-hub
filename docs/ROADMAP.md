# Roadmap

## Next up (validate the prototype)

1. **Real imageboard integration** — replace `example-board.com` placeholders
   with your board's actual selectors, run `comments.classify` and `images.tag`
   on a live thread, tune `batch_size`, labels and instructions.
2. **Real model validation** — pick an installed tag (`qwen3:4b`, `qwen2.5:4b`,
   a vision model for tagging), measure latency on M1 Pro, tune `num_ctx`,
   `keep_alive` and `idle_unload_seconds`.
3. **Persistent signed installation** — unlisted AMO self-signing so the
   extension survives restarts (see INSTALL_MACOS.md).

## Planned interfaces

### Tampermonkey bridge (not implemented)

Design sketch, keeping the security model intact:

* A userscript cannot use native messaging. Bridge options:
  1. **Extension-mediated**: the userscript dispatches a `CustomEvent` on
     `document`; the content script listens only on domains explicitly opted in
     via `"bridge": {"tampermonkey": true}` in the domain config, and only
     accepts *registered plugin ids* + schema-validated input. No config, no
     code, no selectors from the page.
  2. **Local HTTP**: the userscript calls `127.0.0.1:8765` with a token the user
     pastes into the script. Requires enabling the API and adding a strict
     origin allowlist — deliberately not enabled by default.
* Either way: the page never gains the ability to run arbitrary code, and
  results still pass through the allowlisted applier.

### Stable public API

* Versioned `/v1` HTTP surface (already prototyped in `hub/api.py`).
* Optional Unix socket transport for local apps.
* Streaming task events (SSE) instead of polling.
* OpenAPI description + tiny Python/JS client libraries.

## Engine

* Per-plugin capability enforcement (`capabilities` becomes a real permission
  check, incl. optional network/fs denial).
* Optional macOS `sandbox-exec` profile for plugin subprocesses.
* Task queue with priorities and cancellation (`task.cancel`).
* Persistent task history + audit log (input hashes, models, durations).
* Result cache keyed by (plugin, config, input hash) to avoid re-classifying
  unchanged comments.
* Streaming inference (`stream: true`) with incremental progress.
* Model warm-up policy and per-plugin `num_ctx`/`keep_alive` overrides.
* Additional backends behind the same interface (llama.cpp server, MLX, LM
  Studio) — plugins would not change at all.

## Extension

* MV3 variant (event page + port keep-alive) once Firefox's MV3 native
  messaging story is comfortable.
* Result review UI: accept/reject individual labels before applying.
* "Dry run" mode showing what would change.
* Per-feature keyboard shortcuts and a context-menu "send selection to plugin".
* Selector picker: click an element to generate `item_selector`/`fields`.
* Export/import of domain configs as shareable files (import exists already).

## Plugins to add

* `page.summarize` — thread/article summary with structured highlights.
* `text.translate` — local translation of selected blocks.
* `links.check` — classify outbound links (metadata only, no fetching by default).
* `forms.fill` — suggest form values from page context (never auto-submit).
* `images.dedupe` — perceptual hashing to find reposts.
* `moderation.queue` — batch triage with reviewer decisions persisted locally.

## Packaging

* `make package` already builds the project zip and an unsigned `.xpi`.
* Homebrew formula / `pipx`-style installer for the CLI.
* Optional launchd agent for the local API (opt-in).
* Signed, notarized helper bundle if a GUI companion is ever added.
