#!/usr/bin/env python3
"""Retry dual-device WeChat scraping every four hours until both devices collect.

The supervisor owns disjoint failure-first inputs, 10-minute canonical merge,
10-global-failure stop, final merge/dedupe/status rebuild, local catalogue rebuild and source retention.
"""
from __future__ import annotations

import csv
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time
from datetime import datetime

if __package__:
    from .wechat_config import Settings, exclusive_lock, shard_missing
else:
    from wechat_config import Settings, exclusive_lock, shard_missing

SETTINGS = Settings.from_env()
SCRIPTS = Path(__file__).resolve().parent
ROOT = SETTINGS.root
STATE = SETTINGS.state
SUPERVISOR = SCRIPTS / 'wechat_dual_scrape_supervisor.py'
PIDFILE = STATE / 'daily_retry_daemon.pid'
STATE_JSON = STATE / 'daily_retry_state.json'
STATUS_CSV = SETTINGS.data / 'all_mini_apps' / 'mini_app_scraping_status.csv'
ACTIVE_MARKER = SETTINGS.data / 'active_runs' / 'CURRENT_ALL_MINI_APPS_RUN.txt'
DEVICES = SETTINGS.devices
RETRY_SECONDS = 4 * 3600
HEALTH_WINDOW_SECONDS = 600
MAX_ATTEMPTS = 6



def log(message: str) -> None:
    print(f'[{datetime.now().isoformat(timespec="seconds")}] {message}', flush=True)


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def supervisor_pids() -> list[int]:
    result = subprocess.run(['ps', '-axo', 'pid=,command='], text=True, capture_output=True)
    found = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        try:
            argv = shlex.split(parts[1])
        except ValueError:
            continue
        if not argv or not Path(argv[0]).name.startswith('python'):
            continue
        if str(SUPERVISOR) not in argv[1:]:
            continue
        try:
            found.append(int(parts[0]))
        except ValueError:
            pass
    return found


def connected_devices() -> list[str]:
    result = subprocess.run(['adb', 'devices'], text=True, capture_output=True, timeout=30)
    devices = []
    for line in result.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == 'device':
            devices.append(parts[0])
    return devices


def missing_count() -> int:
    if not STATUS_CSV.is_file():
        return -1
    with STATUS_CSV.open(encoding='utf-8-sig', newline='') as f:
        return sum(1 for row in csv.DictReader(f)
                   if row.get('is_scraped') != '1' and row.get('scrape_status') != 'scraped')


def active_run() -> Path | None:
    if not ACTIVE_MARKER.is_file():
        return None
    value = ACTIVE_MARKER.read_text(encoding='utf-8').strip()
    path = Path(value) if value else None
    return path if path and path.is_dir() else None


def device_is_collecting(run_dir: Path, serial: str) -> bool:
    device_dir = run_dir / f'device_{serial}'
    manifest = device_dir / 'manifest.csv'
    if not manifest.is_file():
        return False
    try:
        with manifest.open(encoding='utf-8-sig', newline='') as f:
            successful = any(
                row.get('status') == 'ok' and bool((row.get('sha256') or '').strip() or (row.get('local_files') or '').strip())
                for row in csv.DictReader(f)
            )
    except (OSError, csv.Error):
        return False
    return successful and any(device_dir.rglob('*.wxapkg'))


def request_graceful_stop(run_dir: Path, reason: str) -> None:
    (run_dir / 'STOP_REQUESTED').write_text(reason + '\n', encoding='utf-8')
    log(f'richiesto stop controllato: {reason}')


def wait_for_exit(proc: subprocess.Popen, seconds: int = 360) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return True
        time.sleep(10)
    return proc.poll() is not None


def write_state(**values) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    values['updated_at'] = datetime.now().isoformat(timespec='seconds')
    STATE_JSON.write_text(json.dumps(values, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def main() -> int:
    SETTINGS.check_storage()
    SETTINGS.require_devices()
    STATE.mkdir(parents=True, exist_ok=True)
    PIDFILE.write_text(f'{os.getpid()}\n', encoding='utf-8')
    try:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            write_state(status='checking', attempt=attempt, max_attempts=MAX_ATTEMPTS)
            if SETTINGS.mount and not Path(SETTINGS.mount).is_mount():
                log(f'attempt {attempt}: SSD not mounted; retrying in 4 hours')
                write_state(status='waiting_ssd', attempt=attempt)
                time.sleep(RETRY_SECONDS)
                continue
            existing = supervisor_pids()
            if existing:
                log(f'supervisor already running pid={existing}; no duplicate run started')
                write_state(status='supervisor_already_running', pids=existing, attempt=attempt)
                return 0
            remaining = missing_count()
            if remaining == 0:
                log('No missing Mini Apps: schedule completed without starting the scraper')
                write_state(status='nothing_missing', attempt=attempt)
                return 0
            if remaining < 0:
                log('Canonical CSV unavailable; retrying in 4 hours')
                write_state(status='waiting_canonical_csv', attempt=attempt)
                time.sleep(RETRY_SECONDS)
                continue
            connected = connected_devices()
            absent = [serial for serial in DEVICES if serial not in connected]
            if absent:
                log(f'attempt {attempt}: missing or unauthorized devices {absent}; retrying in 4 hours')
                write_state(status='waiting_devices', connected=connected, missing_devices=absent, attempt=attempt)
                time.sleep(RETRY_SECONDS)
                continue

            attempt_log = STATE / f'supervisor_attempt_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'
            with attempt_log.open('a', encoding='utf-8') as output:
                proc = subprocess.Popen(
                    [sys.executable, '-u', str(SUPERVISOR)], cwd=str(ROOT),
                    stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                    start_new_session=True, close_fds=True,
                )
            log(f'attempt {attempt}: supervisor started pid={proc.pid}; missing={remaining}; log={attempt_log}')
            write_state(status='verifying_both_devices', attempt=attempt, supervisor_pid=proc.pid,
                        missing=remaining, log=str(attempt_log))
            deadline = time.monotonic() + HEALTH_WINDOW_SECONDS
            healthy = False
            run_dir = None
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                run_dir = active_run()
                if run_dir and all(device_is_collecting(run_dir, serial) for serial in DEVICES):
                    healthy = True
                    break
                time.sleep(20)
            if healthy:
                log(f'device attivi {DEVICES} stanno collezionando package; supervisor pid={proc.pid} lasciato attivo')
                write_state(status='running_healthy', attempt=attempt, supervisor_pid=proc.pid,
                            run_dir=str(run_dir), devices=DEVICES)
                return 0

            run_dir = active_run()
            if proc.poll() is None and not run_dir:
                write_state(status='supervisor_without_run_marker', attempt=attempt, supervisor_pid=proc.pid)
                return 1
            if run_dir and proc.poll() is None:
                request_graceful_stop(run_dir, 'both_devices_not_collecting_within_10_minutes')
                if not wait_for_exit(proc):
                    log('supervisor did not exit after a graceful stop; no overlapping run will start')
                    write_state(status='stop_timeout', attempt=attempt, supervisor_pid=proc.pid, run_dir=str(run_dir))
                    return 1
            log(f'attempt {attempt} failed; merge and finalization completed, retrying in 4 hours')
            write_state(status='waiting_retry', attempt=attempt)
            time.sleep(RETRY_SECONDS)

        log('six-attempt limit reached; the configured daily schedule will retry at 19:30')
        write_state(status='daily_attempt_limit_reached', attempts=MAX_ATTEMPTS)
        return 1
    finally:
        try:
            if PIDFILE.is_file() and PIDFILE.read_text().strip() == str(os.getpid()):
                PIDFILE.unlink()
        except OSError:
            pass


if __name__ == '__main__':
    SETTINGS.check_storage()
    with exclusive_lock(STATE / 'daily_retry.lock'):
        raise SystemExit(main())
