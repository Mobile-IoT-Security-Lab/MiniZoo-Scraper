#!/usr/bin/env python3
"""Merge active WeChat packages into local canonical storage."""
from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import re
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from opened_app_identity import SCHEMA, METADATA_FIELDS, load_verified_receipt

if __package__:
    from .wechat_config import Settings, exclusive_lock, shard_missing
else:
    from wechat_config import Settings, exclusive_lock, shard_missing

SETTINGS = Settings.from_env()
SCRIPTS = Path(__file__).resolve().parent
ROOT = SETTINGS.root
STATE = SETTINGS.state
ACTIVE_MARKER = SETTINGS.data / 'active_runs' / 'CURRENT_ALL_MINI_APPS_RUN.txt'
OUT = SETTINGS.data / 'all_mini_apps'



def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def split_field(value: str) -> list[str]:
    return [x.strip() for x in (value or '').split(';') if x.strip()]


def norm_name(value: str) -> str:
    return ''.join((value or '').split()).lower()


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def load_weweplay_enrichment() -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    rows = read_csv(OUT / 'weweplay_rating_url_enrichment.csv')
    by_appid = {}
    by_name_candidates: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        appid = (row.get('appid') or '').strip().lower()
        if appid:
            by_appid[appid] = row
        name_key = norm_name(row.get('name', ''))
        if name_key:
            by_name_candidates[name_key].append(row)
    by_name = {name: matches[0] for name, matches in by_name_candidates.items() if len(matches) == 1}
    return by_appid, by_name


def write_csv(path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def materialize(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        return
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def save_runtime_provenance(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(entries, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    temporary.replace(path)


def load_existing_packages() -> tuple[list[dict[str, str]], set[tuple[str, str]]]:
    """Deduplicate per AppID/content pair, preserving separately proven owners."""
    seen = set()
    for p in OUT.glob('wx*/*.wxapkg'):
        if p.is_file():
            digest = sha256(p)
            if p.stem.lower() != digest:
                raise RuntimeError(f'Canonical filename/hash mismatch: {p}')
            seen.add((p.parent.name, digest))
    return [], seen


def merge_active() -> dict[str, int | str]:
    if not ACTIVE_MARKER.is_file():
        return {'status': 'no_active_marker', 'new_packages': 0}
    active = Path(ACTIVE_MARKER.read_text(encoding='utf-8').strip())
    if not active.resolve().is_relative_to((SETTINGS.data / 'active_runs').resolve()):
        raise ValueError('Active marker points outside active_runs')
    if not active.is_dir():
        return {'status': 'active_run_missing', 'new_packages': 0, 'active': str(active)}

    OUT.mkdir(parents=True, exist_ok=True)
    enrichment_by_appid, enrichment_by_name = load_weweplay_enrichment()
    requested_fields = [
        'csv_id','doc_id','name','category','rating','url','appid','description','icon_url','username','developer',
        'app_version','source_query','metadata_verified_at','status','selection_mode','local_files','sha256','md5',
        'package_size_bytes','remote_sources','device_serial','collected_at','error'
    ]
    package_fields = requested_fields + [
        'size_bytes','local_file','validation_status','shared_with_appids','manifest_hash_status','source_runs','source_files'
    ]
    packages, existing_hashes = load_existing_packages()

    new_packages = 0
    skipped_duplicate_hashes = 0
    missing_source_files = []
    hash_mismatches = []
    new_package_paths: list[str] = []
    unverified_rows_skipped = 0

    for manifest in sorted(active.rglob('manifest.csv')):
        run_dir = manifest.parent
        source_run = active.name + '/' + run_dir.name
        for row in read_csv(manifest):
            verified = None
            if row.get('collection_schema') == SCHEMA:
                if row.get('package_attribution') != 'runtime_verified' or row.get('status') != 'ok':
                    unverified_rows_skipped += 1
                    continue
                verified, _ = load_verified_receipt(row, run_dir)
                row = dict(row)
                row.update({key: verified['metadata'].get(key, '') for key in METADATA_FIELDS})
            appid = (row.get('appid') or row.get('csv_id') or row.get('id') or '').strip()
            if not re.fullmatch(r'wx[a-f0-9]{16}', appid):
                raise ValueError(f'Invalid appid: {appid!r}')
            name = (row.get('name') or '').strip()
            enrichment = enrichment_by_appid.get(appid.lower()) or enrichment_by_name.get(norm_name(name)) or {}
            if enrichment and verified is None:
                row = dict(row)
                row['rating'] = row.get('rating') or enrichment.get('rating', '')
                row['url'] = row.get('url') or enrichment.get('weweplay_url', '')
                row['category'] = row.get('category') or enrichment.get('category', '')
                row['metadata_verified_at'] = row.get('metadata_verified_at') or enrichment.get('metadata_verified_at', '')
            paths = split_field(row.get('local_files', ''))
            declared = split_field(row.get('sha256', ''))
            for idx, rel in enumerate(paths):
                source = run_dir / rel
                if not rel.endswith('.wxapkg') or any(p.is_symlink() for p in [source, *source.parents] if p == run_dir or run_dir in p.parents):
                    raise ValueError('Only regular, non-symlink .wxapkg sources are accepted')
                src = source.resolve()
                if not src.is_relative_to(run_dir.resolve()):
                    raise ValueError('Manifest package path escapes its run directory')
                if verified is None:
                    # Historical search context remains available, but cannot
                    # create new canonical associations under the new policy.
                    unverified_rows_skipped += 1
                    continue
                if not src.is_file():
                    missing_source_files.append(str(src))
                    continue
                digest = sha256(src)
                if idx < len(declared) and declared[idx] and declared[idx] != digest:
                    hash_mismatches.append(str(src))
                    continue
                pair = (appid, digest)
                provenance_path = OUT / appid / (digest + '.runtime-provenance.json')
                if any(p.is_symlink() for p in (provenance_path, *provenance_path.parents)
                       if p == OUT or OUT in p.parents):
                    raise ValueError('Canonical provenance uses a symlink')
                provenance = json.loads(provenance_path.read_text()) if provenance_path.is_file() else []
                entry = {'manifest': str(manifest.relative_to(ROOT)), 'row': row}
                if entry not in provenance:
                    provenance.append(entry)
                if pair in existing_hashes:
                    save_runtime_provenance(provenance_path, provenance)
                    skipped_duplicate_hashes += 1
                    continue
                dest_rel = f"{appid or 'unknown_' + digest[:16]}/{digest}.wxapkg"
                dest = OUT / dest_rel
                if any(p.is_symlink() for p in [dest, *dest.parents] if p == OUT or OUT in p.parents) or not dest.resolve().is_relative_to(OUT.resolve()):
                    raise ValueError('Canonical destination escapes output or uses a symlink')
                materialize(src, dest)
                if sha256(dest) != digest:
                    raise RuntimeError('Canonical package changed during materialization')
                save_runtime_provenance(provenance_path, provenance)
                package_md5 = md5(src)
                package_row = {field: row.get(field, '') for field in requested_fields}
                package_row.update({
                    'csv_id': row.get('csv_id') or row.get('appid') or appid,
                    'appid': appid or f'unknown_{digest[:16]}',
                    'name': name or 'miniapp',
                    'local_files': row.get('local_files', ''),
                    'sha256': digest,
                    'md5': package_md5,
                    'package_size_bytes': str(dest.stat().st_size),
                    'remote_sources': row.get('remote_sources', ''),
                })
                package_row.update({
                    'appid': appid or f'unknown_{digest[:16]}',
                    'name': name or 'miniapp',
                    'sha256': digest,
                    'size_bytes': dest.stat().st_size,
                    'local_file': dest_rel,
                    'validation_status': 'runtime_verified',
                    'shared_with_appids': '',
                    'manifest_hash_status': 'match' if idx < len(declared) and declared[idx] else 'not_declared',
                    'source_runs': source_run,
                    'source_files': str(src),
                })
                packages.append(package_row)
                existing_hashes.add(pair)
                new_packages += 1
                new_package_paths.append(f'all_mini_apps/{dest_rel}')

    canonical_packages = list(OUT.glob('wx*/*.wxapkg'))
    canonical_appids = {p.parent.name.lower() for p in canonical_packages}
    final_builder = ROOT / 'build_final_miniapp_csvs.py'
    if final_builder.is_file():
        subprocess.run([sys.executable, str(final_builder)], cwd=str(ROOT), check=True)
    report = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'output': str(OUT),
        'active_run': str(active),
        'dedup_policy': 'one_package_per_appid_sha256_pair',
        'unverified_rows_skipped': unverified_rows_skipped,
        'apps_with_packages': len(canonical_appids),
        'unique_appid_package_pairs': len(canonical_packages),
        'unique_package_hashes': len({p.stem.lower() for p in canonical_packages}),
        'new_packages_added_this_merge': new_packages,
        'duplicate_hashes_skipped_this_merge': skipped_duplicate_hashes,
        'missing_source_files': missing_source_files,
        'manifest_hash_mismatches': hash_mismatches,
        'new_package_paths': new_package_paths,
    }
    (active / 'merge_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if missing_source_files or hash_mismatches:
        raise RuntimeError('Merge integrity failure; see active merge_report.json; retaining run')
    return {
        'status': 'ok', 'new_packages': new_packages,
        'duplicates_skipped': skipped_duplicate_hashes,
        'apps': len(canonical_appids),
        'hashes': len({p.stem.lower() for p in canonical_packages}),
        'new_package_paths': new_package_paths,
        'active': str(active),
    }


def main() -> int:
    SETTINGS.check_storage()
    result = merge_active()
    print(json.dumps(result, ensure_ascii=False))
    return 0

if __name__ == '__main__':
    SETTINGS.check_storage()
    with exclusive_lock(STATE / 'merge.lock'):
        raise SystemExit(main())
