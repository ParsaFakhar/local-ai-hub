# Security model

## Trust boundaries

```
UNTRUSTED            │ SEMI-TRUSTED           │ TRUSTED (local, user-installed)
webpage DOM/text     │ LLM output             │ extension code
image pixels         │ (structured data only) │ native host + engine
alt text, filenames  │                        │ plugins you install
URLs on the page     │                        │ config files you write
userscript requests  │                        │ domain configs you opt in
```

**Core rule: the platform never executes arbitrary commands or code supplied by
webpages or by LLM responses.** Everything crossing a boundary is data, and it
is validated against a schema or an allowlist.

## What is enforced in code

### 1. No code execution from untrusted input

* Plugins are launched with fixed `argv`, `shell=False`, a minimal environment
  and `cwd` inside their own folder. No string interpolation into shells.
* No `eval`, `exec`, `Function()`, dynamic `import()` of remote code anywhere in
  the engine, SDKs or extension.
* Manifest `runtime.entry` must resolve **inside** the plugin directory
  (path-escape attempts are rejected at discovery time).
* Only registered plugin ids can be invoked; a page cannot name a program.

### 2. Model output is data, never instructions

* Plugins are prompted that page/image text is untrusted data.
* Structured output is requested with a JSON schema, then **re-validated**:
  the classifier drops hallucinated ids and coerces out-of-allowlist labels to
  `unknown`; the tagger normalizes and caps tags.
* The engine validates plugin output against the manifest's output schema.
* Nothing in the pipeline maps model text to a command, a URL fetch or DOM markup.

### 3. The applier is an allowlist

Only these actions exist: `badge`, `class`, `attribute`, `hide`, `show`, `text`.

* Badges/labels are written with `textContent`. **No `innerHTML` anywhere.**
* `class` values must match `[A-Za-z0-9_-]+`.
* `attribute` is restricted to `data-*`, `title`, `aria-label` — never `src`,
  `href`, `on*`, `style`, `srcdoc`.
* `text` (destructive text replacement) requires `allow_text_replace: true`
  in the user's own feature config.
* Unknown actions are ignored. `tests/test_extension.js` asserts these rules.

### 4. Configuration authority

* Config comes from local files and the extension's own settings UI.
* `context.domain_config` overrides are accepted **only** from the extension
  background page, which is the only holder of the native port.
* `security.allow_page_supplied_config` is `false`. For every **page-facing
  interface** (`tampermonkey`, `userscript`, `page`, `web` — see
  `security.page_interfaces`) the engine *drops* page-supplied `options` and
  `domain_config` instead of merging them, and logs the attempt.
* There is no `window.postMessage` API, no `externally_connectable`, no injected
  script and no `web_accessible_resources`. The only page-facing channel is the
  opt-in userscript bridge (§8), which carries input data only.
* The content script's shadow-DOM panel is not reachable from page script.

### 5. Network containment

* Inference is local (`127.0.0.1:11434`). No cloud calls, no telemetry.
* Plugins receive **base64 pixels**, not URLs: image fetching happens in the
  extension background page, which the user controls. Plugins perform no
  network I/O in the bundled implementations.
* Image fetches use `credentials: 'omit'` and only `http(s)`, `data:`, `blob:`
  schemes; results are downscaled (default 768 px) before leaving the browser.
* The optional HTTP API binds to `127.0.0.1`, requires a 32-byte bearer token
  (`0600` file) and emits **no CORS headers**, so a webpage cannot use it. The
  optional Unix-socket transport (`api.unix_socket`) is a filesystem path with
  the same token check.

### 6. Resource limits

| Limit | Default | Purpose |
| ----- | ------- | ------- |
| `security.max_input_bytes` | 4 MiB | rejects oversized task input |
| `plugins.max_output_bytes` | 8 MiB | kills a plugin flooding stdout |
| `plugins.max_llm_calls` | 64 / task | stops runaway model loops |
| `plugins.timeout_seconds` | 180 s | terminate → kill a hung plugin |
| native outgoing frame | 1 MiB | browser hard limit, clean error instead of a broken pipe |
| native incoming frame | 64 MiB | bounds what the host will read |
| `bridge.max_input_bytes` | 256 KiB | per-request cap for page-supplied input |
| `bridge.max_requests_per_minute` | 30 | sliding-window rate limit per page |
| bridge in-flight tasks | 2 | hard concurrency cap, not configurable |
| `max_items` / `maxItems` | 200 | bounds DOM work per task |
| `ollama.idle_unload_seconds` | 300 s | frees unified memory |
| `ollama.warmup` | off | opt-in preload; bounded by `warmup_timeout_seconds` (120 s) and still subject to the idle reaper |

### 7. Native messaging surface

* The Firefox host manifest lists exactly one allowed extension id
  (`local-ai-hub@localaihub.dev`). A Chrome host manifest, if you write one for
  the MV3 build, lists `allowed_origins` with your unpacked extension id — see
  [MV3.md](MV3.md).
* `stdout` carries protocol frames only; logs go to stderr/file, so log injection
  cannot forge a protocol message.
* Malformed frames produce `bad_request`, never a crash; EOF triggers cleanup.

### 8. The userscript bridge is opt-in and input-only

`extension/content/bridge.js` + `hub/bridge.py`, documented in
[TAMPERMONKEY.md](TAMPERMONKEY.md).

* **Silent by default.** Unless the user sets `bridge.tampermonkey: true` for
  that exact domain, the content script does not answer even a `hello`, so a
  page cannot fingerprint the extension.
* **Plugin allowlist.** The page may name only ids in `bridge.plugins` (or, if
  unset, the features the user enabled on that site).
* **Input only.** Just `version`, `id`, `plugin`, `input`, `apply` are read;
  every other key is ignored, logged and reported in `meta.ignored_keys`.
  Options, selectors, actions, config, model names and code are never taken
  from the page.
* **Applying uses the user's spec.** `apply: true` requires `bridge.allow_apply`
  in the domain config *and* the hub config, and then applies the user's own
  `features.<plugin>.apply` actions through the allowlisted applier (§3).
* **Two enforcement points.** The content script mirrors the policy so rejected
  requests never reach the host, and the engine re-resolves the same policy for
  every page-facing task (`bridge.require_domain_opt_in`), so a bypassed content
  script still cannot open the door.
* Rate limit, concurrency cap and byte caps as in §6; JSON strings only, so no
  functions, DOM nodes or getters can cross the boundary.

### 9. Plugin capabilities are enforced

A manifest's `capabilities` are checked by `hub/capabilities.py` **before every
model call**, not just documented:

| Capability | Grants |
| --- | --- |
| `llm.generate` | text inference via `generate` (and `chat`) |
| `llm.chat` | chat inference |
| `llm.vision` | passing `images` (implies model access) |
| `llm.structured` | passing `format` (`"json"` or a JSON schema) |

A plugin that declares no model capability cannot reach the model at all; one
that only declared `llm.generate` cannot send images to a vision model or ask
for structured output. Denials return `capability_denied` and are logged.
Unknown capability names are reported at discovery time but do not block a
plugin, so a newer plugin still loads on an older engine.

This is a least-privilege guard rail, **not a sandbox**: it constrains what the
engine will do on a plugin's behalf, not what the plugin's own process can do.

## What is *not* protected (be honest about it)

* **Plugins are trusted.** A malicious plugin you install can do anything your
  user account can. Install plugins you have read. There is no OS sandbox yet,
  and capability enforcement covers model calls only — there is no filesystem or
  network mediation (roadmap).
* **Local processes**: anything running as your user can talk to Ollama, and to
  the HTTP API if it can read the token file.
* **Page content sent to the model**: extracted text/images stay on your machine
  but are processed by a local model whose output you should still sanity-check
  before acting on it.
* **`<all_urls>` permission**: needed for a general-purpose platform. It grants
  the extension read access to pages; the hub only extracts what your domain
  config asks for, and only runs when you trigger it (or enable `auto_run`).
* **Opted-in bridge sites**: on a domain you opted in, page script can spend
  local model time within the rate limit. Only opt in sites you trust.

## Reporting / hardening ideas

Still open (see [ROADMAP.md](ROADMAP.md)): an optional subprocess sandbox
(`sandbox-exec` on macOS), signed plugin manifests, and a filesystem/network
capability vocabulary with real mediation. Already shipped: per-plugin
capability enforcement (§9), the task audit log with input hashes, and dry-run
mode.
