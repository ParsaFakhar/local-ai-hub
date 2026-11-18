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

## Open project work (not this session)

See `docs/ROADMAP.md`: real board selectors, real-model tuning, signed persistent
install, MV3 in a real browser, OS sandbox, fs/net capabilities, task priorities,
model warm-up, other backends, API stability + client libraries, packaging
(Homebrew, launchd).
