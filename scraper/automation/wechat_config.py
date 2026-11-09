"""Public automation configuration; importing this module performs no I/O."""
from __future__ import annotations

import fcntl
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


def parse_devices(value: str) -> list[str]:
    devices = [part.strip() for part in value.split(',') if part.strip()]
    if not devices or len(devices) != len(set(devices)):
        raise ValueError('WECHAT_DEVICES must contain unique comma-separated ADB serials')
    if any(not re.fullmatch(r'[A-Za-z0-9_.:-]+', serial) or serial in ('.', '..') for serial in devices):
        raise ValueError('Invalid ADB serial (also used as a directory component)')
    return devices


def shard_missing(rows: list[dict], devices: list[str]) -> dict[str, list[dict]]:
    """Stable failure-first round robin, at most one input per appid."""
    parse_devices(','.join(devices))
    selected = []
    seen = set()
    # Prioritize historical attempts before deduplicating duplicate input IDs.
    for row in sorted(rows, key=lambda r: not bool((r.get('statuses_seen') or '').strip() or (r.get('errors_seen') or '').strip())):
        appid = (row.get('appid') or '').strip()
        if not re.fullmatch(r'wx[a-f0-9]{16}', appid):
            raise ValueError(f'Missing or invalid appid: {appid!r}')
        if row.get('is_scraped') == '1' or row.get('scrape_status') == 'scraped' or appid in seen:
            continue
        seen.add(appid)
        selected.append(row)
    return {serial: selected[index::len(devices)] for index, serial in enumerate(devices)}


@dataclass(frozen=True)
class Settings:
    root: Path
    state: Path
    devices: list[str]
    data: Path
    cron_jobs: list[str]
    mount: str

    @classmethod
    def from_env(cls) -> Settings:
        root = Path(os.environ.get('WECHAT_ROOT', str(Path(__file__).resolve().parent.parent))).expanduser().resolve()
        data = Path(os.environ.get('WECHAT_DATA_DIR', str(root.parent / 'collection_results' / 'initial'))).expanduser().resolve()
        raw_devices = os.environ.get('WECHAT_DEVICES', '')
        return cls(
            root=root,
            state=Path(os.environ.get('WECHAT_STATE_DIR', str(root.parent / 'collection_results' / 'automation'))).expanduser().resolve(),
            devices=parse_devices(raw_devices) if raw_devices else [],
            data=data,
            cron_jobs=[x.strip() for x in os.environ.get('WECHAT_HERMES_CRON_IDS', '').split(',') if x.strip()],
            mount=os.environ.get('WECHAT_REQUIRED_MOUNT', ''),
        )

    def check_storage(self) -> None:
        if self.mount and not Path(self.mount).is_mount():
            raise RuntimeError(f'Required volume not mounted: {self.mount}')
        if not self.root.is_dir():
            raise RuntimeError(f'Repository unavailable: {self.root}')

    def require_devices(self) -> None:
        parse_devices(','.join(self.devices))


@contextmanager
def exclusive_lock(path: Path):
    """Kernel lock: stale PID files cannot cause concurrent owners."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f'Another automation process owns {path}') from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
