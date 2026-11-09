"""WeChat collection CLI. Run: python3 main.py --help."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
import io
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
import zipfile

import artifacts
from minizoo_collection.processing import process_acquisition
from phone import Phone, PhoneError, Pacer, RateLimit, Stopped, Suspended, ScreenUnavailable, request
from store import FileLock, Store, now, read_csv_bytes, sha256, write_json, atomic_write
from visual_phone import VisualPhone
from search_observation import search_fields
from search_hints import load_search_hints, supported_absence, valid_query


def load_config(path):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    supported = {
        "adb", "appium_url", "between_apps", "decompiler_command", "devices",
        "empty_search_limit", "encoding", "evidence_command", "expected_records",
        "load_wait", "ocr_command", "output_dir", "pacing", "rate_limit_cooldown",
        "request_gap", "search_hints_file", "search_metadata_command", "selectors",
        "source_csv", "ui_mode", "version_column", "version_kind",
        "version_metadata_columns", "volume_mount", "volume_uuid", "work_csv",
    }
    if not isinstance(config, dict):
        raise ValueError("Configuration must be a JSON object")
    unsupported = sorted(set(config) - supported)
    if unsupported:
        raise ValueError("Unsupported configuration fields: " + ", ".join(unsupported))
    # Adapters can run from an output directory, so resolve repository paths first.
    # Leave runtime substitution tokens (such as {wxapkg}) intact.
    project_dir = str(Path(__file__).resolve().parent)
    # Resolve data paths from the configuration, independent of the launch cwd.
    config_dir = Path(path).expanduser().resolve().parent
    for key in ('source_csv', 'work_csv', 'output_dir', 'search_hints_file'):
        if config.get(key):
            value = Path(config[key]).expanduser()
            config[key] = str(value if value.is_absolute() else config_dir / value)
    for key in ("evidence_command", "decompiler_command", "ocr_command", "search_metadata_command"):
        if isinstance(config.get(key), list):
            config[key] = [arg.replace("{project_dir}", project_dir) if isinstance(arg, str) else arg
                           for arg in config[key]]
    if config.get("ui_mode", "native") not in ("native", "visual"):
        raise ValueError("ui_mode must be native or visual")
    devices = [d for d in config["devices"] if d.get("enabled", True)]
    for key in ("name", "udid", "system_port"):
        values = [d[key] for d in devices]
        if len(values) != len(set(values)):
            raise ValueError("Duplicate device configuration: " + key)
    if not devices:
        raise ValueError("No enabled device")
    return config


def make_phone(config, device, pacer):
    """Visual mode always uses Appium; OCR replaces only the selectors."""
    phone_class = VisualPhone if config.get("ui_mode", "native") == "visual" else Phone
    return phone_class(config, device, pacer)


def verify_volume(config):
    """Reject writes to a different disk mounted with the same name."""
    mount, expected = config.get("volume_mount"), config.get("volume_uuid")
    if mount and expected:
        info = plistlib.loads(subprocess.check_output(
            ["diskutil", "info", "-plist", mount], timeout=20))
        if info.get("VolumeUUID") != expected or info.get("MountPoint") != mount:
            raise RuntimeError("Configured disk unavailable or UUID mismatch: write refused")
        for key in ("work_csv", "output_dir"):
            if Path(mount).resolve() not in Path(config[key]).resolve().parents:
                raise ValueError(key + " must remain on the verified disk")


def selected_devices(config, names=None):
    devices = [d for d in config["devices"] if d.get("enabled", True)]
    if names:
        wanted = set(names.split(","))
        if wanted - {d["name"] for d in devices}:
            raise ValueError("Unknown or disabled device")
        devices = [d for d in devices if d["name"] in wanted]
    return devices


def doctor(config, devices):
    report = {"at": now(), "devices": [], "blockers": [], "warnings": []}
    try:
        data = Path(config["source_csv"]).read_bytes()
        rows, schema = read_csv_bytes(data, config.get("encoding"))
        report["source"] = {"records": len(rows), "sha256": sha256(config["source_csv"]), "schema": schema}
        if config.get("expected_records") is not None and len(rows) != config["expected_records"]:
            report["warnings"].append("Record count differs from the expected value")
    except Exception as error:
        report["blockers"].append("Source: " + str(error))
    try:
        verify_volume(config)
        report["volume"] = "verified"
    except Exception as error:
        report["blockers"].append("Disk: " + str(error))
    try:
        adb = config.get("adb", "adb")
        raw = subprocess.check_output([adb, "devices"], text=True, timeout=30)
        connected = {line.split()[0] for line in raw.splitlines()[1:] if len(line.split()) >= 2 and line.split()[1] == "device"}
        for device in devices:
            item = dict(device, ready=False)
            try:
                if device["udid"] not in connected:
                    item["error"] = "Device absent or unauthorized"
                else:
                    base = [adb, "-s", device["udid"]]
                    package = subprocess.check_output(base + ["shell", "pm", "path", "com.tencent.mm"], text=True, timeout=30)
                    root = subprocess.run(base + ["shell", "su", "-c", "id"], text=True, capture_output=True, timeout=30)
                    item["ready"] = "package:" in package and root.returncode == 0 and "uid=0(" in root.stdout
                    if not item["ready"]:
                        item["error"] = "WeChat or root access to packages unavailable"
            except Exception as error:
                item["error"] = str(error)
            report["devices"].append(item)
    except Exception as error:
        report["warnings"].append("ADB: " + str(error))
    if not any(d["ready"] for d in report["devices"]):
        report["blockers"].append("No usable device")
    try:
        status = request(config["appium_url"].rstrip("/") + "/status")
        if not status.get("ready"):
            raise RuntimeError("Server not ready")
        report["appium"] = status
    except Exception as error:
        report["blockers"].append("Appium unavailable: " + str(error))
    for key, label in (("evidence_command", "Adapter for loaded package identity/version"),
                       ("decompiler_command", "Decompiler")):
        command = config.get(key)
        if not command:
            report["blockers"].append(label + " not configured")
        elif not isinstance(command, list) or not all(isinstance(x, str) for x in command) or not shutil.which(command[0]):
            report["blockers"].append(label + ": invalid or unavailable command")
    if config.get("ui_mode", "native") == "visual":
        command = config.get("ocr_command") or [str(Path(__file__).parent / "tools" / "ocr")]
        if (not isinstance(command, list) or not command
                or not all(isinstance(arg, str) and arg for arg in command)
                or not shutil.which(command[0])):
            report["blockers"].append("OCR executable unavailable or command invalid")
        else:
            report["ocr"] = {"executable": command[0], "ui_mode": "visual"}
    return report


def run_adapter(command, replacements, log):
    arguments = list(command)
    for key, value in replacements.items():
        arguments = [arg.replace("{" + key + "}", str(value)) for arg in arguments]
    with Path(log).open("ab") as stream:
        process = subprocess.run(arguments, stdout=stream, stderr=subprocess.STDOUT, timeout=300)
    if process.returncode:
        raise artifacts.ArtifactError("Runtime adapter failed: " + str(log))


def read_package_list(path):
    packages = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(packages, list) or not all(isinstance(item, dict) for item in packages):
        raise artifacts.ArtifactError("Invalid adapter package list")
    return packages


def finish_local(config, store, manifest_path):
    """Resume from local files without creating Appium sessions or running searches."""
    path = Path(manifest_path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    root = path.parent
    if manifest.get("stage") == "captured" and (root / "packages.json").is_file():
        # Also recover a crash after the adapter, before the manifest was saved.
        manifest["packages"] = read_package_list(root / "packages.json")
    evidence = artifacts.validate_evidence(root / "evidence.json", manifest["appid"],
                                          manifest["packages"], manifest["attempt_id"])
    stage = manifest.get("stage")
    if stage in ("verified", "decompiled", "local_done"):
        checked_keys = ("version", "version_kind", "source", "version_source", "coverage",
                        "coverage_limits", "packages", "evidence_sha256")
        if any(evidence[key] != manifest.get(key) for key in checked_keys):
            raise artifacts.ArtifactError("Runtime evidence or metadata changed after verification")
    manifest.update(evidence, identity_verified=True)
    # Public metadata is a separate outcome from static parsing/decompilation.
    # Save its replay-validated observations before any optional processing.
    status = evidence.get("public_metadata_status")
    if isinstance(status, dict):
        manifest["public_metadata_collection"] = dict(status,
            appid=manifest["appid"], attempt_id=manifest["attempt_id"],
            evidence_sha256=evidence["evidence_sha256"],
            observed_at=evidence.get("runtime_binding", {}).get("runtime_observed_at"),
            retry_required=bool(status.get("missing_fields")),
            retry_method="bounded_cache_read_then_next_verified_acquisition")
    if stage not in ("decompiled", "local_done"):
        # This checkpoint distinguishes decompiler failure from uncertain identity.
        manifest["stage"] = "verified"
        write_json(path, manifest)
        processed = process_acquisition(
            root, evidence["packages"], appid=manifest["appid"], attempt_id=manifest["attempt_id"],
            coverage=evidence["coverage"], decompiler_command=config["decompiler_command"],
            context={"collector": "versions", "observed_version": evidence.get("version"),
                     "device": manifest.get("device"), "observed_at": evidence.get("runtime_binding", {}).get("runtime_observed_at")})
        manifest.update({key: processed[key] for key in
                         ("package_acquisition_status", "metadata_status", "decompilation_status")})
        manifest.update(processing_path=str(root / "processing.json"),
                        processing_sha256=sha256(root / "processing.json"))
        write_json(path, manifest)
        if processed["decompilation_status"] != "verified":
            raise artifacts.ArtifactError(processed.get("decompilation_error", "Decompilation failed"))
        outputs = processed["decompiled"]
        manifest["decompiled"] = []
        archive = root / "decompiled.zip"
        temp = root / "decompiled.zip.tmp"
        with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as zipped:
            for output in sorted(outputs, key=lambda item: item["package_sha256"]):
                for f in output["files"]:
                    member = output["package_sha256"] + "/" + str(Path(f["path"]).relative_to(output["output"]))
                    # Identical contents produce the same ZIP across acquisitions;
                    # filesystem timestamps are not app data.
                    info = zipfile.ZipInfo(member, date_time=(1980, 1, 1, 0, 0, 0))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = 0o100644 << 16
                    with Path(f["path"]).open("rb") as source, zipped.open(info, "w") as target:
                        shutil.copyfileobj(source, target)
                    manifest["decompiled"].append({"local_path": f["path"], "sha256": f["sha256"],
                                                   "bytes": f["bytes"], "archive_member": member})
        temp.replace(archive)
        manifest.update(decompiled_zip=str(archive), decompiled_zip_sha256=sha256(archive),
                        artifacts_verified=True, stage="decompiled")
        write_json(path, manifest)
    # A crash can occur after local_done but before registration: recheck everything.
    if not manifest.get("artifacts_verified") or not manifest.get("decompiled"):
        raise artifacts.ArtifactError("Unverified decompilation")
    for item in manifest["packages"] + manifest["decompiled"]:
        artifact = Path(item["local_path"])
        if (not artifact.is_file() or artifact.is_symlink() or root.resolve() not in artifact.resolve().parents
                or artifact.stat().st_size != item["bytes"] or sha256(artifact) != item["sha256"]):
            raise artifacts.ArtifactError("Local artifact missing, changed or outside the collection")
    archive = Path(manifest["decompiled_zip"])
    if (archive.is_symlink() or root.resolve() not in archive.resolve().parents
            or sha256(archive) != manifest.get("decompiled_zip_sha256")):
        raise artifacts.ArtifactError("Decompiled archive changed")
    with zipfile.ZipFile(archive) as zipped:
        if not zipped.namelist() or zipped.testzip():
            raise artifacts.ArtifactError("Decompiled archive empty or corrupt")
    if stage == "local_done":
        store.record_collection(path)
        store.save_task(manifest["row_index"], manifest["decision"]["status"], path)
        return
    decision = store.apply_collection(manifest)
    if manifest["coverage"] == "partial":
        decision["status"] = "partial"
    elif decision["status"] in ("artifacts_unverified", "ambiguous"):
        raise artifacts.ArtifactError("CSV rejected verification: " + decision["status"])
    manifest.update(decision=decision, stage="local_done", completed_locally_at=now())
    # Pin the completed local manifest before marking the task complete.
    write_json(path, manifest)
    store.record_collection(path)
    store.save_task(manifest["row_index"], decision["status"], path)




def export_report(store, extra=None):
    report = store.report()
    report["device_probes"] = [json.loads(path.read_text(encoding="utf-8"))
                               for path in sorted((store.output / "reports").glob("probe-*/status.json"))]
    if extra:
        report.update(extra)
    records = {item["manifest"]: item for item in report["collections"]}
    rows = []
    for task in report["tasks"]:
        manifest = {}
        if task["manifest"] and Path(task["manifest"]).is_file():
            try:
                manifest = json.loads(Path(task["manifest"]).read_text(encoding="utf-8"))
            except (ValueError, OSError) as error:
                task = dict(task, error=task["error"] + "; Unreadable manifest: " + str(error))
        decision = manifest.get("decision", {})
        record = records.get(task["manifest"], {})
        rows.append({"row_index": task["row_index"], "appid": task["appid"], "name": task["name"],
                     "status": task["status"], "previous_version": decision.get("previous_version", ""),
                     "version": manifest.get("version", ""), "coverage": manifest.get("coverage", ""),
                     "attempts": task["attempts"], "error": task["error"], "manifest": task["manifest"],
                     "local_manifest_sha256": record.get("manifest_sha256", ""),
                     "local_integrity_error": record.get("integrity_error", ""),
                     **(search_fields(manifest, Path(task["manifest"]).parent) if task["manifest"]
                        else search_fields({}, store.output))})
    report["search_observations"] = [{key: value for key, value in row.items()
                                      if key in ("row_index", "appid") or key.startswith("search_")}
                                     for row in rows if row["search_status"] != "unavailable"]
    report["fully_processed"] = sum(bool(r["local_manifest_sha256"]) and not r["local_integrity_error"]
                                    and r["status"] in ("updated", "unchanged", "older", "version_unknown", "baseline_missing")
                                    for r in rows)
    report["partial_collected"] = sum(r["status"] == "partial" for r in rows)
    report["remaining"] = sum(r["status"] in ("pending", "running") for r in rows)
    report["scraping_pause"] = scraping_pause(store)
    report["empty_search_streak"] = empty_search_streak(store)
    write_json(store.output / "reports" / "latest.json", report)
    if rows:
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
        atomic_write(store.output / "reports" / "results.csv", stream.getvalue().encode("utf-8-sig"))
    return {k: v for k, v in report.items() if k not in ("tasks", "collections", "csv_decisions")}


def scraping_pause(store):
    """Read the persisted stop marker without automatically resuming searches."""
    path = store.output / "checkpoints" / "rate_limit_stop.json"
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("Invalid rate limit checkpoint")
    return value if value.get("active") else None


def empty_search_streak(store):
    path = store.output / "checkpoints" / "empty_search_streak.json"
    value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
        "schema_version": 1, "limit": 10, "entries": [], "count": 0}
    if not isinstance(value, dict) or value.get("schema_version") != 1 or not isinstance(value.get("entries"), list):
        raise ValueError("Invalid empty-search checkpoint")
    value["count"] = len(value["entries"])
    return value


def record_scraping_pause(store, event, delay, trigger="explicit_rate_limit", retry_tasks=None):
    """Write the stop marker before optional checkpoint annotations."""
    saved = scraping_pause(store) or {
        "schema_version": 1, "active": True, "stop_id": uuid.uuid4().hex,
        "stopped_at": now(), "resume_not_before": 0, "events": []}
    saved["events"].append(dict(event, trigger=trigger))
    saved["resume_not_before"] = max(saved["resume_not_before"], time.time() + delay)
    saved.update(reason=event["reason"], trigger=trigger,
                 classification="suspected_rate_limit" if trigger == "consecutive_empty_searches" else "explicit_rate_limit",
                 checkpoint=str(store.output / "state.sqlite3"), resume_command="resume", automatic_restart=False)
    if retry_tasks is not None:
        saved["retry_tasks"] = retry_tasks
    try:
        with store.lock:
            saved["in_flight"] = [dict(row) for row in store.conn.execute(
                "SELECT row_index,appid,name,manifest FROM tasks WHERE status='running' ORDER BY row_index")]
            pending = store.conn.execute(
                "SELECT MIN(row_index) FROM tasks WHERE status IN ('pending','running')").fetchone()[0]
        indices = [entry["row_index"] for entry in saved.get("retry_tasks", [])]
        saved["next_row_index"] = min(indices + [pending]) if pending is not None else min(indices, default=None)
    except Exception as annotation_error:
        saved["checkpoint_annotation_error"] = str(annotation_error)
    write_json(store.output / "checkpoints" / "rate_limit_stop.json", saved)
    return saved


def requeue_empty_searches(store, pause):
    """Idempotently preserve results/history while requiring a new search."""
    for entry in pause.get("retry_tasks", []):
        with store.lock:
            row = store.conn.execute("SELECT * FROM tasks WHERE row_index=?", (entry["row_index"],)).fetchone()
            task = dict(row) if row else {}
        if (task.get("appid") != entry["appid"] or task.get("manifest") != entry["manifest"]
                or task.get("status") not in ("running", "pending", "not_found")):
            continue
        path = Path(entry["manifest"])
        if path.is_symlink() or store.output not in path.resolve().parents:
            raise ValueError("Empty-search manifest outside the checkpoint")
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("appid") != task["appid"] or manifest.get("row_index") != task["row_index"]:
            raise ValueError("Incompatible empty-search manifest")
        if manifest.get("stage") != "created":
            continue  # Do not undo a collection completed in the meantime.
        attempt = next((item for item in manifest.get("search_attempt_devices", [])
                        if item.get("attempt") == entry["search_attempt"]), None)
        if attempt is None:
            raise ValueError("Empty attempt missing from history")
        if not attempt.get("retry_exempt"):
            attempt["retry_exempt"] = True
            manifest["search_retry_exemptions"] = manifest.get("search_retry_exemptions", 0) + 1
        attempt["recheck_after_pause"] = pause["stop_id"]
        manifest["empty_search_pause"] = pause["stop_id"]
        write_json(path, manifest)
        store.save_task(task["row_index"], "pending", path, "Empty search to retry after the pause")


def recover_empty_search_stop(store, config):
    """Recover stops between the counter, marker and task updates."""
    pause = scraping_pause(store)
    streak = empty_search_streak(store)
    limit = max(1, int(config.get("empty_search_limit", 10)))
    if not pause and len(streak["entries"]) >= limit:
        event = dict(streak["entries"][-1], reason="%d consecutive mini apps returned zero results" % len(streak["entries"]),
                     retry_after=0)
        pause = record_scraping_pause(store, event, max(60, config.get("rate_limit_cooldown", 900)),
                                      "consecutive_empty_searches", streak["entries"])
    if pause:
        requeue_empty_searches(store, pause)
    return pause


def resume_scraping(store):
    """Only the resume command can clear the stop marker, after cooldown."""
    saved = scraping_pause(store)
    if not saved or time.time() < saved["resume_not_before"]:
        return saved
    requeue_empty_searches(store, saved)
    history = store.output / "checkpoints" / "rate_limit_history"
    streak_archive = history / (saved["stop_id"] + "-empty-search-streak.json")
    if not streak_archive.exists():
        write_json(streak_archive, empty_search_streak(store))
    write_json(store.output / "checkpoints" / "empty_search_streak.json", {
        "schema_version": 1, "limit": empty_search_streak(store)["limit"], "entries": [], "count": 0,
        "reset_at": now(), "reset_reason": "explicit_resume", "previous_stop_id": saved["stop_id"]})
    saved.update(active=False, resumed_at=now())
    archive = history / (saved["stop_id"] + ".json")
    write_json(archive, saved)
    write_json(store.output / "checkpoints" / "rate_limit_stop.json", saved)
    return None


def search_attempts_used(manifest):
    """Interrupted searches remain in history without exhausting retries."""
    return max(0, manifest.get("search_attempts", 0) - manifest.get("search_retry_exemptions", 0))


class ScrapingStop:
    """Combine account pauses and process shutdown into one device stop signal."""
    def __init__(self, shutdown, scraping):
        self.shutdown, self.scraping = shutdown, scraping

    def set(self):
        self.shutdown.set()

    def is_set(self):
        return self.shutdown.is_set() or self.scraping.is_set()

    def wait(self, seconds):
        end = time.monotonic() + max(0, seconds)
        while not self.is_set():
            remaining = end - time.monotonic()
            if remaining <= 0:
                return False
            self.shutdown.wait(min(remaining, 0.25))
        return True


class Runner:
    def __init__(self, config, store, stop, max_items=None):
        self.config, self.store, self.max_items = config, store, max_items
        self.shutdown_stop, self.scrape_stop = stop, threading.Event()
        self.stop = ScrapingStop(stop, self.scrape_stop)
        if recover_empty_search_stop(store, config):
            self.scrape_stop.set()
        self.pacer = Pacer(store.output / "checkpoints" / "accounts.json", self.stop, config.get("request_gap", 5))
        self.claim_lock, self.claimed = threading.RLock(), 0
        self.search_hints = load_search_hints(config.get("search_hints_file"))

    def search_choice(self, task, manifest, directory, allow_hint):
        """Resume the saved query; allow at most one alternative name per collection."""
        query, source = task["name"], {"kind": "csv", "path": str(self.store.source),
                                      "row_index": task["row_index"]}
        latest = next((entry for entry in reversed(manifest.get("search_attempt_devices", []))
                       if "query" in entry), None)
        if latest and latest.get("query_source", {}).get("kind") == "package_name_hint":
            if not allow_hint or not valid_query(latest["query"]):
                raise ValueError("Saved alternative query is unusable")
            return latest["query"], latest["query_source"], True, (latest.get("result") == "not_found"
                                                                   and not latest.get("recheck_after_pause"))
        if latest and latest.get("recheck_after_pause"):
            return latest["query"], latest.get("query_source", source), False, False
        if latest and latest.get("result") == "not_found":
            hint = self.search_hints.get(task["appid"]) if allow_hint else None
            if (hint and hint["query"] != query and search_attempts_used(manifest) < 3
                    and supported_absence(manifest, directory, latest)):
                return hint["query"], hint["query_source"], True, False
            return query, source, False, True
        return query, source, False, False

    def stop_for_rate_limit(self, phone, task, path, error, manifest=None):
        """Save the global checkpoint before another worker can assign a row."""
        self.scrape_stop.set()
        with self.claim_lock:
            delay = max(60, self.config.get("rate_limit_cooldown", 900), error.retry_after or 0)
            current = manifest or {}
            latest = next((entry for entry in reversed(current.get("search_attempt_devices", []))
                           if "query" in entry), {})
            event = {"at": now(), "device": phone.device["name"], "serial": phone.device["udid"],
                     "account": phone.account, "row_index": task["row_index"] if task else None,
                     "appid": task["appid"] if task else None, "manifest": str(path) if path else None,
                     "query": latest.get("query"), "search_attempt": latest.get("attempt"),
                     "reason": str(error), "retry_after": error.retry_after or 0}
            streak = empty_search_streak(self.store)
            pause = record_scraping_pause(self.store, event, delay, retry_tasks=streak["entries"] or None)
            requeue_empty_searches(self.store, pause)
            self.pacer.suspend(phone.account, str(error), delay)

    def observed_nonempty_search(self):
        with self.claim_lock:
            if self.scrape_stop.is_set():
                return  # A late result does not revoke an already requested stop.
            streak = empty_search_streak(self.store)
            if streak["entries"]:
                streak.update(entries=[], count=0, reset_at=now(), reset_reason="nonempty_results")
                write_json(self.store.output / "checkpoints" / "empty_search_streak.json", streak)

    def finish_not_found(self, phone, task, path, manifest):
        """Count apps with empty results, excluding retries and alternative names."""
        with self.claim_lock:
            if self.stop.is_set():
                raise Stopped("Empty result to recheck after the pause")
            had_results = any(entry.get("search_was_empty") is False
                              for entry in manifest.get("search_attempt_devices", []))
            if getattr(phone, "search_was_empty", None) is True and not had_results:
                streak = empty_search_streak(self.store)
                streak["limit"] = max(1, int(self.config.get("empty_search_limit", 10)))
                latest = manifest["search_attempt_devices"][-1]
                entry = {"at": now(), "row_index": task["row_index"], "appid": task["appid"],
                         "manifest": str(path), "query": latest["query"], "query_source": latest["query_source"],
                         "search_attempt": latest["attempt"], "device": phone.device["name"],
                         "serial": phone.device["udid"], "account": phone.account}
                previous = next((item for item in streak["entries"] if item["appid"] == task["appid"]), None)
                if previous is None:
                    streak["entries"].append(entry)
                else:
                    previous.update(entry)  # A retry changes the evidence, not the count.
                streak.update(count=len(streak["entries"]), updated_at=now())
                threshold = streak["count"] >= streak["limit"]
                if threshold:
                    self.scrape_stop.set()
                write_json(self.store.output / "checkpoints" / "empty_search_streak.json", streak)
                if threshold:
                    pause = recover_empty_search_stop(self.store, self.config)
                    self.pacer.suspend(phone.account, pause["reason"], max(0, pause["resume_not_before"] - time.time()))
                    return False
            self.store.save_task(task["row_index"], "not_found", path)
            return True

    def interrupted_search(self, manifest, search_attempt, path, reason):
        if search_attempt is not None and not search_attempt.get("retry_exempt"):
            search_attempt.update(result=reason, finished_at=now(), retry_exempt=True)
            manifest["search_retry_exemptions"] = manifest.get("search_retry_exemptions", 0) + 1
        write_json(path, manifest)

    def read_manifest(self, task, path):
        if path.is_symlink() or self.store.output not in path.resolve().parents:
            raise artifacts.ArtifactError("Manifest outside the workspace")
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(manifest, dict) or manifest.get("row_index") != task["row_index"]
                or manifest.get("appid") != task["appid"]):
            raise artifacts.ArtifactError("Manifest incompatible with the assigned record")
        return manifest

    def claim(self):
        with self.claim_lock:
            if self.stop.is_set() or (self.max_items is not None and self.claimed >= self.max_items):
                return None
            task = self.store.claim()
            if task:
                self.claimed += 1
            return task

    def resume_local(self, retry_failed=False):
        """Recover saved files before checking Appium or opening a device."""
        states = ("pending", "decompilation_failed") if retry_failed else ("pending",)
        placeholders = ",".join("?" for _ in states)
        with self.store.lock:
            tasks = [dict(row) for row in self.store.conn.execute(
                "SELECT * FROM tasks WHERE manifest IS NOT NULL AND status IN (" + placeholders + ") ORDER BY row_index", states)]
        for task in tasks:
            if self.stop.is_set() or (self.max_items is not None and self.claimed >= self.max_items):
                break
            path = Path(task["manifest"])
            try:
                manifest = self.read_manifest(task, path)
            except (OSError, ValueError, artifacts.ArtifactError) as error:
                self.claimed += 1
                self.store.save_task(task["row_index"], "checkpoint_failed", path, str(error))
                continue
            if manifest.get("stage") not in ("captured", "verified", "decompiled", "local_done"):
                continue
            with self.claim_lock, self.store.lock, self.store.conn:
                self.claimed += 1
                self.store.conn.execute("UPDATE tasks SET status='running',attempts=attempts+1,updated_at=? WHERE row_index=?",
                                        (now(), task["row_index"]))
            self.local(task, path)

    def worker(self, device, stagger):
        phone = make_phone(self.config, device, self.pacer)
        try:
            self.pacer.wait(stagger * 5)
            self.wait_account(phone.account)
            phone.connect()
            while not self.stop.is_set():
                self.wait_account(phone.account)
                task = self.claim()
                if task is None:
                    break
                continuing = self.process(phone, task)
                if not continuing:
                    break
        except Stopped:
            pass
        except Exception as error:
            write_json(self.store.output / "reports" / ("device-" + device["name"] + ".json"),
                       {"at": now(), "device": device, "error": str(error)})
            # Keep the report and propagate startup/worker failures to the CLI.
            raise
        finally:
            try:
                phone.close()
            except Exception:
                pass

    def process(self, phone, task):
        if not re.fullmatch(r"wx[0-9a-f]{16}", task["appid"]):
            self.store.save_task(task["row_index"], "invalid_record", error="Invalid AppID format")
            return True
        path = Path(task["manifest"]) if task.get("manifest") else None
        if path is not None and not path.exists():
            self.store.save_task(task["row_index"], "checkpoint_failed", path, "Saved manifest missing")
            return True
        if path and path.exists():
            try:
                manifest = self.read_manifest(task, path)
            except (OSError, ValueError, artifacts.ArtifactError) as error:
                self.store.save_task(task["row_index"], "checkpoint_failed", path, str(error))
                return True
        else:
            collection_id = uuid.uuid4().hex
            path = self.store.output / "collections" / task["appid"] / collection_id / "manifest.json"
            path.parent.mkdir(parents=True)
            (path.parent / "logs").mkdir()
            manifest = {"collection_id": collection_id, "attempt_id": collection_id,
                        "row_index": task["row_index"], "appid": task["appid"], "name": task["name"],
                        "device": phone.device["udid"], "created_at": now(), "stage": "created",
                        "metadata": {"device": phone.device["name"], "last_attempt_id": collection_id},
                        "search_attempts": 0, "search_attempt_devices": [], "errors": [], "identity_verified": False,
                        "artifacts_verified": False, "version": None}
            write_json(path, manifest)
        if isinstance(phone, VisualPhone):
            phone.visual_dir = path.parent / "logs" / "ui"
            phone.expected_appid = task["appid"]
            phone.search_attempt_id = manifest["attempt_id"]
        self.store.save_task(task["row_index"], "running", path)
        if manifest["stage"] in ("captured", "verified", "decompiled", "local_done"):
            return self.local(task, path)
        query, query_source, is_hint, already_not_found = self.search_choice(
            task, manifest, path.parent, isinstance(phone, VisualPhone))
        if already_not_found:
            latest = manifest.get("search_attempt_devices", [{}])[-1]
            phone.search_was_empty = latest.get("search_was_empty")
            try:
                return self.finish_not_found(phone, task, path, manifest)
            except Stopped:
                self.store.save_task(task["row_index"], "pending", path, "Scraping stopped: result needs to be rechecked")
                return False
        while search_attempts_used(manifest) < 3:
            search_attempt = None
            try:
                # Save the attempt before any device operation.
                self.wait_account(phone.account)
                history = manifest.setdefault("search_attempt_devices", [])
                metadata = manifest.setdefault("metadata", {})
                if not history and manifest["search_attempts"]:
                    # A legacy checkpoint retains only its latest recorded assignment;
                    # do not invent earlier attempt history.
                    history.append({"recorded_attempts": manifest["search_attempts"],
                                    "device": manifest.get("device"), "name": metadata.get("device"),
                                    "source": "previous_checkpoint"})
                manifest["search_attempts"] += 1
                manifest["device"] = phone.device["udid"]
                metadata["device"] = phone.device["name"]
                manifest.pop("search_observation", None)
                search_attempt = {"attempt": manifest["search_attempts"], "device": phone.device["udid"],
                                  "name": phone.device["name"], "started_at": now(),
                                  "query": query, "query_source": query_source}
                history.append(search_attempt)
                write_json(path, manifest)
                if isinstance(phone, VisualPhone):
                    phone.require_public_identity = is_hint
                phone.search_was_empty = None
                phone.refresh()
                rate_limited = False
                try:
                    search_result = phone.search_and_open(query)
                    search_attempt["result"] = search_result
                    if search_result == "opened":
                        phone.search_was_empty = False
                except RateLimit:
                    rate_limited = True
                    self.scrape_stop.set()
                    raise
                finally:
                    search_attempt["search_was_empty"] = getattr(phone, "search_was_empty", None)
                    if getattr(phone, "search_was_empty", None) is False:
                        self.observed_nonempty_search()
                    if isinstance(phone, VisualPhone) and phone.search_observation:
                        manifest["search_observation"] = phone.search_observation
                        search_attempt["search_observation"] = phone.search_observation
                    if not rate_limited:
                        write_json(path, manifest)
                if search_result == "not_found":
                    if self.stop.is_set():
                        raise Stopped("Search interrupted: recheck the result on resume")
                    query, query_source, is_hint, finished = self.search_choice(
                        task, manifest, path.parent, isinstance(phone, VisualPhone))
                    if not finished:
                        continue
                    return self.finish_not_found(phone, task, path, manifest)
                self.observed_nonempty_search()
                # The common runtime adapter selects and copies the opened app's
                # files, including packages that were already cached by search.
                manifest["packages"] = []
                manifest["stage"] = "captured"
                write_json(path, manifest)
                package_list = path.parent / "packages.json"
                write_json(package_list, manifest["packages"])
                run_adapter(self.config["evidence_command"], {
                    "serial": phone.device["udid"], "appid": task["appid"],
                    "attempt_id": manifest["attempt_id"], "output": path.parent / "evidence.json",
                    "packages_json": package_list, "collection_dir": path.parent}, path.parent / "logs" / "evidence.log")
                manifest["packages"] = read_package_list(package_list)
                # Actual package-copy completion, before parsing, rather than
                # the search start or a later processing timestamp.
                manifest["captured_at"] = now()
                write_json(path, manifest)
                return self.local(task, path)
            except RateLimit as error:
                # Stop all devices immediately, before Appium diagnostics.
                try:
                    self.stop_for_rate_limit(phone, task, path, error, manifest)
                finally:
                    manifest["errors"].append({"at": now(), "message": str(error), "type": "RateLimit"})
                    self.interrupted_search(manifest, search_attempt, path, "rate_limited")
                    self.store.save_task(task["row_index"], "pending", path, str(error))
                try:
                    phone.diagnostics(path.parent / "logs")
                except Exception:
                    pass
                return False
            except Stopped:
                self.interrupted_search(manifest, search_attempt, path, "interrupted")
                self.store.save_task(task["row_index"], "pending", path, "Stop requested")
                return False
            except ScreenUnavailable as error:
                if self.scrape_stop.is_set():
                    self.interrupted_search(manifest, search_attempt, path, "interrupted")
                self.store.save_task(task["row_index"], "pending", path, str(error))
                try:
                    phone.diagnostics(path.parent / "logs")
                except Exception:
                    pass
                return False
            except Suspended as error:
                self.pacer.suspend(phone.account, str(error))
                if self.scrape_stop.is_set():
                    self.interrupted_search(manifest, search_attempt, path, "interrupted")
                self.store.save_task(task["row_index"], "pending", path, str(error))
                return False
            except Exception as error:
                manifest["errors"].append({"at": now(), "message": str(error), "type": type(error).__name__})
                if self.scrape_stop.is_set() and manifest["stage"] == "created":
                    self.interrupted_search(manifest, search_attempt, path, "interrupted")
                    self.store.save_task(task["row_index"], "pending", path, "Scraping stopped by rate limit; " + str(error))
                    return False
                write_json(path, manifest)
                try:
                    phone.diagnostics(path.parent / "logs")
                except Exception:
                    pass
                if isinstance(error, artifacts.ArtifactError) or manifest["stage"] == "captured":
                    self.store.save_task(task["row_index"], "ambiguous" if manifest["stage"] == "captured" else "collection_failed", path, str(error))
                    return True
                elif type(error).__name__ == "Ambiguous":
                    self.store.save_task(task["row_index"], "ambiguous", path, str(error))
                    return True
                elif search_attempts_used(manifest) >= 3:
                    self.store.save_task(task["row_index"], "search_failed", path, str(error))
                    return True
                else:
                    try:
                        self.pacer.wait(10 * search_attempts_used(manifest))
                    except Stopped:
                        self.store.save_task(task["row_index"], "pending", path, "Stop requested")
                        return False
            finally:
                phone.last_finished = time.monotonic()
        self.store.save_task(task["row_index"], "retry_exhausted", path)
        return True

    def wait_account(self, account):
        """An already suspended account must not consume new records or attempts."""
        while True:
            if self.stop.is_set():
                raise Stopped("Stop requested")
            with self.pacer.lock:
                state = dict(self.pacer.accounts.get(account, {}))
            if state.get("blocked"):
                raise Suspended(state.get("reason", "Account suspended"))
            delay = state.get("until", 0) - time.time()
            if delay <= 0:
                return
            self.pacer.wait(min(delay, 1))

    def local(self, task, path):
        try:
            self.read_manifest(task, path)
        except (OSError, ValueError, artifacts.ArtifactError) as error:
            self.store.save_task(task["row_index"], "checkpoint_failed", path, str(error))
            return True
        try:
            finish_local(self.config, self.store, path)
        except Exception as error:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(error, ValueError):
                self.store.save_task(task["row_index"], "csv_conflict", path, str(error))
                self.stop.set()
                return False
            status = "decompilation_failed" if manifest.get("identity_verified") else "ambiguous"
            self.store.save_task(task["row_index"], status, path, str(error))
        return True


    def run(self, devices):
        with ThreadPoolExecutor(max_workers=len(devices)) as pool:
            futures = [pool.submit(self.worker, device, i) for i, device in enumerate(devices)]
            for future in futures:
                future.result()


def main(argv=None):
    parser = argparse.ArgumentParser(description="WeChat collection with Appium and persistent checkpoints")
    parser.add_argument("--config", default=str(Path(__file__).with_name("config.json")))
    parser.add_argument("command", choices=("doctor", "init", "probe", "run", "resume", "retry", "status", "report"))
    parser.add_argument("--devices", help="Comma-separated names")
    parser.add_argument("--appids", help="Comma-separated AppIDs to requeue with retry")
    parser.add_argument("--max-items", type=int, help="Global limit of assigned records, including errors")
    args = parser.parse_args(argv)
    if args.max_items is not None and args.max_items < 1:
        parser.error("--max-items must be at least 1")
    if (args.command == "retry") != bool(args.appids):
        parser.error("retry requires --appids; --appids is only available with retry")
    config = load_config(args.config)
    devices = selected_devices(config, args.devices)
    if args.command == "doctor":
        result = doctor(config, devices)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result["blockers"] else 0
    store = Store(config)
    if args.command in ("status", "report"):
        store.open_readonly()
        try:
            if args.command == "status":
                result = store.report()
                result = {k: v for k, v in result.items() if k not in ("tasks", "collections", "csv_decisions")}
                result["scraping_pause"] = scraping_pause(store)
                result["empty_search_streak"] = empty_search_streak(store)
            else:
                verify_volume(config)
                with FileLock(store.output / ".lock"):
                    result = export_report(store)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            store.close()
        return 0
    verify_volume(config)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    with FileLock(store.output / ".lock"):
        store.initialize()
        extra = {}
        code = 0
        try:
            if args.command in ("run", "resume"):
                prior_pause = recover_empty_search_stop(store, config)
                pause = resume_scraping(store) if args.command == "resume" else prior_pause
                runner = Runner(config, store, stop, args.max_items)
                if not pause:
                    # Resume the rate-limit checkpoint without reopening
                    # all earlier failed decompilations.
                    runner.resume_local(retry_failed=args.command == "resume" and not prior_pause)
                remaining = store.conn.execute("SELECT COUNT(*) FROM tasks WHERE status='pending'").fetchone()[0]
                limit_reached = args.max_items is not None and runner.claimed >= args.max_items
                if pause:
                    extra["scraping_pause"] = pause
                    code = 2
                elif remaining and not stop.is_set() and not limit_reached:
                    checks = doctor(config, devices)
                    write_json(store.output / "reports" / "preflight.json", checks)
                    if checks["blockers"]:
                        extra["blocked_prerequisites"] = checks["blockers"]
                        code = 2
                    else:
                        ready = {d["udid"] for d in checks["devices"] if d["ready"]}
                        runner.run([d for d in devices if d["udid"] in ready])
                if scraping_pause(store):
                    extra["scraping_pause"] = scraping_pause(store)
                    code = 2
            elif args.command == "retry":
                extra["retry"] = store.requeue([appid.strip() for appid in args.appids.split(",")])
            elif args.command == "probe":
                probe_runner = Runner(config, store, stop)
                pacer = probe_runner.pacer
                probe_results = []
                for device in devices:
                    if probe_runner.stop.is_set():
                        code = 2
                        break
                    phone = make_phone(config, device, pacer)
                    directory = store.output / "reports" / ("probe-" + device["name"])
                    directory.mkdir(parents=True, exist_ok=True)
                    if isinstance(phone, VisualPhone):
                        phone.visual_dir = directory / "ui"
                    result = {"at": now(), "device": device, "ready": False}
                    try:
                        phone.connect()
                        try:
                            phone.diagnostics(directory / "initial")
                        except Exception as error:
                            result["initial_diagnostics_error"] = str(error)
                        phone.refresh()
                        result["ready"] = True
                    except Exception as error:
                        result.update(error=str(error), error_type=type(error).__name__)
                        if isinstance(error, RateLimit):
                            probe_runner.stop_for_rate_limit(phone, None, None, error)
                        elif isinstance(error, Suspended):
                            pacer.suspend(phone.account, str(error))
                        code = 2
                    finally:
                        try:
                            phone.diagnostics(directory)
                        except Exception as error:
                            result["diagnostics_error"] = str(error)
                        try:
                            phone.close()
                        except Exception as error:
                            result["close_error"] = str(error)
                        write_json(directory / "status.json", result)
                        probe_results.append(result)
                extra["probe_results"] = probe_results
        finally:
            print(json.dumps(export_report(store, extra), ensure_ascii=False, indent=2))
            store.close()
        return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(type(error).__name__ + ": " + str(error), file=sys.stderr)
        sys.exit(1)
