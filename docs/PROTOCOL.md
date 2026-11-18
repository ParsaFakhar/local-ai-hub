# Universal task protocol v1.0

One protocol, every interface: Firefox native messaging, userscript bridge, CLI,
Python, HTTP.

## Envelopes

### Request

```json
{
  "protocol": "1.0",
  "id": "task-7f3a91",
  "type": "task",
  "plugin": "comments.classify",
  "input": { "comments": [{ "id": "c0", "text": "…" }] },
  "options": { "labels": ["ok", "spam"], "batch_size": 8 },
  "context": {
    "domain": "example-board.com",
    "url": "https://example-board.com/t/1",
    "title": "Thread title",
    "interface": "firefox",
    "tab_id": 12,
    "domain_config": { "features": { "comments.classify": { "enabled": true } } }
  }
}
```

* `id` — caller-generated, echoed back; used to correlate events too.
* `priority` — optional scheduling hint, `0`-`9` (**higher runs first**) or the
  names `low` (0), `normal` (5), `high` (9). Default `tasks.default_priority`
  (5). Unusable values fall back to the default; they never fail a task.
* `options` — plugin config overrides (trusted UI/caller only).
* `context.domain_config` — extension-side override, merged over file config.
  Only accepted from the extension, never from a webpage.
* `context.interface` — who is calling. **Page-facing interfaces**
  (`tampermonkey`, `userscript`, `page`, `web`; configurable via
  `security.page_interfaces`) may send `input` only: the engine drops their
  `options` and `domain_config` and re-checks the domain's bridge policy
  (`bridge.require_domain_opt_in`). See [TAMPERMONKEY.md](TAMPERMONKEY.md).

### Result

```json
{
  "protocol": "1.0",
  "id": "task-7f3a91",
  "type": "result",
  "ok": true,
  "output": { "results": [{ "id": "c0", "label": "spam", "confidence": 0.93 }] },
  "meta": {
    "plugin": "comments.classify",
    "plugin_version": "0.2.0",
    "models": ["qwen3.5:4b"],
    "mock_model": false,
    "duration_ms": 812,
    "domain": "example-board.com",
    "priority": 5,
    "output_schema_errors": []
  }
}
```

When the result cache is enabled and hits, `meta.cache` carries
`{hit: true, age_ms, key}` (the first 16 hex chars of the cache key).
`meta.priority` is the effective priority and is present on cache hits too.

### Error

```json
{
  "protocol": "1.0", "id": "task-7f3a91", "type": "result", "ok": false,
  "error": { "code": "invalid_input", "message": "input does not match the plugin schema",
             "details": ["$.comments: expected type array, got str"] }
}
```

| Code | Meaning |
| ---- | ------- |
| `bad_request` | Malformed envelope or control argument |
| `unknown_type` | Unsupported message type |
| `unknown_plugin` | Plugin id not registered |
| `plugin_disabled` | Feature disabled for this domain |
| `invalid_input` | Input violates the manifest schema |
| `input_too_large` | Over `security.max_input_bytes` (or the bridge's own cap) |
| `plugin_failed` | Plugin crashed / returned failure |
| `plugin_timeout` | Exceeded the plugin/task timeout |
| `runtime_missing` | python/node interpreter unavailable |
| `model_error` | Inference failed |
| `model_unavailable` | Ollama missing, stopped, or model not installed |
| `capability_denied` | The plugin used an `llm.*` capability it never declared |
| `task_cancelled` | The task was cancelled before it finished |
| `unknown_task` | `tasks.get` / cancel target is neither running nor in history |
| `message_too_large` | Frame exceeds the native messaging caps (1 MiB out / 64 MiB in) |
| `forbidden` | Bridge policy denies this domain/plugin, or `apply` is not allowed |
| `rate_limited` | Too many bridge requests per minute, or too many in flight |
| `unauthorized` | HTTP API: missing/invalid bearer token |
| `not_found` | HTTP API: unknown route or object |
| `internal_error` | Unexpected engine error (logged with traceback) |

### Event (unsolicited)

```json
{ "protocol": "1.0", "type": "event", "event": "progress",
  "id": "task-7f3a91", "data": { "value": 0.5, "message": "batch 2/4", "plugin": "comments.classify" } }
```

Events: `host.ready`, `task.started`, `task.finished`, `task.failed`,
`task.cancelling`, `task.cancelled`, `progress`, `plugin.log`, `model.server_starting`,
`model.server_ready`, `model.generating`, `model.token`, `model.unloaded`,
`model.warming`, `model.warmed`, `model.warmup_failed`, `model.warmup_done`.

`model.token` carries coalesced streaming fragments as
`{plugin, text}` and is emitted only while streaming is active (`ollama.stream`,
a per-call `stream` flag, or a plugin using `on_token`). `task.cancelling` carries
`{id}` and precedes the cancelled task's result. `task.cancelled` carries
`{id, queued: true}` and is emitted instead when a task is dropped from the
intake queue before it ever started (no result frame follows it).

## Control messages

| Type | Output |
| ---- | ------ |
| `ping` | `{pong, app, version, protocol, time_ms, echo}` |
| `status` | uptime, plugin/domain counts, task counters + `queued` depth, full model status, config sources |
| `plugins.list` | `{plugins: [manifest summaries], errors: []}` |
| `domains.list` | `{domains: [{domain, features, label}]}` |
| `domains.get` | effective config for `domain` (+ `overrides`), plus `available_plugins` and the resolved `bridge_policy` |
| `models.list` | backend, running, loaded/installed models, default/vision model, `warmup` report |
| `models.unload` | `{unloaded: [names]}` — frees memory immediately |
| `models.warmup` | `{warmup: report}` — starts an asynchronous preload of `models` (list of names, optional; default is the configured warm-up set) and returns immediately |
| `config.get` | effective hub config + sources |
| `tasks.list` | `{running, queued, history, counters}` — `queued` is the intake queue in run order (`limit` trims `history` only) |
| `tasks.get` | `{task: record}` for `task_id`, running or recent; `unknown_task` otherwise |
| `task.cancel` | `{cancelled: [ids]}`; `task_id: "*"` (or omitted) cancels everything and adds `all: true`; an unknown id returns `{cancelled: [], unknown_task: id}`. A task still waiting in the queue is dropped and returns `{cancelled: [id], queued: true}`; `*` covers running **and** queued tasks |
| `cache.clear` | `{cleared: n, cache: stats}` |
| `shutdown` | cleanup (unload models, optionally stop Ollama) then exit |

`ping`, `status`, `shutdown`, `task.cancel`, `tasks.list` and `tasks.get` are
**inline control types**: the native host answers them immediately instead of
queueing them behind a running task, so a long classification never blocks a
cancel or a status poll.

### Task priorities

Waiting tasks are ordered by the engine's intake queue (`hub/tasks.py:TaskQueue`,
owned by `Engine.queue`, consumed by the native host's two workers):

* highest `priority` first; ties keep arrival order (FIFO);
* `tasks.queue_size` (default 64) bounds the queue — the host stops reading
  frames while it is full, exactly as before priorities existed;
* `tasks.list` shows the waiting tasks as
  `{id, plugin, priority, queued_ms, position}`, next-to-run first;
* `task.cancel` on a waiting id removes it, writes a cancelled record to the
  history/audit log (`error: "cancelled before start"`) and emits
  `task.cancelled`; nothing is ever executed for it;
* a running task is never preempted — priority decides *start* order only.

Scope: the queue belongs to the native-messaging host. The CLI and your own
Python program call `Engine.handle` directly, and the HTTP API serves each
request in its own thread, so those callers run immediately; `priority` is still
echoed in `meta` and recorded in the task history.

### Model warm-up

Cold-loading a 4B model costs seconds; warm-up moves that cost out of the first
task. It is **off by default** (`ollama.warmup`).

* Policy keys: `ollama.warmup` (master switch), `ollama.warmup_on_start`
  (default `true` — the engine starts warming as soon as it is constructed),
  `ollama.warmup_vision` (also warm `ollama.vision_model`),
  `ollama.warmup_models` (extra tags), `ollama.warmup_timeout_seconds` (120).
* `models.warmup` triggers it on demand and **always returns immediately**; the
  load runs on a daemon thread. A second call while one is in flight returns the
  running report unchanged.
* The report (also in `status.models.warmup` and `models.list`) is
  `{state, models, loaded, skipped, failed, duration_ms, error, mock}` with
  `state` ∈ `idle`, `running`, `ready`, `partial` (some tags failed), `failed`,
  `cancelled` (shutdown mid-run).
* Events: `model.warming` `{model}`, `model.warmed` `{model, duration_ms, mock}`,
  `model.warmup_failed` `{model, error}`, `model.warmup_done`
  `{state, loaded, skipped, failed, duration_ms}`.
* Mechanism: `POST /api/generate` with an **empty prompt** and the configured
  `keep_alive` — Ollama loads the weights without generating. A tag that is not
  installed fails with the usual `ollama pull …` hint; other tags still warm.
  Models the hub already loaded are reported as `skipped`.
* Warm-up does not pin anything: the idle reaper still unloads after
  `ollama.idle_unload_seconds`. With `ollama.mock` the report is deterministic
  and nothing is actually loaded.

## Transport: native messaging

```
[4 bytes: uint32 little-endian length][UTF-8 JSON payload]
```

* host → browser: **max 1 MiB per message** (browser hard limit; enforced by
  `hub/protocol.py`, which returns `message_too_large` instead of breaking the pipe)
* browser → host: frames up to **64 MiB** are read; `security.max_input_bytes`
  (4 MiB default) still applies to `input`

Practical consequence: batch comments and downscale images (the extension
defaults to 768 px JPEG q0.85, typically 50–150 KB each).

## Transport: plugin stdio RPC

Newline-delimited JSON, one process per task.

```
engine → plugin  {"type":"invoke","protocol":"1.0","task_id":"…",
                  "plugin":{"id","version","dir"},
                  "input":{…},"config":{…},"context":{…},
                  "models":{"default":"…","vision":"…","requested":{…}},
                  "limits":{"max_llm_calls":64,"timeout_seconds":180}}

plugin → engine  {"type":"llm.request","id":"1","method":"generate",
                  "params":{"prompt":"…","system":"…","images":["<base64>"],
                            "format":{…schema…},"model":null,"options":{…},
                            "stream":false}}
engine → plugin  {"type":"llm.token","id":"1","text":"partial","index":0}   (streaming only)
engine → plugin  {"type":"llm.response","id":"1","ok":true,
                  "result":{"text":"…","model":"qwen3.5:4b","mock":false,"duration_ms":742}}

plugin → engine  {"type":"log","level":"info","message":"batch 1"}
plugin → engine  {"type":"progress","value":0.5,"message":"batch 2/4"}
plugin → engine  {"type":"result","ok":true,"output":{…}}
```

`method` is `generate` or `chat`. `format` is `"json"` or a JSON schema — the
engine passes it to Ollama's structured-output mode.

Rules:

* stdout carries **only** these frames (the Python SDK redirects `print` to stderr)
* one `result` frame ends the task; the process is then reaped
* the engine enforces wall-clock timeout, output byte cap and LLM call cap
* **capabilities are checked before every model call**: `images` needs
  `llm.vision`, `format` needs `llm.structured`, and any model access needs
  `llm.generate` / `llm.chat` / `llm.vision`. Denials come back as a failed
  `llm.response` and surface as `capability_denied`.
* streaming: pass `on_token=fn` (Python) / `onToken` (JS) or `stream: true`.
  `llm.token` frames deliver raw fragments to the plugin (capped at 20 000 frames
  per call); the engine separately coalesces them into `model.token` events for
  the interface. The final `llm.response` still carries the complete text.

## Transport: HTTP (experimental, opt-in)

```bash
./install.sh --api          # or: ./bin/laihub serve
TOKEN=$(cat ~/Library/Application\ Support/LocalAIHub/api-token)

curl -s http://127.0.0.1:8765/v1/status -H "Authorization: Bearer $TOKEN"
curl -s http://127.0.0.1:8765/v1/task -H "Authorization: Bearer $TOKEN" \
     -d '{"plugin":"hello.python","input":{"name":"curl"}}'
```

| Method | Route | Purpose |
| ------ | ----- | ------- |
| GET | `/v1/ping` | liveness + version |
| GET | `/v1/status` | engine status |
| GET | `/v1/plugins` | manifest summaries |
| GET | `/v1/domains`, `/v1/domains/<domain>` | configured domains / effective config |
| GET | `/v1/models` | model backend status |
| GET | `/v1/config` | effective hub config |
| GET | `/v1/tasks`, `/v1/tasks/<id>` | running + recent history / one task |
| GET | `/v1/events` | Server-Sent Events stream |
| GET | `/v1/openapi.json` | machine-readable description |
| POST | `/v1/task` | run a task (same envelope as above) |
| POST | `/v1/task/cancel` | cancel one task or all |
| POST | `/v1/models/unload` | free model memory |
| POST | `/v1/models/warmup` | preload models (`{"models": [...]}` optional), async |
| POST | `/v1/cache/clear` | drop cached results |
| POST | `/v1/shutdown` | clean shutdown |

Localhost only, bearer token, **no CORS headers**. `api.allowed_origins` can
restrict origins further; it is empty (no cross-origin access) by default.

### Server-Sent Events

```bash
curl -N "http://127.0.0.1:8765/v1/events?access_token=$TOKEN"
```

The stream opens with `event: ready`, then forwards every engine event as
`event: <name>` + `data: <JSON envelope>`, with a `: keepalive` comment every
`api.sse_keepalive_seconds` (15 s). `EventSource` cannot set headers, so this
route — and only this route — accepts `?access_token=…`, and only while
`api.allow_token_query` is true.

### Unix socket (optional)

Set `api.unix_socket` to a path (e.g. `~/Library/Application Support/LocalAIHub/hub.sock`)
to serve the same surface on an `AF_UNIX` socket. The token check is unchanged.

```bash
curl --unix-socket ~/Library/Application\ Support/LocalAIHub/hub.sock \
     -H "Authorization: Bearer $TOKEN" http://localhost/v1/status
```
