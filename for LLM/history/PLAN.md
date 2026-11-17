# Completion plan — local-ai-hub

Audit result: engine/host/extension are real and working (77 unit + 12 ext + 15 smoke).
Gaps found:

## P0 bugs / hygiene
- [ ] `Engine.handle` double-wraps error envelopes returned by control handlers
      -> `task.cancel` with non-string id returns ok:true (failing unit test).
- [ ] `config/hub.json` stale: missing cache/tasks/security/api keys the code reads.
- [ ] `.DS_Store` tracked, no `.gitignore`.
- [ ] version drift: manifest 0.1.0, plugins 0.2.0, CHANGELOG only 0.1.0.

## P1 Tampermonkey bridge (roadmap: not implemented)
- [ ] `extension/content/bridge.js` — opt-in per domain, plugin allowlist,
      input-only (no config/selectors/code), rate limit, nonce correlation.
- [ ] userscript client `examples/tampermonkey/local-ai-hub.user.js`
- [ ] `docs/TAMPERMONKEY.md` + security doc update + tests

## P2 extension UX (roadmap)
- [ ] dry-run mode (plan actions, show diff, apply nothing)
- [ ] result review UI (accept/reject per item before applying)
- [ ] selector picker (click element -> item_selector/fields)
- [ ] context menu "send selection to plugin" + per-feature commands

## P3 new plugins (roadmap)
- [ ] page.summarize, text.translate, links.check, forms.fill,
      images.dedupe (pure-python PNG perceptual hash), moderation.queue

## P4 engine/CLI/API parity
- [ ] CLI: tasks / cancel / cache / serve / doctor / --version
- [ ] streaming inference (ollama stream + mock + SDK on_token + events)
- [ ] API: GET /v1/tasks/<id>, unix socket transport (opt-in)

## P5 MV3
- [ ] `scripts/build_mv3.py` -> dist/mv3-firefox + dist/mv3-chrome
- [ ] browser compat shim + OffscreenCanvas image path + keep-alive

## P6 docs / tests / packaging
- [ ] tests for hub/api.py, hub/cache.py, bridge, new plugins
- [ ] refresh README/PROTOCOL/SECURITY/ARCHITECTURE/ROADMAP/CHANGELOG
