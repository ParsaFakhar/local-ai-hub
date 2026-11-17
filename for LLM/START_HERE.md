# START HERE — LLM handoff for local-ai-hub

> Read this first. Then `PLAN.md` (what's left) and `WORKLOG.md` (what happened).
> Older session notes are preserved in `history/`.

## What this project is (30 seconds)

**Local AI Hub** is a local, extensible AI automation platform for macOS.
Firefox Nightly is its *first interface*, not its purpose. A Firefox MV2
extension extracts page data via per-domain selector configs, sends **tasks**
over native messaging to a Python **engine** (`hub/`), which routes them to
**plugins** (independent Python/Node subprocesses) and brokers all model calls
to **Ollama** (or an offline mock). Results return to the page through an
**allowlisted applier** (badges/classes/attributes/hide — never innerHTML).
The same engine is reachable via CLI (`bin/laihub`), Python import, and an
opt-in token-authenticated localhost HTTP API. Comment classification and
image tagging are just the first two applications of ten bundled plugins.

Security invariant: **the platform never executes code or commands supplied by
webpages or LLM output.** Everything crossing a trust boundary is
schema-validated data. See `docs/SECURITY.md`.

## Repo map

```
hub/            engine: router, config, domains, plugins, runtime, models,
                cache, tasks, capabilities, bridge policy, HTTP api
native/         native messaging host (stdio framing ⇄ Firefox)
extension/      MV2 Firefox extension (background, content, options, popup)
plugins/        10 plugins: comments.classify, images.tag, hello.python,
                hello.javascript, page.summarize, text.translate, links.check,
                forms.fill, images.dedupe, moderation.queue
sdk/            stdlib-only plugin SDKs (python, js)
config/         hub.json + per-domain configs (incl. news.ycombinator.com demo)
scripts/        install helpers, doctor, smoke test, packaging, build_mv3.py
templates/      plugin skeleton
examples/       inputs for every plugin + Tampermonkey userscript + API clients
tests/          Python unittest + node extension-logic tests + image_fixtures
docs/           architecture, protocol, plugin API, domain config, security…
for LLM/        ← you are here: handoff notes, plans, scratch space for LLMs
```

## Verify the baseline (do this before changing anything)

```bash
python3 -m unittest discover -s tests   # unit tests
node tests/test_extension.js            # extension logic checks
python3 scripts/smoke_test.py --quiet   # end-to-end over real native pipe
python3 scripts/build_mv3.py            # MV3 builds (firefox + chrome)
./scripts/check.sh                      # all of the above except MV3
```

Everything runs **offline** with the mock model backend — no Ollama, no model
download, no Firefox needed. Node is used for JS plugins/tests.

## State when this file was last updated

Session of 2026-11-17, repo HEAD `32b7582` ("Replace project with updated version").
Verified green at session start: **237 Python tests, 28 extension checks,
15 smoke checks, MV3 builds validate for both targets.** Versions unified at
0.2.0. The MV3 chrome validator bug from `history/AUDIT.md` is already fixed.

This session is working through the remaining gaps — see `PLAN.md` for the
live checklist and `WORKLOG.md` for what has been done and verified.

## Conventions & gotchas (save yourself debugging time)

- **Engine harness for tests**: set `LOCAL_AI_HUB_DATA` to a temp dir, then
  `Engine(load_config({"ollama": {"mock": True}}))`; call
  `engine.handle({"type": "task", "plugin": ..., "input": ...})`.
  Copy the pattern from `tests/test_engine.py` (setUpClass/tearDownClass).
- **Mock model**: `hub/models/manager.py` `_mock_payload`/`_mock_fill` fill
  JSON schemas deterministically. Structured outputs come back schema-shaped;
  tests assert structure/coercion, not model quality.
- **`tests/image_fixtures.py`** generates real PNG/JPEG bytes in pure Python
  (`png_bytes`, `jpeg_bytes`) — use it for images.dedupe / images.tag tests.
- Plugin manifests are single-line JSON files; `python3 -m json.tool` to read.
- stdout of host/plugins is **protocol-only**; logs go to stderr. Never print
  to stdout in plugin code (Python SDK redirects print already).
- The repo is committed by the user from the LLM workspace ("Replace project
  with updated version" commits). `for LLM/` holds LLM scratch artifacts
  (old README dumps in `uploads/`, `audit/dump.txt`, an API probe script,
  apple stock images). They are harmless history; don't treat them as code.
- No network access is needed for any test. Don't add dependencies: engine and
  SDKs are **stdlib-only** by design; plugins must stay dependency-free.
- Docs drift is the recurring failure mode of this repo: after changing
  behavior, grep docs for stale claims (test counts, "not implemented",
  plugin lists) and update README/CHANGELOG/ROADMAP in the same session.
