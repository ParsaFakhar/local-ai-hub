# Completion worklog — local-ai-hub

Repo cloned to `repo/` (HEAD 94c3f04). Baseline: 93 python tests (1 failing),
28 extension checks, 15 smoke checks.

## Audit vs PLAN.md (state at HEAD)

| Plan item | Found | Action |
| --- | --- | --- |
| P0 engine double-wrap | already fixed | verify + regression test |
| P0 config/hub.json keys | present | keep in sync with new keys |
| P0 .DS_Store / .gitignore | still tracked, no ignore file | fix |
| P0 version drift | hub 0.1.0, ext 0.1.0, plugins 0.2.0 | unify on 0.2.0 + test |
| P1 bridge (js/py/userscript/tests) | implemented | failing policy test -> fix |
| P1 docs/TAMPERMONKEY.md | missing | write |
| P2 dry-run / review / picker / menus | implemented | verify |
| P3 six new plugins | missing | implement |
| P4 CLI tasks/cancel/cache/serve/doctor/--version | missing | implement |
| P4 streaming inference | missing (`stream:false` hardcoded) | implement end to end |
| P4 API GET /v1/tasks/<id>, unix socket | missing | implement |
| P5 MV3 build + compat + offscreen imaging | missing | implement |
| P6 tests api/cache/plugins/cli | missing | implement |
| P6 docs refresh | stale | rewrite |

## Progress

- [ ] A hygiene + failing test + version unify
- [ ] B streaming (ollama -> manager -> runner -> engine -> SDKs -> events)
- [ ] C tasks.get + API route + unix socket
- [ ] D CLI parity
- [ ] E plugins: page.summarize, text.translate, links.check, forms.fill, images.dedupe, moderation.queue
- [ ] F MV3 builder + compat shim + worker imaging + keep-alive
- [ ] G tests
- [ ] H docs
