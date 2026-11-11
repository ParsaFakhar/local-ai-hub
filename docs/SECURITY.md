# Security model

## Trust boundaries

```
UNTRUSTED            │ SEMI-TRUSTED           │ TRUSTED (local, user-installed)
webpage DOM/text     │ LLM output             │ extension code
image pixels         │ (structured data only) │ native host + engine
alt text, filenames  │                        │ plugins you install
URLs on the page     │                        │ config files you write
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
* `security.allow_page_supplied_config` is `false` and there is no page-facing
  message channel at all: no `window.postMessage` API, no `externally_connectable`,
  no injected script, no `web_accessible_resources`.
* The content script's shadow-DOM panel is not reachable from page script.

### 5. Network containment

* Inference is local (`127.0.0.1:11434`). No cloud calls, no telemetry.
* Plugins receive **base64 pixels**, not URLs: image fetching happens in the
  extension background page, which the user controls. Plugins perform no
  network I/O in the bundled implementations.
* Image fetches use `credentials: 'omit'` and only `http(s)`, `data:`, `blob:`
  schemes; results are downscaled (default 768 px) before leaving the browser.
* The optional HTTP API binds to `127.0.0.1`, requires a 32-byte bearer token
  (`0600` file) and emits **no CORS headers**, so a webpage cannot use it.

### 6. Resource limits

| Limit | Default | Purpose |
| ----- | ------- | ------- |
| `security.max_input_bytes` | 4 MiB | rejects oversized task input |
| `plugins.max_output_bytes` | 8 MiB | kills a plugin flooding stdout |
| `plugins.max_llm_calls` | 64 / task | stops runaway model loops |
| `plugins.timeout_seconds` | 180 s | terminate → kill a hung plugin |
| native outgoing frame | 1 MiB | Firefox hard limit, clean error instead of a broken pipe |
| `max_items` / `maxItems` | 200 | bounds DOM work per task |
| `ollama.idle_unload_seconds` | 300 s | frees unified memory |

### 7. Native messaging surface

* The manifest lists exactly one allowed extension id.
* `stdout` carries protocol frames only; logs go to stderr/file, so log injection
  cannot forge a protocol message.
* Malformed frames produce `bad_request`, never a crash; EOF triggers cleanup.

## What is *not* protected (be honest about it)

* **Plugins are trusted.** A malicious plugin you install can do anything your
  user account can. Install plugins you have read. There is no sandbox yet —
  capability declarations are documentation, not enforcement (roadmap).
* **Local processes**: anything running as your user can talk to Ollama, and to
  the HTTP API if it can read the token file.
* **Page content sent to the model**: extracted text/images stay on your machine
  but are processed by a local model whose output you should still sanity-check
  before acting on it.
* **`<all_urls>` permission**: needed for a general-purpose platform. It grants
  the extension read access to pages; the hub only extracts what your domain
  config asks for, and only runs when you trigger it (or enable `auto_run`).

## Reporting / hardening ideas

Roadmap items: per-plugin capability enforcement, optional subprocess sandbox
(`sandbox-exec` on macOS), signed plugin manifests, an audit log of every task
with input hashes, and a "dry run" mode that shows what *would* be applied.
