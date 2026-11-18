# START HERE — LLM handoff for local-ai-hub

## What this project is (5 lines)

Local AI Hub is a local, extensible AI automation platform for macOS; Firefox is
its first interface, not its purpose. A browser extension extracts page data with
per-domain selector configs and sends **tasks** over native messaging to a Python
**engine** (`hub/`) that routes them to **plugins** (Python/Node subprocesses) and
brokers model calls to Ollama (or an offline mock). Results return through an
allowlisted applier — never innerHTML, never page- or LLM-supplied code.

## Phase 0 — do this first, every session

1. Repo in the workspace? Clone if not. `git rev-parse --short HEAD` = base commit
   (informational only). **Never commit, never push.**
2. Read `for LLM/STATE.md`, then this file. Read nothing else here until needed.
3. For **every ledger row** in STATE.md run, from the repo root:
   `sh "for LLM/fingerprint.sh" <paths from that row>`
   - same fingerprint → the result still holds, **cite it, do not rerun**;
   - different fingerprint → row is stale: rerun that one check, update result +
     fingerprint, and note it in WORKLOG.md.
4. Continue from **Current task** in STATE.md. Docs-only edits never invalidate
   code rows — that is what the *Paths covered* column is for.

## Rules (condensed)

- **Notes as you go.** After every milestone update STATE.md (rewrite in place),
  prepend a WORKLOG.md entry, tick PLAN.md — in the *same* tool call as the work.
  Assume the session can be cut off at any moment.
- **Ledger rule.** No check is "known green" without a row + matching fingerprint.
  Never rerun a check whose fingerprint still matches.
- **Verify, don't guess.** One grep beats an assumption. If you must write
  something unconfirmed, mark it UNVERIFIED and list it in STATE.md.
- **Docs describe the code as it is.** Fix stale claims (test counts, "not
  implemented", plugin lists) in the same session that changes behavior.
- **Files.** The repo is the deliverable and must mirror the intended repo
  exactly: only project content, no dumps, uploads, probes, scratch or images.
  Scratch goes in `/tmp/scratch/`. `for LLM/` holds exactly the 5 files below.
- **Batch tool calls**; never dump large output (`head`, `grep -n`, `sed -n`).
- Everything runs **offline**: stdlib-only Python, `node` for JS. No new deps.

## Commands (no `make` needed)

```bash
python3 -m unittest discover -s tests    # Python unit tests
node tests/test_extension.js             # extension logic checks
python3 scripts/smoke_test.py --quiet    # end-to-end over the real native pipe
python3 scripts/build_mv3.py             # MV3 firefox + chrome builds + validation
./scripts/check.sh                       # all of the above + syntax/JSON lint
python3 scripts/package.py               # dist zip + xpi (verify flags before use)
```

## File map of `for LLM/`

| File | Purpose |
| --- | --- |
| `START_HERE.md` | this file: orientation + procedure + rules |
| `STATE.md` | live status: test ledger, current task, next tasks, blockers |
| `PLAN.md` | one consolidated plan, `[x]` done / `[ ]` todo / `[!]` blocked |
| `WORKLOG.md` | one short entry per session, newest at the TOP (never edit old ones) |
| `fingerprint.sh` | `sh "for LLM/fingerprint.sh" hub plugins tests` → 12-char hash |

## Gotchas worth knowing

- Engine test harness: `LOCAL_AI_HUB_DATA` → temp dir, then
  `Engine(load_config({"ollama": {"mock": True}}))`; copy `tests/test_engine.py`.
- Mock model fills JSON schemas deterministically (`hub/models/manager.py`);
  assert structure/coercion, not model quality.
- `tests/image_fixtures.py` generates real PNG/JPEG bytes in pure Python.
- Plugin manifests are single-line JSON; stdout of host/plugins is protocol-only.
- Recurring failure mode of this repo: **doc drift**. Grep docs after changes.
