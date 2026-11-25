"""Restart photo download if the backup.py download process dies."""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKUP = ROOT / 'backup.py'


def _resolve_data_root() -> Path:
    """Prefer YANDEX_PHOTO_ROOT; never silently fall back to the system drive home."""
    env = os.environ.get('YANDEX_PHOTO_ROOT')
    if env:
        return Path(env)
    marker = ROOT / '.backup_root'
    if marker.is_file():
        line = marker.read_text(encoding='utf-8').strip().splitlines()[0].strip()
        if line:
            return Path(line)
    # Legacy Windows external disks used by this project
    for legacy in (Path(r'G:\YandexPhoto'), Path(r'D:\YandexPhoto')):
        if legacy.is_dir():
            return legacy
    return Path.home() / 'YandexPhoto'


DATA_ROOT = _resolve_data_root()
LOG = DATA_ROOT / 'logs' / 'download-adaptive.log'
# Photos first (DB order), then videos. No --with-md5-only: download everything.
DEFAULT_ARGS = [
    str(BACKUP),
    'download',
    '--media',
    'all',
    '--workers',
    '6',
]


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s | %(levelname)s | watchdog | %(message)s',
    )


def unpark_queue(data_root: Path) -> int:
    """Put failed / failed_permanent rows back into pending when the queue is empty."""
    db_path = data_root / 'yandex_photo_backup.sqlite'
    if not db_path.exists():
        return 0
    db = sqlite3.connect(db_path, timeout=120)
    row = db.execute(
        """
        SELECT
          SUM(CASE WHEN download_status IN ('pending', 'failed') THEN 1 ELSE 0 END),
          SUM(CASE WHEN verify_status != 'verified' THEN 1 ELSE 0 END)
        FROM objects
        """
    ).fetchone()
    pending = int(row[0] or 0)
    unverified = int(row[1] or 0)
    if pending > 0 or unverified == 0:
        db.close()
        return 0
    now = time.time()
    cur = db.execute(
        """
        UPDATE objects
        SET download_status = 'pending',
            verify_status = 'pending',
            attempts = 0,
            error = NULL,
            updated_at = ?
        WHERE verify_status != 'verified'
        """,
        (now,),
    )
    db.commit()
    n = cur.rowcount
    db.close()
    logging.info('Unparked %s non-verified objects for another download pass', n)
    return n


def download_running() -> bool:
    """Return True if a backup.py download process is alive (Windows)."""
    try:
        out = subprocess.check_output(
            [
                'powershell',
                '-NoProfile',
                '-Command',
                'Get-CimInstance Win32_Process -Filter "Name=\'python.exe\'" | '
                "Where-Object { "
                "$_.CommandLine -match 'backup\\.py\\s+download' "
                "-and $_.CommandLine -notmatch 'watchdog_download' "
                '} | Select-Object -ExpandProperty ProcessId',
            ],
            text=True,
            timeout=60,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False
    return any(line.strip().isdigit() for line in out.splitlines())


def start_download(extra_args: list[str]) -> subprocess.Popen:
    """Spawn download with unbuffered Python, appending to the adaptive log."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    log_f = LOG.open('a', encoding='utf-8')
    env = dict(**{k: v for k, v in os.environ.items()})
    env['PYTHONUNBUFFERED'] = '1'
    env.setdefault('YANDEX_PHOTO_MAX_ATTEMPTS', '0')
    env.setdefault('YANDEX_PHOTO_ROOT', str(DATA_ROOT))
    cmd = [sys.executable, '-u', *extra_args]
    logging.info('Starting: %s', ' '.join(cmd))
    return subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        stdout=log_f,
        stderr=subprocess.STDOUT,
        env=env,
    )


def main() -> int:
    """Poll and restart download until interrupted."""
    _setup_logging()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--interval', type=int, default=180, help='Check interval seconds')
    ap.add_argument(
        '--args',
        nargs=argparse.REMAINDER,
        default=None,
        help='Override download args after --',
    )
    args = ap.parse_args()
    dl_args = args.args if args.args else DEFAULT_ARGS
    if dl_args and dl_args[0] == '--':
        dl_args = dl_args[1:]
    child: subprocess.Popen | None = None
    try:
        while True:
            alive = download_running()
            if child is not None and child.poll() is not None:
                logging.warning('Child exited code=%s', child.returncode)
                child = None
            if not alive:
                unpark_queue(DATA_ROOT)
                logging.warning('Download not running — restarting')
                child = start_download(dl_args)
                time.sleep(15)
                if not download_running():
                    logging.error('Restart failed to stay up')
            else:
                logging.info('Download alive')
            time.sleep(max(30, int(args.interval)))
    except KeyboardInterrupt:
        logging.info('Watchdog stopped')
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
