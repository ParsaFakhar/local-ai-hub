# STATE — local-ai-hub

**Base commit:** `29d4d13` ("replacing the repo") · **Session date:** 2026-11-18
Informational only. Never commit or push; the workspace folder is the deliverable.

## Test ledger

Run `sh "for LLM/fingerprint.sh" <paths>` from the repo root. Match = still valid,
cite it. Mismatch = rerun that row only, then update Result + Fingerprint.

| Check | Command | Result | Paths covered | Fingerprint |
| --- | --- | --- | --- | --- |
| Python unit tests | `python3 -m unittest discover -s tests` | PASS — 261 tests, OK (11.7s) | `hub native plugins sdk config scripts tests bin` | `089f0ef91e37` |
| Extension logic | `node tests/test_extension.js` | PASS — 28/28 | `extension tests/test_extension.js` | `286c63e0186e` |
| End-to-end smoke | `python3 scripts/smoke_test.py --quiet` | PASS — 15/15 | `hub native plugins sdk config scripts/smoke_test.py` | `8396972d339c` |
| MV3 build + validation | `python3 scripts/build_mv3.py` | PASS — firefox + chrome both validated | `extension scripts/build_mv3.py` | `cd6e0afcd509` |

Measured 2026-11-18 at base `29d4d13`, re-fingerprinted after the final cleanup:
unchanged. **No code was touched this session** (docs, README, CHANGELOG,
`.gitignore`, `for LLM/` only), so all four rows still hold. Do not rerun them.

## Current task

None in flight — the 2026-11-18 documentation + handoff session is complete
(PLAN P0–P8 all ticked). Artifacts: `handoff_out/local-ai-hub-0.2.0.zip` and
`handoff_out/local-ai-hub-extension-0.2.0.xpi`, **outside** the repo at the
workspace root; `dist/` was removed from the repo afterwards.

**Exact next step for the next session:** re-fingerprint the four ledger rows
(Phase 0 in START_HERE.md), then pick from `docs/ROADMAP.md`. The highest-value
open items need a real machine, not this sandbox: load the MV2/MV3 build in
Firefox/Chrome with a real Ollama model, and validate a real board's selectors.
Pure-code candidates that need no browser: task priorities, model warm-up policy,
a second model backend behind `hub/models/ollama.py`'s interface.

## Next 5 tasks, in order (suggested)

1. Real-browser validation of MV3 (`docs/MV3.md` §9 says it is static-only today).
2. Real-model + real-board validation, then tune `batch_size` / `num_ctx`.
3. Task priority queue in `hub/tasks.py` (cancellation already exists).
4. Model warm-up policy in `hub/models/manager.py`.
5. Second inference backend behind the same interface (llama.cpp / MLX / LM Studio).

## Verified facts worth reusing (don't re-grep)

- Bridge: events `local-ai-hub:{hello,ready,request,response}`, `detail` = JSON
  string, v1; ready = `{version, domain, plugins, allow_apply,
  max_requests_per_minute, max_input_bytes}`; request keys `version,id,plugin,
  input,apply`; 30/min, 2 in flight, 256 KiB input, 4 MiB detail cap.
- Capabilities enforced (`hub/capabilities.py`): `llm.generate|chat|vision|
  structured`; unknown names informational; no fs/net capabilities exist.
- `hub/tasks.py`: bounded history + JSONL audit of input hashes; cancel/get/counters.
- `hub/cache.py`: sha256 `cache_key(...)`, TTL, LRU cap, `meta.cache={hit,age_ms,key}`.
- Control types incl. `tasks.list/get`, `task.cancel`, `cache.clear`; inline set =
  ping, status, shutdown, task.cancel, tasks.list, tasks.get. `task.cancel` →
  `{cancelled:[…]}` (+`all:true` for `*`), `cache.clear` → `{cleared, cache}`.
- Streaming: `llm.token` frames `{type,id,text,index}` (cap 20 000/call) →
  coalesced `model.token` events `{plugin,text}`.
- API: full `/v1` route table, SSE `event: ready` + `: keepalive` every 15 s,
  `?access_token=` only on `/v1/events` when `api.allow_token_query`, AF_UNIX opt-in.
- `scripts/package.py` needs no flags (`--zip-only` / `--xpi-only` exist); it
  excludes `.git`, `dist`, caches, `.DS_Store`, `*.log/zip/xpi` and **includes
  `for LLM/`**, which is intended.
- MV2: 7 suggested shortcuts (Chrome build keeps 4); host `local_ai_hub`; gecko id
  `local-ai-hub@localaihub.dev`; `install.sh` registers Firefox only.

## Known bugs / blockers

- None known in the code.
- Sandbox: no `make`, no `zip`, no `pip` — use the raw python3/node commands;
  packaging works anyway (stdlib zipfile).
- No browser and no Ollama here: nothing can be validated end-to-end against a
  real page or model. The docs say so explicitly.
- Remote here is `ParsaFakhar/local-ai-hub`; the prompt's `RafaelRibeiro/...` 404s.

## Do NOT do this

- Do not rerun the four ledger checks while their fingerprints match.
- Do not resurrect `for LLM/history|audit|uploads|scratch|image-search`.
- Do not re-fix engine double-wrap, config keys, versions, MV3 validator,
  `.gitignore`/`.DS_Store`.
- Do not rewrite the docs finished on 2026-11-18 (TAMPERMONKEY, MV3, ROADMAP,
  SECURITY, ARCHITECTURE, PROTOCOL, PLUGIN_API, DOMAIN_CONFIG, INSTALL_MACOS,
  README, CHANGELOG 0.2.0). Extend them only with newly verified facts.
- No new deps, no commits/pushes, no zip or `dist/` left inside the repo.

## Unverified claims (check before repeating them in docs)

- `tasks.list` output keys are described in prose only ("running, history,
  counters"); exact JSON keys not read (`hub/engine.py:190`).
- The `status` output description is inherited from the old PROTOCOL.md and was
  not re-verified field by field.
- Extension UX items (dry run, review UI, picker, menus) are carried from the
  prior audit + green tests, not re-read line by line this session.
