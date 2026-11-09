#!/usr/bin/env python3
"""Enrich canonical WeChat Mini App metadata with rating/url from WeWePlay pages.

The local WeChat search/catalog export has empty rating/url fields. This script
crawls public WeWePlay detail pages listed in sitemap.xml, extracts structured
metadata where available, matches by appid first and normalized name second, and
writes enriched canonical CSVs without fabricating missing values.
"""
from __future__ import annotations

import csv
import html
import json
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = Path(os.environ.get("WECHAT_DATA_DIR", str(ROOT.parent / "collection_results" / "initial"))) / "all_mini_apps"
SITEMAP = "https://www.weweplay.com/sitemap.xml"
USER_AGENT = "Mozilla/5.0 (compatible; WeChatResearchBot/1.0; +https://github.com/luca959/WeChatMiniAppScraper)"
WX_APPID_RE = re.compile(r"wx[a-fA-F0-9]{16}")
TAG_RE = re.compile(r"<[^>]+>")
DETAIL_RE = re.compile(r"https://www\.weweplay\.com/home/we/\d+\.html")

REQUESTED_FIELDS = [
    "csv_id", "doc_id", "name", "category", "rating", "url", "appid", "description",
    "icon_url", "username", "developer", "app_version", "source_query", "metadata_verified_at",
    "status", "selection_mode", "local_files", "sha256", "md5", "package_size_bytes",
    "remote_sources", "device_serial", "collected_at", "error",
]


def fetch(url: str, *, data: bytes | None = None, timeout: int = 20) -> str:
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "ignore")


def norm_name(value: str) -> str:
    value = html.unescape(TAG_RE.sub("", value or ""))
    return re.sub(r"\s+", "", value).strip().lower()


def text(value) -> str:
    if value is None:
        return ""
    return html.unescape(TAG_RE.sub("", str(value))).strip()


def sitemap_urls() -> list[str]:
    xml_text = fetch(SITEMAP)
    try:
        root = ET.fromstring(xml_text)
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        urls = [loc.text or "" for loc in root.findall(".//sm:loc", ns)]
    except ET.ParseError:
        urls = DETAIL_RE.findall(xml_text)
    return sorted({u for u in urls if re.match(r"https://www\.weweplay\.com/home/we/\d+\.html$", u)})


def extract_jsonld(html_text: str) -> dict:
    m = re.search(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html_text, re.S | re.I)
    if not m:
        return {}
    raw = html.unescape(m.group(1)).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    graph = data.get("@graph") if isinstance(data, dict) else None
    if isinstance(graph, list):
        for item in graph:
            if isinstance(item, dict) and item.get("@type") == "MobileApplication":
                return item
    return data if isinstance(data, dict) else {}


def parse_page(url: str) -> dict[str, str]:
    try:
        page = fetch(url)
    except Exception as exc:
        return {"url": url, "fetch_error": str(exc)}
    data = extract_jsonld(page)
    rating = ""
    rating_count = ""
    agg = data.get("aggregateRating") if isinstance(data, dict) else None
    if isinstance(agg, dict):
        rating = str(agg.get("ratingValue") or "").strip()
        rating_count = str(agg.get("ratingCount") or "").strip()
    if not rating:
        m = re.search(r"評分\s*</?[^>]*>\s*([0-9](?:\.[0-9])?)", page)
        if not m:
            m = re.search(r"評分\s*([0-9](?:\.[0-9])?)", text(page))
        if m:
            rating = m.group(1)
    appids = WX_APPID_RE.findall(page)
    name = text(data.get("name")) if isinstance(data, dict) else ""
    if not name:
        tm = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
        if tm:
            name = text(tm.group(1)).replace("- 小程序商店 WeChat Mini Program", "").strip()
    category = ""
    # Breadcrumb category from JSON-LD graph if present.
    mcat = re.search(r'<a[^>]+href="/home/categories/\d+\.html"[^>]*>(.*?)</a>', page, re.S)
    if mcat:
        category = text(mcat.group(1))
    id_match = re.search(r"/home/we/(\d+)\.html", url)
    weweplay_id = id_match.group(1) if id_match else ""
    return {
        "weweplay_url": url,
        "weweplay_id": weweplay_id,
        "appid": appids[0].lower() if appids else "",
        "name": name,
        "name_norm": norm_name(name),
        "category": category,
        "rating": rating,
        "rating_count": rating_count,
        "description": text(data.get("description")) if isinstance(data, dict) else "",
        "icon_url": str(data.get("image") or "") if isinstance(data, dict) else "",
        "metadata_source": "weweplay_detail_page",
        "metadata_verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fetch_error": "",
    }


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)


def main() -> int:
    urls = sitemap_urls()
    print(f"detail_urls={len(urls)}")
    records: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(parse_page, u): u for u in urls}
        for i, fut in enumerate(as_completed(futs), 1):
            rec = fut.result()
            records.append(rec)
            if i % 100 == 0:
                print(f"fetched={i}/{len(urls)}", flush=True)
            time.sleep(0.01)
    fields = ["weweplay_url", "weweplay_id", "appid", "name", "name_norm", "category", "rating", "rating_count", "description", "icon_url", "metadata_source", "metadata_verified_at", "fetch_error"]
    write_csv(OUT / "weweplay_rating_url_enrichment.csv", fields, sorted(records, key=lambda r: r.get("weweplay_url", "")))

    by_appid = {r["appid"]: r for r in records if r.get("appid")}
    by_name: dict[str, list[dict[str, str]]] = {}
    for r in records:
        if r.get("name_norm"):
            by_name.setdefault(r["name_norm"], []).append(r)

    packages = read_csv(OUT / "packages_full_metadata.csv") or read_csv(OUT / "packages.csv")
    miniapps = read_csv(OUT / "miniapps_full_metadata.csv") or read_csv(OUT / "miniapps.csv")

    def enrich(rows: list[dict[str, str]]) -> tuple[list[dict[str, str]], int, int]:
        enriched = []
        matches = 0
        ratings = 0
        for row in rows:
            appid = (row.get("appid") or row.get("csv_id") or "").strip().lower()
            name_key = norm_name(row.get("name", ""))
            match = by_appid.get(appid)
            if not match:
                candidates = by_name.get(name_key, [])
                if len(candidates) == 1:
                    match = candidates[0]
            outrow = dict(row)
            if match:
                matches += 1
                outrow["url"] = outrow.get("url") or match.get("weweplay_url", "")
                outrow["rating"] = outrow.get("rating") or match.get("rating", "")
                outrow["category"] = outrow.get("category") or match.get("category", "")
                outrow["metadata_verified_at"] = outrow.get("metadata_verified_at") or match.get("metadata_verified_at", "")
                outrow["description"] = outrow.get("description") or match.get("description", "")
                outrow["icon_url"] = outrow.get("icon_url") or match.get("icon_url", "")
                outrow["weweplay_url"] = match.get("weweplay_url", "")
                outrow["weweplay_rating_count"] = match.get("rating_count", "")
                outrow["metadata_sources"] = (outrow.get("metadata_sources") or "") + (";" if outrow.get("metadata_sources") else "") + "weweplay_rating_url_enrichment"
            if outrow.get("rating"):
                ratings += 1
            enriched.append(outrow)
        return enriched, matches, ratings

    package_rows, package_matches, package_ratings = enrich(packages)
    miniapp_rows, miniapp_matches, miniapp_ratings = enrich(miniapps)
    enriched_fields = list(dict.fromkeys((package_rows[0].keys() if package_rows else REQUESTED_FIELDS) | {"weweplay_url", "weweplay_rating_count", "metadata_sources"})) if False else None
    package_fields = list(dict.fromkeys(REQUESTED_FIELDS + ["canonical_local_file", "canonical_source_runs", "canonical_source_files", "validation_status", "shared_with_appids", "manifest_hash_status", "metadata_sources", "weweplay_url", "weweplay_rating_count", "size_bytes", "local_file", "source_runs", "source_files"]))
    miniapp_fields = package_fields
    write_csv(OUT / "packages_full_metadata.csv", package_fields, package_rows)
    write_csv(OUT / "miniapps_full_metadata.csv", miniapp_fields, miniapp_rows)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "weweplay_detail_urls": len(urls),
        "weweplay_records": len(records),
        "weweplay_records_with_rating": sum(1 for r in records if r.get("rating")),
        "weweplay_records_with_appid": sum(1 for r in records if r.get("appid")),
        "package_rows": len(package_rows),
        "package_matches": package_matches,
        "package_rating_filled_or_present": package_ratings,
        "package_url_filled_or_present": sum(1 for r in package_rows if r.get("url")),
        "miniapp_rows": len(miniapp_rows),
        "miniapp_matches": miniapp_matches,
        "miniapp_rating_filled_or_present": miniapp_ratings,
        "miniapp_url_filled_or_present": sum(1 for r in miniapp_rows if r.get("url")),
        "note": "rating/url are filled only when a public WeWePlay detail page matches by appid or unique normalized name; otherwise left empty.",
    }
    (OUT / "weweplay_rating_url_enrichment_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
