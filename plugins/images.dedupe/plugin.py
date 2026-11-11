#!/usr/bin/env python3
"""images.dedupe — find reposts with perceptual hashing (no model, no network).

Input : {"images": [{"id": "i1", "data_base64": "..."}],
         "reference_hashes": [{"id": "seen-1", "hash": "9f3c..."}]}
Output: {"results": [{"id", "hash", "duplicate_of", "distance", "group",
                      "keeper", ...}],
         "groups":  [{"keeper", "members", "size", "max_distance"}],
         "summary": {...}}

Two images are "the same" when the Hamming distance between their perceptual
hashes is within ``threshold`` bits.  That survives re-encoding, resizing,
slight crops, watermark tweaks and brightness changes — exactly what a repost
looks like — while a genuinely different picture lands ~32 bits away.

Everything happens locally: pixels come in as base64 (the interface fetches
them), the decoder is pure Python (``imaging.py``), and no model is involved.
``reference_hashes`` lets a caller carry known hashes across pages without the
plugin keeping any state of its own.
"""

from __future__ import annotations

import base64
import binascii
import re

from local_ai_hub import Plugin, PluginError

import imaging

_DATA_URL = re.compile(r"^data:[^;]+;base64,", re.IGNORECASE)

ALGORITHMS = {
    "phash": lambda image, size, hash_size: imaging.perceptual_hash(image, dct_size(size), hash_size),
    "dhash": lambda image, size, hash_size: imaging.difference_hash(image, hash_size),
    "ahash": lambda image, size, hash_size: imaging.average_hash(image, hash_size),
}


def dct_size(configured: int) -> int:
    """pHash works on a square grid; keep it a sane power-of-two-ish size."""
    return max(8, min(64, int(configured)))


def decode_pixels(value: str) -> bytes:
    text = _DATA_URL.sub("", str(value or "")).strip()
    if not text:
        raise ValueError("no_image_data: the interface did not supply base64 pixels")
    try:
        return base64.b64decode(text, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"invalid base64 image data: {exc}") from exc


def hash_image(image: dict, algorithm: str, hash_size: int, size: int) -> dict:
    raw = decode_pixels(image.get("data_base64"))
    gray = imaging.decode(raw)
    value = ALGORITHMS[algorithm](gray, size, hash_size)
    return {
        "hash": imaging.hash_hex(value, hash_size * hash_size),
        "value": value,
        "width": gray.w,
        "height": gray.h,
        "bytes": len(raw),
    }


class _Union:
    """Tiny union-find so duplicate clusters are transitive (a~b, b~c ⇒ a~c)."""

    def __init__(self, count: int):
        self.parent = list(range(count))

    def find(self, index: int) -> int:
        while self.parent[index] != index:
            self.parent[index] = self.parent[self.parent[index]]
            index = self.parent[index]
        return index

    def join(self, left: int, right: int) -> None:
        a, b = self.find(left), self.find(right)
        if a != b:
            self.parent[max(a, b)] = min(a, b)


def main(task, hub):
    images = task.input.get("images")
    if not isinstance(images, list):
        raise PluginError("'images' must be an array", "invalid_input")

    algorithm = str(task.input.get("algorithm") or task.option("algorithm", "phash")).lower()
    if algorithm not in ALGORITHMS:
        raise PluginError(
            f"unknown algorithm {algorithm!r}; use one of {', '.join(sorted(ALGORITHMS))}",
            "invalid_input",
        )
    hash_size = int(task.option("hash_size", 8))
    hash_size = max(4, min(16, hash_size))
    bits = hash_size * hash_size
    size = dct_size(task.option("dct_size", 32))
    threshold = int(task.input.get("threshold") if task.input.get("threshold") is not None
                    else task.option("threshold", 6))
    threshold = max(0, min(bits, threshold))
    keep = str(task.option("keep", "first")).lower()
    max_images = int(task.option("max_images", 200))

    references = []
    for entry in task.input.get("reference_hashes") or []:
        if not isinstance(entry, dict) or not entry.get("hash"):
            continue
        try:
            references.append(
                {
                    "id": str(entry.get("id") or f"ref{len(references)}"),
                    "value": imaging.hash_from_hex(entry["hash"]),
                }
            )
        except ValueError as exc:
            hub.log(f"ignoring reference hash: {exc}", "warning")

    selected = images[:max_images]
    if len(images) > max_images:
        hub.log(f"{len(images)} images supplied, hashing the first {max_images}", "warning")

    results: list[dict] = []
    hashed: list[dict] = []
    total = len(selected) or 1
    for index, image in enumerate(selected):
        if not isinstance(image, dict):
            continue
        identifier = str(image.get("id") or f"i{index}")
        hub.progress(index / total, f"hashing image {index + 1}/{len(selected)}")
        entry = {"id": identifier, "algorithm": algorithm}
        if image.get("filename"):
            entry["filename"] = str(image["filename"])[:300]
        try:
            info = hash_image(image, algorithm, hash_size, size)
        except (ValueError, imaging.UnsupportedImage) as exc:
            entry.update({"hash": "", "duplicate_of": "", "distance": -1, "error": str(exc)[:300]})
            results.append(entry)
            continue
        entry.update(
            {
                "hash": info["hash"],
                "width": info["width"],
                "height": info["height"],
                "bytes": info["bytes"],
                "duplicate_of": "",
                "distance": 0,
            }
        )
        entry["_value"] = info["value"]
        results.append(entry)
        hashed.append(entry)

    # ---- reference comparison (reposts seen on earlier pages) -------------
    for entry in hashed:
        best = None
        for reference in references:
            distance = imaging.hamming(entry["_value"], reference["value"])
            if distance <= threshold and (best is None or distance < best[1]):
                best = (reference["id"], distance)
        if best:
            entry["duplicate_of"] = best[0]
            entry["distance"] = best[1]
            entry["reference"] = True

    # ---- pairwise clustering ---------------------------------------------
    union = _Union(len(hashed))
    distances: dict[tuple[int, int], int] = {}
    for i in range(len(hashed)):
        for j in range(i + 1, len(hashed)):
            distance = imaging.hamming(hashed[i]["_value"], hashed[j]["_value"])
            if distance <= threshold:
                union.join(i, j)
                distances[(i, j)] = distance

    clusters: dict[int, list[int]] = {}
    for index in range(len(hashed)):
        clusters.setdefault(union.find(index), []).append(index)

    groups = []
    for root, members in clusters.items():
        if len(members) < 2:
            continue
        if keep == "largest":
            keeper = max(members, key=lambda i: (hashed[i]["width"] * hashed[i]["height"], -i))
        elif keep == "smallest":
            keeper = min(members, key=lambda i: (hashed[i]["bytes"], i))
        else:
            keeper = min(members)
        worst = 0
        for position, index in enumerate(members):
            entry = hashed[index]
            entry["group"] = hashed[keeper]["id"]
            entry["keeper"] = index == keeper
            if index != keeper and not entry.get("reference"):
                entry["duplicate_of"] = hashed[keeper]["id"]
                pair = (min(index, keeper), max(index, keeper))
                entry["distance"] = distances.get(pair, imaging.hamming(entry["_value"], hashed[keeper]["_value"]))
            worst = max(worst, entry.get("distance") or 0)
        groups.append(
            {
                "keeper": hashed[keeper]["id"],
                "members": [hashed[index]["id"] for index in members],
                "size": len(members),
                "max_distance": worst,
            }
        )

    for entry in hashed:
        entry.setdefault("group", entry["id"])
        entry.setdefault("keeper", True)
        entry.pop("_value", None)

    groups.sort(key=lambda group: (-group["size"], group["keeper"]))
    duplicates = sum(1 for entry in results if entry.get("duplicate_of"))
    failed = sum(1 for entry in results if entry.get("error"))
    hub.progress(1.0, f"{duplicates} duplicate(s) in {len(groups)} group(s)")
    hub.log(
        f"{len(hashed)} hashed, {duplicates} duplicate(s), {failed} unreadable, "
        f"algorithm={algorithm} threshold={threshold}"
    )
    return {
        "results": results,
        "groups": groups,
        "summary": {
            "total": len(results),
            "hashed": len(hashed),
            "duplicates": duplicates,
            "unique": len(hashed) - sum(1 for entry in hashed if entry.get("duplicate_of")),
            "groups": len(groups),
            "failed": failed,
            "references": len(references),
            "algorithm": algorithm,
            "threshold": threshold,
            "hash_bits": bits,
            "truncated": len(images) > max_images,
        },
    }


if __name__ == "__main__":
    Plugin(main).run()
