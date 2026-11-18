# STATE — local-ai-hub

**Base commit:** `b1178b6` ("replaced") · **Session date:** 2026-11-19
Informational only. Never commit or push; the workspace folder is the deliverable.

## Test ledger

Run `sh "for LLM/fingerprint.sh" <paths>` from the repo root. Match = still valid,
cite it. Mismatch = rerun that row only, then update Result + Fingerprint.

| Check | Command | Result | Paths covered | Fingerprint |
| --- | --- | --- | --- | --- |
| Python unit tests | `python3 -m unittest discover -s tests` | PASS — 330 tests, OK (13.4s) | `hub native plugins sdk config scripts tests bin` | `288dcb778fe9` |
| Extension logic | `node tests/test_extension.js` | PASS — 28/28 | `extension tests/test_extension.js` | `286c63e0186e` |
| End-to-end smoke | `python3 scripts/smoke_test.py --quiet` | PASS — 18/18 | `hub native plugins sdk config scripts/smoke_test.py` | `b1eceb0f5c20` |
| MV3 build + validation | `python3 scripts/build_mv3.py` | PASS — firefox + chrome both validated | `extension scripts/build_mv3.py` | `cd6e0afcd509` |

2026-11-19: both Python rows rerun after the priority + warm-up features; both
extension rows still match (nothing under `extension/` changed). Docs edits: no-op.

## Current task

None in flight — 2026-11-19 shipped **task priorities** (P9) and **model warm-up**
(P10): code, tests, docs, packaging done. Artifacts outside the repo at the
workspace root: `handoff_out/local-ai-hub-0.2.0.zip` (134 files) +
`handoff_out/local-ai-hub-extension-0.2.0.xpi` (19 files); `dist/` + caches removed.

**Exact next step for the next session:** re-fingerprint the four rows (Phase 0),
then take the next pure-code item: **a second inference backend** behind the
`hub/models/ollama.py` interface (llama.cpp / LM Studio speak a similar HTTP API;
`ModelManager` is the only place that knows the backend). Alternative: API rate
limiting mirroring `hub/bridge.py`. Both touch `hub/` → rerun the 2 Python rows.

## Next 5 tasks, in order (suggested)

1. Second inference backend behind the same interface (`ollama.backend` switch).
2. API rate limiting for local clients (mirror the bridge's per-minute policy).
3. Real-browser validation of MV3 (`docs/MV3.md` §9: static-only today).
4. Real-model + real-board validation; tune `batch_size`/`num_ctx`/`warmup`.
5. Per-site extension profiles (several selector sets for one domain).

## Verified facts worth reusing (don't re-grep)

- Priorities: int 0-9, **higher first**, default 5, names low=0/normal=5/high=9,
  garbage → default. `hub/tasks.py:TaskQueue` (heap+Condition, FIFO per level,
  bounded blocking `put`, `snapshot/remove/clear`) is owned by `Engine.queue` and
  consumed by the host's 2 workers; running tasks are never preempted. CLI/HTTP
  call the engine directly (ThreadingHTTPServer) → unqueued.
- `tasks.list` → `{running, queued, history, counters}`; `status.tasks` =
  counters + `queued`. `task.cancel <queued id>` → `{cancelled:[id], queued:true}`
  + `task.cancelled` event + cancelled history/audit record; `*` covers both.
  `meta.priority` on every result incl. cache hits; `tasks.queue_size` (64),
  `tasks.default_priority` (5), CLI `task --priority`.
- Warm-up: off by default. Keys `ollama.warmup`, `warmup_on_start` (true),
  `warmup_vision` (false), `warmup_models` ([]), `warmup_timeout_seconds` (120).
  `ModelManager.warmup(models=None, block=False)` → `{state, models, loaded,
  skipped, failed, duration_ms, error, mock}`; states idle/running/ready/partial/
  failed/cancelled; events `model.warming|warmed|warmup_failed|warmup_done`; also
  in `status.models.warmup` / `models.list`. `OllamaClient.load` = empty prompt +
  keep_alive. Mock warms any tag; real backend resolves it first. Idle reaper
  still unloads warmed models.
- `models.warmup` is a control type but **not** inline (a cold load must not
  block the host reader thread). `POST /v1/models/warmup` forwards `models`;
  other argument-less POST control routes still ignore the body.
- Bridge: events `local-ai-hub:{hello,ready,request,response}`, `detail` = JSON
  string, v1; 30/min, 2 in flight, 256 KiB input, 4 MiB detail cap.
- Capabilities enforced: `llm.generate|chat|vision|structured`; no fs/net ones.
  `hub/cache.py`: sha256 `cache_key`, TTL, LRU cap, `meta.cache={hit,age_ms,key}`.
  Streaming: `llm.token` `{type,id,text,index}` → `model.token` `{plugin,text}`.
- API: `/v1` routes, SSE `event: ready` + 15 s keepalive, `?access_token=` only on
  `/v1/events` when `api.allow_token_query`, AF_UNIX opt-in.
- `scripts/package.py` needs no flags; excludes `.git`, `dist`, caches,
  `.DS_Store`, `*.log/zip/xpi`; **includes `for LLM/`** (intended).
- CLI state is per-process: one-shot `laihub tasks` / `laihub models` show no
  history/warm-up from an earlier one-shot call. The native host is long-lived.

## Known bugs / blockers

- None known in the code.
- Sandbox: no `make`/`zip`/`pip` — use the raw python3/node commands. No browser
  or Ollama: MV3 and real-model behaviour (incl. warm-up latency) are untestable
  end to end here; the docs say so.

## Do NOT do this

- Do not rerun the four ledger checks while their fingerprints match.
- Do not resurrect `for LLM/history|audit|uploads|scratch|image-search`.
- Do not re-fix engine double-wrap, config keys, versions, MV3 validator, the
  API POST body forwarding for `models.warmup`, or `on_event(name, data)` arity.
- `.DS_Store` is **tracked upstream**, so every fresh clone restores it; deleted
  again on 2026-11-19, `.pytest_cache/` in `.gitignore`. Keep it deleted.
- Do not rewrite the docs finished on 2026-11-18/19 (all of `docs/`, README,
  CHANGELOG 0.2.0). Extend them only with newly verified facts.
- No new deps, no commits/pushes, no zip or `dist/` left inside the repo.

## Unverified claims (check before repeating them in docs)

- The `status` output description is inherited from the old PROTOCOL.md; only
  `tasks` and `models.warmup` were re-verified. Extension UX items (dry run,
  review UI, picker, menus) come from the prior audit + green tests, not a read.
