# Installing on macOS (Apple Silicon)

## Requirements

| Component | Requirement | Check |
| --------- | ----------- | ----- |
| macOS | Apple Silicon or Intel | — |
| Firefox | Nightly (or Developer Edition) | `about:support` |
| Python | 3.9+ (system or Homebrew) | `python3 --version` |
| Node.js | 18+ — only for JavaScript plugins | `node --version` |
| Ollama | any recent version — only for real inference | `ollama --version` |

No third-party Python packages are required. Nothing needs `sudo`.

## 1. Install the hub

```bash
unzip local-ai-hub.zip
cd local-ai-hub
./install.sh
```

What it does:

1. verifies Python (and warns about Node/Ollama if missing)
2. creates `~/Library/Application Support/LocalAIHub/{logs,config/domains}`
3. writes a user `config/hub.json` (absolute python path, optional model/mock/API)
4. registers the native messaging manifest:
   `~/Library/Application Support/Mozilla/NativeMessagingHosts/local_ai_hub.json`
   pointing at `<project>/bin/local-ai-hub-host`, with
   `allowed_extensions: ["local-ai-hub@localaihub.dev"]`
5. runs the offline smoke test

Useful flags:

```bash
./install.sh --model qwen3:4b   # set the default text+vision model
./install.sh --mock             # offline mock backend, no Ollama needed
./install.sh --api              # enable the experimental localhost HTTP API
./install.sh --no-test
```

Keep the project folder where it is — the manifest stores an absolute path.
If you move it, re-run `./install.sh`.

## 2. Get a model

```bash
ollama pull qwen3.5:4b        # starter config's default
# or whatever you have / prefer:
ollama pull qwen3:4b
./install.sh --model qwen3:4b
```

Ollama does not need to be running: the hub starts `ollama serve` on demand and
unloads idle models. If you already run Ollama as an app/service, the hub simply
uses it (`started_by_hub: false`).

Vision tagging needs a vision-capable model. Set it separately if you like:

```json
"ollama": { "default_model": "qwen3:4b", "vision_model": "llava:7b" }
```

## 3. Load the extension

### Temporary (development)

1. `about:debugging#/runtime/this-firefox`
2. **Load Temporary Add-on…** → `extension/manifest.json`
3. Toolbar button → should read **connected**

Temporary add-ons vanish when Firefox restarts. `web-ext` makes this pleasant:

```bash
npm install --global web-ext
cd extension
web-ext run --firefox=nightly --keep-profile-changes
```

### Persistent (signed)

Firefox release/nightly builds require signed extensions for permanent install.
Options, in order of practicality:

1. **Unlisted self-signing** (recommended):
   ```bash
   npm install --global web-ext
   cd extension
   web-ext sign --channel=unlisted \
     --api-key=$AMO_JWT_ISSUER --api-secret=$AMO_JWT_SECRET
   ```
   Install the resulting `.xpi` from `about:addons` → gear → *Install Add-on From File*.
   The gecko id is already set, so the native manifest keeps matching.
2. **Developer Edition / Nightly with signing disabled**: set
   `xpinstall.signatures.required = false` in `about:config` (works on
   Developer Edition, ESR and unbranded builds; not on release).
3. **Enterprise policy** (`policies.json` → `ExtensionSettings`) for managed machines.

`make xpi` builds an unsigned `.xpi` you can submit to AMO or load in a
signature-disabled build.

## 4. Configure your first site

Either edit `config/domains/<domain>.json` (see `example-board.com.json`), or use
**Settings → Domains** in the extension. `news.ycombinator.com.json` is a working
demo you can try immediately: open a HN comment page and press **Alt+Shift+L**.

## 5. Verify

```bash
python3 scripts/doctor.py            # paths, runtimes, manifest, models, plugins
./bin/laihub status
make smoke                           # 15 end-to-end checks
```

In Firefox: **Settings → Diagnostics → Ping host / Copy diagnostics**.

## Logs

| Log | Location |
| --- | -------- |
| Hub | `~/Library/Application Support/LocalAIHub/logs/hub.log` |
| Ollama (when hub-started) | `~/Library/Application Support/LocalAIHub/logs/ollama.log` |
| Extension console | `about:debugging` → Inspect the extension |
| Content script | page devtools console (`[LocalAIHub]` prefix, enable Debug logging) |

## Troubleshooting

| Symptom | Fix |
| ------- | --- |
| Popup says *disconnected* | `./install.sh`, then check the manifest path exists and is executable |
| `No such native application local_ai_hub` | Manifest missing/misnamed; must be `local_ai_hub.json` in `NativeMessagingHosts` |
| `This extension does not have permission to use native application` | `allowed_extensions` must contain `local-ai-hub@localaihub.dev`; the loaded extension must have that gecko id |
| Host connects then dies instantly | Read `hub.log`; usually a Python path issue — set `runtime.python` to an absolute path |
| `model_unavailable` | Install Ollama, or `ollama pull <tag>`; check `ollama.binary` in `hub.json` |
| `runtime_missing` for JS plugins | `brew install node`, or set `runtime.node` |
| `message_too_large` | Lower `batch_size` / `max_items`, or reduce `imageMaxSize` |
| Tasks time out on first run | First model load is slow; raise `plugins.timeout_seconds` and the extension's task timeout |
| Nothing matches on my board | Selectors are site-specific — inspect the DOM and update `item_selector`/`fields` |
| Panel invisible | Some sites have aggressive z-index/CSS; use **Alt+Shift+H**, or the popup's *Run on this page* |

## Uninstall

```bash
./uninstall.sh            # unregister the native host, keep config
./uninstall.sh --purge    # also delete state, logs and user config
```

Remove the extension in `about:addons`. Ollama and your models are untouched.
