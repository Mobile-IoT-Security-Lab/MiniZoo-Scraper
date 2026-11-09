#!/usr/bin/env python3
"""Daily detached launcher for the dual-device WeChat scraper retry daemon."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

if __package__:
    from .wechat_config import Settings, exclusive_lock, shard_missing
else:
    from wechat_config import Settings, exclusive_lock, shard_missing

SETTINGS = Settings.from_env()
SCRIPTS = Path(__file__).resolve().parent
ROOT = SETTINGS.root
STATE = SETTINGS.state
DAEMON = SCRIPTS / 'wechat_daily_retry_daemon.py'
PIDFILE = STATE / 'daily_retry_daemon.pid'
LOG = STATE / 'daily_retry_daemon.log'
MIGRATION_MARKER = STATE / 'MAINTENANCE_IN_PROGRESS'



def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def main() -> int:
    SETTINGS.check_storage()
    SETTINGS.require_devices()
    STATE.mkdir(parents=True, exist_ok=True)
    if MIGRATION_MARKER.is_file():
        print('Collection deferred: local maintenance is in progress.')
        return 0
    if PIDFILE.is_file():
        try:
            old_pid = int(PIDFILE.read_text().strip())
        except ValueError:
            old_pid = 0
        if old_pid and alive(old_pid):
            # A live recorded PID blocks another launch (conservative on PID reuse).
            return 0
        PIDFILE.unlink(missing_ok=True)
    log = LOG.open('a', encoding='utf-8')
    proc = subprocess.Popen(
        [sys.executable, '-u', str(DAEMON)],
        cwd=str(STATE), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True, close_fds=True,
    )
    PIDFILE.write_text(f'{proc.pid}\n', encoding='utf-8')
    # Logs remain local; no messaging integration is required.
    return 0


if __name__ == '__main__':
    SETTINGS.check_storage()
    with exclusive_lock(STATE / 'kickoff.lock'):
        raise SystemExit(main())
