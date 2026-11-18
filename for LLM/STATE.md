# STATE — local-ai-hub

**Base commit:** `d4aa30e` ("replaced") · **Session date:** 2026-11-19
Informational only. Never commit or push; the workspace folder is the deliverable.

## Test ledger

Run `sh "for LLM/fingerprint.sh" <paths>` from the repo root. Match = still valid,
cite it. Mismatch = rerun that row only, then update Result + Fingerprint.

| Check | Command | Result | Paths covered | Fingerprint |
| --- | --- | --- | --- | --- |
| Python unit tests | `python3 -m unittest discover -s tests` | PASS — 376 tests, OK (16.9s) | `hub native plugins sdk config scripts tests bin` | `10a38ae13eb7` |
| Extension logic | `node tests/test_extension.js` | PASS — 28/28 | `extension tests/test_extension.js` | `286c63e0186e` |
| End-to-end smoke | `python3 scripts/smoke_test.py --quiet` | PASS — 18/18 | `hub native plugins sdk config scripts/smoke_test.py` | `9347527bcf6a` |
| MV3 build + validation | `python3 scripts/build_mv3.py` | PASS — firefox + chrome both validated | `extension scripts/build_mv3.py` | `cd6e0afcd509` |

2026-11-19 (session 3): Python rows rerun after P11+P12; extension rows unchanged; all re-fingerprinted at session end. Docs edits never invalidate rows.

## Current task

None in flight — session 3 shipped **P11** (OpenAI-compatible backend behind `ollama.backend`: llama.cpp / LM Studio), **P12** (API rate limiting) and P13 hygiene/packaging. Artifacts OUTSIDE the repo at the workspace root: `handoff_out/local-ai-hub-0.2.0.zip` (136 files) + `handoff_out/local-ai-hub-extension-0.2.0.xpi` (19 files). Tracked `.DS_Store` deleted again; `dist/` + caches removed; `git status` = intended changes only.

**Exact next step for the next session:** Phase 0 re-fingerprint (all 4 rows should match), then pick from `docs/ROADMAP.md`: per-site extension profiles, native MLX backend, or API stability + client libraries.

## Next 5 tasks (suggested)

1. Per-site extension profiles (several selector sets for one domain).
2. Native MLX backend behind the same `ModelManager` interface.
3. API stability guarantees + published client libraries.
4. Real-browser validation of MV3 (`docs/MV3.md` §9) — [!] blocked in sandbox.
5. Real-model + real-board validation (incl. openai backend vs real servers) — [!] blocked.

## Verified facts worth reusing (don't re-grep)

- P11: `ollama.backend` = ollama (default) | openai; aliases llama.cpp /
  llamacpp / llama-cpp / lmstudio / lm-studio / openai-compatible /
  openai_compatible, case-insensitive; unknown → ModelError `config_error`.
  `OpenAICompatClient` (`hub/models/openai_backend.py`): /v1/models (id→name),
  /v1/chat/completions JSON+SSE; fmt→response_format json_object/json_schema;
  Ollama base64 images→data-URI parts (mime sniffed); options mapped
  (num_predict→max_tokens; num_ctx/top_k dropped); keep_alive ignored;
  load = 1-token "ping" completion; unload no-op; running() = []; optional
  Bearer `ollama.api_key`. External servers: no auto-start (ensure_server
  raises model_unavailable with a start-it-yourself hint), no binary search.
  `status.models.backend` = mock|ollama|openai. Errors reuse `OllamaError`.
- P12: `api.max_requests_per_minute` = 120 (0 disables); one global sliding
  60 s window (`api.RateLimiter`, injectable clock) checked in `_guard` AFTER
  auth (401s never consume quota); 429 + Retry-After + `rate_limited` envelope;
  every route counts, SSE once at open; OpenAPI lists 429. test_api.py shared
  harness runs quota 0; rate tests use one fresh server per test.
- External (web-verified 2026-11-19): llama.cpp server + LM Studio support
  response_format json_schema; LM Studio port 1234; llama-server port 9931 in
  recent releases, 8080 in older builds.
- Priorities: int 0-9 higher first, default 5, names low/normal/high;
  `TaskQueue` owned by `Engine.queue`, 2 host workers; `tasks.list` →
  {running, queued, history, counters}; queued cancel → {cancelled, queued:true}.
- Warm-up: off by default; `ollama.warmup*` keys; `ModelManager.warmup` async,
  states idle/running/ready/partial/failed/cancelled; `models.warmup` control
  (not inline); `POST /v1/models/warmup` forwards `models`.
- Bridge: events `local-ai-hub:{hello,ready,request,response}`, detail = JSON
  string v1; 30/min, 2 in flight, 256 KiB input; ready payload = {version,
  domain, plugins, allow_apply, max_requests_per_minute, max_input_bytes}.
- Capabilities enforced for llm.generate|chat|vision|structured; cache sha256
  key + TTL + LRU; streaming `llm.token` {type,id,text,index} → `model.token`.
- API: /v1 routes, SSE `event: ready` + 15 s keepalive, `?access_token=` only
  on /v1/events when api.allow_token_query, AF_UNIX opt-in.
- `scripts/package.py` needs no flags; excludes .git/dist/caches/.DS_Store/
  *.log/zip/xpi; includes `for LLM/` (intended). CLI state is per-process.

## Known bugs / blockers

- None known in the code.
- Sandbox: no make/zip/pip, no browser, no Ollama/GPU → MV3 real-browser and
  real-model behaviour (incl. openai backend vs real servers) untestable here.

## Do NOT do this

- Do not rerun ledger checks while their fingerprints match.
- Do not resurrect `for LLM/history|audit|uploads|scratch|image-search`.
- Do not re-fix engine double-wrap, config keys, versions, MV3 validator, API
  POST body forwarding, `on_event(name, data)` arity, the openai backend
  wiring, or the API rate limiter — all tested and documented.
- `.DS_Store` is tracked upstream; every fresh clone restores it. Keep it deleted.
- Do not rewrite docs finished 2026-11-18/19 (all docs/, README, CHANGELOG
  0.2.0); extend only with newly verified facts.
- No new deps, no commits/pushes, no zip or `dist/` left inside the repo.

## Unverified claims (check before repeating them in docs)

- `status` output description inherited from old PROTOCOL.md; only `tasks` and
  `models.warmup` re-verified. Extension UX items come from audit + green tests.
- Openai backend verified against a fake OpenAI server only; real llama.cpp /
  LM Studio behaviour (schema strictness, vision parts, JIT warm-up) untested.
