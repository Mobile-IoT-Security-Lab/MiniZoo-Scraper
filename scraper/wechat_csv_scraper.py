#!/usr/bin/env python3
"""
Scrape WeChat Mini App packages from a CSV list of mini app names.

Input CSV columns expected by default:
  id,name,category,rating,url,description

The script:
1. Reads names from the CSV.
2. Optionally obtains a mini-code image for QR navigation.
3. Opens WeChat -> Mini Programs -> search.
4. Searches each name using ADBKeyboard for Unicode/Chinese input.
5. Opens the first Mini Program result.
6. Identifies the actually focused app and its package through typed runtime evidence.
7. Pulls exact runtime paths, checks content hashes, and rechecks foreground identity.

Important:
- Android root is required to pull WeChat's private cache.
- ADBKeyboard is strongly recommended/required for Chinese names.
"""

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from wxapkg_metadata_csv import append_collected_package_metadata
from opened_app_identity import (
    SCHEMA, METADATA_FIELDS, EvidenceError, PackageUnavailable, RuntimeProbe,
    collect_opened_app, load_verified_receipt, resolve_frida,
)
from minizoo_collection.processing import process_acquisition

ROOT = Path(__file__).resolve().parent
REMOTE_TMP_ROOT = "/sdcard/wechat_miniapp_collection"
WECHAT_PACKAGE = "com.tencent.mm"
ADBKEYBOARD_PKG = "com.android.adbkeyboard"
ADBKEYBOARD_IME = "com.android.adbkeyboard/.AdbIME"
ADB_SERIAL = ""


def run(cmd, check=True, timeout=None, input_text=None):
    p = subprocess.run(cmd, input=input_text, text=True, capture_output=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"Command failed ({p.returncode}): {' '.join(cmd)}\n{p.stdout}\n{p.stderr}")
    return p


def adb(args, check=True, timeout=None):
    command = ["adb"]
    if ADB_SERIAL:
        command.extend(["-s", ADB_SERIAL])
    return run(command + list(args), check=check, timeout=timeout)


def shell(cmd, check=True, timeout=None):
    return adb(["shell", cmd], check=check, timeout=timeout)


def su(cmd, check=True, timeout=None):
    return shell("su -c " + shlex.quote(cmd), check=check, timeout=timeout)


def stamp():
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def slug(s):
    s = re.sub(r"[\\/:*?\"<>|\s]+", "_", s.strip())
    return s[:80] or "miniapp"


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(1024 * 1024), b""):
            h.update(c)
    return h.hexdigest()


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(1024 * 1024), b""):
            h.update(c)
    return h.hexdigest()


def iso_now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def first_nonempty(*values):
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def split_field(value):
    return [item.strip() for item in (value or "").split(";") if item.strip()]


def manifest_fields():
    return [
        "csv_id",
        "doc_id",
        "name",
        "category",
        "rating",
        "url",
        "appid",
        "description",
        "icon_url",
        "username",
        "developer",
        "app_version",
        "source_query",
        "metadata_verified_at",
        "mini_code",
        "mini_code_local",
        "status",
        "selection_mode",
        "local_files",
        "sha256",
        "md5",
        "package_size_bytes",
        "remote_sources",
        "device_serial",
        "collected_at",
        "error",
        "collection_schema",
        "attempt_id",
        "queried_csv_id",
        "queried_appid",
        "queried_name",
        "queried_app_version",
        "queried_developer",
        "query_metadata_json",
        "query_match",
        "package_attribution",
        "identity_status",
        "runtime_evidence_path",
        "runtime_evidence_sha256",
        "coverage",
        "coverage_limits_json",
        "package_acquisition_status",
        "metadata_status",
        "decompilation_status",
        "processing_path",
        "processing_sha256",
    ]


def package_history_fields():
    return [
        "appid",
        "csv_id",
        "doc_id",
        "name",
        "category",
        "rating",
        "url",
        "developer",
        "username",
        "app_version",
        "source_query",
        "metadata_verified_at",
        "package_sha256",
        "package_md5",
        "manifest_sha256",
        "manifest_md5",
        "hash_status",
        "package_size_bytes",
        "package_local_path",
        "remote_source",
        "source_run",
        "device_serial",
        "collected_at",
        "status",
        "selection_mode",
        "description",
        "icon_url",
        "collection_schema",
        "attempt_id",
        "queried_appid",
        "queried_name",
        "package_attribution",
        "identity_status",
        "runtime_evidence_path",
        "runtime_evidence_sha256",
    ]


def make_manifest_record(row, fields, id_column="id", name_column="name"):
    rec = {k: "" for k in fields}
    csv_id = first_nonempty(row.get(id_column), row.get("appid"), row.get("id"), row.get("csv_id"))
    appid = first_nonempty(row.get("appid"), csv_id if str(csv_id).startswith("wx") else "")
    rec.update({
        "csv_id": csv_id,
        "doc_id": first_nonempty(row.get("doc_id")),
        "name": first_nonempty(row.get(name_column), row.get("name")),
        "category": first_nonempty(row.get("category")),
        "rating": first_nonempty(row.get("rating")),
        "url": first_nonempty(row.get("url")),
        "appid": appid,
        "description": first_nonempty(row.get("description")),
        "icon_url": first_nonempty(row.get("icon_url")),
        "username": first_nonempty(row.get("username")),
        "developer": first_nonempty(row.get("developer")),
        "app_version": first_nonempty(row.get("app_version"), row.get("version"), row.get("version_name")),
        "source_query": first_nonempty(row.get("source_query")),
        "metadata_verified_at": first_nonempty(row.get("verified_at"), row.get("metadata_verified_at")),
    })
    context = {key: value for key, value in rec.items() if value}
    # The CSV describes what to search for, not what actually opened.
    for key in ("csv_id", "doc_id", "name", "category", "rating", "url", "appid",
                "description", "icon_url", "username", "developer", "app_version",
                "source_query", "metadata_verified_at"):
        rec[key] = ""
    rec.update(collection_schema=SCHEMA, attempt_id=uuid.uuid4().hex,
               queried_csv_id=context.get("csv_id", ""), queried_appid=context.get("appid", ""),
               queried_name=context.get("name", ""), queried_app_version=context.get("app_version", ""),
               queried_developer=context.get("developer", ""),
               query_metadata_json=json.dumps(context, ensure_ascii=False, sort_keys=True),
               package_attribution="UNASSIGNED", identity_status="not_observed")
    return rec


def apply_opened_collection(rec, evidence, receipt, run_dir):
    """Populate owned fields exclusively from the successfully captured runtime."""
    rec.update({key: receipt["metadata"].get(key, "") for key in METADATA_FIELDS})
    rec["csv_id"] = receipt["appid"]
    rec["metadata_verified_at"] = receipt["observed_at"] or ""
    rec["local_files"] = ";".join(str((evidence.parent / p["local_file"]).relative_to(run_dir))
                                for p in receipt["packages"])
    rec["sha256"] = ";".join(p["sha256"] for p in receipt["packages"])
    rec["md5"] = ";".join(p["md5"] for p in receipt["packages"])
    rec["package_size_bytes"] = ";".join(str(p["bytes"]) for p in receipt["packages"])
    rec["remote_sources"] = ";".join(p["remote_source"] for p in receipt["packages"])
    rec["runtime_evidence_path"] = str(evidence.relative_to(run_dir))
    rec["runtime_evidence_sha256"] = sha256(evidence)
    rec["package_attribution"] = "runtime_verified"
    rec["identity_status"] = "runtime_verified"
    rec["query_match"] = ("same_appid" if rec["queried_appid"] == rec["appid"] else
                          "different_appid" if rec["queried_appid"] else "no_requested_appid")
    rec["selection_mode"] = "focused_runtime_package"
    rec["status"] = "ok"
    rec["coverage"] = receipt["coverage"]
    rec["coverage_limits_json"] = json.dumps(receipt["coverage_limits"], ensure_ascii=False)
    rec["package_acquisition_status"] = "acquired"


def package_file_metadata(local_files):
    return {
        "sha256": ";".join(sha256(p) for p in local_files),
        "md5": ";".join(md5(p) for p in local_files),
        "package_size_bytes": ";".join(str(p.stat().st_size) for p in local_files),
    }


def append_package_history(history_path, rec, run_dir, source_run=None, collected_at=None):
    local_files = split_field(rec.get("local_files"))
    if not local_files:
        return 0
    source_run = source_run or Path(run_dir).name
    append_collected_package_metadata(
        Path(run_dir) / "package_metadata.csv",
        [Path(run_dir) / relative for relative in local_files], rec,
        source_run=source_run, remote_sources=split_field(rec.get("remote_sources")),
        base_fields=manifest_fields(),
    )
    rec = dict(rec)
    if rec.get("package_attribution") == "runtime_verified":
        receipt, _ = load_verified_receipt(rec, run_dir)
        rec.update({key: receipt["metadata"].get(key, "") for key in METADATA_FIELDS})
    else:
        for key in ("appid", "name", "developer", "username", "app_version",
                    "description", "icon_url", "category", "rating", "url", "doc_id"):
            rec[key] = ""
        rec["selection_mode"] = "context_candidate_unassigned"
    collected_at = collected_at or rec.get("collected_at") or iso_now()
    manifest_sha256 = split_field(rec.get("sha256"))
    manifest_md5 = split_field(rec.get("md5"))
    remote_sources = split_field(rec.get("remote_sources"))
    fields = package_history_fields()
    exists = Path(history_path).exists()
    written = 0
    Path(history_path).parent.mkdir(parents=True, exist_ok=True)
    with open(history_path, "a", newline="", encoding="utf-8") as destination:
        writer = csv.DictWriter(destination, fieldnames=fields)
        if not exists:
            writer.writeheader()
        for index, relative in enumerate(local_files):
            package_path = Path(run_dir) / relative
            actual_sha256 = sha256(package_path) if package_path.is_file() else ""
            actual_md5 = md5(package_path) if package_path.is_file() else ""
            expected_sha256 = manifest_sha256[index] if index < len(manifest_sha256) else ""
            expected_md5 = manifest_md5[index] if index < len(manifest_md5) else ""
            if not expected_sha256 and not expected_md5:
                hash_status = "not_declared"
            elif (not expected_sha256 or expected_sha256 == actual_sha256) and (not expected_md5 or expected_md5 == actual_md5):
                hash_status = "match"
            else:
                hash_status = "mismatch"
            writer.writerow({
                "appid": rec.get("appid", ""),
                "csv_id": rec.get("csv_id", ""),
                "doc_id": rec.get("doc_id", ""),
                "name": rec.get("name", ""),
                "category": rec.get("category", ""),
                "rating": rec.get("rating", ""),
                "url": rec.get("url", ""),
                "developer": rec.get("developer", ""),
                "username": rec.get("username", ""),
                "app_version": rec.get("app_version", ""),
                "source_query": rec.get("source_query", ""),
                "metadata_verified_at": rec.get("metadata_verified_at", ""),
                "package_sha256": actual_sha256,
                "package_md5": actual_md5,
                "manifest_sha256": expected_sha256,
                "manifest_md5": expected_md5,
                "hash_status": hash_status,
                "package_size_bytes": package_path.stat().st_size if package_path.is_file() else "",
                "package_local_path": relative,
                "remote_source": remote_sources[index] if index < len(remote_sources) else "",
                "source_run": source_run,
                "device_serial": rec.get("device_serial", ""),
                "collected_at": collected_at,
                "status": rec.get("status", ""),
                "selection_mode": rec.get("selection_mode", ""),
                "description": rec.get("description", ""),
                "icon_url": rec.get("icon_url", ""),
                **{key: rec.get(key, "") for key in (
                    "collection_schema", "attempt_id", "queried_appid", "queried_name",
                    "package_attribution", "identity_status",
                    "runtime_evidence_path", "runtime_evidence_sha256")},
            })
            written += 1
    return written


def check_env(require_keyboard=True):
    state = adb(["get-state"], check=False).stdout.strip()
    if state != "device":
        selected = ADB_SERIAL or "default"
        raise RuntimeError(f"ADB device {selected} is not connected/authorized (state={state!r}).")
    root = shell("su -c id", check=False).stdout
    if "uid=0" not in root:
        raise RuntimeError("Root not available: `adb shell su -c id` must return uid=0.")
    kb = shell(f"pm list packages {ADBKEYBOARD_PKG}", check=False).stdout
    has_kb = ADBKEYBOARD_PKG in kb
    if require_keyboard and not has_kb:
        raise RuntimeError(
            "ADBKeyboard is not installed. Chinese names cannot be typed reliably.\n"
            "Run: ./setup_adbkeyboard.sh"
        )
    print(f"[ok] adb device/root serial={ADB_SERIAL or 'default'}")
    print(f"[ok] ADBKeyboard installed: {has_kb}")
    return has_kb


def grant_wechat_scan_permissions():
    # Required for QR-code opening through WeChat's scanner. Root is already
    # required for package extraction, so grant directly instead of stopping at
    # Android permission dialogs.
    for perm in [
        "android.permission.CAMERA",
        "android.permission.READ_MEDIA_IMAGES",
        "android.permission.READ_EXTERNAL_STORAGE",
    ]:
        su(f"pm grant {WECHAT_PACKAGE} {perm}", check=False)


def screen_size():
    out = shell("wm size", check=False).stdout
    m = re.search(r"Physical size: (\d+)x(\d+)", out or "")
    return (int(m.group(1)), int(m.group(2))) if m else (1080, 2424)


def tap(x, y, wait=0.8):
    shell(f"input tap {int(x)} {int(y)}", check=False)
    time.sleep(wait)


def back(n=1):
    for _ in range(n):
        shell("input keyevent 4", check=False)
        time.sleep(0.8)


def enter():
    shell("input keyevent 66", check=False)
    time.sleep(1.5)


def input_ascii(text):
    safe = text.replace(" ", "%s")
    safe = re.sub(r"([&|;<>()$`\\\"'*!?\[\]{}])", r"\\\1", safe)
    shell(f"input text {safe}", check=False)


def input_unicode(text, has_adbkeyboard):
    if text.isascii():
        input_ascii(text)
        return
    if not has_adbkeyboard:
        raise RuntimeError("Need ADBKeyboard to input non-ASCII/Chinese text")
    shell(f"ime enable {ADBKEYBOARD_IME}", check=False)
    shell(f"ime set {ADBKEYBOARD_IME}", check=False)
    # Broadcast supports unicode if quoted correctly on host and device shell.
    quoted = shlex.quote(text)
    shell(f"am broadcast -a ADB_INPUT_TEXT --es msg {quoted}", check=False)
    time.sleep(0.5)


def clear_field():
    shell("input keyevent 123", check=False)
    for _ in range(80):
        shell("input keyevent 67", check=False)
    time.sleep(0.2)


def launch_wechat():
    # ``monkey`` resumes WeChat's last activity.  If the previous tap opened a
    # web/NFC page (for example Bus Pass Service), every following coordinate
    # would be applied to that page.  Always return to WeChat's real tabbed
    # launcher before navigating to Discover -> Mini Programs.
    shell(f"am start -n {WECHAT_PACKAGE}/.ui.LauncherUI >/dev/null 2>&1", check=False)
    time.sleep(2.5)


def force_stop_wechat():
    shell(f"am force-stop {WECHAT_PACKAGE}", check=False)
    time.sleep(1.0)


def current_focus():
    out = shell("dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'", check=False).stdout
    return out.strip()


def open_miniprogram_search():
    """Navigate strictly to the dedicated Mini Programs search tab.

    Never fall back to WeChat's global/ALL search: those results can select a
    contact, article, or unrelated Mini Program and associate the wrong package
    with the CSV row. Two full navigation attempts handle transient UI state;
    after that the current row fails and the caller continues with the next one.
    """
    w, h = screen_size()
    for navigation_attempt in range(1, 3):
        force_stop_wechat()
        launch_wechat()
        # Bottom nav: Discover, then the Mini Programs row.
        tap(w * 0.625, h * 0.955, 1.2)
        tap(w * 0.22, h * 0.515, 2.0)
        for _ in range(6):
            tap(w * 0.50, h * 0.147, 1.0)
            focus = current_focus()
            if "MMFTSSearchTabWebViewUI" in focus:
                return "mini_programs"
            time.sleep(0.8)
        print(
            f"[ui] dedicated Mini Programs search unavailable "
            f"(attempt {navigation_attempt}/2)",
            flush=True,
        )
    raise RuntimeError("Dedicated Mini Programs search unavailable; global/ALL search is disabled")


def open_global_search():
    """Open WeChat's top-level global search.

    Root cause of the previous bug: the search box rendered inside the
    Mini Programs landing page is not reachable by adb taps on this WeChat
    build, while the main WeChat search icon is reachable and stable.
    """
    w, h = screen_size()
    force_stop_wechat()
    launch_wechat()
    # Main WeChat top bar search icon, calibrated as a screen ratio so it
    # survives this device's 1080x2424 resolution and similar layouts.
    tap(w * 0.83, h * 0.098, 1.2)


def wait_for_appbrand(timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        f = current_focus()
        if "AppBrandUI" in f or "appbrand" in f.lower():
            return True
        time.sleep(1)
    return False


def open_by_qr_image(image_path, wait_seconds):
    """Open a Mini Program by scanning its weweplay Mini Program code image."""
    print(f"[ui] open via QR image: {image_path}")
    grant_wechat_scan_permissions()
    w, h = screen_size()
    remote = f"/sdcard/DCIM/wechat_scraper_qr_{int(time.time())}.jpg"
    adb(["push", str(image_path), remote], check=True, timeout=60)
    shell(f"am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d file://{remote} >/dev/null", check=False)
    time.sleep(1)
    su(f"am start -n {WECHAT_PACKAGE}/.plugin.scanner.ui.BaseScanUI", check=False)
    time.sleep(3)
    # Bottom-right album/gallery button in WeChat scanner.
    tap(w * 0.89, h * 0.845, 3)
    # Newest pushed image appears first after the Take Photo tile.
    tap(w * 0.375, h * 0.183, 5)
    opened = wait_for_appbrand(timeout=15)
    print(f"[ui] appbrand focus: {opened}")
    if opened:
        print(f"[ui] waiting {wait_seconds}s for package cache")
        time.sleep(wait_seconds)
    else:
        print("[ui] no Mini Program result opened; skipping to next CSV row")
    return opened


def search_and_open_name(name, has_adbkeyboard, wait_seconds, manual_before_type=False, manual_open_result=False):
    print(f"[ui] search/open: {name}")
    opened = False
    search_mode = "manual"
    if manual_before_type:
        print("[manual] On the device, open WeChat Mini Programs search and focus the search field.")
        input("[manual] When the search field is ready, press ENTER here: ")
        clear_field()
    else:
        search_mode = open_miniprogram_search()
    input_unicode(name, has_adbkeyboard)
    # ADBKeyboard often inserts text but does not submit the WeChat search with
    # KEYCODE_ENTER. Tap the visible green Search button as the primary submit
    # action, then send ENTER as a harmless fallback for layouts/keyboards that
    # do support it.
    w, h = screen_size()
    tap(w * 0.88, h * 0.098, 2.5)
    enter()
    time.sleep(3.0)
    if manual_open_result:
        print("[manual] On the device, open the correct Mini Program search result.")
        input("[manual] When the Mini Program has loaded, press ENTER here: ")
        opened = True
    else:
        w, h = screen_size()
        result_targets = [(0.25, 0.16), (0.25, 0.22), (0.50, 0.16)]
        for x_ratio, y_ratio in result_targets:
            tap(w * x_ratio, h * y_ratio, 1.5)
            if wait_for_appbrand(timeout=5):
                opened = True
                break
        print(f"[ui] appbrand focus: {opened}")
    if opened:
        print(f"[ui] waiting {wait_seconds}s for package cache")
        time.sleep(wait_seconds)
    else:
        print("[ui] no Mini Program result opened; skipping to next CSV row")
    return opened


def list_packages():
    cmd = "find /data/data/com.tencent.mm/MicroMsg -type f -name '*.wxapkg' 2>/dev/null"
    out = su(cmd, check=False, timeout=60).stdout
    return sorted(set(x.strip() for x in out.splitlines() if x.strip()))


def clear_appbrand_package_cache():
    """Remove cached Mini Program packages so each opened app must re-create/download its wxapkg.

    Keep commlib/runtime packages because those are shared WeChat runtime dependencies,
    not target app packages. The scraper is interested in app packages under general/
    and firstParty/.
    """
    cmd = (
        "find /data/data/com.tencent.mm/MicroMsg/appbrand/pkg "
        "\\( -path '*/general/*.wxapkg' -o -path '*/firstParty/*.wxapkg' \\) "
        "-type f -delete 2>/dev/null"
    )
    r = su(cmd, check=False, timeout=60)
    print(f"[cache] cleared appbrand target wxapkg cache rc={r.returncode}", flush=True)


def stat_remote(path):
    q = shlex.quote(path)
    out = su(f"stat -c '%Y %s' {q} 2>/dev/null", check=False).stdout.strip()
    parts = out.split()
    if len(parts) >= 2:
        return int(parts[0]), int(parts[1])
    return 0, 0


def package_priority(path):
    p = path.lower()
    # The searched Mini Program package is stored under general/.  Opening the
    # Mini Programs launcher also recreates several firstParty/ UI packages;
    # those must never win candidate selection over the actual target.
    if "/general/" in p:
        return 0
    if "/firstparty/" in p:
        return 20
    if "/commlib/" in p:
        return 30
    return 10


def choose_candidates(before, after, started_at, max_files, allow_cached_fallback=False):
    new = sorted(set(after) - set(before))
    if not new:
        if not allow_cached_fallback:
            return [], "none"
        # If already cached, choose app-specific packages first, newest first.
        rows = []
        for p in after:
            mt, sz = stat_remote(p)
            rows.append((package_priority(p), -mt, p, sz))
        rows.sort()
        return [r[2] for r in rows[:max_files]], "cached_fallback"
    rows = []
    for p in new:
        mt, sz = stat_remote(p)
        rows.append((package_priority(p), -mt, p, sz))
    rows.sort()
    return [r[2] for r in rows[:max_files]], "new_since_open"


def pull_for_app(remote_files, app_dir):
    app_dir.mkdir(parents=True, exist_ok=True)
    # Unique staging: no cleanup of previous attempts or cached bytes.
    import uuid
    remote_dir = f"{REMOTE_TMP_ROOT}/{uuid.uuid4().hex}"
    shell(f"mkdir -p {shlex.quote(remote_dir)}", check=True)
    copied = []
    for i, rp in enumerate(remote_files, 1):
        base = os.path.basename(rp)
        dest = f"{remote_dir}/{i:02d}_{base}"
        r = su(f"cp {shlex.quote(rp)} {shlex.quote(dest)} && chmod 0644 {shlex.quote(dest)}", check=False)
        if r.returncode == 0:
            copied.append((rp, dest))
    adb(["pull", remote_dir + "/.", str(app_dir)], check=False, timeout=120)
    return copied


def fetch_weweplay_metadata(row, qrcode_dir):
    meta = {"appid": "", "mini_code": ""}
    url = row.get("url", "")
    try:
        if url:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            html = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
            m = re.search(r"appid=([a-zA-Z0-9_\-]+)&name=", html)
            if m:
                meta["appid"] = m.group(1)
        # Detailed CSV exports store the WeWePlay document id as doc_id.  Copying
        # it into id lets continuation/retry runs fetch mini-code QR images and
        # avoid the fragile WeChat text-search screen.
        idv = first_nonempty(row.get("id", ""), row.get("doc_id", ""))
        if idv:
            data = urllib.parse.urlencode({"id": idv}).encode()
            req2 = urllib.request.Request(
                "https://www.weweplay.com/home/index/get_mini_code.html",
                data=data,
                headers={"User-Agent": "Mozilla/5.0", "X-Requested-With": "XMLHttpRequest"},
            )
            js = json.loads(urllib.request.urlopen(req2, timeout=20).read().decode("utf-8", "ignore"))
            rel = js.get("data", {}).get("mini_code", "")
            if rel:
                full = urllib.parse.urljoin("https://www.weweplay.com", rel)
                meta["mini_code"] = full
                qrcode_dir.mkdir(parents=True, exist_ok=True)
                out = qrcode_dir / f"{idv}_{slug(row.get('name',''))}.jpg"
                req3 = urllib.request.Request(full, headers={"User-Agent": "Mozilla/5.0"})
                out.write_bytes(urllib.request.urlopen(req3, timeout=20).read())
                meta["mini_code_local"] = str(out)
    except Exception as e:
        meta["metadata_error"] = str(e)
    return meta


def validate_retention_policy(manifest_path, clear_cache):
    if clear_cache:
        raise ValueError("cache_deletion_disabled: omit --clear-cache-before-open")
    if Path(manifest_path).exists():
        raise FileExistsError("Use a fresh --output; historical manifest retained")


def open_manifest_exclusive(path):
    """Never truncate previous manifests; select a fresh --output for each run."""
    return Path(path).open("x", newline="", encoding="utf-8")


def main():
    global ADB_SERIAL
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True, help="CSV with mini app names")
    ap.add_argument("--name-column", default="name")
    ap.add_argument("--id-column", default="id")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--packages-per-app", type=int, default=None, help=argparse.SUPPRESS)
    ap.add_argument("--decompiler-command", default=os.environ.get("MINIZOO_DECOMPILER_COMMAND", ""),
                    help="Optional JSON argv for the shared decompiler; the default uses wxappUnpacker")
    ap.add_argument("--wait-seconds", type=int, default=15)
    ap.add_argument("--output", default=str(Path(os.environ.get("WECHAT_DATA_DIR", str(ROOT.parent / "collection_results" / "initial"))) / f"csv_batch_{stamp()}"))
    ap.add_argument("--serial", default="", help="ADB device serial; required when multiple devices are connected")
    ap.add_argument("--frida", default=os.environ.get("WECHAT_FRIDA", ""), help="Frida CLI for observing the app actually opened")
    ap.add_argument("--prepare-only", action="store_true", help="prepare search/QR context without device access")
    ap.add_argument("--allow-no-adbkeyboard", action="store_true", help="allow ASCII-only adb input fallback")
    ap.add_argument("--open-method", choices=["auto", "qr", "search"], default="search", help="search by default; optional auto prefers an available QR code")
    ap.add_argument("--allow-cached-fallback", action="store_true", help="compatibility flag; cached files still require an exact runtime binding")
    ap.add_argument("--clear-cache-before-open", action="store_true", help="unsupported: cache deletion is disabled")
    ap.add_argument("--manual-before-type", action="store_true", help="you manually open/focus WeChat search; script only types each CSV name")
    ap.add_argument("--manual-open-result", action="store_true", help="after typing/searching, you manually open the correct result before dumping packages")
    args = ap.parse_args()
    ADB_SERIAL = args.serial.strip()

    csv_path = Path(args.csv).expanduser().resolve()
    out = Path(args.output).expanduser().resolve()
    app_root = out / "attempts"
    qr_root = out / "qr_codes"
    out.mkdir(parents=True, exist_ok=True)

    # utf-8-sig accepts regular UTF-8 files and strips a BOM when exporters
    # include one, keeping first-column names such as "id" or "appid" intact.
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    rows = rows[args.start: args.start + args.limit]

    manifest_path = out / "manifest.csv"
    history_path = out / "package_history.csv"
    fields = manifest_fields()
    validate_retention_policy(manifest_path, args.clear_cache_before_open)
    has_kb = False
    probe = None
    # Older launchers may still pass the former limit; it never truncates collection.
    if args.packages_per_app is not None:
        print("[compat] --packages-per-app is ignored; collecting all available components", flush=True)
    decompiler_command = json.loads(args.decompiler_command) if args.decompiler_command else None
    if decompiler_command is not None and (not isinstance(decompiler_command, list)
            or not decompiler_command or not all(isinstance(v, str) for v in decompiler_command)):
        raise ValueError("--decompiler-command must be a nonempty JSON argument list")
    if not args.prepare_only:
        frida = resolve_frida(args.frida)
        has_kb = check_env(require_keyboard=not args.allow_no_adbkeyboard)
        if not ADB_SERIAL:
            ADB_SERIAL = adb(["get-serialno"], timeout=15).stdout.strip()
        probe = RuntimeProbe(ADB_SERIAL, frida)

    with open_manifest_exclusive(manifest_path) as mf:
        writer = csv.DictWriter(mf, fieldnames=fields)
        writer.writeheader()
        for idx, row in enumerate(rows, 1):
            name = row.get(args.name_column, "").strip()
            rec = make_manifest_record(row, fields, id_column=args.id_column, name_column=args.name_column)
            rec["device_serial"] = ADB_SERIAL
            meta = fetch_weweplay_metadata(row, qr_root) if args.open_method != "search" else {}
            for k in ["mini_code", "mini_code_local"]:
                rec[k] = meta.get(k, "")
            if args.prepare_only:
                rec["status"] = "prepared"
                writer.writerow(rec); mf.flush(); continue
            opened = False
            app_dir = app_root / rec["attempt_id"]
            try:
                qr_local = rec.get("mini_code_local", "")
                if args.open_method in ["auto", "qr"] and qr_local:
                    opened = open_by_qr_image(qr_local, args.wait_seconds)
                    if not opened and args.open_method == "auto":
                        print(f"[ui] QR open failed, falling back to search: {name}", flush=True)
                        opened = search_and_open_name(
                            name,
                            has_kb,
                            args.wait_seconds,
                            manual_before_type=args.manual_before_type,
                            manual_open_result=args.manual_open_result,
                        )
                elif args.open_method == "qr":
                    raise RuntimeError("QR open requested but no mini_code_local was available")
                else:
                    opened = search_and_open_name(
                        name,
                        has_kb,
                        args.wait_seconds,
                        manual_before_type=args.manual_before_type,
                        manual_open_result=args.manual_open_result,
                    )
                if opened:
                    evidence, receipt = collect_opened_app(
                        probe, rec["attempt_id"], app_dir, pull_for_app)
                    apply_opened_collection(rec, evidence, receipt, out)
                    packages = [{"local_path": str(evidence.parent / p["local_file"]),
                                 "remote_path": p["remote_source"], "sha256": p["sha256"],
                                 "bytes": p["bytes"], "role": p["role"]} for p in receipt["packages"]]
                    processed = process_acquisition(
                        app_dir, packages, appid=receipt["appid"], attempt_id=rec["attempt_id"],
                        coverage=receipt["coverage"], decompiler_command=decompiler_command,
                        context={"collector": "initial", "requested_appid": rec["queried_appid"],
                                 "observed_version": receipt["metadata"].get("app_version"),
                                 "device": ADB_SERIAL, "observed_at": receipt["observed_at"]})
                    rec.update({k: processed[k] for k in ("package_acquisition_status", "metadata_status", "decompilation_status")})
                    rec["processing_path"] = str((app_dir / "processing.json").relative_to(out))
                    rec["processing_sha256"] = sha256(app_dir / "processing.json")
                else:
                    rec["status"] = "search_open_failed"
                    rec["selection_mode"] = "not_opened_skip"
                if rec["status"] == "ok":
                    print(
                        f"[PACKAGE_DOWNLOADED] {idx}/{len(rows)} observed_appid={rec['appid']} "
                        f"queried_appid={rec['queried_appid']} sha256={rec['sha256']}",
                        flush=True,
                    )
                else:
                    print(
                        f"[PACKAGE_NOT_DOWNLOADED] {idx}/{len(rows)} id={rec['csv_id']} name={name} "
                        f"status={rec['status']} files=0",
                        flush=True,
                    )
            except Exception as e:
                rec["status"] = ("opened_but_no_package" if isinstance(e, PackageUnavailable) else
                                 "opened_identity_unverified" if opened and isinstance(e, EvidenceError) else "error")
                rec["identity_status"] = "unverified" if opened else "not_observed"
                rec["error"] = str(e)
                retained = sorted(p for p in (app_dir / "packages").glob("*.wxapkg")
                                  if p.is_file() and not p.is_symlink())
                rec["local_files"] = ";".join(str(p.relative_to(out)) for p in retained)
                rec.update(package_file_metadata(retained))
                print(f"[PACKAGE_ERROR] {idx}/{len(rows)} id={rec['csv_id']} name={name} error={e}", flush=True)
            rec["collected_at"] = iso_now()
            writer.writerow(rec)
            mf.flush()
            metadata_rows = append_package_history(
                history_path, rec, out, source_run=out.name, collected_at=rec.get("collected_at"))
            if metadata_rows:
                print(f"[METADATA_RECORDED] {idx}/{len(rows)} observed_appid={rec['appid']} "
                      f"packages={metadata_rows} csv={out / 'package_metadata.csv'}", flush=True)
            back(2)

    print(f"DONE: {out}")
    print(f"Manifest: {manifest_path}")
    print(f"Package history: {history_path}")


if __name__ == "__main__":
    main()
