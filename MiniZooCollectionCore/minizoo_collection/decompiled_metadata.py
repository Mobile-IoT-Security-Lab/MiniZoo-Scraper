"""Replayable, static metadata from verified main-package reconstruction.

These are app-declared values, not platform observations. Most WeChat app.json
files contain only page configuration, so an empty result is normal. This reader
never imports or executes reconstructed JavaScript and never derives versions or
acquisition dates. Callers must additionally verify the acquisition's runtime
identity evidence before publishing a fallback value.
"""
import argparse
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from . import artifacts

SCHEMA = "minizoo_decompiled_metadata_v1"
PARSER_VERSION = 1
MANIFEST_NAMES = ("app.json", "app-config.json", "game.json", "manifest.json")
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_RECORD_BYTES = 64 * 1024 * 1024
MAX_FILES = 100000
SHA256 = re.compile(r"[0-9a-f]{64}")
# Root 'name', 'author', 'title' and 'version' can describe a build project or
# library. Page/window titles, extAppid and nested vendor objects are not read.
ALIASES = {
    "name": ("appName", "app_name", "displayName"),
    "description": ("appDescription", "app_description", "description"),
    "developer": ("developerName", "developer_name", "appDeveloper", "developer"),
    "icon_url": ("iconUrl", "iconURL", "icon_url", "appIconUrl"),
    "category": ("appCategory", "categoryName", "category"),
}
IDENTITY_KEYS = ("appid", "appId", "appID")
# Templates commonly declare host-specific values in ext. Generic ext.name,
# description or logo can instead describe the template provider or a service.
EXT_ALIASES = {
    "name": ("appName", "app_name"),
    "description": ("appDescription", "app_description"),
    "developer": ("appDeveloper", "developerName", "developer_name"),
    "icon_url": ("appLogo", "appIconUrl"),
    "category": ("appCategory", "categoryName"),
}


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON key: " + key)
        value[key] = item
    return value


def _json(path, limit=MAX_RECORD_BYTES):
    if path.stat().st_size > limit:
        raise ValueError("JSON exceeds size limit: " + str(path))
    return json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_unique_object,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Non-finite JSON")))


def _path(value, root=None):
    path = Path(value)
    if not path.is_absolute() or path.resolve() != path or path.is_symlink():
        raise ValueError("Noncanonical or symlink evidence path: " + str(path))
    if root is not None and not path.is_relative_to(root):
        raise ValueError("Evidence path escapes acquisition: " + str(path))
    return path


def _digest(value):
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise ValueError("Invalid SHA-256")
    return value


def _pinned_file(value, digest, root=None):
    path = _path(value, root)
    if not path.is_file() or artifacts.sha256_file(path) != _digest(digest):
        raise ValueError("Evidence bytes changed: " + str(path))
    return path


def _bounded(value, depth=0):
    if depth > 32:
        raise ValueError("Manifest exceeds JSON nesting limit")
    if isinstance(value, dict):
        for item in value.values():
            _bounded(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _bounded(item, depth + 1)


def _value(field, value):
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        return None
    value = value.strip()
    if any(ord(c) < 32 and c not in "\t\n\r" for c in value):
        return None
    if field == "icon_url":
        try:
            url = urlsplit(value)
            if url.scheme not in ("http", "https") or not url.netloc or url.username or url.password:
                return None
        except ValueError:
            return None
    return value


def _verify_source(processing_path, expected_appid, expected_attempt_id):
    path = _path(processing_path)
    root = path.parent
    report = _json(path)
    appid = report.get("observed_appid")
    attempt = report.get("attempt_id")
    if (report.get("schema") != "minizoo_processing_v1"
            or report.get("package_acquisition_status") != "acquired"
            or report.get("decompilation_status") != "verified"
            or not isinstance(appid, str) or not artifacts.APPID.fullmatch(appid)
            or not isinstance(attempt, str) or not attempt
            or (expected_appid is not None and appid != expected_appid)
            or (expected_attempt_id is not None and attempt != expected_attempt_id)):
        raise ValueError("Processing identity or verification status does not match")
    metadata_path = _pinned_file(report["metadata_path"], report["metadata_sha256"], root)
    metadata = _json(metadata_path)
    if not isinstance(metadata, list) or not metadata:
        raise ValueError("Missing package metadata")
    main = [row for row in metadata if row.get("role") == "main"]
    if len(main) != 1:
        raise ValueError("Exactly one verified main package is required")
    package = main[0]
    if package.get("observed_appid") != appid or package.get("attempt_id") != attempt:
        raise ValueError("Package identity or attempt does not match")
    original = _pinned_file(package["local_path"], package["sha256"], root)
    if type(package.get("bytes")) is not int or original.stat().st_size != package["bytes"]:
        raise ValueError("Original package size changed")
    receipts = [item for item in report["decompiled"] if item.get("role") == "main"]
    if len(receipts) != 1:
        raise ValueError("Exactly one main decompiler receipt is required")
    receipt = receipts[0]
    command_hash = _digest(receipt.get("command_sha256"))
    receipt_path = _path(root / "checkpoints" / (
        "decompile-" + package["sha256"] + "-main-" + command_hash[:12] + ".json"), root)
    if _json(receipt_path) != receipt:
        raise ValueError("On-disk decompiler receipt does not match processing")
    if (receipt.get("appid") != appid or receipt.get("package_sha256") != package["sha256"]
            or receipt.get("status") != "verified"
            or receipt.get("method") != "configured_external_decompiler"):
        raise ValueError("Decompiler receipt identity does not match")
    output = _path(receipt["output"], root)
    if output.parent != root / "decompiled" or not output.is_dir():
        raise ValueError("Reconstruction directory does not match acquisition")
    inventory = receipt.get("files")
    if not isinstance(inventory, list) or not inventory or len(inventory) > MAX_FILES:
        raise ValueError("Invalid reconstructed file inventory")
    files = []
    for item in inventory:
        file = _pinned_file(item["path"], item["sha256"], output)
        if type(item.get("bytes")) is not int or file.stat().st_size != item["bytes"]:
            raise ValueError("Reconstructed file size changed")
        files.append(str(file))
    actual = []
    for file in output.rglob("*"):
        _path(file, output)
        if file.is_file():
            actual.append(str(file))
            if len(actual) > MAX_FILES:
                raise ValueError("Too many reconstructed files")
    if len(files) != len(set(files)) or sorted(files) != sorted(set(actual)):
        raise ValueError("Reconstructed inventory changed")
    return report, package, receipt, receipt_path, output, metadata_path


def extract_report(processing_path, *, expected_appid=None, expected_attempt_id=None):
    """Read a verified processing report; do not modify it or any package file."""
    try:
        source, package, receipt, receipt_path, output, metadata_path = _verify_source(
            processing_path, expected_appid, expected_attempt_id)
        appid = source["observed_appid"]
        provenance, inspected, identity_conflicts = {}, [], []
        inventory = {row["path"]: row for row in receipt["files"]}
        for name in MANIFEST_NAMES:
            path = output / name
            if str(path) not in inventory:
                continue
            entry = {"path": str(path), "sha256": inventory[str(path)]["sha256"],
                     "status": "no_app_identity"}
            inspected.append(entry)
            try:
                manifest = _json(path, MAX_MANIFEST_BYTES)
                _bounded(manifest)
            except (OSError, ValueError, UnicodeError, RecursionError):
                entry["status"] = "invalid_or_oversized_json"
                continue
            if not isinstance(manifest, dict):
                entry["status"] = "not_an_object"
                continue
            # No recursive traversal: only explicitly named application scopes.
            scopes = [("", manifest)] + [("/" + key, manifest[key])
                for key in ("appInfo", "miniProgram", "ext") if isinstance(manifest.get(key), dict)]
            for prefix, scope in scopes:
                identities = [scope[key] for key in IDENTITY_KEYS if key in scope]
                if not identities:
                    continue
                if any(value != appid for value in identities):
                    entry["status"] = "identity_conflict"
                    identity_conflicts.append({"path": str(path), "pointer": prefix or "/",
                                               "declared_appids": identities})
                    continue
                entry["status"] = "matched_app_identity"
                for field, aliases in (EXT_ALIASES if prefix == "/ext" else ALIASES).items():
                    for key in aliases:
                        value = _value(field, scope.get(key))
                        if value is not None:
                            provenance.setdefault(field, []).append({
                                "path": str(path), "sha256": entry["sha256"],
                                "pointer": prefix + "/" + key, "value": value,
                                "kind": "app_declared"})
        fields, conflicts = {}, {}
        for field, entries in provenance.items():
            values = sorted({entry["value"] for entry in entries})
            if len(values) == 1:
                fields[field] = values[0]
            else:
                conflicts[field] = values
        if identity_conflicts:
            fields = {}
        return {"schema": SCHEMA, "parser_version": PARSER_VERSION,
                "appid": appid, "attempt_id": source["attempt_id"],
                "package_sha256": package["sha256"], "fields": fields,
                "provenance": provenance, "conflicts": conflicts,
                "identity_conflicts": identity_conflicts,
                "status": ("identity_conflict" if identity_conflicts else
                           "verified" if fields else "no_app_metadata"),
                "processing_path": str(Path(processing_path)),
                "processing_sha256": artifacts.sha256_file(processing_path),
                "metadata_path": str(metadata_path),
                "metadata_sha256": source["metadata_sha256"],
                "receipt_path": str(receipt_path),
                "receipt_sha256": artifacts.sha256_file(receipt_path),
                "original_package": {key: package[key] for key in ("local_path", "sha256", "bytes", "role")},
                "inspected_manifests": inspected}
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError("Malformed decompiled metadata source") from error


def replay_report(report, *, expected_appid, expected_package_sha256, expected_attempt_id=None):
    """Revalidate carried evidence and recompute its exact declarations."""
    if (not isinstance(report, dict) or report.get("schema") != SCHEMA
            or report.get("appid") != expected_appid
            or report.get("package_sha256") != expected_package_sha256):
        raise ValueError("Metadata report identity does not match")
    source = _pinned_file(report["processing_path"], report["processing_sha256"])
    actual = extract_report(source, expected_appid=expected_appid,
                            expected_attempt_id=expected_attempt_id)
    if actual != report:
        raise ValueError("Decompiled metadata replay differs from the saved report")
    return actual


def verify_report(report_path, expected_sha256, *, expected_appid,
                  expected_package_sha256, expected_attempt_id=None):
    path = _pinned_file(report_path, expected_sha256)
    return replay_report(_json(path), expected_appid=expected_appid,
                         expected_package_sha256=expected_package_sha256,
                         expected_attempt_id=expected_attempt_id)


def write_report(processing_path, output_path, *, expected_appid=None, expected_attempt_id=None):
    """Write a new immutable report. Existing reports and acquisitions stay intact."""
    report = extract_report(processing_path, expected_appid=expected_appid,
                            expected_attempt_id=expected_attempt_id)
    output = _path(Path(output_path).absolute())
    if output.exists():
        raise ValueError("Refusing to overwrite an existing report")
    if output.is_relative_to(Path(processing_path).parent / "decompiled"):
        raise ValueError("Reports must be separate from frozen reconstructed outputs")
    output.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    artifacts._immutable(output, data)
    return {"path": str(output), "sha256": artifacts.sha256_file(output), "report": report}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    extract = commands.add_parser("extract", help="Create a separate report from existing verified processing")
    extract.add_argument("processing")
    extract.add_argument("--output", required=True)
    extract.add_argument("--appid", required=True)
    extract.add_argument("--attempt-id")
    verify = commands.add_parser("verify", help="Replay a saved report against every original evidence file")
    verify.add_argument("report")
    verify.add_argument("--sha256", required=True, help="Expected report SHA-256")
    verify.add_argument("--appid", required=True)
    verify.add_argument("--package-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "extract":
            result = write_report(Path(args.processing).absolute(), args.output,
                                  expected_appid=args.appid, expected_attempt_id=args.attempt_id)
        else:
            result = verify_report(Path(args.report).absolute(), args.sha256,
                                   expected_appid=args.appid, expected_package_sha256=args.package_sha256)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, artifacts.ArtifactError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
