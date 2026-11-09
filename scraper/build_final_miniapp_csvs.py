#!/usr/bin/env python3
"""Build the only two canonical Mini App CSVs.

1. all_mini_apps_total.csv: one row per known Mini App/appid from MiniCrawler,
   public enrichment, and collected packages.
2. mini_app_scraping_status.csv: one row per known Mini App/appid, with whether
   we have scraped a package, missing status, package hashes/paths, provenance,
   and all future-use metadata columns.

The script preserves empty columns when data is not available and never invents
rating/url.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from opened_app_identity import SCHEMA, METADATA_FIELDS, EvidenceError, load_verified_receipt, confined_file

ROOT = Path(os.environ.get("WECHAT_ROOT", str(Path(__file__).resolve().parent))).expanduser().resolve()
DATA = Path(os.environ.get("WECHAT_DATA_DIR", str(ROOT.parent / "collection_results" / "initial"))).expanduser().resolve()
OUT = DATA / "all_mini_apps"
DB = Path(os.environ.get("WECHAT_DISCOVERY_DB", str(ROOT.parent / "collection_results" / "discovery" / "data.db")))

FIELDS_BASE = [
    "csv_id", "doc_id", "name", "category", "rating", "url", "appid", "description",
    "icon_url", "username", "developer", "app_version", "source_query", "metadata_verified_at",
    "identity_status",
]
STATUS_FIELDS = [
    "scrape_status", "is_scraped", "is_missing", "package_count", "unique_package_hash_count",
    "package_sha256s", "package_md5s", "package_size_bytes", "local_files", "remote_sources",
    "device_serials", "first_collected_at", "last_collected_at", "statuses_seen", "selection_modes_seen",
    "errors_seen", "source_runs", "source_files", "validation_status", "manifest_hash_status",
    "shared_with_appids", "metadata_sources", "raw_json",
]
TOTAL_FIELDS = FIELDS_BASE + ["metadata_sources", "raw_json"]
TRACKING_FIELDS = FIELDS_BASE + STATUS_FIELDS



def source_label(path: Path) -> str:
    """Describe local sources without requiring data below the code directory."""
    for base in (DATA, ROOT):
        if path.is_relative_to(base):
            return str(path.relative_to(base))
    return str(path)


def provenance_manifest(provenance: dict) -> Path:
    anchor = provenance.get("manifest_root", "code")
    if anchor not in {"data", "code"}:
        raise EvidenceError("Unknown canonical provenance root")
    return confined_file(DATA if anchor == "data" else ROOT, provenance["manifest"])


def norm(v) -> str:
    return "" if v is None else str(v).strip()


def norm_name(v: str) -> str:
    return re.sub(r"\s+", "", v or "").lower()


def appid_of(row: dict[str, str]) -> str:
    if row.get("collection_schema") == SCHEMA:
        return norm(row.get("appid")).lower() if row.get("identity_status") == "runtime_verified" else ""
    return norm(row.get("appid") or row.get("csv_id") or row.get("id") or row.get("\ufeffappid") or row.get("\ufeffid")).lower()


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def add_source(apps: dict[str, dict[str, str]], row: dict[str, str], source: str) -> None:
    appid = appid_of(row)
    if not appid:
        return
    current = apps.setdefault(appid, {field: "" for field in TOTAL_FIELDS})
    mapped = {
        "csv_id": norm(row.get("csv_id") or row.get("id") or row.get("appid")),
        "doc_id": norm(row.get("doc_id")),
        "name": norm(row.get("name") or row.get("title")),
        "category": norm(row.get("category")),
        "rating": norm(row.get("rating")),
        "url": norm(row.get("url") or row.get("weweplay_url")),
        "appid": appid,
        "description": norm(row.get("description")),
        "icon_url": norm(row.get("icon_url")),
        "username": norm(row.get("username")),
        "developer": norm(row.get("developer")),
        "app_version": norm(row.get("app_version")),
        "source_query": norm(row.get("source_query")),
        "metadata_verified_at": norm(row.get("metadata_verified_at") or row.get("verified_at")),
        "raw_json": norm(row.get("raw_json")),
        "identity_status": norm(row.get("identity_status")),
    }
    observed = row.get("identity_status") == "runtime_verified"
    if observed and current.get("identity_status") == "runtime_verified":
        old_time = current.get("metadata_verified_at") or ""
        new_time = mapped.get("metadata_verified_at") or ""
        observed = new_time.replace("Z", "+00:00") >= old_time.replace("Z", "+00:00")
    for key, value in mapped.items():
        if value and (not current.get(key) or observed and key in (*METADATA_FIELDS, "metadata_verified_at", "identity_status")):
            current[key] = value
    sources = [s for s in current.get("metadata_sources", "").split(";") if s]
    sources.extend(s for s in norm(row.get("metadata_sources")).split(";") if s and s not in sources)
    if source not in sources:
        sources.append(source)
    current["metadata_sources"] = ";".join(sources)


def load_db_sources(apps: dict[str, dict[str, str]]) -> None:
    if not DB.exists():
        return
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    try:
        columns = [row[1] for row in con.execute("PRAGMA table_info(miniapps)").fetchall()]
        if not columns:
            return
        for row in con.execute("SELECT * FROM miniapps"):
            add_source(apps, dict(row), "MiniCrawler/MetadataCrawler/data.db")
    finally:
        con.close()


def split_values(value: str) -> list[str]:
    return [v.strip() for v in (value or "").split(";") if v.strip()]


def local_md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def local_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_package_records() -> list[dict[str, str]]:
    """Read package truth directly from canonical appid/*.wxapkg folders.

    After cleanup, the intermediate packages.csv/package_history.csv files may not
    exist anymore. The canonical filesystem layout is therefore the source of
    truth for scraped Mini Apps: collection_results/all_mini_apps/<appid>/<sha>.wxapkg.
    """
    records: list[dict[str, str]] = []
    if not OUT.exists():
        return records
    for app_dir in sorted(OUT.iterdir()):
        if not app_dir.is_dir() or not app_dir.name.startswith("wx"):
            continue
        appid = app_dir.name.lower()
        for package in sorted(app_dir.glob("*.wxapkg")):
            actual_sha = local_sha256(package)
            filename_sha = package.stem.lower()
            hash_status = "filename_matches_sha256" if filename_sha == actual_sha else "filename_sha256_mismatch"
            record = {
                "appid": appid,
                "sha256": actual_sha,
                "md5": local_md5(package),
                "package_size_bytes": str(package.stat().st_size),
                "local_file": str(package.relative_to(OUT)),
                "validation_status": "canonical_package_present",
                "manifest_hash_status": hash_status,
                "source_runs": "canonical_all_mini_apps",
                "source_files": source_label(package),
                # File modification time can change during copies or merges;
                # it is not evidence of when this package was acquired.
                "collected_at": "",
                "status": "ok",
                "_metadata_source": "canonical_package_dirs",
            }
            sidecar = package.with_suffix(".runtime-provenance.json")
            if sidecar.is_file():
                if sidecar.is_symlink():
                    raise EvidenceError("Canonical runtime provenance is a symlink")
                for provenance in json.loads(sidecar.read_text(encoding="utf-8")):
                    manifest = provenance_manifest(provenance)
                    receipt, verified_packages = load_verified_receipt(provenance["row"], manifest.parent)
                    if receipt["appid"] != appid or not any(p["sha256"] == actual_sha for p in verified_packages):
                        raise EvidenceError("Canonical provenance does not match AppID/package")
                    record.update(receipt["metadata"])
                    record.update(identity_status="runtime_verified", validation_status="runtime_verified",
                                  metadata_verified_at=receipt["observed_at"],
                                  device_serial=receipt["device_serial"],
                                  _metadata_source=source_label(sidecar))
            records.append(record)
    return records


def package_records() -> list[dict[str, str]]:
    records = canonical_package_records()
    for source in [
        OUT / "packages_full_metadata.csv",
        OUT / "packages.csv",
        OUT / "legacy_metadata" / "package_history.csv",
    ]:
        for row in read_csv(source):
            if appid_of(row) and norm(row.get("sha256") or row.get("package_sha256") or row.get("latest_package_sha256")):
                row = dict(row)
                row["_metadata_source"] = source_label(source)
                records.append(row)
    # Deduplicate by appid+sha256.
    unique = {}
    for row in records:
        appid = appid_of(row)
        digest = norm(row.get("sha256") or row.get("package_sha256") or row.get("latest_package_sha256"))
        key = (appid, digest)
        # Prefer rows with more non-empty fields.
        if key not in unique or sum(bool(norm(v)) for v in row.values()) > sum(bool(norm(v)) for v in unique[key].values()):
            unique[key] = row
    return list(unique.values())


def aggregate_packages(packages: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in packages:
        grouped[appid_of(row)].append(row)
    result: dict[str, dict[str, str]] = {}
    for appid, rows in grouped.items():
        sha = [] ; md5s=[] ; sizes=[] ; locals_=[] ; remotes=[] ; devices=[] ; collected=[]
        statuses=[] ; modes=[] ; errors=[] ; runs=[] ; files=[] ; validations=[] ; hashstates=[] ; shared=[] ; sources=[]
        for r in rows:
            sha += split_values(norm(r.get("sha256") or r.get("package_sha256") or r.get("latest_package_sha256")))
            md5s += split_values(norm(r.get("md5") or r.get("package_md5") or r.get("manifest_md5")))
            sizes += split_values(norm(r.get("package_size_bytes") or r.get("size_bytes") or r.get("latest_package_size_bytes")))
            locals_ += split_values(norm(r.get("local_files") or r.get("local_file") or r.get("package_local_path") or r.get("latest_package_local_path")))
            remotes += split_values(norm(r.get("remote_sources") or r.get("remote_source") or r.get("latest_remote_source")))
            devices += split_values(norm(r.get("device_serial")))
            collected += split_values(norm(r.get("collected_at") or r.get("first_seen_at") or r.get("last_seen_at")))
            statuses += split_values(norm(r.get("status") or r.get("statuses_seen")))
            modes += split_values(norm(r.get("selection_mode")))
            errors += split_values(norm(r.get("error")))
            runs += split_values(norm(r.get("source_runs") or r.get("source_run")))
            files += split_values(norm(r.get("source_files") or r.get("package_local_path")))
            validations += split_values(norm(r.get("validation_status")))
            hashstates += split_values(norm(r.get("manifest_hash_status") or r.get("hash_status")))
            shared += split_values(norm(r.get("shared_with_appids")))
            sources += split_values(norm(r.get("metadata_sources") or r.get("_metadata_source")))
        uniq = lambda xs: ";".join(dict.fromkeys(x for x in xs if x))
        dates = sorted(set(x for x in collected if x))
        result[appid] = {
            "scrape_status": "scraped",
            "is_scraped": "1",
            "is_missing": "0",
            "package_count": str(len(rows)),
            "unique_package_hash_count": str(len(set(sha))),
            "package_sha256s": uniq(sha),
            "package_md5s": uniq(md5s),
            "package_size_bytes": uniq(sizes),
            "local_files": uniq(locals_),
            "remote_sources": uniq(remotes),
            "device_serials": uniq(devices),
            "first_collected_at": dates[0] if dates else "",
            "last_collected_at": dates[-1] if dates else "",
            "statuses_seen": uniq(statuses),
            "selection_modes_seen": uniq(modes),
            "errors_seen": uniq(errors),
            "source_runs": uniq(runs),
            "source_files": uniq(files),
            "validation_status": uniq(validations) or "unique_hash_only",
            "manifest_hash_status": uniq(hashstates),
            "shared_with_appids": uniq(shared),
            "metadata_sources": uniq(sources),
        }
    return result


def main() -> int:
    apps: dict[str, dict[str, str]] = {}
    # Retain a restored catalog even when the optional MiniCrawler DB is absent.
    previous_status = read_csv(OUT / "mini_app_scraping_status.csv")
    for source in [OUT / "all_mini_apps_total.csv", OUT / "mini_app_scraping_status.csv"]:
        for record in read_csv(source):
            add_source(apps, record, source_label(source))
    attempts = list(previous_status)
    for manifest in sorted((OUT.parent / "active_runs").glob("*/device_*/manifest.csv")):
        for record in read_csv(manifest):
            record = dict(record)
            record["source_runs"] = record.get("source_runs") or source_label(manifest.parent)
            record["source_files"] = record.get("source_files") or source_label(manifest)
            if record.get("collection_schema") == SCHEMA:
                query = json.loads(record.get("query_metadata_json") or "{}")
                query.update(appid=record.get("queried_appid", ""), status=record.get("status", ""),
                             source_files=record["source_files"], source_runs=record["source_runs"],
                             error=record.get("error", ""))
                if record.get("query_match") == "different_appid":
                    query["status"] = "opened_different_app"
                    query["error"] = "Opened AppID: " + record.get("appid", "")
                if re.fullmatch(r"wx[a-f0-9]{16}", query["appid"]):
                    add_source(apps, query, source_label(manifest) + ":search_context")
                    attempts.append(query)
                if record.get("package_attribution") != "runtime_verified" or record.get("status") != "ok":
                    continue
                receipt, _ = load_verified_receipt(record, manifest.parent)
                record.update({key: receipt["metadata"].get(key, "") for key in METADATA_FIELDS})
                record["identity_status"] = "runtime_verified"
            add_source(apps, record, source_label(manifest))
            attempts.append(record)
    attempts_by_app: dict[str, list[dict[str, str]]] = defaultdict(list)
    for record in attempts:
        attempts_by_app[appid_of(record)].append(record)
    load_db_sources(apps)
    for source in [
        ROOT / "wechat_miniapps_200k_detailed.csv",
        ROOT / "wechat_miniapps_200k.csv",
        ROOT / "MiniCrawler" / "wechat_miniapps_200k_detailed.csv",
        ROOT / "MiniCrawler" / "wechat_miniapps_200k.csv",
        OUT / "weweplay_rating_url_enrichment.csv",
        OUT / "miniapps_full_metadata.csv",
        OUT / "legacy_metadata" / "consolidated_miniapps.csv",
        OUT / "legacy_metadata" / "collection_attempts.csv",
    ]:
        for row in read_csv(source):
            add_source(apps, row, source_label(source))

    packages = package_records()
    for row in packages:
        add_source(apps, row, norm(row.get("_metadata_source")) or "package_records")
    package_by_app = aggregate_packages(packages)

    total_rows = [apps[appid] for appid in sorted(apps, key=lambda a: (apps[a].get("name") or a).lower())]
    tracking_rows = []
    for appid in sorted(apps, key=lambda a: (apps[a].get("name") or a).lower()):
        row = {field: apps[appid].get(field, "") for field in FIELDS_BASE}
        status = package_by_app.get(appid)
        if status:
            row.update(status)
        else:
            row.update({field: "" for field in STATUS_FIELDS})
            row.update({"scrape_status": "missing", "is_scraped": "0", "is_missing": "1", "package_count": "0", "unique_package_hash_count": "0"})
        # Attempt history is not proof of package presence; preserve it separately.
        for target, keys in {
            "statuses_seen": ("statuses_seen", "status"),
            "errors_seen": ("errors_seen", "error"),
            "selection_modes_seen": ("selection_modes_seen", "selection_mode"),
            "remote_sources": ("remote_sources",),
            "device_serials": ("device_serials", "device_serial"),
            "source_runs": ("source_runs", "source_run"),
            "source_files": ("source_files",),
            "validation_status": ("validation_status",),
            "manifest_hash_status": ("manifest_hash_status",),
            "shared_with_appids": ("shared_with_appids",),
            "metadata_sources": ("metadata_sources",),
        }.items():
            values = split_values(row.get(target, ""))
            for attempt in attempts_by_app.get(appid, []):
                for key in keys:
                    values.extend(split_values(attempt.get(key, "")))
            row[target] = ";".join(dict.fromkeys(values))
        dates = [value for value in (row.get("first_collected_at"), row.get("last_collected_at")) if value]
        for attempt in attempts_by_app.get(appid, []):
            dates.extend(value for value in (attempt.get("first_collected_at"), attempt.get("last_collected_at")) if value)
            if attempt.get("status") == "ok" and attempt.get("collected_at"):
                dates.append(attempt["collected_at"])
        if dates:
            row["first_collected_at"] = min(dates)
            row["last_collected_at"] = max(dates)
        # combine app-level and package-level metadata source provenance
        app_sources = split_values(apps[appid].get("metadata_sources", ""))
        package_sources = split_values(row.get("metadata_sources", ""))
        row["metadata_sources"] = ";".join(dict.fromkeys(app_sources + package_sources))
        row["raw_json"] = apps[appid].get("raw_json", "")
        tracking_rows.append(row)

    write_csv(OUT / "all_mini_apps_total.csv", TOTAL_FIELDS, total_rows)
    write_csv(OUT / "mini_app_scraping_status.csv", TRACKING_FIELDS, tracking_rows)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total_mini_apps": len(total_rows),
        "tracking_rows": len(tracking_rows),
        "scraped_mini_apps": sum(1 for r in tracking_rows if r.get("is_scraped") == "1"),
        "missing_mini_apps": sum(1 for r in tracking_rows if r.get("is_missing") == "1"),
        "unique_package_hashes": len({h for r in tracking_rows for h in split_values(r.get("package_sha256s", ""))}),
        "rating_rows_total": sum(1 for r in total_rows if norm(r.get("rating"))),
        "url_rows_total": sum(1 for r in total_rows if norm(r.get("url"))),
        "files": ["all_mini_apps_total.csv", "mini_app_scraping_status.csv"],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
