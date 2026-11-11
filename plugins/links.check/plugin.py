#!/usr/bin/env python3
"""links.check — offline link audit (no network, no model).

Input : {"links": [{"id": "l0", "href": "...", "text": "..."}], "page_url": "..."}
Output: {"results": [{"id", "href", "normalized", "kind", "domain", "external",
                      "risk", "issues", "duplicate_of", "status"}], "summary": {...}}

Why no fetching?  A plugin is a local program that must not turn a page visit
into a burst of outbound requests (tracking, SSRF, rate limits).  The audit is
therefore purely static.  If *you* probed the URLs elsewhere — a CLI script,
a crawler you wrote — pass the results in as ``statuses``:

    {"statuses": {"l3": {"status": 404}, "l7": {"status": 301}}}

and they are folded into the report.  Nothing here opens a socket.
"""

from __future__ import annotations

import re
from collections import Counter
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from local_ai_hub import Plugin, PluginError

ISSUE_WEIGHTS = {
    "script_url": 1.0,
    "data_url": 0.6,
    "file_url": 0.5,
    "userinfo_in_url": 0.8,
    "text_domain_mismatch": 0.8,
    "punycode_host": 0.5,
    "ip_literal_host": 0.4,
    "insecure_scheme": 0.3,
    "mixed_content": 0.3,
    "shortener": 0.3,
    "dead_link": 0.9,
    "server_error": 0.6,
    "redirect": 0.1,
    "missing_noopener": 0.2,
    "tracking_params": 0.1,
    "many_subdomains": 0.1,
    "very_long_url": 0.1,
    "empty_href": 0.2,
    "not_allowlisted": 0.3,
    "duplicate": 0.0,
    "non_http_scheme": 0.0,
}

URL_IN_TEXT = re.compile(r"\b(?:https?://)?([a-z0-9-]+(?:\.[a-z0-9-]+)+)(?:/[^\s]*)?", re.IGNORECASE)
IP_HOST = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def host_of(url: str, treat_www_same: bool) -> str:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    if treat_www_same and host.startswith("www."):
        host = host[4:]
    return host


def registrable(host: str) -> str:
    """Cheap eTLD+1 approximation (no public-suffix list, no network)."""
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    two_level = {"co.uk", "org.uk", "ac.uk", "com.au", "co.jp", "com.br", "co.nz", "com.tr"}
    if ".".join(parts[-2:]) in two_level:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def normalize(url: str, tracking: set[str], strip_tracking: bool) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    query = parse_qsl(parts.query, keep_blank_values=True)
    if strip_tracking:
        query = [(key, value) for key, value in query if key.lower() not in tracking]
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def classify(href: str, page_host: str, treat_www_same: bool) -> tuple[str, str, bool]:
    lowered = href.strip().lower()
    if not href.strip() or lowered in ("#", "javascript:void(0)", "about:blank"):
        return "empty", "", False
    if lowered.startswith("#"):
        return "anchor", "", False
    for scheme, kind in (("mailto:", "mailto"), ("tel:", "tel"), ("sms:", "tel"),
                         ("javascript:", "script"), ("data:", "data"), ("file:", "file"),
                         ("blob:", "blob"), ("ftp:", "non_http")):
        if lowered.startswith(scheme):
            return kind, "", False
    host = host_of(href, treat_www_same)
    if not host:
        return "unknown", "", False
    if not lowered.startswith(("http://", "https://")):
        return "non_http", host, False
    external = bool(page_host) and registrable(host) != registrable(page_host)
    return ("external" if external else "internal"), host, external


def audit(link: dict, page_url: str, config: dict, statuses: dict) -> dict:
    identifier = str(link.get("id", ""))
    href = str(link.get("href", ""))
    text = str(link.get("text", ""))
    treat_www_same = bool(config.get("treat_www_same", True))
    tracking = {str(name).lower() for name in config.get("tracking_params", [])}
    shorteners = {str(name).lower() for name in config.get("shorteners", [])}
    allowlist = {host_of(str(entry), treat_www_same) for entry in config.get("allowlist_domains", [])}
    allowlist = {registrable(host) for host in allowlist if host}

    absolute = urljoin(page_url, href) if page_url else href
    page_host = host_of(page_url, treat_www_same)
    kind, host, external = classify(absolute, page_host, treat_www_same)
    issues: list[str] = []

    if kind == "empty":
        issues.append("empty_href")
    if kind == "script":
        issues.append("script_url")
    if kind == "data":
        issues.append("data_url")
    if kind == "file":
        issues.append("file_url")
    if kind == "non_http":
        issues.append("non_http_scheme")

    try:
        parts = urlsplit(absolute)
    except ValueError:
        parts = None

    if parts is not None and parts.scheme in ("http", "https"):
        if parts.username or parts.password:
            issues.append("userinfo_in_url")
        if parts.scheme == "http":
            issues.append("insecure_scheme")
            if page_url.startswith("https://") and config.get("flag_mixed_content", True):
                issues.append("mixed_content")
        if host.startswith("xn--") or ".xn--" in host:
            issues.append("punycode_host")
        if IP_HOST.match(host) or ":" in host:  # IPv4 literal, or IPv6 (urlsplit strips the brackets)
            issues.append("ip_literal_host")
        if host in shorteners or registrable(host) in shorteners:
            issues.append("shortener")
        if len([part for part in host.split(".") if part]) > 4:
            issues.append("many_subdomains")
        if len(absolute) > int(config.get("max_url_length", 512)):
            issues.append("very_long_url")
        keys = {key.lower() for key, _ in parse_qsl(parts.query, keep_blank_values=True)}
        if keys & tracking:
            issues.append("tracking_params")
        if (
            config.get("flag_missing_noopener", True)
            and str(link.get("target", "")).lower() == "_blank"
            and "noopener" not in str(link.get("rel", "")).lower()
        ):
            issues.append("missing_noopener")

    # The anchor text promises one destination, the href points at another.
    match = URL_IN_TEXT.search(text)
    if match and host:
        claimed = match.group(1).lower().removeprefix("www.")
        if "." in claimed and registrable(claimed) != registrable(host):
            issues.append("text_domain_mismatch")

    if allowlist and external and registrable(host) not in allowlist:
        issues.append("not_allowlisted")

    status = None
    entry = statuses.get(identifier)
    if isinstance(entry, dict):
        entry = entry.get("status")
    if isinstance(entry, (int, float)) and not isinstance(entry, bool):
        status = int(entry)
        # 5xx first: a server error is transient, a 4xx is a dead target.
        if status >= 500:
            issues.append("server_error")
        elif status >= 400:
            issues.append("dead_link")
        elif 300 <= status < 400:
            issues.append("redirect")

    risk = min(1.0, round(sum(ISSUE_WEIGHTS.get(issue, 0.05) for issue in issues), 3))
    return {
        "id": identifier,
        "href": href,
        "normalized": normalize(absolute, tracking, bool(config.get("strip_tracking", True))),
        "kind": kind,
        "domain": host,
        "external": external,
        "risk": risk,
        "issues": issues,
        **({"status": status} if status is not None else {}),
    }


def main(task, hub):
    links = task.input.get("links")
    if not isinstance(links, list):
        raise PluginError("'links' must be an array", "invalid_input")

    config = task.config
    page_url = str(task.input.get("page_url") or task.context.get("url") or "")
    statuses = task.input.get("statuses") or {}
    if not isinstance(statuses, dict):
        statuses = {}
    threshold = float(config.get("risk_threshold", 0.4))

    results = []
    seen: dict[str, str] = {}
    for index, link in enumerate(links):
        if not isinstance(link, dict):
            continue
        entry = audit({**link, "id": str(link.get("id") or f"l{index}")}, page_url, config, statuses)
        key = entry["normalized"]
        if key and key in seen:
            entry["duplicate_of"] = seen[key]
            entry["issues"] = entry["issues"] + ["duplicate"]
        elif key:
            seen[key] = entry["id"]
        results.append(entry)
        if index % 50 == 0:
            hub.progress(index / max(1, len(links)), f"audited {index}/{len(links)} links")

    issue_counts = Counter(issue for entry in results for issue in entry["issues"])
    flagged = [entry for entry in results if entry["risk"] >= threshold and entry["issues"]]
    hub.progress(1.0, "link audit complete")
    return {
        "results": results,
        "summary": {
            "total": len(results),
            "kinds": dict(Counter(entry["kind"] for entry in results)),
            "external": sum(1 for entry in results if entry["external"]),
            "unique_targets": len(seen),
            "duplicates": sum(1 for entry in results if entry.get("duplicate_of")),
            "issues": dict(issue_counts),
            "flagged": len(flagged),
            "flagged_ids": [entry["id"] for entry in flagged][:100],
            "domains": sorted({entry["domain"] for entry in results if entry["domain"]})[:100],
            "network_requests": 0,
        },
    }


if __name__ == "__main__":
    Plugin(main).run()
