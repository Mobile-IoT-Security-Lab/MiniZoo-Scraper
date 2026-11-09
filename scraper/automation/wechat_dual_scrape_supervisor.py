#!/usr/bin/env python3
"""Start/monitor dual-device WeChat Mini App scraping.

- Uses mini_app_scraping_status.csv to select only missing apps.
- Splits inputs between the two connected devices.
- Merges new packages into local canonical storage every 10 minutes.
- Stops both workers after 10 consecutive global failures.
- On stop/completion: final merge, duplicate report, local CSV rebuild.
- Preserve source runs, package bytes and metadata evidence unconditionally.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os

import signal
import subprocess
import sys
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

if __package__:
    from .wechat_config import Settings, exclusive_lock, shard_missing
else:
    from wechat_config import Settings, exclusive_lock, shard_missing

SETTINGS = Settings.from_env()
SCRIPTS = Path(__file__).resolve().parent
ROOT = SETTINGS.root
STATE = SETTINGS.state
OUT = SETTINGS.data / 'all_mini_apps'
ACTIVE_ROOT = SETTINGS.data / 'active_runs'
CURRENT_MARKER = ACTIVE_ROOT / 'CURRENT_ALL_MINI_APPS_RUN.txt'
STATUS_CSV = OUT / 'mini_app_scraping_status.csv'
BUILDER = ROOT / 'build_final_miniapp_csvs.py'
MERGE = SCRIPTS / 'wechat_active_merge.py'
DEVICES = SETTINGS.devices



def run(cmd, *, cwd=ROOT, check=True, timeout=None, stdout=None, stderr=None):
    p = subprocess.run(cmd, cwd=str(cwd), text=True, capture_output=(stdout is None and stderr is None), stdout=stdout, stderr=stderr, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"command failed {p.returncode}: {' '.join(map(str, cmd))}\n{p.stdout or ''}\n{p.stderr or ''}")
    return p


def stream(msg: str) -> None:
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)


def missing_rows() -> tuple[list[str], list[dict[str, str]]]:
    rows = read_csv(STATUS_CSV)
    fields = list(rows[0].keys()) if rows else []
    selected = [r for r in rows if (r.get('is_scraped') != '1' and r.get('scrape_status') != 'scraped')]
    return fields, selected


def prepare_run() -> Path:
    ACTIVE_ROOT.mkdir(parents=True, exist_ok=True)
    run_dir = ACTIVE_ROOT / f"wechat_dual_missing_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex}"
    run_dir.mkdir(parents=True)
    fields, rows = missing_rows()
    if not rows:
        raise RuntimeError('No missing Mini Apps left in mini_app_scraping_status.csv')
    # Retry unresolved historical failures first, then apps with no recorded
    # attempt. Python's stable sort preserves the catalog order within groups.
    def was_attempted(row: dict[str, str]) -> bool:
        return bool((row.get('statuses_seen') or '').strip() or (row.get('errors_seen') or '').strip())
    rows.sort(key=lambda row: 0 if was_attempted(row) else 1)
    retry_count = sum(was_attempted(row) for row in rows)
    shards = shard_missing(rows, DEVICES)
    for serial, shard in shards.items():
        input_csv = run_dir / f'input_{serial}.csv'
        write_csv(input_csv, fields, shard)
        (run_dir / f'device_{serial}').mkdir(parents=True, exist_ok=True)
        stream(f"input {serial}: {len(shard)} missing apps -> {input_csv}")
    CURRENT_MARKER.write_text(str(run_dir) + '\n', encoding='utf-8')
    (run_dir / 'run_setup.json').write_text(json.dumps({
        'created_at': datetime.now(timezone.utc).isoformat(),
        'devices': DEVICES,
        'total_missing_input': len(rows),
        'unresolved_retry_input': retry_count,
        'never_attempted_input': len(rows) - retry_count,
        'per_device': {s: len(shards[s]) for s in DEVICES},
        'policy': 'active-device split; unresolved failures first, then never-attempted missing apps; stop after 10 global consecutive failures; local merge every 10m; preserve all source history',
    }, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return run_dir


def wake_devices() -> None:
    for serial in DEVICES:
        for cmd in [
            ['adb', '-s', serial, 'shell', 'input', 'keyevent', 'KEYCODE_WAKEUP'],
            ['adb', '-s', serial, 'shell', 'wm', 'dismiss-keyguard'],
        ]:
            run(cmd, check=False, timeout=30)


def start_workers(run_dir: Path) -> dict[str, subprocess.Popen]:
    # Validate every output before spawning even the first worker. Never reuse
    # manifests, metadata sidecars, logs or symlinked output from an older run.
    for serial in DEVICES:
        out_dir = run_dir / f'device_{serial}'
        if (not out_dir.is_dir() or any(p.is_symlink() for p in (out_dir, *out_dir.parents))
                or any(out_dir.iterdir())):
            raise ValueError(f'fresh_worker_output_required: {out_dir}')
    workers: dict[str, subprocess.Popen] = {}
    try:
        for serial in DEVICES:
            out_dir = run_dir / f'device_{serial}'
            input_csv = run_dir / f'input_{serial}.csv'
            limit = len(read_csv(input_csv))
            if not limit:
                continue
            log = out_dir / 'run.log'
            cmd = [
                sys.executable, '-u', 'wechat_csv_scraper.py',
                '--csv', str(input_csv),
                '--id-column', 'appid',
                '--name-column', 'name',
                '--limit', str(limit),
                '--start', '0',
                '--wait-seconds', '15',
                '--output', str(out_dir),
                '--serial', serial,
                '--open-method', 'search',
            ]
            with log.open('a', encoding='utf-8') as lf:
                p = subprocess.Popen(cmd, cwd=str(ROOT), stdout=lf, stderr=subprocess.STDOUT, text=True, start_new_session=True)
            workers[serial] = p
            (out_dir / 'pid').write_text(str(p.pid) + '\n', encoding='utf-8')
            stream(f"started {serial}: pid={p.pid} log={log}")
    except BaseException:
        terminate_workers(workers)
        raise
    return workers


def read_events(run_dir: Path) -> list[dict[str, str]]:
    events = []
    for manifest in sorted(run_dir.glob('device_*/manifest.csv')):
        serial = manifest.parent.name.removeprefix('device_')
        try:
            with manifest.open(encoding='utf-8-sig', newline='') as f:
                for idx, row in enumerate(csv.DictReader(f)):
                    row['_serial'] = serial
                    row['_idx'] = str(idx)
                    row['_sort'] = row.get('collected_at') or f'{serial}:{idx:09d}'
                    events.append(row)
        except FileNotFoundError:
            pass
    return sorted(events, key=lambda r: (r.get('_sort',''), r.get('_serial',''), int(r.get('_idx','0'))))


def global_bad_streak(events: list[dict[str, str]]) -> int:
    streak = 0
    for row in events:
        status = (row.get('status') or '').strip()
        has_pkg = bool((row.get('sha256') or '').strip() or (row.get('local_files') or '').strip())
        if status == 'ok' and has_pkg:
            streak = 0
        else:
            streak += 1
    return streak


def terminate_workers(workers: dict[str, subprocess.Popen]) -> None:
    if not workers:
        return
    # A worker can exit while its ADB descendants still own its process group.
    for serial, p in workers.items():
        stream(f'terminating process group {serial} pgid={p.pid}')
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    time.sleep(5)
    for p in workers.values():
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    for p in workers.values():
        p.wait(timeout=30)


def merge_once(label: str) -> None:
    stream(f"local merge start: {label}")
    p = run([sys.executable, str(MERGE)], check=False, timeout=1800)
    if p.stdout.strip():
        print(p.stdout.strip(), flush=True)
    if p.stderr.strip():
        print(p.stderr.strip(), flush=True)
    if p.returncode != 0:
        raise RuntimeError(f'local merge failed rc={p.returncode}; retaining active run')
    else:
        stream(f"local merge done: {label}")


def dedupe_canonical() -> dict[str, int]:
    by_hash: dict[str, list[Path]] = defaultdict(list)
    for p in sorted(OUT.glob('wx*/*.wxapkg')):
        by_hash[sha256(p)].append(p)
    # Report duplicates without breaking historical manifest/path references.
    return {'removed_duplicate_files': 0, 'removed_empty_dirs': 0,
            'preserved_duplicate_files': sum(len(paths) - 1 for paths in by_hash.values())}


def clean_junk(run_dir: Path) -> None:
    # Retention is unconditional, including WECHAT_CLEANUP_VERIFIED_RUN=1.
    # Manifests, extraction sidecars, logs, marker and package paths are evidence.
    stream(f'Retention policy: no deletion; preserving run and canonical history: {run_dir}')


def final_catalogue_rebuild(reason: str) -> None:
    run([sys.executable, str(BUILDER)], check=True, timeout=900)
    stream(f'final local catalogue rebuilt: {reason}')


def pause_automation_crons() -> None:
    for job in SETTINGS.cron_jobs:
        run(['hermes', 'cron', 'pause', job], cwd=Path.home(), check=False, timeout=120)
        stream(f'paused WeChat automation cron job {job}')


def main() -> int:
    stream('dual scrape supervisor starting')
    SETTINGS.check_storage()
    SETTINGS.require_devices()
    for serial in DEVICES:
        if run(['adb', '-s', serial, 'get-state'], timeout=30).stdout.strip() != 'device':
            raise RuntimeError(f'Device unavailable: {serial}')
    for job in SETTINGS.cron_jobs:
        run(['hermes', 'cron', 'resume', job], cwd=Path.home(), check=False, timeout=120)
    run_dir = prepare_run()
    def stop_signal(signum, frame):
        raise KeyboardInterrupt(f'Stop signal {signum}')
    signal.signal(signal.SIGTERM, stop_signal)
    workers: dict[str, subprocess.Popen] = {}
    # Internal timer by default; explicit Hermes jobs may own periodic merges.
    next_merge = time.monotonic() + 600
    stop_reason = 'completed'
    try:
        wake_devices()
        workers = start_workers(run_dir)
        while True:
            events = read_events(run_dir)
            streak = global_bad_streak(events)
            counts = Counter((r.get('status') or '') for r in events)
            running = {s: p.poll() is None for s, p in workers.items()}
            stream(f"monitor rows={len(events)} streak={streak} running={running} statuses={dict(counts)}")
            if (run_dir / 'STOP_REQUESTED').is_file() and any(running.values()):
                requested = (run_dir / 'STOP_REQUESTED').read_text(encoding='utf-8').strip() or 'external_stop_request'
                stop_reason = f'stopped_by_launcher_{requested}'
                terminate_workers(workers)
                break
            if streak >= 10 and any(running.values()):
                stop_reason = 'stopped_after_10_global_failures'
                stream(f'failure threshold reached: observed_streak={streak}, threshold=10')
                (run_dir / 'STOP_REASON.txt').write_text(stop_reason + '\n', encoding='utf-8')
                terminate_workers(workers)
                break
            if not any(running.values()):
                break
            if not SETTINGS.cron_jobs and time.monotonic() >= next_merge:
                merge_once('periodic')
                next_merge = time.monotonic() + 600
            # Poll faster than the workers can normally append rows so the
            # global threshold is acted on at 10 rather than several rows later.
            time.sleep(2)
    except BaseException:
        stop_reason = 'interrupted_or_monitor_error'
        raise
    finally:
        terminate_workers(workers)
        pause_automation_crons()
        stream(f'finalizing reason={stop_reason}')
        merge_once('final')
        stats = dedupe_canonical()
        stream(f'dedupe stats: {stats}')
        final_catalogue_rebuild(stop_reason)
        if stop_reason == 'completed' and os.environ.get('WECHAT_CLEANUP_VERIFIED_RUN') == '1':
            clean_junk(run_dir)
        else:
            stream(f'Retaining source run for audit/recovery: {run_dir}')
        stream('dual scrape supervisor finished')
    return 0


if __name__ == '__main__':
    SETTINGS.check_storage()
    with exclusive_lock(STATE / 'supervisor.lock'):
        raise SystemExit(main())
