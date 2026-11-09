#!/usr/bin/env python3
"""Stop all WeChat scraper workers after a global streak of failed rows."""
from __future__ import annotations

import argparse
import csv
import os
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path

BAD_STATUSES = {
    'search_open_failed',
    'opened_but_no_package',
    'error',
    'package_not_downloaded',
    'not_opened_skip',
}


def is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def stop_pid(pid: int) -> bool:
    if not is_running(pid):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    time.sleep(3)
    if is_running(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return True


def parse_time(value: str) -> str:
    return value or ''


def read_events(base: Path) -> list[dict[str, str]]:
    events = []
    for manifest in sorted(base.glob('device_*/manifest.csv')):
        serial = manifest.parent.name.removeprefix('device_')
        try:
            with manifest.open(encoding='utf-8-sig', newline='') as f:
                for idx, row in enumerate(csv.DictReader(f)):
                    row['_serial'] = serial
                    row['_idx'] = str(idx)
                    row['_sort'] = parse_time(row.get('collected_at', '')) or f'{serial}:{idx:09d}'
                    events.append(row)
        except FileNotFoundError:
            pass
    return sorted(events, key=lambda r: (r.get('_sort',''), r.get('_serial',''), int(r.get('_idx','0'))))


def global_streak(events: list[dict[str, str]]) -> int:
    streak = 0
    for row in events:
        status = (row.get('status') or '').strip()
        has_pkg = bool((row.get('sha256') or '').strip() or (row.get('local_files') or '').strip())
        if status == 'ok' and has_pkg:
            streak = 0
        else:
            streak += 1
    return streak


def status_counts(events: list[dict[str, str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in events:
        status = row.get('status') or ''
        counts[status] = counts.get(status, 0) + 1
    return counts


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument('--base', type=Path, required=True)
    p.add_argument('--threshold', type=int, default=10)
    p.add_argument('--interval', type=int, default=60)
    p.add_argument('--worker', action='append', default=[], help='serial:pid')
    args = p.parse_args()
    workers = {}
    for item in args.worker:
        serial, pid = item.rsplit(':', 1)
        workers[serial] = int(pid)
    print(f"[global-watchdog] started base={args.base} threshold={args.threshold} interval={args.interval}s workers={workers}", flush=True)
    stopped = False
    while True:
        events = read_events(args.base)
        streak = global_streak(events)
        running = {s: is_running(pid) for s, pid in workers.items()}
        print(f"[global-watchdog] rows={len(events)} global_streak_bad={streak} running={running} statuses={status_counts(events)}", flush=True)
        if streak >= args.threshold and any(running.values()):
            print(f"[global-watchdog] STOP ALL: {streak} consecutive failures >= {args.threshold}", flush=True)
            for serial, pid in workers.items():
                did = stop_pid(pid)
                print(f"[global-watchdog] stopped={did} serial={serial} pid={pid}", flush=True)
            stopped = True
        if stopped or not any(is_running(pid) for pid in workers.values()):
            print('[global-watchdog] all workers done/stopped; exiting', flush=True)
            return 0
        time.sleep(args.interval)


if __name__ == '__main__':
    raise SystemExit(main())
