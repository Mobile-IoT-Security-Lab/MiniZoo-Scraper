"""Synchronous metadata extraction and decompilation for either collector.

Observed identity, literal metadata, coverage and reconstruction outcomes are
separate. Reconstruction failure never deletes or invalidates an intact package.
"""
import csv
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

from . import artifacts, decompiled_metadata, static_reader

SCHEMA = "minizoo_processing_v1"


def default_decompiler_command():
    return [sys.executable, str(Path(__file__).with_name("tools") / "decompile_wxapkg.py"),
            "--package", "{package}", "--output", "{output}", "--main-dir", "{main_output}"]


def atomic_json(path, data):
    path = Path(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def extract_package(path, expected_sha256, directory):
    path, directory = Path(path).absolute(), Path(directory)
    if path.is_symlink() or path.resolve() != path or artifacts.sha256_file(path) != expected_sha256:
        raise artifacts.ArtifactError("Package changed before metadata extraction")
    directory.mkdir(parents=True, exist_ok=True)
    # The static reader only parses bytes; it never executes embedded code.
    result, evidence = static_reader.parse_one((expected_sha256, path.stat().st_size, str(path)))
    for item in evidence:
        item["source_sha256"] = expected_sha256
    evidence_path = directory / "evidence.json"
    summary_path = directory / "summary.json"
    atomic_json(evidence_path, evidence)
    atomic_json(summary_path, result)
    grouped = {}
    for item in evidence:
        grouped.setdefault(item["kind"], [])
        value = item.get("value")
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                pass
        if value not in grouped[item["kind"]]:
            grouped[item["kind"]].append(value)
    return {"status": result["status"], "package_role": result.get("role", "unknown"),
            "fields": grouped, "host_appid_candidates": result.get("host_appids", []),
            "plugin_ids": result.get("plugin_ids", []),
            "parser_version": result["parser_version"], "parser_sha256": result["parser_sha256"],
            "evidence_path": str(evidence_path), "evidence_sha256": artifacts.sha256_file(evidence_path),
            "summary_path": str(summary_path), "summary_sha256": artifacts.sha256_file(summary_path)}


def process_acquisition(collection_dir, packages, *, appid, attempt_id,
                        coverage, context=None, decompiler_command=None):
    """Finish local processing before the caller is allowed to search again."""
    root = Path(collection_dir).absolute()
    root.mkdir(parents=True, exist_ok=True)
    if not artifacts.APPID.fullmatch(appid) or not packages:
        raise artifacts.ArtifactError("Processing requires an observed AppID and verified packages")
    metadata = []
    for package in packages:
        path = Path(package["local_path"])
        if (not path.resolve().is_relative_to(root.resolve()) or path.is_symlink()
                or artifacts.sha256_file(path) != package["sha256"]
                or path.stat().st_size != package["bytes"]):
            raise artifacts.ArtifactError("Package outside acquisition or changed before processing")
        extraction = extract_package(path, package["sha256"], root / "metadata" / package["sha256"])
        metadata.append({"schema": SCHEMA, "attempt_id": attempt_id,
                         "observed_appid": appid, "sha256": package["sha256"],
                         "bytes": package["bytes"], "role": package["role"],
                         "local_path": str(path), "remote_path": package["remote_path"],
                         "static_metadata": extraction})
    metadata_path = root / "package_metadata.json"
    atomic_json(metadata_path, metadata)
    csv_path = root / "package_metadata.csv"
    fields = ["schema", "attempt_id", "observed_appid", "sha256", "bytes", "role",
              "local_path", "remote_path", "extraction_status", "static_metadata_json"]
    temporary = csv_path.with_name(csv_path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("x", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in metadata:
            writer.writerow({**{k: row[k] for k in fields if k in row},
                             "extraction_status": row["static_metadata"]["status"],
                             "static_metadata_json": json.dumps(row["static_metadata"], ensure_ascii=False)})
    temporary.replace(csv_path)
    command = decompiler_command if decompiler_command is not None else default_decompiler_command()
    report = {"schema": SCHEMA, "attempt_id": attempt_id, "observed_appid": appid,
              "package_acquisition_status": "acquired", "coverage": coverage,
              "metadata_status": "completed", "metadata_path": str(metadata_path),
              "metadata_sha256": artifacts.sha256_file(metadata_path),
              "metadata_csv": str(csv_path), "context": context or {},
              "decompilation_status": "pending", "decompiled": [],
              "decompiled_metadata_status": "not_available"}
    atomic_json(root / "processing.json", report)
    try:
        report["decompiled"] = artifacts.decompile(packages, root, appid, command)
        report["decompilation_status"] = "verified"
    except (artifacts.ArtifactError, OSError, ValueError) as error:
        report["decompilation_status"] = "failed"
        report["decompilation_error"] = str(error)
    if report["decompilation_status"] == "verified":
        try:
            # A separate immutable snapshot avoids a circular hash: the final
            # processing report points to metadata that pins this earlier view.
            run_id = uuid.uuid4().hex
            source = root / ("processing-decompiled-" + run_id + ".json")
            artifacts._immutable(source, json.dumps(report, ensure_ascii=False, indent=2).encode())
            result = decompiled_metadata.write_report(
                source, root / "metadata" / ("decompiled-" + run_id + ".json"),
                expected_appid=appid, expected_attempt_id=attempt_id)
            report["decompiled_metadata_status"] = result["report"]["status"]
            report["decompiled_metadata_path"] = result["path"]
            report["decompiled_metadata_sha256"] = result["sha256"]
        except (artifacts.ArtifactError, OSError, ValueError) as error:
            # Optional fallback failure must not revoke captured bytes, live
            # metadata, or successful decompilation.
            report["decompiled_metadata_status"] = "failed"
            report["decompiled_metadata_error"] = str(error)
    for package in packages:
        if artifacts.sha256_file(package["local_path"]) != package["sha256"]:
            report["package_acquisition_status"] = "integrity_failed"
            atomic_json(root / "processing.json", report)
            raise artifacts.ArtifactError("Original package changed during processing")
    report["processing_completed_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    atomic_json(root / "processing.json", report)
    return report
