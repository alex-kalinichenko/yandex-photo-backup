"""Restart photo download if the backup.py download process dies."""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKUP = ROOT / 'backup.py'
DATA_ROOT = Path(os.environ.get('YANDEX_PHOTO_ROOT', Path.home() / 'YandexPhoto'))
LOG = DATA_ROOT / 'logs' / 'download-adaptive.log'
DEFAULT_ARGS = [
    str(BACKUP),
    'download',
    '--media',
    'photos',
    '--with-md5-only',
    '--workers',
    '6',
]


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s | %(levelname)s | watchdog | %(message)s',
    )


def download_running() -> bool:
    """Return True if a backup.py download process is alive (Windows)."""
    try:
        out = subprocess.check_output(
            [
                'powershell',
                '-NoProfile',
                '-Command',
                "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                "Where-Object { $_.CommandLine -match 'backup.py download' } | "
                'Select-Object -ExpandProperty ProcessId',
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
    env = dict(**{k: v for k, v in __import__('os').environ.items()})
    env['PYTHONUNBUFFERED'] = '1'
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
