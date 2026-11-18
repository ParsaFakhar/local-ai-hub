# Manifest V3 builds

**Status: implemented and validated** by `scripts/build_mv3.py` (Firefox and
Chrome targets). The MV2 extension in `extension/` stays the reference
implementation and is never modified — the builder *copies* it and rewrites only
what MV3 requires.

Everything above the manifest is unchanged: same content scripts, same allowlisted
applier, same userscript bridge, same protocol, same engine and plugins.

---

## 1. Build

```bash
python3 scripts/build_mv3.py                  # both targets (or: make mv3)
python3 scripts/build_mv3.py --target firefox
python3 scripts/build_mv3.py --target chrome --out /tmp/lah-chrome
python3 scripts/build_mv3.py --zip            # also write dist/mv3-<target>.zip
```

Outputs: `dist/mv3-firefox/` and `dist/mv3-chrome/`, each with its own
`manifest.json` and a generated `README.md`. `--out` requires `--target`.
Validation runs on every build (there is no way to skip it); `--check` is
accepted for explicitness. A failing check prints `✗ …` and exits non-zero.

`./scripts/check.sh` runs the MV3 builds as its last step, so a normal check run
also proves both MV3 variants are sound.

## 2. What the builder rewrites

| Item | MV2 (`extension/`) | MV3 |
| --- | --- | --- |
| `manifest_version` | 2 | 3 |
| Host patterns | inside `permissions` (`<all_urls>`) | moved to `host_permissions` |
| Toolbar | `browser_action` | `action` |
| Background (Firefox) | `scripts` + `persistent: true` | `scripts` (event page), `type: "classic"`, no `persistent` |
| Background (Chrome) | `scripts` | `service_worker: "service-worker.js"` |
| Script lists | as authored | `lib/browser.js` prepended to **every** list |
| Keep-alive | — | `alarms` permission added |
| `web_accessible_resources` | `[]` | `[]` (MV3 would want the object form; nothing is exposed) |
| Firefox settings | `gecko.id`, `strict_min_version` | `strict_min_version: "115.0"` |
| Chrome settings | — | `browser_specific_settings` removed, `minimum_chrome_version: "116"`, `menus` → `contextMenus`, at most **4** suggested keys |

`lib/browser.js` is the compatibility shim: it normalises `browser.*` /
`chrome.*`, `action` / `browserAction`, `menus` / `contextMenus`, and promisifies
callback-only methods, so the MV2 code runs unchanged in both browsers.

### The Chrome service worker

Chrome MV3 has no background page. The builder generates a **classic** service
worker whose only job is to `importScripts` the MV2 background files in their
original order, shim first:

```js
importScripts(
  'lib/browser.js',
  'lib/protocol.js',
  'background.js'
);
```

Do not edit `dist/` by hand — re-run the builder.

### Keyboard shortcuts

The MV2 manifest suggests seven shortcuts. Chrome allows at most four, so the
Chrome build keeps suggested keys for `lah-run-all` (Alt+Shift+L),
`lah-toggle-panel` (Alt+Shift+H), `lah-dry-run` (Alt+Shift+D) and
`lah-pick-selector` (Alt+Shift+P); the three `lah-run-feature-N` commands still
exist but ship without a default key. Assign them at
`chrome://extensions/shortcuts`. Firefox keeps all seven.

## 3. Load the Firefox MV3 build

Firefox 115+ (Nightly / Developer Edition recommended).

1. `./install.sh` once, from the project root — it registers the native host at
   `~/Library/Application Support/Mozilla/NativeMessagingHosts/local_ai_hub.json`
   with `allowed_extensions: ["local-ai-hub@localaihub.dev"]`. The MV3 build keeps
   that same gecko id, so **no re-registration is needed**.
2. `about:debugging#/runtime/this-firefox` → **Load Temporary Add-on…** → pick
   `dist/mv3-firefox/manifest.json`.
3. The toolbar button should say **connected**. Alt+Shift+L runs the enabled
   features, Alt+Shift+H toggles the panel — identical to MV2.

Firefox MV3 uses an **event page**: it still has a DOM, so image re-encoding uses
`document.createElement('canvas')`.

## 4. Load the Chrome MV3 build

Chrome / Edge / Brave 116+. `install.sh` only registers the Firefox host, so
Chrome needs one extra file.

1. Load the extension: `chrome://extensions` → **Developer mode** →
   **Load unpacked** → pick the `dist/mv3-chrome/` folder. Copy the extension
   **ID** shown on the card.
2. Write the native-host manifest (name must stay `local_ai_hub`, path must be
   absolute):

   ```bash
   DIR="$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts"
   mkdir -p "$DIR"
   cat > "$DIR/local_ai_hub.json" <<JSON
   {
     "name": "local_ai_hub",
     "description": "Local AI Hub native messaging host",
     "path": "$PWD/bin/local-ai-hub-host",
     "type": "stdio",
     "allowed_origins": ["chrome-extension://PASTE_YOUR_EXTENSION_ID/"]
   }
   JSON
   ```

   Run that from the project root so `$PWD` is correct, and keep the trailing
   slash in the origin. Chrome does **not** accept wildcards in
   `allowed_origins`, and it uses `allowed_origins` where Firefox uses
   `allowed_extensions` — the two manifests are separate files and can coexist.
3. Reload the extension. The popup should say **connected**.

Manifest locations on macOS (per-user; system-wide equivalents live under
`/Library/…`):

| Browser | Directory |
| --- | --- |
| Google Chrome | `~/Library/Application Support/Google/Chrome/NativeMessagingHosts/` |
| Chromium | `~/Library/Application Support/Chromium/NativeMessagingHosts/` |
| Chrome for Testing | `~/Library/Application Support/Google/ChromeForTesting/NativeMessagingHosts/` |

Other Chromium-based browsers use their own profile directory — check their
documentation for the exact path. On Linux the per-user directory is
`~/.config/google-chrome/NativeMessagingHosts/` (Chromium:
`~/.config/chromium/NativeMessagingHosts/`).

The extension ID changes if you load the folder from a different path or remove
and re-add it unpacked; update `allowed_origins` when that happens.

Chrome MV3 uses a **service worker**: no DOM, so image re-encoding uses
`OffscreenCanvas.convertToBlob()` instead of a canvas element.

## 5. Worker lifetime and keep-alive

* An open native-messaging port keeps the background alive while work is in
  flight; the engine also streams events over it.
* The background creates the `lah-keepalive` alarm with `periodInMinutes: 0.5`
  (every 30 s). On each tick it sweeps request deadlines whose `setTimeout` died
  with the worker, reconnects if the port is down (when auto-connect is on), and
  otherwise sends a `ping` over the port. The same code path runs under MV2.
* **In-flight requests do not survive a worker restart.** Requests whose timers
  were lost are rejected, the content script shows the failure, and the next run
  reconnects automatically. Re-run the feature; nothing is applied twice.

## 6. Message size limits

Native messaging frames are a 4-byte length prefix plus UTF-8 JSON in both
browsers, but the caps differ per browser:

| Direction | Chrome | Firefox |
| --- | --- | --- |
| host → browser | 1 MB | 1 MB |
| browser → host | 64 MiB | 4 GB |

The engine enforces the common denominator in `hub/protocol.py`
(`MAX_OUTGOING_BYTES = 1 MiB`, `MAX_INCOMING_BYTES = 64 MiB`) and answers
`message_too_large` instead of letting the browser kill the port. In practice
this limits **results**, not inputs: keep base64 image batches and large result
sets small (`batch_size`, image `max_size`).

## 7. What validation checks

`validate()` in `scripts/build_mv3.py` fails the build if: `manifest_version` is
not 3; `browser_action` or `background.persistent` survived; host patterns are
still in `permissions`; `alarms` is missing; the Chrome service worker is missing,
imports nothing, or imports something before `lib/browser.js`; any script list
(background, `importScripts`, each content script) does not start with the shim;
a referenced script or icon does not exist; Chrome kept
`browser_specific_settings`, `menus`, or more than four suggested keys. Finally it
runs `node --check` on every generated `.js` file when Node is available.

## 8. Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| Popup stays **disconnected** (Chrome) | host manifest missing/misnamed, non-absolute `path`, or the extension ID in `allowed_origins` does not match |
| Firefox: host not found | `./install.sh` not run, or gecko id changed from `local-ai-hub@localaihub.dev` |
| Connects, then drops after ~30 s idle | expected worker suspension; the keep-alive alarm reconnects. If it does not, check that `alarms` is in the manifest |
| Images fail only in Chrome | `OffscreenCanvas` path — check the worker console (`chrome://extensions` → Inspect views: service worker) |
| A shortcut does nothing in Chrome | only four suggested keys survive; set the rest at `chrome://extensions/shortcuts` |
| `message_too_large` | result exceeds 1 MiB; reduce batch size or image dimensions |

Chrome logs native-messaging failures to its own stderr; launching it with
`--enable-logging=stderr --log-level=1` shows them.

## 9. Validation status

Both targets build and pass validation, and all JavaScript passes `node --check`
(see the test ledger in `for LLM/STATE.md`). The MV3 builds have **not** been
exercised in a real Chrome or Firefox browser by the maintainers' CI — there is
no browser in the test environment. Report browser-specific findings with the
browser version and the worker/background console output.
