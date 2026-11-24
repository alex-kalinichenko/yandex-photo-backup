"""Export photos that are not verified for manual recovery (links + paths)."""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from yandex_photo_backup.config import Config  # noqa: E402


def _disk_client_url(remote_path: str | None) -> str:
    if not remote_path:
        return ''
    p = remote_path.replace('\\', '/')
    if p.startswith('/disk/'):
        rel = p[len('/disk/') :]
        return 'https://disk.yandex.ru/client/disk/' + quote(rel, safe='/')
    if p.startswith('/photounlim/'):
        return 'https://disk.yandex.ru/client/photo'
    return 'https://disk.yandex.ru/client/photo'


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root', type=Path, default=None)
    ap.add_argument(
        '--out-dir',
        type=Path,
        default=None,
        help='Output directory (default: backup root on disk)',
    )
    args = ap.parse_args()

    cfg = Config.from_env(args.root)
    out = args.out_dir or cfg.root
    out.mkdir(parents=True, exist_ok=True)
    tsv_path = out / 'failed_photos_manual.tsv'
    md_path = out / 'failed_photos_manual.md'

    db = sqlite3.connect(cfg.db_path, timeout=30)
    db.row_factory = sqlite3.Row
    rows = list(
        db.execute(
            """
            SELECT remote_id, name, remote_path, original_url, size, md5,
                   taken_at, year, month, download_status, verify_status, error
            FROM objects
            WHERE COALESCE(media_type, '') NOT LIKE 'video/%'
              AND verify_status != 'verified'
            ORDER BY year, month, name
            """
        )
    )
    db.close()

    with tsv_path.open('w', encoding='utf-8', newline='') as fh:
        writer = csv.writer(fh, delimiter='\t')
        writer.writerow(
            [
                'name',
                'size_bytes',
                'taken_at',
                'year',
                'month',
                'remote_path',
                'disk_web_url',
                'original_url',
                'md5',
                'download_status',
                'error',
                'remote_id',
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row['name'],
                    row['size'],
                    row['taken_at'],
                    row['year'],
                    row['month'],
                    row['remote_path'],
                    _disk_client_url(row['remote_path']),
                    row['original_url'] or '',
                    row['md5'] or '',
                    row['download_status'],
                    (row['error'] or '').replace('\t', ' ').replace('\n', ' '),
                    row['remote_id'],
                ]
            )

    with md_path.open('w', encoding='utf-8') as fh:
        fh.write('# Photos not verified in backup (manual download list)\n\n')
        fh.write(f'Total: **{len(rows)}** files\n\n')
        fh.write(
            'Signed `original_url` values expire; open `disk_web_url` in a logged-in '
            'browser or find the file by name/path in Yandex Disk / Photos.\n\n'
        )
        fh.write('---\n\n')
        for row in rows:
            size_mb = (int(row['size'] or 0)) / (1024 * 1024)
            fh.write(f'## {row["name"]}\n\n')
            fh.write(f'- **Size:** {size_mb:.2f} MB\n')
            if row['taken_at']:
                fh.write(f'- **Taken:** {row["taken_at"]}\n')
            if row['remote_path']:
                fh.write(f'- **Remote path:** `{row["remote_path"]}`\n')
            disk_url = _disk_client_url(row['remote_path'])
            if disk_url:
                fh.write(f'- **Open in Disk (web):** {disk_url}\n')
            if row['original_url']:
                fh.write(f'- **CDN URL (may be expired):** {row["original_url"]}\n')
            fh.write(
                f'- **Status:** {row["download_status"]} — {row["error"] or "n/a"}\n'
            )
            fh.write(f'- **MD5:** `{row["md5"] or ""}`\n\n')

    print(f'Wrote {len(rows)} rows to {tsv_path}')
    print(f'Wrote {md_path}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
