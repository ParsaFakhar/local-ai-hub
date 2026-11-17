# Completion audit — local-ai-hub (HEAD b6c595b)

Status of every item in `PLAN.md` / `WORKLOG.md`, verified by running the code.

Baseline measured on a clean clone: **94 Python tests pass, 28 extension checks
pass, 15 smoke checks pass**, 10 plugins discovered, no manifest errors.

| Plan item | State at HEAD | Verdict |
| --- | --- | --- |
| P0 `Engine.handle` double-wrap | fixed (`_is_error_envelope`) | done |
| P0 `config/hub.json` keys | cache/tasks/security/api/bridge present | done |
| P0 `.DS_Store` / `.gitignore` | `.DS_Store` tracked, no ignore file | **todo** |
| P0 version drift | hub / extension / plugins / template all 0.2.0 | done (no test) |
| P1 bridge js + py + userscript + tests | implemented, tests pass | done |
| P1 `docs/TAMPERMONKEY.md` | missing | **todo** |
| P2 dry-run / review / picker / menus | implemented, tests pass | done |
| P3 six new plugins | all present and runnable | done (untested) |
| P4 CLI tasks/cancel/cache/serve/doctor/--version | implemented | done (untested) |
| P4 streaming | ollama → manager → runner → engine → SDKs → CLI | done (untested) |
| P4 API `GET /v1/tasks/<id>` + unix socket | implemented | done (untested) |
| P5 MV3 builder | **`--target chrome` fails its own validation** | **bug** |
| P5 compat shim / offscreen imaging / keep-alive | implemented | done |
| P5 `docs/MV3.md` (referenced by generated READMEs) | missing | **todo** |
| P6 tests for api / cache / cli / new plugins | missing (`tests/image_fixtures.py` written but imported by nothing) | **todo** |
| P6 docs refresh | README/PROTOCOL/ARCHITECTURE/SECURITY/ROADMAP stale | **todo** |
| extra: options-UI feature templates | only comments.classify / images.tag / hello.* | **todo** |
| extra: examples for the six new plugins | only comments.json / images.json | **todo** |
| extra: `make mv3`, MV3 build in `check.sh` | missing | **todo** |

## Reproduce the MV3 bug

```
$ python3 scripts/build_mv3.py
dist/mv3-firefox  (MV3, 0.2.0, firefox)
  ✓ manifest, scripts and syntax validated
dist/mv3-chrome  (MV3, 0.2.0, chrome)
  ✗ service-worker.js is loaded before the compatibility shim
```

`validate()` puts the service-worker bootstrap in the same list as the content
scripts and then requires index 0 to be `lib/browser.js`. For Chrome the
bootstrap *is* the entry point — what must start with the shim is its
`importScripts(...)` list. The build is actually correct; the check is wrong.

## Work executed (see WORKLOG.md for the running log)

A hygiene + version test · B MV3 fix + docs · C test suites · D extension
templates · E examples · F docs refresh · G full verification.
