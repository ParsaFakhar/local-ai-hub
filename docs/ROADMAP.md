# Roadmap

Everything below is **still open**. Features that shipped in 0.2.0 (userscript
bridge, MV3 builds, dry run, result review, selector picker, context menus, the
six additional plugins, result cache, task history + audit log, cancellation,
streaming, capability enforcement, task priorities, model warm-up, SSE and the
Unix-socket API transport) are
documented in [CHANGELOG.md](../CHANGELOG.md) and the relevant `docs/` file, not
here.

## Next up (validate the prototype)

1. **Real imageboard integration** — replace `example-board.com` placeholders
   with your board's actual selectors, run `comments.classify` and `images.tag`
   on a live thread, tune `batch_size`, labels and instructions.
2. **Real model validation** — pick an installed tag (`qwen3:4b`, `qwen2.5:4b`,
   a vision model for tagging), measure latency on M1 Pro, tune `num_ctx`,
   `keep_alive` and `idle_unload_seconds`, and decide whether `ollama.warmup`
   pays off on your machine.
3. **Persistent signed installation** — unlisted AMO self-signing so the
   extension survives restarts (see [INSTALL_MACOS.md](INSTALL_MACOS.md)).
4. **MV3 in a real browser** — both MV3 variants build and pass static
   validation, but have not been exercised in a running Firefox/Chrome with a
   real model. See [MV3.md](MV3.md).

## Engine

* **OS-level sandbox** for plugin subprocesses (optional `sandbox-exec` profile
  on macOS). Plugins are still trusted local programs today.
* **Filesystem / network capability vocabulary.** Only the `llm.*` capabilities
  are enforced so far; `fs.*` / `net.*` declarations would need real mediation
  in the runtime, not just a manifest check.
* **Additional backends** behind the same interface (llama.cpp server, MLX,
  LM Studio) — plugins would not change at all.
* **Signed plugin manifests** so a plugin's provenance can be checked at
  discovery time.

## Interfaces

* **API stability guarantees** for `/v1` (today it is experimental and opt-in),
  plus published, versioned Python/JS client libraries — the current clients are
  examples (`examples/use_http_api.py`, `examples/use_engine.py`).
* **API quotas / rate limiting** for local clients, mirroring what the userscript
  bridge already enforces.
* **Chrome Web Store / AMO packaging** for the MV3 builds, including a stable
  extension id so the native-host `allowed_origins` entry does not change.

## Extension

* Per-site **profiles** (several selector/label sets for the same domain).
* **Config sharing** — a curated library of domain configs users can import
  (export/import of a single config already exists).
* Richer review UI for the newer plugins (translation diffs, dedupe clusters,
  moderation queue decisions).

## Plugins

No plugin backlog is committed: adding a capability requires no core changes
(`cp -r templates/plugin-template plugins/my.thing`). Ideas worth prototyping:
table extraction, on-page rewriting suggestions, and a local RSS/digest builder.

## Packaging

* `make package` builds the project zip and an unsigned `.xpi`;
  `scripts/build_mv3.py --zip` adds MV3 zips. Still open:
* Homebrew formula / `pipx`-style installer for the CLI.
* Optional launchd agent for the local API (opt-in).
* Signed, notarized helper bundle if a GUI companion is ever added.
