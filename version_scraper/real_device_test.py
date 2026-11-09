"""Limited real-device test: search, open and copy packages without writing CSV.

Full release verification uses evidence_command when configured.
Without this evidence packages remain candidates, even when the name matches.
"""
import argparse
import json
from pathlib import Path
import re
import shutil
import threading
import uuid

from artifacts import capture, decompile, inventory, validate_evidence
from main import load_config, selected_devices, verify_volume, run_adapter
from phone import Pacer, RateLimit, Suspended
from store import FileLock, now, sha256, write_json, compare_versions, read_csv_bytes
from visual_phone import VisualPhone


def verify_result(config, result, root):
    """Verify existing bytes/current runtime metadata without navigating WeChat."""
    if not config.get("evidence_command"):
        result["verification_note"] = "Runtime adapter not configured: collection unverified"
        result["status"] = "captured_unverified" if result["packages"] else "no_new_cache_files"
        return result
    root = Path(root).absolute()
    if root.resolve() != root or (root / "collection").resolve() != root / "collection":
        raise ValueError("Collection directory cannot be a symlink")
    (root / "collection").mkdir(exist_ok=True)
    run_adapter(config["evidence_command"], {
        "serial": result["device"]["udid"], "appid": result["appid"],
        "attempt_id": result["attempt_id"], "output": root / "evidence.json",
        "packages_json": root / "packages.json", "collection_dir": root / "collection",
    }, root / "evidence.log")
    # The adapter may add active packages that were already cached before search.
    result["packages"] = json.loads((root / "packages.json").read_text(encoding="utf-8"))
    evidence = validate_evidence(root / "evidence.json", result["appid"],
                                 result["packages"], result["attempt_id"])
    result.update(evidence, identity_verified=True, version_verified=evidence["version"] is not None)
    if not result["baseline"]:
        comparison = "baseline_missing"
    elif evidence["version_kind"] != config.get("version_kind", "integer"):
        comparison = "version_unknown"
    else:
        order = compare_versions(evidence["version"], result["baseline"], config.get("version_kind", "integer"))
        comparison = {1: "newer", 0: "unchanged", -1: "older"}.get(order, "version_unknown")
    result["version_comparison"] = comparison
    result["decompiled"] = decompile(evidence["packages"], root / "collection", result["appid"],
                                     config.get("decompiler_command", []))
    result["decompilation_verified"] = True
    result["status"] = comparison if evidence["coverage"] == "complete" else "partial"
    result.pop("error", None)
    result["verified_at"] = now()
    return result


def source_rows(config):
    return read_csv_bytes(Path(config["source_csv"]).read_bytes(), config.get("encoding"))[0]


def validate_saved_result(config, result, device, rows, root):
    """Constrain a resumed diagnostic run to its original listed app and device."""
    if result.get("device") != device or root.name != device["name"]:
        raise ValueError("Result device differs from configuration")
    index = result.get("row_index")
    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(rows):
        raise ValueError("Invalid result source index")
    expected = rows[index]
    if (result.get("appid") != expected["appid"] or result.get("name") != expected["name"]
            or result.get("baseline") != expected.get(config["version_column"], "")
            or not re.fullmatch(r"wx[0-9a-f]{16}", result.get("appid", ""))
            or not re.fullmatch(r"[0-9a-f]{32}", result.get("attempt_id", ""))):
        raise ValueError("Result identity, baseline or attempt incompatible with source")
    if sum(row["appid"] == expected["appid"] for row in rows) != 1:
        raise ValueError("Duplicate source AppID: ambiguous verification")
    live_root = Path(config["output_dir"]).resolve() / "live_tests"
    for package in result.get("packages", []):
        local = Path(package["local_path"]).absolute()
        if local.is_symlink() or local.resolve() != local or live_root not in local.parents:
            raise ValueError("Test package outside the live_tests directory")


def verify_run(config, devices, run):
    """Resume only metadata/decompilation; never create an Appium session."""
    output = Path(config["output_dir"]).resolve()
    run = Path(run).absolute()
    if (run.resolve() != run or run.parent != output / "live_tests" or not run.is_dir()
            or not re.fullmatch(r"smoke-[0-9a-f]{12}", run.name)):
        raise ValueError("--verify-run requires an existing output/live_tests/smoke-<12 hex digits> directory")
    with FileLock(output / ".lock"):
        report_path = run / "report.json"
        if report_path.is_symlink():
            raise ValueError("Test report cannot be a symlink")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        before = {key: sha256(config[key]) for key in ("source_csv", "work_csv")}
        if report.get("csv_before") != before:
            raise ValueError("CSV files differ from those recorded at test startup")
        rows = source_rows(config)
        configured = {device["name"]: device for device in config["devices"] if device.get("enabled", True)}
        selected = {device["name"] for device in devices}
        results = report.get("results")
        if not isinstance(results, list) or not results:
            raise ValueError("Report has no verifiable results")
        loaded, seen = [], set()
        for original in results:
            name = original.get("device", {}).get("name")
            if name not in configured or name in seen:
                raise ValueError("Unknown or duplicate device in report")
            seen.add(name)
            root = run / name
            result_path = root / "result.json"
            if root.resolve() != root or result_path.is_symlink():
                raise ValueError("Invalid result path")
            result = json.loads(result_path.read_text(encoding="utf-8"))
            validate_saved_result(config, result, configured[name], rows, root)
            for key in ("appid", "name", "row_index", "attempt_id", "device"):
                if result.get(key) != original.get(key):
                    raise ValueError("Result and report belong to different attempts")
            if result.get("opened_at") and name in selected:
                packages_path = root / "packages.json"
                if packages_path.is_symlink() or not packages_path.is_file():
                    raise ValueError("Saved package inventory missing or invalid")
                saved_packages = json.loads(packages_path.read_text(encoding="utf-8"))
                validate_saved_result(config, dict(result, packages=saved_packages), configured[name], rows, root)
            loaded.append((root, result, name in selected))
        if not any(chosen for _, _, chosen in loaded):
            raise ValueError("No selected device present in test")
        verification_id = uuid.uuid4().hex
        shutil.copyfile(report_path, run / ("report.before-verify-" + verification_id + ".json"))
        report["verification_started_at"] = now()
        report["results"] = [result for _, result, _ in loaded]
        processed = []
        try:
            for root, result, chosen in loaded:
                if not chosen:
                    continue
                processed.append(result)
                if not result.get("opened_at"):
                    result["verification_note"] = "App not opened in test: search not repeated"
                    continue
                for filename in ("result.json", "evidence.json", "packages.json"):
                    source = root / filename
                    if source.exists():
                        if source.is_symlink():
                            raise ValueError("Collection file cannot be a symlink")
                        shutil.copyfile(source, root / (source.stem + ".before-verify-" + verification_id + source.suffix))
                result["verification_started_at"] = now()
                result.update(identity_verified=False, version_verified=False, decompilation_verified=False)
                try:
                    verify_result(config, result, root)
                except Exception as error:
                    result.update(status=type(error).__name__, error=str(error))
                finally:
                    result["verification_finished_at"] = now()
                    write_json(root / "result.json", result)
                    write_json(report_path, report)
                print(json.dumps({"device": result["device"]["name"], "appid": result["appid"],
                                  "status": result["status"], "packages": len(result["packages"])}, ensure_ascii=False), flush=True)
        finally:
            after = {key: sha256(config[key]) for key in before}
            report["verification_csv_before"] = before
            report["verification_csv_after"] = after
            report["csv_unchanged"] = before == after
            report["verification_finished_at"] = now()
            write_json(report_path, report)
            print("Report: " + str(report_path), flush=True)
        success = processed and all(r["status"] in ("newer", "unchanged", "older") and r.get("decompilation_verified") for r in processed)
        return 0 if success and report["csv_unchanged"] else 2


def test_one(config, device, row, row_index, root, pacer):
    root.mkdir(parents=True, exist_ok=False)
    result = {"started_at": now(), "appid": row["appid"], "name": row["name"],
              "baseline": row.get(config["version_column"], ""), "row_index": row_index,
              "device": device, "attempt_id": uuid.uuid4().hex,
              "identity_verified": False, "version_verified": False, "decompilation_verified": False,
              "version": None, "status": "started", "packages": []}
    write_json(root / "result.json", result)
    phone = VisualPhone(config, device, pacer)
    phone.visual_dir = root / "ui"
    phone.expected_appid = row["appid"]
    phone.search_attempt_id = result["attempt_id"]
    try:
        phone.connect()
        phone.refresh()
        before = inventory(device["udid"], config.get("adb", "adb"))
        write_json(root / "cache_before.json", before)
        state = phone.search_and_open(row["name"])
        if state == "not_found":
            result["status"] = "not_found"
            return result
        result["opened_at"] = now()
        phone.diagnostics(root / "opened")
        result["packages"] = capture(device["udid"], before, root / "collection", config.get("adb", "adb"))
        result["status"] = "captured_unverified" if result["packages"] else "no_new_cache_files"
        write_json(root / "packages.json", result["packages"])
        verify_result(config, result, root)
        return result
    except Exception as error:
        result.update(status=type(error).__name__, error=str(error))
        if isinstance(error, RateLimit):
            pacer.suspend(phone.account, str(error), max(60, error.retry_after))
        elif isinstance(error, Suspended):
            pacer.suspend(phone.account, str(error))
        return result
    finally:
        if phone.search_observation:
            result["search_observation"] = phone.search_observation
        if phone.session:
            try:
                phone.diagnostics(root / "final_screen")
            except Exception as error:
                result["diagnostics_error"] = str(error)
            try:
                phone.close()
            except Exception as error:
                result["close_error"] = str(error)
        result["finished_at"] = now()
        write_json(root / "result.json", result)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--devices", help="Comma-separated names")
    parser.add_argument("--start-index", type=int, default=0, help="Zero-based CSV index")
    parser.add_argument("--max-items", type=int, default=3, help="From 1 to 3, a different app per device")
    parser.add_argument("--verify-run", help="Complete an existing test without searches or Appium sessions")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    verify_volume(config)
    devices = selected_devices(config, args.devices)
    if args.verify_run:
        return verify_run(config, devices, args.verify_run)
    if args.start_index < 0 or not 1 <= args.max_items <= min(3, len(devices)):
        parser.error("Choose 1 to 3 apps, at most one per device, with index >=0")
    rows = source_rows(config)
    if args.start_index + args.max_items > len(rows):
        parser.error("Selection exceeds the available records")
    output = Path(config["output_dir"])
    run = output / "live_tests" / ("smoke-" + uuid.uuid4().hex[:12])
    run.mkdir(parents=True)
    before = {key: sha256(config[key]) for key in ("source_csv", "work_csv")}
    report = {"started_at": now(), "results": [], "csv_before": before}
    pacer = Pacer(output / "checkpoints" / "accounts.json", threading.Event(), config.get("request_gap", 5))
    with FileLock(output / ".lock"):
        try:
            for i, device in enumerate(devices[:args.max_items]):
                if i:
                    pacer.wait(5)
                index = args.start_index + i
                result = test_one(config, device, rows[index], index, run / device["name"], pacer)
                report["results"].append(result)
                write_json(run / "report.json", report)
                print(json.dumps({"device": device["name"], "appid": rows[index]["appid"],
                                  "status": result["status"], "packages": len(result["packages"])}, ensure_ascii=False), flush=True)
        finally:
            report["csv_after"] = {key: sha256(config[key]) for key in before}
            report["csv_unchanged"] = before == report["csv_after"]
            report["finished_at"] = now()
            write_json(run / "report.json", report)
            print("Report: " + str(run / "report.json"), flush=True)
    return 0 if report["csv_unchanged"] and all(r["status"] in ("newer", "unchanged", "older") for r in report["results"]) else 2


if __name__ == "__main__":
    raise SystemExit(main())
