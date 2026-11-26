"""Export unverified photos/videos for manual recovery (links + local targets)."""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from yandex_photo_backup.config import Config  # noqa: E402
from yandex_photo_backup.downloader import local_path_for  # noqa: E402


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
    def _fetch(video_only: bool) -> list[sqlite3.Row]:
        if video_only:
            media = "COALESCE(media_type, '') LIKE 'video/%'"
        else:
            media = "COALESCE(media_type, '') NOT LIKE 'video/%'"
        return list(
            db.execute(
                f"""
                SELECT remote_id, name, remote_path, original_url, size, md5,
                       taken_at, year, month, download_status, verify_status,
                       error, media_type
                FROM objects
                WHERE {media} AND verify_status != 'verified'
                ORDER BY year, month, name
                """
            )
        )

    photo_rows = _fetch(False)
    video_rows = _fetch(True)
    db.close()

    stamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    video_tsv = out / 'failed_videos_manual.tsv'
    video_md = out / 'failed_videos_manual.md'

    def _write_tsv(path: Path, rows: list[sqlite3.Row]) -> None:
        with path.open('w', encoding='utf-8', newline='') as fh:
            writer = csv.writer(fh, delimiter='\t')
            writer.writerow(
                [
                    'name',
                    'size_bytes',
                    'taken_at',
                    'year',
                    'month',
                    'expected_local_path',
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
                local = local_path_for(row, cfg.files_dir)
                writer.writerow(
                    [
                        row['name'],
                        row['size'],
                        row['taken_at'],
                        row['year'],
                        row['month'],
                        str(local),
                        row['remote_path'],
                        _disk_client_url(row['remote_path']),
                        row['original_url'] or '',
                        row['md5'] or '',
                        row['download_status'],
                        (row['error'] or '').replace('\t', ' ').replace('\n', ' '),
                        row['remote_id'],
                    ]
                )

    def _write_md(path: Path, rows: list[sqlite3.Row], title: str) -> None:
        with path.open('w', encoding='utf-8') as fh:
            fh.write(f'# {title}\n\n')
            fh.write(f'Generated: **{stamp}**\n\n')
            fh.write(f'Total: **{len(rows)}** files\n\n')
            fh.write(
                '`remote_path` is the Yandex cloud path (find in web UI). '
                '`expected_local_path` is where this backup expects the file on disk '
                f'under `{cfg.files_dir}`.\n\n'
            )
            fh.write(
                'Signed `original_url` values expire; open `disk_web_url` in a logged-in '
                'browser or find the file by name/path in Yandex Disk / Photos.\n\n'
            )
            fh.write('---\n\n')
            for row in rows:
                size_mb = (int(row['size'] or 0)) / (1024 * 1024)
                local = local_path_for(row, cfg.files_dir)
                fh.write(f'## {row["name"]}\n\n')
                fh.write(f'- **Size:** {size_mb:.2f} MB\n')
                if row['taken_at']:
                    fh.write(f'- **Taken:** {row["taken_at"]}\n')
                fh.write(f'- **Save as (local backup):** `{local}`\n')
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

    _write_tsv(tsv_path, photo_rows)
    _write_md(md_path, photo_rows, 'Photos not verified in backup (manual download list)')
    if video_rows:
        _write_tsv(video_tsv, video_rows)
        _write_md(video_md, video_rows, 'Videos not verified in backup (manual download list)')

    print(f'Wrote {len(photo_rows)} rows to {tsv_path}')
    print(f'Wrote {md_path}')
    if video_rows:
        print(f'Wrote {len(video_rows)} rows to {video_tsv}')
        print(f'Wrote {video_md}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
