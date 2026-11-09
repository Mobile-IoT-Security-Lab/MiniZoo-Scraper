#!/usr/bin/env python3
"""Consolidate WeChat collection runs into one directory, grouped by appid.

The source runs are left untouched. Package files are hard-linked by default,
so the consolidated view does not consume another copy of every wxapkg. Use
``--mode copy`` when the destination is on another filesystem or an entirely
independent copy is required.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_SOURCE_ROOT = Path(os.environ.get("WECHAT_DATA_DIR", str(ROOT.parent / "collection_results" / "initial")))
DEFAULT_OUTPUT = DEFAULT_SOURCE_ROOT / "all_mini_apps"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def slug(value: str) -> str:
    clean = re.sub(r"[\\/:*?\"<>|\s]+", "_", (value or "").strip())
    return clean[:80] or "miniapp"


def split_field(value: str) -> list[str]:
    return [item.strip() for item in (value or "").split(";") if item.strip()]


def inside(path: Path, directory: Path) -> bool:
    try:
        path.resolve().relative_to(directory.resolve())
        return True
    except ValueError:
        return False


@dataclass
class Package:
    appid: str
    digest: str
    name_candidates: Counter = field(default_factory=Counter)
    sources: list[tuple[str, Path]] = field(default_factory=list)
    manifest_hash_states: set[str] = field(default_factory=set)


def discover_manifests(source_root: Path, output: Path) -> list[Path]:
    manifests = []
    # Multi-device batches keep one run directory per phone below a shared
    # batch directory, so manifests can be more than one level deep.
    for manifest in source_root.rglob("manifest.csv"):
        if not inside(manifest, output):
            manifests.append(manifest)
    return sorted(manifests)


def load_runs(manifests: list[Path]):
    packages: dict[tuple[str, str], Package] = {}
    attempts: list[dict[str, str]] = []
    missing_files: list[str] = []
    hash_mismatches: list[str] = []

    for manifest in manifests:
        run_dir = manifest.parent
        with manifest.open(newline="", encoding="utf-8-sig") as source:
            for row in csv.DictReader(source):
                appid = (row.get("appid") or row.get("csv_id") or "").strip()
                name = (row.get("name") or "").strip()
                attempts.append(
                    {
                        "source_run": run_dir.name,
                        "appid": appid,
                        "name": name,
                        "status": row.get("status", ""),
                        "selection_mode": row.get("selection_mode", ""),
                        "error": row.get("error", ""),
                    }
                )

                paths = split_field(row.get("local_files", ""))
                declared_hashes = split_field(row.get("sha256", ""))
                for index, relative in enumerate(paths):
                    package_path = (run_dir / relative).resolve()
                    if not package_path.is_file():
                        missing_files.append(str(package_path))
                        continue

                    actual_hash = sha256(package_path)
                    declared_hash = declared_hashes[index] if index < len(declared_hashes) else ""
                    if not declared_hash:
                        hash_state = "not_declared"
                    elif declared_hash == actual_hash:
                        hash_state = "match"
                    else:
                        hash_state = "mismatch"
                        hash_mismatches.append(str(package_path))

                    package_appid = appid or f"unknown_{actual_hash[:16]}"
                    key = (package_appid, actual_hash)
                    package = packages.setdefault(key, Package(package_appid, actual_hash))
                    if name:
                        package.name_candidates[name] += 1
                    package.sources.append((run_dir.name, package_path))
                    package.manifest_hash_states.add(hash_state)

    return packages, attempts, missing_files, hash_mismatches


def load_existing_output(output: Path):
    """Load the canonical catalog so later merges can be incremental.

    This permits old run directories to be removed after a verified merge:
    future batches are merged with the already canonical package files rather
    than requiring every historical source directory to remain on disk.
    """
    packages: dict[tuple[str, str], Package] = {}
    attempts: list[dict[str, str]] = []
    missing_files: list[str] = []
    hash_mismatches: list[str] = []

    package_index = output / "packages.csv"
    if package_index.is_file():
        with package_index.open(newline="", encoding="utf-8-sig") as source:
            for row in csv.DictReader(source):
                appid = (row.get("appid") or "").strip()
                relative = (row.get("local_file") or "").strip()
                declared_hash = (row.get("sha256") or "").strip()
                package_path = (output / relative).resolve()
                if not appid or not package_path.is_file():
                    if relative:
                        missing_files.append(str(package_path))
                    continue
                actual_hash = sha256(package_path)
                if declared_hash and declared_hash != actual_hash:
                    hash_mismatches.append(str(package_path))
                    continue
                package = packages.setdefault(
                    (appid, actual_hash), Package(appid, actual_hash)
                )
                name = (row.get("name") or "").strip()
                if name:
                    package.name_candidates[name] += 1
                package.sources.append(("canonical", package_path))
                package.manifest_hash_states.add("canonical_verified")

    attempt_index = output / "attempts.csv"
    if attempt_index.is_file():
        with attempt_index.open(newline="", encoding="utf-8-sig") as source:
            attempts.extend(csv.DictReader(source))

    return packages, attempts, missing_files, hash_mismatches


def preferred_name(package_groups: list[Package]) -> str:
    names = Counter()
    for package in package_groups:
        names.update(package.name_candidates)
    if not names:
        return "miniapp"
    # Count first, then pick the lexicographically smallest value for stable runs.
    return sorted(names, key=lambda value: (-names[value], value))[0]


def materialize(source: Path, destination: Path, mode: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.is_file() and sha256(destination) == destination.stem:
            if mode == "hardlink":
                source_stat = source.stat()
                destination_stat = destination.stat()
                if (source_stat.st_dev, source_stat.st_ino) != (
                    destination_stat.st_dev,
                    destination_stat.st_ino,
                ):
                    temporary = destination.with_name(destination.name + ".dedup_tmp")
                    if temporary.exists():
                        temporary.unlink()
                    os.link(source, temporary)
                    os.replace(temporary, destination)
            return
        raise RuntimeError(f"Unexpected file already exists: {destination}")
    if mode == "hardlink":
        try:
            os.link(source, destination)
            return
        except OSError:
            # Cross-filesystem destinations cannot use hard links.
            pass
    shutil.copy2(source, destination)


def write_csv(path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as destination:
        writer = csv.DictWriter(destination, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def consolidate(source_root: Path, output: Path, mode: str) -> dict[str, object]:
    source_root = source_root.resolve()
    output = output.resolve()
    manifests = discover_manifests(source_root, output)
    packages, attempts, missing_files, hash_mismatches = load_runs(manifests)
    existing_packages, existing_attempts, existing_missing, existing_mismatches = (
        load_existing_output(output)
    )
    for key, existing in existing_packages.items():
        package = packages.setdefault(key, Package(existing.appid, existing.digest))
        package.name_candidates.update(existing.name_candidates)
        package.sources.extend(existing.sources)
        package.manifest_hash_states.update(existing.manifest_hash_states)
    attempts.extend(existing_attempts)
    missing_files.extend(existing_missing)
    hash_mismatches.extend(existing_mismatches)
    if not manifests and not packages:
        raise RuntimeError(f"No run manifests or canonical packages found below {source_root}")
    output.mkdir(parents=True, exist_ok=True)

    # Re-running an incremental merge must not duplicate historical attempts.
    attempts = list(
        {
            tuple(row.get(field, "") for field in (
                "source_run", "appid", "name", "status", "selection_mode", "error"
            )): row
            for row in attempts
        }.values()
    )

    by_appid: dict[str, list[Package]] = defaultdict(list)
    appids_by_hash: dict[str, set[str]] = defaultdict(set)
    for package in packages.values():
        by_appid[package.appid].append(package)
        appids_by_hash[package.digest].add(package.appid)

    package_rows: list[dict[str, object]] = []
    app_rows: list[dict[str, object]] = []
    materialized_by_hash: dict[str, Path] = {}
    for appid in sorted(by_appid):
        groups = sorted(by_appid[appid], key=lambda item: item.digest)
        name = preferred_name(groups)
        # The directory name is the stable unique key. Names can change between
        # runs; including them in the path would leave duplicate app folders.
        app_dir = output / appid
        warnings = set()
        for package in groups:
            source_run, source_path = sorted(
                package.sources, key=lambda item: (item[0], str(item[1]))
            )[0]
            destination = app_dir / f"{package.digest}.wxapkg"
            # Reuse the first canonical file for identical content. App folders
            # retain their own path, while hard links ensure the bytes exist
            # only once on disk even when an old run associated the same hash
            # with more than one appid.
            materialize(
                materialized_by_hash.get(package.digest, source_path),
                destination,
                mode,
            )
            materialized_by_hash.setdefault(package.digest, destination)
            shared_appids = sorted(appids_by_hash[package.digest])
            validation = "unique_hash"
            if len(shared_appids) > 1:
                validation = "shared_hash_across_appids"
                warnings.add(validation)
            package_rows.append(
                {
                    "appid": appid,
                    "name": name,
                    "sha256": package.digest,
                    "size_bytes": destination.stat().st_size,
                    "local_file": str(destination.relative_to(output)),
                    "validation_status": validation,
                    "shared_with_appids": ";".join(shared_appids),
                    "manifest_hash_status": ";".join(sorted(package.manifest_hash_states)),
                    "source_runs": ";".join(sorted({run for run, _ in package.sources})),
                    "source_files": ";".join(
                        sorted(str(path) for _, path in package.sources)
                    ),
                }
            )
        app_rows.append(
            {
                "appid": appid,
                "name": name,
                "package_count": len(groups),
                "directory": str(app_dir.relative_to(output)),
                "validation_status": ";".join(sorted(warnings)) or "unique_hashes",
            }
        )

    write_csv(
        output / "miniapps.csv",
        ["appid", "name", "package_count", "directory", "validation_status"],
        app_rows,
    )
    write_csv(
        output / "packages.csv",
        [
            "appid",
            "name",
            "sha256",
            "size_bytes",
            "local_file",
            "validation_status",
            "shared_with_appids",
            "manifest_hash_status",
            "source_runs",
            "source_files",
        ],
        package_rows,
    )
    write_csv(
        output / "attempts.csv",
        ["source_run", "appid", "name", "status", "selection_mode", "error"],
        attempts,
    )

    shared_hashes = {digest: appids for digest, appids in appids_by_hash.items() if len(appids) > 1}
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_root": str(source_root),
        "output": str(output),
        "link_mode_requested": mode,
        "source_runs": [manifest.parent.name for manifest in manifests],
        "attempts": len(attempts),
        "apps_with_packages": len(by_appid),
        "unique_appid_package_pairs": len(packages),
        "unique_package_hashes": len(appids_by_hash),
        "hashes_shared_across_appids": len(shared_hashes),
        "apps_affected_by_shared_hashes": len(
            set().union(*shared_hashes.values()) if shared_hashes else set()
        ),
        "missing_source_files": missing_files,
        "manifest_hash_mismatches": hash_mismatches,
    }
    (output / "merge_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Merge all collection_results runs into one appid-based directory"
    )
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--mode",
        choices=("hardlink", "copy"),
        default="hardlink",
        help="hardlink avoids duplicating disk usage; it falls back to copy across filesystems",
    )
    args = parser.parse_args()

    report = consolidate(args.source_root, args.output, args.mode)
    subprocess.run([sys.executable, "build_final_miniapp_csvs.py"], cwd=Path(__file__).resolve().parent, check=True)
    print(f"DONE: {report['output']}")
    print(f"Apps with packages: {report['apps_with_packages']}")
    print(f"Unique app/package pairs: {report['unique_appid_package_pairs']}")
    print(f"Unique package hashes: {report['unique_package_hashes']}")
    print(f"Shared hashes requiring review: {report['hashes_shared_across_appids']}")
    if report["missing_source_files"] or report["manifest_hash_mismatches"]:
        raise SystemExit("Merge completed with source integrity warnings; see merge_report.json")


if __name__ == "__main__":
    main()
