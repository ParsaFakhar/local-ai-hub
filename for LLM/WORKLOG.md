# WORKLOG — local-ai-hub

Newest session at the top. One short entry per session. Never edit old entries.

## 2026-11-18 — handoff system rebuilt, ledger baseline measured (base `29d4d13`)

- Clone: the prompt's URL (`RafaelRibeiro/local-ai-hub`) 404s; the user supplied
  `ParsaFakhar/local-ai-hub`, which cloned fine. Workspace started empty.
- Phase 1: `for LLM/` rebuilt to exactly 5 files (START_HERE, STATE, PLAN,
  WORKLOG, fingerprint.sh). Folded `history/AUDIT.md`, `history/PLAN.md`,
  `history/WORKLOG.md` into the new PLAN.md/STATE.md, then deleted
  `for LLM/history/`, `for LLM/audit/`, `for LLM/uploads/`, `for LLM/scratch/`,
  `for LLM/image-search/` and every tracked `.DS_Store`. Added `.pytest_cache/`
  to `.gitignore` (`.DS_Store`, `dist/`, `__pycache__/` were already covered).
- Ledger baseline (all run once, all green): **261 Python tests OK**,
  **28/28 extension checks**, **15/15 smoke checks**, **MV3 firefox + chrome both
  build and validate**. This confirms the previous session's unverified numbers
  and confirms the old MV3 chrome-validator bug is fixed.
- Environment note: sandbox has no `make`, no `zip`, no `pip`; the raw
  python3/node commands and `scripts/check.sh` work unchanged.
- Verified while triaging: 10 plugin folders, examples for all plugins, options
  UI + bridge content script present, `docs/TAMPERMONKEY.md` and `docs/MV3.md`
  still missing, CHANGELOG still stops at 0.1.0, README still claims 45/12 tests,
  four plugins and "Tampermonkey: Not implemented" — all queued in PLAN P7.
- Wrote **`docs/TAMPERMONKEY.md`** from `extension/content/bridge.js`,
  `hub/bridge.py`, `hub/engine.py` and the example userscript. The previously
  guessed `ready` payload is now verified: `{version, domain, plugins,
  allow_apply, max_requests_per_minute, max_input_bytes}`, sent only in answer to
  `local-ai-hub:hello` and only on opted-in domains; `detail` is a JSON string.
- Wrote **`docs/MV3.md`** from `scripts/build_mv3.py`, `extension/background.js`
  and `install.sh`. Verified: `install.sh` registers the **Firefox** host only, so
  Chrome needs a manual `allowed_origins` manifest (path + name documented);
  Chrome keeps 4 of the 7 suggested shortcuts; keep-alive alarm is 30 s; native
  size caps checked against Chrome/MDN docs (host→browser 1 MB both, browser→host
  64 MiB Chrome / 4 GB Firefox) and against `hub/protocol.py`, which enforces
  1 MiB out / 64 MiB in and returns `message_too_large`.
- Wrote the **`CHANGELOG.md` 0.2.0** entry (engine capabilities/tasks/cache/
  streaming/bridge policy, CLI parity, API routes + SSE + unix socket, extension
  UX + bridge, MV3 builds, six new plugins + the four plugin fixes, config/docs/
  tests). Every line was checked against the code first; the cache key shape was
  left out because it is still unverified.
- Rewrote **`README.md`**: 261/28/15 counts, ten-plugin table with runtime +
  model capability, MV3 quick-start, userscript-bridge section, engine-services
  and security additions, CLI examples for tasks/cancel/cache/serve/doctor,
  status table (Tampermonkey → implemented, MV3 → validated statically), docs map
  incl. the two new files.
- Rewrote **`docs/ROADMAP.md`**: shipped items removed (bridge, MV3, review UI,
  dry run, picker, menus, six plugins, cache, history/audit, cancellation,
  streaming, capabilities, SSE, unix socket); kept real gaps and added "MV3 in a
  real browser". Verified there is no priority queue / warm-up code before
  leaving those on the roadmap.
- Rewrote **`docs/SECURITY.md`**: §4 no longer claims "no page-facing channel at
  all" (the opt-in bridge exists) and now states that page-supplied
  options/domain_config are dropped; new §8 bridge policy and §9 enforced
  capabilities; limits table gained bridge + incoming-frame caps; the "capabilities
  are documentation, not enforcement" line and the shipped hardening ideas are gone.
- Patched **`docs/PLUGIN_API.md`**: `capabilities` row said "documentation and
  future permissioning" → enforced, with the vocabulary table and denial codes.
- Rewrote **`docs/ARCHITECTURE.md`**: added bridge/shim/protocol files to the
  extension table, the MV3 builder (was "an MV3 port is listed in the roadmap"),
  the missing engine modules (capabilities, tasks, cache, bridge, client),
  inline control types, streaming frames, cache/capability steps in the data-flow
  diagram, the userscript + SSE interfaces, and new failure rows.
- Rewrote **`docs/PROTOCOL.md`**: full control-type table (tasks.list/get,
  task.cancel, cache.clear + inline set), error codes (capability_denied,
  task_cancelled, unknown_task, forbidden, rate_limited, unauthorized,
  not_found), `model.token`/`task.cancelling` events, `llm.token` RPC frame with
  the 20 000-frame cap, capability rules, cache metadata, 1 MiB/64 MiB caps, the
  complete `/v1` route table, SSE framing + `?access_token=`, and the Unix socket.
- Added the **`bridge` section to `docs/DOMAIN_CONFIG.md`** and an **MV3 pointer
  to `docs/INSTALL_MACOS.md` §3**.
- Also verified for the upcoming doc patches: capabilities are enforced for
  `llm.generate|chat|vision|structured`; `hub/tasks.py` keeps bounded history +
  JSONL audit with input hashes; `hub/api.py` has SSE `/v1/events`,
  `/v1/openapi.json`, `/v1/tasks/<id>` and an opt-in AF_UNIX transport.
- QA: every relative markdown link in README/CHANGELOG/docs resolves (0 broken);
  `for LLM/` contains exactly the 5 handoff files; `git status --short` shows only
  intended edits, deletions and the 6 new files.
- Packaged LAST from the final tree with `python3 scripts/package.py` and copied
  `local-ai-hub-0.2.0.zip` + `local-ai-hub-extension-0.2.0.xpi` to `handoff_out/`
  outside the repo, then removed `dist/` and all `__pycache__` from the repo.
  Re-fingerprinted the four ledger rows afterwards: unchanged, so the green
  results still stand. No code was modified in this session.
