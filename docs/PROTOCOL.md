# Universal task protocol v1.0

One protocol, every interface: Firefox native messaging, CLI, Python, HTTP.

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
* `options` — plugin config overrides (trusted UI/caller only).
* `context.domain_config` — extension-side override, merged over file config.
  Only accepted from the extension, never from a webpage.

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
    "output_schema_errors": []
  }
}
```

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
| `bad_request` | Malformed envelope |
| `unknown_type` | Unsupported message type |
| `unknown_plugin` | Plugin id not registered |
| `plugin_disabled` | Feature disabled for this domain |
| `invalid_input` | Input violates the manifest schema |
| `input_too_large` | Over `security.max_input_bytes` |
| `plugin_failed` | Plugin crashed / returned failure |
| `plugin_timeout` | Exceeded the plugin/task timeout |
| `runtime_missing` | python/node interpreter unavailable |
| `model_error` | Inference failed |
| `model_unavailable` | Ollama missing, stopped, or model not installed |
| `message_too_large` | Result exceeds the 1 MiB native messaging limit |
| `internal_error` | Unexpected engine error (logged with traceback) |

### Event (unsolicited)

```json
{ "protocol": "1.0", "type": "event", "event": "progress",
  "id": "task-7f3a91", "data": { "value": 0.5, "message": "batch 2/4", "plugin": "comments.classify" } }
```

Events: `host.ready`, `task.started`, `task.finished`, `task.failed`,
`progress`, `plugin.log`, `model.server_starting`, `model.server_ready`,
`model.generating`, `model.unloaded`.

## Control messages

| Type | Output |
| ---- | ------ |
| `ping` | `{pong, app, version, protocol, time_ms, echo}` |
| `status` | uptime, plugin/domain counts, task counters, full model status, config sources |
| `plugins.list` | `{plugins: [manifest summaries], errors: []}` |
| `domains.list` | `{domains: [{domain, features, label}]}` |
| `domains.get` | effective config for `domain` (+ `overrides`), plus `available_plugins` |
| `models.list` | backend, running, loaded/installed models, default/vision model |
| `models.unload` | `{unloaded: [names]}` — frees memory immediately |
| `config.get` | effective hub config + sources |
| `shutdown` | cleanup (unload models, optionally stop Ollama) then exit |

## Transport: native messaging

```
[4 bytes: uint32 little-endian length][UTF-8 JSON payload]
```

* host → Firefox: **max 1 MiB per message** (enforced by the engine)
* Firefox → host: large inputs allowed, but `security.max_input_bytes` (4 MiB
  default) applies to `input`

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
                            "format":{…schema…},"model":null,"options":{…}}}
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

## Transport: HTTP (experimental, opt-in)

```bash
./install.sh --api
./bin/laihub-api
TOKEN=$(cat ~/Library/Application\ Support/LocalAIHub/api-token)

curl -s http://127.0.0.1:8765/v1/status -H "Authorization: Bearer $TOKEN"
curl -s http://127.0.0.1:8765/v1/task -H "Authorization: Bearer $TOKEN" \
     -d '{"plugin":"hello.python","input":{"name":"curl"}}'
```

Routes: `GET /v1/status|plugins|domains|models`, `POST /v1/task`.
Localhost only, bearer token, no CORS.
