# Tampermonkey / userscript bridge

**Status: implemented** (`extension/content/bridge.js`, `hub/bridge.py`,
`examples/tampermonkey/local-ai-hub.user.js`). Covered by `tests/test_bridge.py`
and the bridge checks in `tests/test_extension.js`.

A userscript cannot open a native-messaging port. Instead it talks to the Local
AI Hub **content script** through `CustomEvent`s on `document`, and the content
script relays the task to the engine over the normal native port. The page never
gets a privileged channel: it may name an allowlisted plugin and send **input
data**, nothing else.

---

## 1. Requirements

1. The Local AI Hub extension is loaded and the native host is registered
   (`./install.sh`, see [INSTALL_MACOS.md](INSTALL_MACOS.md)).
2. The site is **explicitly opted in** by you, in `config/domains/<host>.json`
   or in **Settings → Domains**. Without the opt-in the bridge is completely
   silent: no reply, no error, no way to detect the extension.

   An opt-in saved in Settings → Domains is pushed to the host on connect and
   after every save, so the engine-side re-check (§5) honors it exactly like a
   file opt-in. Overrides live in the host's memory only and are re-sent on each
   connect; `domains.allow_extension_overrides: false` disables the sync.
3. A userscript manager (Tampermonkey, Violentmonkey) running your script on
   that site.

The content script runs on `<all_urls>`, but "runs" ≠ "listens": every event is
dropped before any reply unless the resolved policy says `enabled`.

## 2. Opting a domain in

```json
{
  "domain": "example-board.com",
  "bridge": {
    "tampermonkey": true,
    "plugins": ["comments.classify", "hello.python"],
    "allow_apply": false,
    "max_requests_per_minute": 30,
    "max_input_bytes": 262144
  },
  "features": { "comments.classify": { "enabled": true, "...": "..." } }
}
```

| Key | Default | Meaning |
| --- | --- | --- |
| `bridge.tampermonkey` | `false` | **Required opt-in.** Nothing works without it. |
| `bridge.plugins` | *(empty)* | Plugin ids exposed to the page. If omitted/empty, the allowlist is exactly the site's **enabled features**. |
| `bridge.allow_apply` | `false` | May the page ask to apply results to the DOM? |
| `bridge.max_requests_per_minute` | `30` | Sliding-window rate limit, per page. |
| `bridge.max_input_bytes` | `262144` (256 KiB) | Cap on the JSON-encoded `input`. |

Hub-level switches in `config/hub.json` (`bridge.*`) apply on top and can only
tighten the policy:

| Key | Default | Effect |
| --- | --- | --- |
| `bridge.enabled` | `true` | Master switch; `false` disables the bridge everywhere. |
| `bridge.require_domain_opt_in` | `true` | Engine-side re-check of the domain policy. |
| `bridge.allow_apply` | `true` | Global veto over every domain's `allow_apply`. |
| `bridge.max_requests_per_minute` / `bridge.max_input_bytes` | `30` / `262144` | Fallbacks when the domain does not set them. |

`hub/bridge.py::policy()` resolves all of this from **local configuration only**
(files + the user's own extension overrides). Nothing is ever read from the page.

## 3. Wire protocol

Four events on `document`. **`event.detail` is a JSON string in both
directions** — a string cannot carry functions, DOM nodes or getters, and it
crosses the Firefox content/page boundary without `cloneInto`.

| Event | Direction | Purpose |
| --- | --- | --- |
| `local-ai-hub:hello` | page → hub | "are you there, and what may I call?" (`detail: '{}'`) |
| `local-ai-hub:ready` | hub → page | answer to hello; **only sent if the domain opted in** |
| `local-ai-hub:request` | page → hub | run a plugin |
| `local-ai-hub:response` | hub → page | result or error, correlated by `id` |

Protocol `version` is `1`.

### `ready` payload

```json
{
  "version": 1,
  "domain": "example-board.com",
  "plugins": ["comments.classify", "hello.python"],
  "allow_apply": false,
  "max_requests_per_minute": 30,
  "max_input_bytes": 262144
}
```

There is no unsolicited announcement: dispatch `hello` and wait for `ready`.
No answer within your timeout means "not opted in / extension not running".

### `request` payload

```json
{
  "version": 1,
  "id": "r1",
  "plugin": "comments.classify",
  "input": { "comments": [{ "id": "c1", "text": "buy cheap pills" }] },
  "apply": false
}
```

* Only `version`, `id`, `plugin`, `input`, `apply` are read. **Any other key is
  ignored**, logged in the panel, and listed in `meta.ignored_keys` of the
  response — sending `options`, `selectors`, `apply_spec`, `config`, `model` or
  `code` has no effect.
* `id` must match `[A-Za-z0-9_.:-]{1,128}`; it is generated for you if omitted.
* `input` must be a JSON **object** (not an array), serialisable, and within
  `max_input_bytes`.
* `apply` must be the boolean `true` to request applying, and is refused unless
  the domain **and** the hub allow it.

### `response` payload

```json
{
  "version": 1,
  "id": "r1",
  "ok": true,
  "output": { "results": [], "summary": {} },
  "meta": { "ignored_keys": [], "duration_ms": 812 },
  "applied": { "applied": 3, "matched": 3, "skipped": 0 }
}
```

`applied` is present only when you asked to apply and the feature has an `apply`
spec. On failure `output` is absent and `error` is `{code, message}`:

| Code | Cause |
| --- | --- |
| `bad_request` | detail is not a JSON object/string, bad `id`, unsupported `version` |
| `forbidden` | domain not opted in, plugin not allowlisted, or `apply` not permitted |
| `invalid_input` | `input` is not a serialisable JSON object |
| `input_too_large` | encoded input exceeds `max_input_bytes` |
| `rate_limited` | more than N requests/minute, or more than **2** tasks in flight |
| `plugin_failed` / `internal_error` | the task failed; the engine's own error (`plugin_timeout`, `model_unavailable`, `capability_denied`, …) is passed through unchanged |

A detail string larger than 4 MiB is rejected before parsing.

## 4. Applying results

`apply: true` never uses a page-supplied action list. The content script applies
the **user's own `features.<plugin>.apply` spec** through the allowlisted applier
(badge / class / attribute / hide / show, opt-in text), matching elements by the
`data-lah-id` attribute the extractor stamped earlier. No `innerHTML`, ever.
See [DOMAIN_CONFIG.md](DOMAIN_CONFIG.md) and [SECURITY.md](SECURITY.md).

## 5. Two enforcement points, one policy

1. **Content script** (`bridge.js`) mirrors the policy so a rejected request
   never reaches the native host.
2. **Engine** (`hub/engine.py`) re-checks every task whose
   `context.interface` is page-facing — `tampermonkey`, `userscript`, `page`,
   `web` (`security.page_interfaces`). It resolves the domain policy again with
   `hub/bridge.py` and additionally **drops any page-supplied `options` or
   `domain_config`**. A bypassed or buggy content script still cannot open the
   door.

The content script submits with `context.interface = "tampermonkey"`, the page
URL/title/domain, and empty options.

## 6. Minimal client

```js
function run(plugin, input, apply = false) {
  return new Promise((resolve, reject) => {
    const id = 'us-' + Date.now();
    const onResponse = (event) => {
      const msg = JSON.parse(event.detail);
      if (msg.id !== id) return;
      document.removeEventListener('local-ai-hub:response', onResponse);
      msg.ok ? resolve(msg) : reject(new Error(msg.error.code + ': ' + msg.error.message));
    };
    document.addEventListener('local-ai-hub:response', onResponse);
    document.dispatchEvent(new CustomEvent('local-ai-hub:request', {
      detail: JSON.stringify({ version: 1, id, plugin, input, apply })
    }));
  });
}
```

The full example — `@grant none`, a promise client with timeouts, a `hello()`
helper and a demo button — is
[`examples/tampermonkey/local-ai-hub.user.js`](../examples/tampermonkey/local-ai-hub.user.js).
Install it, edit `@match` to your site, and it also exposes `window.LocalAIHub`
so you can experiment from the devtools console:

```js
await LocalAIHub.hello();                       // null if the site is not opted in
await LocalAIHub.run('hello.python', { name: 'bridge' });
```

## 7. Troubleshooting

| Symptom | Check |
| --- | --- |
| `hello()` resolves `null`, nothing logged | `bridge.tampermonkey: true` for that exact host; `bridge.enabled` in `config/hub.json`; extension connected |
| `forbidden: plugin … is not exposed` | add the id to `bridge.plugins`, or enable that feature on the site |
| `forbidden: … does not allow the page to apply results` | set `bridge.allow_apply` in the domain config **and** keep hub `bridge.allow_apply` true |
| `rate_limited` | you exceeded 30/min (or your own limit) or have >2 tasks running |
| `input_too_large` | send fewer items per call, or raise `bridge.max_input_bytes` |
| Task runs but nothing changes on the page | `apply: true` plus an `apply` spec in the feature config; elements need `data-lah-id` from a previous extraction |

The extension panel logs every rejected request and every ignored page-supplied
key, so you can see exactly what the bridge did.

## 8. Alternative: the local HTTP API

If you do not want the extension in the loop, the engine also exposes an opt-in,
token-authenticated `127.0.0.1` API (`./install.sh --api`, `hub/api.py`,
`examples/use_http_api.py`). It is **off by default**, sends no CORS headers and
is not reachable from a webpage — use it from local apps, not from page scripts.
