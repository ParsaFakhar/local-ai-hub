# PLAN — local-ai-hub (consolidated)

One plan for the whole project. `[x]` done · `[ ]` todo · `[!]` blocked.
Items marked *(prior audit)* were verified by an earlier session at HEAD
`b6c595b`/`32b7582`; everything else was verified against the code on 2026-11-18.

## P0 — bugs / hygiene

- [x] `Engine.handle` no longer double-wraps error envelopes *(prior audit)*
- [x] `config/hub.json` carries cache / tasks / security / api / bridge keys
- [x] Versions unified at 0.2.0 (hub, extension, plugins, template)
- [x] `.gitignore` present; tracked `.DS_Store` deleted; `.pytest_cache/` added
- [x] `for LLM/` reduced to the 5 handoff files; history/audit/uploads/scratch/
      image-search deleted

## P1 — Tampermonkey bridge

- [x] `extension/content/bridge.js` + `tests/test_bridge.py`
- [x] `hub/bridge.py` policy + engine re-check for page-facing interfaces
- [x] `examples/tampermonkey/local-ai-hub.user.js`
- [x] `docs/TAMPERMONKEY.md`
- [x] SECURITY.md §8 bridge trust boundary

## P2 — extension UX *(prior audit, tests green)*

- [x] Dry-run mode · [x] result review UI · [x] selector picker
- [x] Context menu "send selection to plugin" + per-feature commands
- [x] Options-UI feature templates for the 6 newer plugins

## P3 — plugins (10 bundled)

- [x] comments.classify, images.tag, hello.python, hello.javascript
- [x] page.summarize, text.translate, links.check, forms.fill, images.dedupe,
      moderation.queue
- [x] `tests/test_new_plugins.py` (24 tests) + 4 plugin bug fixes
- [x] `examples/` inputs for every plugin

## P4 — engine / CLI / API parity

- [x] CLI parity incl. tasks/cancel/cache/serve/doctor, `--version`, `--remote`
- [x] Streaming inference end to end (`llm.token` → `model.token`)
- [x] API `GET /v1/tasks/<id>`, `/v1/events` (SSE), `/v1/openapi.json`, unix socket
- [x] Result cache, task registry + cancellation, history + JSONL audit
- [x] Capability enforcement for `llm.generate|chat|vision|structured`

## P5 — MV3

- [x] `scripts/build_mv3.py` → both targets build and validate
- [x] Compat shim, OffscreenCanvas imaging, 30 s keep-alive alarm
- [x] `make mv3` + MV3 step in `scripts/check.sh`
- [x] `docs/MV3.md` incl. manual Chrome native-host registration

## P6 — tests

- [x] 261 Python tests across api/cache/cli/bridge/capabilities/streaming/tasks/
      schema/protocol/domains/engine/plugins/new plugins
- [x] `tests/image_fixtures.py` used by the image plugin tests
- [x] Ledger baseline established (261 / 28 / 15 / MV3 both targets)

## P7 — documentation (all finished 2026-11-18)

- [x] `docs/TAMPERMONKEY.md` (new)
- [x] `docs/MV3.md` (new)
- [x] `CHANGELOG.md` 0.2.0 entry
- [x] `README.md` rewrite
- [x] `docs/ROADMAP.md` — shipped items removed
- [x] `docs/PROTOCOL.md` — control types, errors, token frames, SSE, unix socket
- [x] `docs/ARCHITECTURE.md` — bridge, MV3, cache/tasks/capabilities/client
- [x] `docs/SECURITY.md` — §4 corrected, §8 bridge, §9 enforced capabilities
- [x] `docs/PLUGIN_API.md` — capabilities enforced
- [x] `docs/DOMAIN_CONFIG.md` — `bridge` keys
- [x] `docs/INSTALL_MACOS.md` — MV3 pointer

## P8 — packaging / delivery

- [x] `python3 scripts/package.py` LAST, from the final tree →
      `dist/local-ai-hub-0.2.0.zip` + `dist/local-ai-hub-extension-0.2.0.xpi`;
      copy both to `handoff_out/` outside the repo (done: 132-file zip, 19-file xpi)
- [x] Final tree clean: no `dist/`, no `__pycache__`, `for LLM/` = 5 files,
      `git status --short` shows only intended changes

## P9 — task priorities (2026-11-19)

- [x] `normalize_priority` + `TaskQueue` in `hub/tasks.py` (0-9, higher first,
      FIFO per level, bounded, snapshot/remove/clear)
- [x] `priority` in registry running/get/history/audit + `cancel_before_start`
- [x] Engine: `engine.queue`, `meta.priority`, `tasks.list.queued`,
      `status.tasks.queued`, queue-aware `task.cancel`, shutdown clears queue
- [x] Native host workers consume the priority queue
- [x] Config `tasks.queue_size` / `tasks.default_priority` (defaults + hub.json)
- [x] CLI `laihub task --priority`
- [x] `tests/test_priority.py` (32 tests) + 2 smoke checks → 293 / 17 green
- [x] Docs: PROTOCOL (priority field, meta, `task.cancelled`, queued control
      outputs, new "Task priorities" section), ARCHITECTURE, CHANGELOG 0.2.0,
      ROADMAP (item removed), README (counts 293/17, feature bullet, CLI example)
- [x] Repackaged from the final tree → `handoff_out/local-ai-hub-0.2.0.zip`
      (133 files) + `handoff_out/local-ai-hub-extension-0.2.0.xpi` (19 files),
      both outside the repo; `dist/` and caches removed again

## P10 — model warm-up policy (2026-11-19)

- [x] `OllamaClient.load` (empty prompt, keep_alive, timeout)
- [x] `ModelManager.warmup` + targets/status/async run, skip + failure handling
- [x] `models.warmup` control type, engine start-up warm-up (opt-in)
- [x] Config keys + `config/hub.json`; `status().warmup`
- [x] `POST /v1/models/warmup`, `LocalClient.warmup`, CLI `laihub warmup`
- [x] `tests/test_warmup.py` (33 tests) + 1 smoke check → 326 / 18 green
- [x] Docs: PROTOCOL (control row, events, "Model warm-up" section, HTTP row),
      ARCHITECTURE, SECURITY limit table, CHANGELOG 0.2.0, ROADMAP, README
- [x] Bug found by verifying the docs: generic POST control routes dropped the
      body, so `POST /v1/models/warmup` ignored `models`. Fixed in `hub/api.py`
      + 4 real-HTTP tests.
- [x] Repackaged from the final tree → `handoff_out/` (134-file zip, 19-file xpi)

## Open project work (not this session)

See `docs/ROADMAP.md`: real board selectors, real-model tuning, signed persistent
install, MV3 in a real browser, OS sandbox, fs/net capabilities, other backends,
API stability + client libraries, packaging (Homebrew, launchd).
Task priorities (P9) and model warm-up (P10) shipped on 2026-11-19.
