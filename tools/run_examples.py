#!/usr/bin/env python3
"""Run small offline examples with synthetic inputs and check their outputs."""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "version_scraper/examples/apps.csv"
APPID = "wx0123456789abcdef"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def command(*args):
    result = subprocess.run([sys.executable, "-B", str(ROOT / "minizoo.py"), *map(str, args)],
                            text=True, capture_output=True, timeout=60)
    require(result.returncode == 0,
            f"Command failed ({' '.join(map(str, args))}): {result.stderr or result.stdout}")
    return result.stdout


def rows(path):
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def discovery(directory):
    # Exercise the actual catalogue writer/exporter after native response parsing.
    # The live MiniCrawler adapter is not needed for this persistence example.
    sys.path.insert(0, str(ROOT / "discovery"))
    from main import export
    from metadata import COLUMNS, save_records
    record = dict(rows(SAMPLE)[0], description="Synthetic catalogue example",
                  developer="Example developer", source_query="example")
    with sqlite3.connect(directory / "catalogue.sqlite3") as db:
        db.execute("CREATE TABLE miniapps (" + ",".join(
            column + (" TEXT PRIMARY KEY" if column == "appid" else " TEXT")
            for column in COLUMNS) + ")")
        db.execute("CREATE TABLE searches (query TEXT, offset INTEGER, item_count INTEGER, "
                   "continue_flag INTEGER, searched_at TEXT, PRIMARY KEY(query,offset))")
        save_records(db, [record], "example", 0, 0)
        # A later sparse observation must update the same AppID without losing metadata.
        save_records(db, [{"appid": APPID, "source_query": "example again"}],
                     "example again", 0, 0)
        export(db, directory)
    catalogue = rows(directory / "catalog.csv")
    require(len(catalogue) == 1 and catalogue[0]["appid"] == APPID,
            "Catalogue must contain exactly one synthetic AppID")
    require(catalogue[0]["description"] == record["description"] and
            catalogue[0]["name"] == record["name"], "Sparse update lost earlier metadata")
    require(catalogue[0]["category"] == "", "Missing category must remain empty")
    return ["SQLite catalogue upsert", "sparse metadata preservation", "CSV export"]


def collection(directory):
    command("collection", "--csv", SAMPLE, "--prepare-only", "--open-method", "search",
            "--limit", "1", "--output", directory)
    manifest = rows(directory / "manifest.csv")
    require(len(manifest) == 1 and manifest[0]["status"] == "prepared",
            "Expected one prepared manifest row")
    record = manifest[0]
    require(record["queried_appid"] == APPID and record["identity_status"] == "not_observed",
            "Preparation must preserve query identity separately")
    require(not record["appid"] and not record["local_files"] and not record["sha256"],
            "Preparation must not claim a captured package or observed identity")
    return ["CSV input", "offline search preparation", "manifest and identity fields"]


def version(directory):
    original = SAMPLE.read_bytes()
    config_path = directory / "version.json"
    command("configure-version", "--serial", "EXAMPLE_DEVICE", "--csv", SAMPLE,
            "--output", config_path, "--data-dir", directory / "data")
    config = json.loads(config_path.read_text())
    # Only init/status/report are called; the example serial is never contacted.
    for _ in range(2):
        command("version", "--config", config_path, "init")
    status = json.loads(command("version", "--config", config_path, "status"))
    require(status["task_counts"] == {"pending": 1}, "Queue initialization duplicated or lost its item")
    require(Path(config["work_csv"]).read_bytes() == original and SAMPLE.read_bytes() == original,
            "Initialization changed the input or version baseline")
    report = json.loads(command("version", "--config", config_path, "report"))
    require(report["task_counts"] == {"pending": 1}, "Report differs from queue status")
    return ["configuration generation", "repeatable queue initialization", "baseline preservation", "status/report"]


def core(directory):
    # Generate a tiny plaintext WXAPKG containing only our own synthetic JSON.
    name = b"/app.json"
    content = json.dumps({"appid": APPID, "appName": "Example Mini App",
                          "pages": ["pages/index"], "appVersion": "1"}).encode()
    index_size = 4 + 4 + len(name) + 8
    package = (b"\xbe" + struct.pack(">III", 0, index_size, len(content)) + b"\xed"
               + struct.pack(">II", 1, len(name)) + name
               + struct.pack(">II", 14 + index_size, len(content)) + content)
    path = directory / "example.wxapkg"
    path.write_bytes(package)
    result = json.loads(command("inspect", path))
    (directory / "inspection.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    report = result["package"]
    require(report["status"] == "plaintext_parsed" and report["host_appids"] == [APPID],
            "Synthetic package was not parsed with its declared AppID")
    require(report["sha256"] == hashlib.sha256(package).hexdigest() and path.read_bytes() == package,
            "Inspection changed the package or returned the wrong checksum")
    require(report["entry_count"] == 1 and not report["warnings"] and result["evidence"],
            "Package index or static metadata evidence is invalid")
    return ["WXAPKG parsing", "static metadata evidence", "SHA-256 and original-byte preservation"]


EXAMPLES = {"discovery": discovery, "collection": collection, "version": version, "core": core}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("module", nargs="?", choices=[*EXAMPLES, "all"], default="all")
    parser.add_argument("--output", type=Path, help="New result directory; existing paths are refused")
    args = parser.parse_args()
    if args.output:
        output = args.output.expanduser().resolve()
        try:
            output.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            parser.error("Output already exists; choose a new directory or omit --output")
    else:
        base = Path(os.environ.get("MINIZOO_DATA_DIR", ROOT / "collection_results")) / "examples"
        base.mkdir(parents=True, exist_ok=True)
        output = Path(tempfile.mkdtemp(prefix=args.module + "-", dir=base)).resolve()
    selected = EXAMPLES if args.module == "all" else {args.module: EXAMPLES[args.module]}
    results = []
    for name, run in selected.items():
        directory = output / name
        directory.mkdir()
        try:
            checks = run(directory)
            result = {"module": name, "status": "passed", "checks": checks}
        except (OSError, ValueError, RuntimeError, KeyError, sqlite3.Error, subprocess.SubprocessError) as exc:
            result = {"module": name, "status": "failed", "error": str(exc)}
        result.update(synthetic=True, output=str(directory))
        results.append(result)
        print(f"{result['status'].upper()}: {name} -> {directory}", flush=True)
        if result["status"] == "failed":
            print(result["error"], file=sys.stderr)
    report_path = output / "result.json"
    report_path.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(f"Report: {report_path}\nOffline examples only; live device acquisition is a separate step.")
    return 0 if all(result["status"] == "passed" for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
