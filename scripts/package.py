#!/usr/bin/env python3
"""Build distributables (stdlib only — no `zip` binary required).

    python3 scripts/package.py               # project zip + extension xpi
    python3 scripts/package.py --zip-only
    python3 scripts/package.py --xpi-only

Outputs:
    dist/local-ai-hub-<version>.zip            full platform (install this)
    dist/local-ai-hub-extension-<version>.xpi  extension only (AMO / about:addons)
"""

from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"

EXCLUDE_DIRS = {
    ".git", ".github", "__pycache__", ".venv", "venv", "node_modules",
    "dist", "build", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".DS_Store",
}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".log", ".zip", ".xpi"}
EXCLUDE_NAMES = {".DS_Store"}


def included(path: Path) -> bool:
    if any(part in EXCLUDE_DIRS for part in path.relative_to(ROOT).parts):
        return False
    if path.name in EXCLUDE_NAMES:
        return False
    if path.suffix in EXCLUDE_SUFFIXES:
        return False
    return path.is_file()


def version() -> str:
    return json.loads((ROOT / "extension" / "manifest.json").read_text())["version"]


def build_zip(target: Path) -> tuple[int, int]:
    files = sorted(p for p in ROOT.rglob("*") if included(p))
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            archive.write(path, Path("local-ai-hub") / path.relative_to(ROOT))
    return len(files), target.stat().st_size


def build_xpi(target: Path) -> tuple[int, int]:
    extension = ROOT / "extension"
    files = sorted(p for p in extension.rglob("*") if p.is_file() and p.name not in EXCLUDE_NAMES)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            archive.write(path, path.relative_to(extension))  # manifest.json at the root
    return len(files), target.stat().st_size


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--zip-only", action="store_true")
    parser.add_argument("--xpi-only", action="store_true")
    args = parser.parse_args()

    DIST.mkdir(parents=True, exist_ok=True)
    ver = version()
    both = not (args.zip_only or args.xpi_only)

    if both or args.zip_only:
        target = DIST / f"local-ai-hub-{ver}.zip"
        count, size = build_zip(target)
        print(f"{target.relative_to(ROOT)}  ({count} files, {size / 1024:.1f} KiB)")
    if both or args.xpi_only:
        target = DIST / f"local-ai-hub-extension-{ver}.xpi"
        count, size = build_xpi(target)
        print(f"{target.relative_to(ROOT)}  ({count} files, {size / 1024:.1f} KiB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
