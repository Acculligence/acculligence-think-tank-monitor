#!/usr/bin/env python3
"""
Acculligence Top-20 Route Probe v0.1

Purpose:
- Validate route availability and parser compatibility.
- Discover alternate RSS/Atom links from official HTML pages.
- Produce compact CSV and JSON evidence for Route Decision Log.

This script does not collect full articles and does not activate routes.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from xml.etree import ElementTree as ET

USER_AGENT = (
    "AcculligenceRouteProbe/0.1 "
    "(route validation; contact: info@acculligence.com)"
)
MAX_BODY_BYTES = 8 * 1024 * 1024

@dataclass
class ProbeResult:
    source_id: str
    official_name: str
    domain: str
    route_key: str
    route_type: str
    proposed_role: str
    requested_url: str
    desk_status: str
    probed_at: str
    http_status: int | None = None
    final_url: str = ""
    content_type: str = ""
    elapsed_ms: int | None = None
    bytes_read: int = 0
    parser_type: str = ""
    entry_count: int | None = None
    latest_date: str = ""
    alternate_feeds: str = ""
    canonical_hint: str = ""
    content_sha256_16: str = ""
    runtime_status: str = "error"
    error: str = ""

def build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=2,
        connect=2,
        read=2,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, text/html;q=0.9, */*;q=0.5",
    })
    return session

def safe_date(value: Any) -> str:
    if not value:
        return ""
    try:
        dt = date_parser.parse(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return ""

def parse_feed(body: bytes) -> tuple[int, str]:
    parsed = feedparser.parse(body)
    dates = []
    for entry in parsed.entries:
        value = entry.get("published") or entry.get("updated") or entry.get("created")
        parsed_date = safe_date(value)
        if parsed_date:
            dates.append(parsed_date)
    return len(parsed.entries), max(dates) if dates else ""

def parse_sitemap(body: bytes) -> tuple[int, str]:
    root = ET.fromstring(body)
    ns_match = re.match(r"\{([^}]+)\}", root.tag)
    ns = {"sm": ns_match.group(1)} if ns_match else {}
    if ns:
        locs = root.findall(".//sm:loc", ns)
        lastmods = root.findall(".//sm:lastmod", ns)
    else:
        locs = root.findall(".//loc")
        lastmods = root.findall(".//lastmod")
    dates = [safe_date(x.text) for x in lastmods if x.text]
    dates = [x for x in dates if x]
    return len(locs), max(dates) if dates else ""

def parse_html(base_url: str, text: str) -> tuple[list[str], str, int]:
    soup = BeautifulSoup(text, "html.parser")
    feeds = []
    for link in soup.find_all("link", href=True):
        rel = " ".join(link.get("rel", []))
        typ = (link.get("type") or "").lower()
        if "alternate" in rel.lower() and ("rss" in typ or "atom" in typ or "xml" in typ):
            feeds.append(urljoin(base_url, link["href"]))
    canonical = ""
    can = soup.find("link", rel=lambda v: v and "canonical" in v)
    if can and can.get("href"):
        canonical = urljoin(base_url, can["href"])
    article_like = 0
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        text_value = " ".join(a.get_text(" ", strip=True).split())
        if len(text_value) >= 20 and href.startswith("http"):
            article_like += 1
    return sorted(set(feeds)), canonical, article_like

def probe_route(session: requests.Session, source: dict[str, Any], route: dict[str, Any], timeout: int) -> ProbeResult:
    result = ProbeResult(
        source_id=source["source_id"],
        official_name=source["official_name"],
        domain=source["domain"],
        route_key=route["route_key"],
        route_type=route["type"],
        proposed_role=route.get("proposed_role", ""),
        requested_url=route["url"],
        desk_status=route.get("desk_status", ""),
        probed_at=datetime.now(timezone.utc).isoformat(),
    )
    started = time.perf_counter()
    try:
        with session.get(route["url"], timeout=timeout, allow_redirects=True, stream=True) as response:
            result.http_status = response.status_code
            result.final_url = response.url
            result.content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            chunks = []
            total = 0
            for chunk in response.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                remaining = MAX_BODY_BYTES - total
                if remaining <= 0:
                    break
                chunks.append(chunk[:remaining])
                total += min(len(chunk), remaining)
            body = b"".join(chunks)
            result.bytes_read = len(body)
            result.content_sha256_16 = hashlib.sha256(body).hexdigest()[:16] if body else ""

        result.elapsed_ms = int((time.perf_counter() - started) * 1000)
        if result.http_status is None or result.http_status >= 400:
            result.runtime_status = "http_error"
            result.error = f"HTTP {result.http_status}"
            return result

        lower_url = result.final_url.lower()
        ctype = result.content_type

        if any(x in ctype for x in ("rss", "atom")):
            result.parser_type = "feed"
            result.entry_count, result.latest_date = parse_feed(body)
        elif "xml" in ctype or lower_url.endswith((".xml", ".rss", ".atom")):
            if b"<urlset" in body[:2000] or b"<sitemapindex" in body[:2000]:
                result.parser_type = "sitemap"
                result.entry_count, result.latest_date = parse_sitemap(body)
            else:
                result.parser_type = "feed_or_xml"
                result.entry_count, result.latest_date = parse_feed(body)
        elif "html" in ctype or body.lstrip().lower().startswith((b"<!doctype html", b"<html")):
            result.parser_type = "html"
            text = body.decode(response.encoding or "utf-8", errors="replace")
            alternate, canonical, article_like = parse_html(result.final_url, text)
            result.alternate_feeds = " | ".join(alternate)
            result.canonical_hint = canonical
            result.entry_count = article_like
        else:
            result.parser_type = "unknown"

        if result.parser_type in ("feed", "feed_or_xml") and (result.entry_count or 0) == 0:
            result.runtime_status = "parse_empty"
        elif result.parser_type == "unknown":
            result.runtime_status = "unsupported_content"
        else:
            result.runtime_status = "success"
        return result
    except Exception as exc:
        result.elapsed_ms = int((time.perf_counter() - started) * 1000)
        result.runtime_status = "exception"
        result.error = f"{type(exc).__name__}: {exc}"
        return result

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", required=True, type=Path)
    ap.add_argument("--output-dir", default=Path("route_probe_output"), type=Path)
    ap.add_argument("--timeout", default=25, type=int)
    ap.add_argument("--include-google-news", action="store_true")
    ap.add_argument("--max-routes", default=0, type=int, help="0 means all routes")
    args = ap.parse_args()

    if not args.registry.exists():
        print(f"Registry not found: {args.registry}", file=sys.stderr)
        return 2

    data = json.loads(args.registry.read_text(encoding="utf-8"))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    session = build_session()
    results: list[ProbeResult] = []

    route_counter = 0
    for source in data.get("top20_sources", []):
        for route in source.get("routes", []):
            if route.get("type") == "google_news" and not args.include_google_news:
                continue
            if args.max_routes and route_counter >= args.max_routes:
                break
            print(f"[probe] {source['domain']} | {route['route_key']} | {route['url']}")
            result = probe_route(session, source, route, args.timeout)
            results.append(result)
            route_counter += 1
        if args.max_routes and route_counter >= args.max_routes:
            break

    json_path = args.output_dir / "route_probe_results.json"
    csv_path = args.output_dir / "route_probe_results.csv"
    summary_path = args.output_dir / "route_probe_summary.json"

    json_path.write_text(
        json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    fieldnames = list(asdict(results[0]).keys()) if results else list(ProbeResult.__dataclass_fields__.keys())
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for result in results:
            writer.writerow(asdict(result))

    counts: dict[str, int] = {}
    for result in results:
        counts[result.runtime_status] = counts.get(result.runtime_status, 0) + 1
    summary = {
        "probed_at": datetime.now(timezone.utc).isoformat(),
        "registry": str(args.registry),
        "routes_probed": len(results),
        "status_counts": counts,
        "successful_sources": sorted({r.domain for r in results if r.runtime_status == "success"}),
        "failed_sources": sorted({r.domain for r in results if r.runtime_status not in ("success",)}),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
