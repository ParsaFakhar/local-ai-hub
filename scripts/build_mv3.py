#!/usr/bin/env python3
"""Build Manifest V3 variants of the extension (stdlib only).

    python3 scripts/build_mv3.py                  # both targets
    python3 scripts/build_mv3.py --target firefox
    python3 scripts/build_mv3.py --check          # build + validate, no zip
    python3 scripts/build_mv3.py --zip            # also write .zip packages

Outputs
-------
    dist/mv3-firefox/   MV3 event page   (Firefox 115+)
    dist/mv3-chrome/    MV3 service worker (Chrome/Edge 116+)

The MV2 extension in ``extension/`` stays the reference implementation and is
not modified: this script *copies* it and rewrites only what MV3 requires.

What changes
------------
* ``manifest_version: 3``; host patterns move to ``host_permissions``
* ``browser_action`` → ``action``
* background: Firefox keeps ``scripts`` (event page, no ``persistent`` key);
  Chrome gets a ``service_worker`` bootstrap that ``importScripts`` the same
  files in the same order
* ``lib/browser.js`` (the browser/chrome compatibility shim) is prepended to
  every script list, so ``browser.*`` and promise-style calls work everywhere
* ``alarms`` permission for the native-port keep-alive
* Chrome: at most four suggested keyboard shortcuts (its hard limit), no
  ``browser_specific_settings``, ``minimum_chrome_version``
* ``web_accessible_resources`` becomes the MV3 object form (still empty — the
  hub exposes nothing to pages)

Everything else — content scripts, applier allowlist, bridge policy, UI — is
byte-identical to the MV2 build.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "extension"
DIST = ROOT / "dist"
SHIM = "lib/browser.js"
TARGETS = ("firefox", "chrome")

#: Chrome allows at most four commands with a suggested key.
CHROME_SUGGESTED_KEY_LIMIT = 4
MINIMUM_CHROME_VERSION = "116"
MINIMUM_GECKO_VERSION = "115.0"

SKIP_NAMES = {".DS_Store"}


def load_manifest() -> dict:
    return json.loads((SOURCE / "manifest.json").read_text(encoding="utf-8"))


def copy_tree(target: Path) -> list[Path]:
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    copied = []
    for path in sorted(SOURCE.rglob("*")):
        if not path.is_file() or path.name in SKIP_NAMES:
            continue
        relative = path.relative_to(SOURCE)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        copied.append(destination)
    return copied


def with_shim(scripts: list[str]) -> list[str]:
    return [SHIM] + [name for name in scripts if name != SHIM]


def split_permissions(manifest: dict) -> tuple[list[str], list[str]]:
    """MV3 separates API permissions from host permissions."""
    api, hosts = [], []
    for entry in manifest.get("permissions", []):
        (hosts if "<" in entry or "://" in entry or entry in ("*/*",) else api).append(entry)
    for entry in manifest.get("host_permissions", []):
        if entry not in hosts:
            hosts.append(entry)
    if "alarms" not in api:
        api.append("alarms")
    return api, hosts


def mv3_manifest(base: dict, target: str) -> dict:
    manifest = json.loads(json.dumps(base))  # deep copy, keep key order
    manifest["manifest_version"] = 3
    api, hosts = split_permissions(manifest)
    manifest["permissions"] = api
    manifest["host_permissions"] = hosts

    if "browser_action" in manifest:
        manifest["action"] = manifest.pop("browser_action")

    background = manifest.get("background", {})
    scripts = with_shim(list(background.get("scripts", [])))
    if target == "firefox":
        # Firefox MV3 runs an event page: same scripts, no "persistent" flag.
        manifest["background"] = {"scripts": scripts, "type": "classic"}
    else:
        manifest["background"] = {"service_worker": "service-worker.js"}

    for entry in manifest.get("content_scripts", []):
        entry["js"] = with_shim(list(entry.get("js", [])))

    # Nothing is exposed to webpages; MV3 wants the object form regardless.
    manifest["web_accessible_resources"] = []

    if target == "firefox":
        settings = manifest.setdefault("browser_specific_settings", {}).setdefault("gecko", {})
        settings["strict_min_version"] = MINIMUM_GECKO_VERSION
    else:
        manifest.pop("browser_specific_settings", None)
        manifest["minimum_chrome_version"] = MINIMUM_CHROME_VERSION
        # Chrome spells the context-menu permission differently; the shim
        # exposes it as browser.menus either way.
        manifest["permissions"] = [
            "contextMenus" if name == "menus" else name for name in manifest["permissions"]
        ]
        commands = manifest.get("commands", {})
        kept = 0
        for command in commands.values():
            if "suggested_key" not in command:
                continue
            kept += 1
            if kept > CHROME_SUGGESTED_KEY_LIMIT:
                command.pop("suggested_key", None)
    return manifest


SERVICE_WORKER = """/**
 * Local AI Hub — MV3 service-worker bootstrap (generated by scripts/build_mv3.py).
 *
 * A classic service worker may importScripts, which lets the MV2 background
 * files run unchanged, in the same order, with the same globals.
 * Do not edit: re-run the builder instead.
 */
'use strict';

importScripts(
{imports}
);
"""


def write_service_worker(target: Path, scripts: list[str]) -> None:
    imports = ",\n".join(f"  '{name}'" for name in scripts)
    (target / "service-worker.js").write_text(
        SERVICE_WORKER.format(imports=imports), encoding="utf-8"
    )


def service_worker_imports(source: str) -> list[str]:
    """The importScripts() list of a generated bootstrap, in order.

    Only the generated form is understood (one quoted path per line); anything
    else yields an empty list so validation reports it instead of guessing.
    """
    match = re.search(r"importScripts\((.*?)\);", source, re.DOTALL)
    if not match:
        return []
    return re.findall(r"""['"]([^'"]+)['"]""", match.group(1))


def build_readme(target: str, manifest: dict) -> str:
    name = "Firefox (Nightly / Developer Edition / 115+)" if target == "firefox" else "Chrome / Edge / Brave 116+"
    load = (
        "about:debugging#/runtime/this-firefox → Load Temporary Add-on… → pick `manifest.json`"
        if target == "firefox"
        else "chrome://extensions → Developer mode → Load unpacked → pick this folder"
    )
    worker = (
        "Firefox MV3 uses an **event page**: it has a DOM, so image re-encoding uses "
        "`document.createElement('canvas')`."
        if target == "firefox"
        else "Chrome MV3 uses a **service worker**: there is no DOM, so image re-encoding "
        "uses `OffscreenCanvas.convertToBlob()`."
    )
    return f"""# Local AI Hub — MV3 build ({target})

Generated by `python3 scripts/build_mv3.py --target {target}`. Do not edit by hand.

* Target: {name}
* Version: {manifest['version']}
* Background: {'event page (`background.scripts`)' if target == 'firefox' else 'service worker (`service-worker.js`)'}

## Load it

1. Install the native host once from the project root: `./install.sh`
2. {load}
3. The toolbar button should say **connected**; the rest of the workflow is
   identical to the MV2 build (Alt+Shift+L to run, Alt+Shift+H for the panel).

## What is different from MV2

* `lib/browser.js` normalises `browser.*` / `chrome.*`, `action` /
  `browserAction`, `menus` / `contextMenus` and callback-only methods.
* {worker}
* The background is suspended when idle. An open native-messaging port keeps
  it alive; the `lah-keepalive` alarm (every 30 s) reconnects after a
  suspension and rejects requests whose timers were lost.
* In-flight requests do not survive a worker restart: the content script shows
  the failure and the next run reconnects automatically.

The engine, protocol, plugins and security model are unchanged — see
`docs/MV3.md` in the project root.
"""


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def validate(target: Path, manifest: dict, browser: str) -> list[str]:
    problems: list[str] = []

    if manifest.get("manifest_version") != 3:
        problems.append("manifest_version is not 3")
    if "browser_action" in manifest:
        problems.append("MV2 browser_action survived the rewrite")
    if "persistent" in manifest.get("background", {}):
        problems.append("MV3 background must not declare 'persistent'")
    if any("<" in entry or "://" in entry for entry in manifest.get("permissions", [])):
        problems.append("host patterns must live in host_permissions")
    if "alarms" not in manifest.get("permissions", []):
        problems.append("alarms permission missing (keep-alive would fail)")

    background = manifest.get("background", {})
    worker_imports: list[str] = []
    if browser == "chrome":
        worker = background.get("service_worker")
        if not worker or not (target / worker).is_file():
            problems.append("service_worker file is missing")
        else:
            # The bootstrap *is* the entry point, so it is legitimately first.
            # What must start with the shim is its importScripts() list.
            worker_imports = service_worker_imports((target / worker).read_text(encoding="utf-8"))
            if not worker_imports:
                problems.append(f"{worker} does not importScripts anything")
            elif worker_imports[0] != SHIM:
                problems.append(f"{worker} imports {worker_imports[0]} before {SHIM}")
        if "browser_specific_settings" in manifest:
            problems.append("Chrome rejects browser_specific_settings")
        if "menus" in manifest.get("permissions", []):
            problems.append("Chrome needs 'contextMenus', not 'menus'")
        suggested = sum(1 for c in manifest.get("commands", {}).values() if "suggested_key" in c)
        if suggested > CHROME_SUGGESTED_KEY_LIMIT:
            problems.append(f"{suggested} suggested keys (Chrome allows {CHROME_SUGGESTED_KEY_LIMIT})")
        scripts = ([worker] if worker else []) + worker_imports
    else:
        scripts = list(background.get("scripts", []))
        if not scripts:
            problems.append("Firefox MV3 needs background.scripts")

    for entry in manifest.get("content_scripts", []):
        scripts += list(entry.get("js", []))
    for name in dict.fromkeys(scripts):
        if not name:
            continue
        if not (target / name).is_file():
            problems.append(f"script not found: {name}")

    # Every *script list* the browser loads in order must begin with the shim.
    groups = [
        ("background.scripts", manifest.get("background", {}).get("scripts", [])),
        ("service-worker importScripts", worker_imports),
        *[
            (f"content_scripts[{index}].js", entry.get("js", []))
            for index, entry in enumerate(manifest.get("content_scripts", []))
        ],
    ]
    for label, group in groups:
        if group and group[0] != SHIM:
            problems.append(f"{label} does not start with {SHIM}: {group}")

    for icon in manifest.get("icons", {}).values():
        if not (target / icon).is_file():
            problems.append(f"icon missing: {icon}")

    node = shutil.which("node")
    if node:
        for path in sorted(target.rglob("*.js")):
            result = subprocess.run(
                [node, "--check", str(path)], capture_output=True, text=True, check=False
            )
            if result.returncode:
                problems.append(f"syntax error in {path.name}: {result.stderr.strip()[:200]}")
    return problems


def zip_target(target: Path) -> Path:
    archive_path = target.with_suffix(".zip")
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(target.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(target))
    return archive_path


def build(target: str, output: Path | None = None) -> tuple[Path, dict, list[str]]:
    base = load_manifest()
    destination = output or (DIST / f"mv3-{target}")
    copy_tree(destination)
    manifest = mv3_manifest(base, target)
    if target == "chrome":
        write_service_worker(destination, with_shim(list(base.get("background", {}).get("scripts", []))))
    (destination / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (destination / "README.md").write_text(build_readme(target, manifest), encoding="utf-8")
    return destination, manifest, validate(destination, manifest, target)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="build_mv3", description=__doc__.splitlines()[0])
    parser.add_argument("--target", choices=TARGETS, help="build only one target")
    parser.add_argument("--out", type=Path, help="output directory (single target only)")
    parser.add_argument("--zip", action="store_true", help="also write a .zip per target")
    parser.add_argument("--check", action="store_true", help="validate the builds (default)")
    args = parser.parse_args(argv)

    targets = (args.target,) if args.target else TARGETS
    if args.out and len(targets) != 1:
        parser.error("--out requires --target")

    failed = False
    for target in targets:
        destination, manifest, problems = build(target, args.out)
        label = destination.relative_to(ROOT) if destination.is_relative_to(ROOT) else destination
        print(f"{label}  (MV3, {manifest['version']}, {target})")
        if problems:
            failed = True
            for problem in problems:
                print(f"  ✗ {problem}")
        else:
            print("  ✓ manifest, scripts and syntax validated")
        if args.zip:
            archive = zip_target(destination)
            print(f"  → {archive.name} ({archive.stat().st_size / 1024:.1f} KiB)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
