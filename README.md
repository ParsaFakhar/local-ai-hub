# Local AI Hub

**A local, extensible AI automation platform for macOS.** Firefox Nightly is its
first interface — not its purpose.

Local AI Hub runs AI models, local scripts and custom automation tasks on your
own machine, without cloud services. The browser detects pages, extracts data,
submits *tasks* and applies the results. Everything else — routing, plugins,
model lifecycle — happens in a local engine that any other program can use too.

> It is **not** a comment moderation extension. The comment classifier and the
> image tagger are simply the first two of ten bundled applications.

---

## What's in the box

```
local-ai-hub/
├── extension/          Firefox WebExtension (MV2): interface + userscript bridge
├── native/             Native Messaging host (stdio ⇄ Firefox)
├── hub/                Core engine: router, plugins, models, domains, tasks,
│                       cache, capabilities, bridge policy, HTTP API
├── plugins/            Ten independent capability programs (Python / JavaScript)
├── sdk/                Plugin SDKs: sdk/python, sdk/js
├── config/             hub.json + per-domain configurations
├── scripts/            installer helpers, doctor, smoke test, packaging, MV3 builder
├── templates/          Skeleton for writing your own plugin
├── examples/           Sample inputs, Tampermonkey userscript, API/engine clients
├── tests/              261 Python unit tests + 28 extension logic checks
└── docs/               Architecture, protocol, plugin API, security, MV3, install
```

## Architecture

```
┌────────────────────────── Firefox Nightly ──────────────────────────┐
│  content script            background page              options UI  │
│  extract ▸ apply  ◂─────▸  native port + router  ◂─────▸  config    │
│        ▲                                                            │
│        │ opt-in CustomEvent bridge (input data only)                │
│   userscript (Tampermonkey / Violentmonkey)                         │
└──────────────────────────────────┬──────────────────────────────────┘
                    native messaging (4-byte length + JSON)
┌──────────────────────────────────┴──────────────────────────────────┐
│  native/local_ai_hub_host.py                                        │
│  ┌──────────────────────── hub.Engine ─────────────────────────┐    │
│  │  universal task router                                      │    │
│  │   ├── PluginManager   manifests, discovery, validation      │    │
│  │   ├── DomainRegistry  per-site selectors + options          │    │
│  │   ├── run_plugin()    python / node subprocess, stdio RPC   │    │
│  │   ├── ModelManager    Ollama start ▸ load ▸ idle-unload     │    │
│  │   └── Tasks / Cache / Capabilities / bridge policy          │    │
│  └─────────────────────────────────────────────────────────────┘    │
│         ↕ stdin/stdout JSON RPC              ↕ HTTP 127.0.0.1:11434  │
│   plugins/*.py, *.js                          Ollama (local models)  │
└─────────────────────────────────────────────────────────────────────┘
        ↑ same protocol, other interfaces: CLI, HTTP API, your own code
```

Firefox never needs to know how a plugin works or which model it uses.

## Quick start (macOS, Apple Silicon)

```bash
unzip local-ai-hub.zip && cd local-ai-hub

./install.sh                     # registers the native host, checks deps, smoke-tests
ollama pull qwen3.5:4b           # or any model you prefer (see note below)
```

Then in **Firefox Nightly**:

1. Open `about:debugging#/runtime/this-firefox`
2. **Load Temporary Add-on…** → select `extension/manifest.json`
3. Click the toolbar button — it should say **connected**
4. Open **Settings → Domains** to configure a site (or edit `config/domains/*.json`)
5. On a configured page press **Alt+Shift+L** to run, **Alt+Shift+H** for the panel

**No model yet?** Everything except real inference works offline:

```bash
./install.sh --mock              # mock model backend
./bin/laihub --mock task comments.classify --domain example-board.com \
    --input @examples/comments.json
```

> **Model name:** the starter config asks for `qwen3.5:4b`. If that tag is not
> available on your Ollama install, set one you actually have —
> `./install.sh --model qwen3:4b`, or edit `ollama.default_model` in
> `config/hub.json`. The engine also tries `model_fallbacks` automatically and
> tells you the exact `ollama pull` command when nothing matches.

### Manifest V3

`python3 scripts/build_mv3.py` (or `make mv3`) builds and validates MV3 variants
from the same sources: `dist/mv3-firefox` (event page, Firefox 115+) and
`dist/mv3-chrome` (service worker, Chrome/Edge/Brave 116+). Chrome needs one extra
native-host manifest — see [docs/MV3.md](docs/MV3.md).

## Command line (no Firefox involved)

```bash
./bin/laihub status
./bin/laihub plugins
./bin/laihub domain example-board.com
./bin/laihub task hello.python --input '{"name":"M1 Pro"}'
./bin/laihub task images.tag --input @examples/images.json
./bin/laihub --mock --events task comments.classify --input @examples/comments.json
./bin/laihub tasks --running          # what is in flight
./bin/laihub tasks --id task-7f3a91   # one task, running or recent
./bin/laihub cancel '*'               # cancel everything running
./bin/laihub cache                    # result-cache statistics
./bin/laihub serve                    # opt-in local HTTP API
./bin/laihub doctor                   # diagnose the installation
./bin/laihub --version
```

`--remote` sends any request to an already-running local API instead of starting
an engine in-process.

## The ten bundled plugins

| Plugin              | Runtime | Model | Purpose                                              |
| ------------------- | ------- | ----- | ---------------------------------------------------- |
| `comments.classify` | Python  | text  | Contextual comment classification, batched, JSON      |
| `images.tag`        | Python  | vision| Vision descriptions + normalized tags                 |
| `page.summarize`    | Python  | text  | Article/thread abstract, bullets, per-item highlights |
| `text.translate`    | Python  | text  | Local translation preserving ids and placeholders     |
| `links.check`       | Python  | —     | Offline link audit: normalization, dedupe, host rules |
| `forms.fill`        | Python  | text  | Suggest form values from a profile (never submits)    |
| `images.dedupe`     | Python  | —     | Perceptual hashing (pHash/dHash/aHash) to find reposts|
| `moderation.queue`  | Python  | text  | Batch triage into a prioritized reviewer queue        |
| `hello.python`      | Python  | text  | Reference plugin: runtime, config, logging, LLM       |
| `hello.javascript`  | Node.js | text  | Same contract in JavaScript                           |

`links.check` and `images.dedupe` need no model at all — they run fully offline.
The **Model** column reflects each manifest's `capabilities`, which the engine
enforces on every model call.

Add a capability by dropping a folder into `plugins/` — **no core changes**:

```bash
cp -r templates/plugin-template plugins/my.thing
$EDITOR plugins/my.thing/manifest.json plugins/my.thing/plugin.py
./bin/laihub plugins          # it is discovered immediately
```

## Per-domain configuration

```json
{
  "domain": "example-board.com",
  "features": {
    "comments.classify": {
      "enabled": true,
      "root_selector": "div.comments",
      "item_selector": "p.comment",
      "fields": { "text": { "attr": "text" }, "author": { "selector": ".author" } },
      "input": { "comments": "@items", "page_title": "@page.title" },
      "options": { "labels": ["ok", "spam", "toxic", "off-topic"] },
      "apply": {
        "results_path": "results",
        "match_by": "id",
        "actions": [
          { "action": "badge", "text": "{label} {confidence}" },
          { "action": "class", "map": { "spam": "lah-flag-spam" } },
          { "action": "hide", "when": { "field": "label", "in": ["spam"] } }
        ]
      }
    },
    "images.tag": {
      "enabled": true,
      "item_selector": "img.original",
      "fields": { "data_base64": { "attr": "src", "as": "image", "max_size": 768 } },
      "input": { "images": "@items" }
    }
  }
}
```

Multiple capabilities coexist on one site, each with its own selectors,
instructions and options. Configs live in `config/domains/*.json` (files) and can
be overridden per-site from the extension's Settings UI. See
[docs/DOMAIN_CONFIG.md](docs/DOMAIN_CONFIG.md).

## Userscript bridge (Tampermonkey / Violentmonkey)

A userscript can use the hub too, without ever gaining a privileged channel.
Opt a site in and expose only the plugins you choose:

```json
{ "bridge": { "tampermonkey": true, "plugins": ["comments.classify"] } }
```

The page then exchanges JSON strings over `CustomEvent`s and may send **input
data only** — no options, selectors, actions, config or code. Without the opt-in
the bridge is completely silent. Ready-to-edit client:
`examples/tampermonkey/local-ai-hub.user.js`. Full protocol, limits and error
codes: [docs/TAMPERMONKEY.md](docs/TAMPERMONKEY.md).

## Model runtime lifecycle

* Ollama is started **on demand** (`ollama serve`) only when a task needs it
* models load on first use, with `keep_alive` (default `5m`)
* idle models are unloaded automatically (default 300 s) to free unified memory
* `stop_on_exit` can shut down a server the hub started itself
* structured JSON output is enforced with a schema, not by hoping the model behaves
* per-plugin model requests: a plugin may ask for a different text/vision model
* streaming (`ollama.stream`, or `on_token` in a plugin) emits `model.token` events

## Engine services

* **Task registry** — running tasks, bounded history and an append-only JSONL
  audit log of input *hashes*, models and durations; cancel one task or all.
* **Result cache** — TTL + entry cap, off by default (`cache.enabled`), so
  unchanged inputs are not re-classified.
* **Capability enforcement** — a plugin may only use the `llm.*` capabilities its
  manifest declares; anything else is refused with `capability_denied`.
* **Local HTTP API** — opt-in, token-authenticated, localhost only, with SSE
  events and an optional Unix socket (see [docs/PROTOCOL.md](docs/PROTOCOL.md)).

## Security model

* Plugins are **trusted local programs** you install.
* The platform **never** executes commands or code supplied by webpages or by
  LLM responses. There is no `eval`, no shell interpolation, no dynamic imports
  of remote code.
* Model output can only reach the page through an **allowlisted applier**:
  text badges, CSS classes, `data-*`/`title`/`aria-label` attributes, hide/show.
  Never `innerHTML`. Text replacement is opt-in per feature.
* Page-facing interfaces (the userscript bridge) may submit input, never
  configuration: the engine drops page-supplied `options` and `domain_config`
  and re-checks the domain's bridge policy on every task.
* Manifest capabilities are enforced before each model call.
* Plugins receive pixels, not URLs: the extension fetches and downscales images,
  so plugins perform no network I/O.
* The optional HTTP API binds to `127.0.0.1`, requires a bearer token and sends
  no CORS headers — a webpage cannot drive it.

Details: [docs/SECURITY.md](docs/SECURITY.md).

## Testing

```bash
make test        # 261 Python unit tests + 28 extension logic checks
make smoke       # 15 end-to-end checks over the real native messaging pipe
make mv3         # build + validate the MV3 firefox and chrome variants
make doctor      # diagnose an installation
./scripts/check.sh   # everything above in one go, no make required
```

The smoke test spawns `bin/local-ai-hub-host` exactly as Firefox does and speaks
the framed protocol, using the offline mock backend — no model download needed.

## Development status

| Feature                                | Status                 |
| -------------------------------------- | ---------------------- |
| Firefox extension (MV2)                | Implemented            |
| MV3 builds (Firefox + Chrome)          | Implemented, validated statically |
| Native Messaging                       | Implemented            |
| Automatic native-host startup          | Implemented            |
| Ollama lifecycle management            | Implemented            |
| Universal task protocol                | Implemented            |
| Python / JavaScript plugins            | Implemented            |
| Domain-specific configurations         | Implemented            |
| Ten bundled plugins                    | Implemented            |
| Dry run, result review, selector picker| Implemented            |
| Task history, audit log, cancellation  | Implemented            |
| Result cache                           | Implemented (off by default) |
| Capability enforcement                 | Implemented (`llm.*`)  |
| Streaming inference                    | Implemented            |
| CLI + experimental local HTTP API      | Implemented            |
| Tampermonkey integration               | Implemented (opt-in per domain) |
| Actual imageboard integration          | Pending (needs your selectors) |
| Persistent signed Firefox installation | Pending                |

Validated here: syntax, unit tests, extension logic tests, MV3 build validation
and end-to-end protocol/plugin/model-mock execution. **Not yet validated against
your real imageboard or your Firefox/Chrome/Ollama installation** — run
`make doctor` and `./bin/laihub status` there, then adjust selectors.

## Documentation

* [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — components and data flow
* [docs/PROTOCOL.md](docs/PROTOCOL.md) — universal task protocol + plugin RPC + HTTP
* [docs/PLUGIN_API.md](docs/PLUGIN_API.md) — manifests, SDKs, writing plugins
* [docs/DOMAIN_CONFIG.md](docs/DOMAIN_CONFIG.md) — selectors, mapping, actions, bridge
* [docs/TAMPERMONKEY.md](docs/TAMPERMONKEY.md) — userscript bridge protocol
* [docs/MV3.md](docs/MV3.md) — MV3 builds, loading, native-host registration
* [docs/INSTALL_MACOS.md](docs/INSTALL_MACOS.md) — install, signing, troubleshooting
* [docs/SECURITY.md](docs/SECURITY.md) — trust boundaries
* [docs/ROADMAP.md](docs/ROADMAP.md) — what is still open
* [CHANGELOG.md](CHANGELOG.md) — release notes

## License

MIT — see [LICENSE](LICENSE).
