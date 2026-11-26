"""Mark manifest rows verified when the expected local file already exists.

Used after SQLite recovery / re-collect when files on disk outlive the DB.
Builds an in-memory index of the backup tree (one filesystem walk), then matches
pending rows by relative path / (name, size) — avoids per-row stat storms on D:.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from yandex_photo_backup.config import Config  # noqa: E402
from yandex_photo_backup.db import ManifestDB  # noqa: E402
from yandex_photo_backup.downloader import local_path_for  # noqa: E402
from yandex_photo_backup.paths import year_month_subdir  # noqa: E402
from yandex_photo_backup.verify import sniff_is_error_page, verify_local_file  # noqa: E402

logger = logging.getLogger('relink_local_files')


def _index_files(
    files_dir: Path,
) -> tuple[dict[str, Path], dict[tuple[str, int], list[Path]]]:
    """Return (rel_posix_lower -> path, (name_lower, size) -> paths)."""
    by_rel: dict[str, Path] = {}
    by_name_size: dict[tuple[str, int], list[Path]] = defaultdict(list)
    n = 0
    t0 = time.time()
    for path in files_dir.rglob('*'):
        if not path.is_file():
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size <= 0:
            continue
        rel = path.relative_to(files_dir).as_posix().lower()
        by_rel[rel] = path
        by_name_size[(path.name.lower(), size)].append(path)
        n += 1
        if n % 10000 == 0:
            logger.info('indexed %s files (%.0fs)', n, time.time() - t0)
    logger.info('index ready files=%s (%.0fs)', n, time.time() - t0)
    return by_rel, by_name_size


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root', type=Path, default=None)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--limit', type=int, default=None)
    ap.add_argument(
        '--media',
        choices=['all', 'photos', 'videos'],
        default='all',
    )
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s'
    )

    cfg = Config.from_env(args.root)
    db = ManifestDB(cfg.db_path)

    media_filter = ''
    if args.media in {'photo', 'photos'}:
        media_filter = " AND COALESCE(media_type, '') NOT LIKE 'video/%'"
    elif args.media in {'video', 'videos'}:
        media_filter = " AND COALESCE(media_type, '') LIKE 'video/%'"

    rows = list(
        db.conn.execute(
            f"""
            SELECT remote_id, name, remote_path, size, md5, media_type, year, month,
                   local_path, verify_status, download_status
            FROM objects
            WHERE verify_status != 'verified'
               OR download_status IN ('pending', 'failed', 'failed_permanent')
            {media_filter}
            """
        )
    )
    if args.limit is not None:
        rows = rows[: args.limit]

    logger.info('Candidates=%s; indexing %s', len(rows), cfg.files_dir)
    by_rel, by_name_size = _index_files(cfg.files_dir)

    linked = 0
    linked_bytes = 0
    missing = 0
    bad = 0
    ambiguous = 0
    pending_updates: list[tuple[str, str, None]] = []

    def flush() -> None:
        if not pending_updates or args.dry_run:
            pending_updates.clear()
            return
        now = time.time()
        db.conn.executemany(
            """
            UPDATE objects SET
                local_path=?,
                download_status='verified',
                verify_status='verified',
                error=NULL,
                verified_at=?,
                updated_at=?
            WHERE remote_id=?
            """,
            [
                (local_path, now, now, remote_id)
                for remote_id, local_path, _ in pending_updates
            ],
        )
        db.conn.commit()
        pending_updates.clear()

    try:
        for row in rows:
            size = int(row['size'] or 0)
            found: Path | None = None

            if row['local_path']:
                lp = Path(row['local_path'])
                try:
                    if lp.is_file() and (size <= 0 or lp.stat().st_size == size):
                        found = lp
                except OSError:
                    found = None

            if found is None:
                expected = local_path_for(row, cfg.files_dir)
                if expected.is_file():
                    ok, _reason, _ = verify_local_file(
                        expected,
                        expected_size=row['size'],
                        expected_md5=row['md5'],
                    )
                    if ok:
                        found = expected
                if found is None:
                    rel = expected.relative_to(cfg.files_dir).as_posix().lower()
                    cand = by_rel.get(rel)
                    if cand is not None:
                        ok, _reason, _ = verify_local_file(
                            cand,
                            expected_size=row['size'],
                            expected_md5=row['md5'],
                        )
                        if ok:
                            found = cand

            if found is None and size > 0:
                matches = by_name_size.get((str(row['name']).lower(), size), [])
                valid_matches: list[Path] = []
                for cand in matches:
                    ok, _reason, _ = verify_local_file(
                        cand,
                        expected_size=row['size'],
                        expected_md5=row['md5'],
                    )
                    if ok:
                        valid_matches.append(cand)
                if len(valid_matches) == 1:
                    found = valid_matches[0]
                elif len(valid_matches) > 1:
                    matches = valid_matches
                elif len(matches) == 1:
                    ok, _reason, _ = verify_local_file(
                        matches[0],
                        expected_size=row['size'],
                        expected_md5=row['md5'],
                    )
                    if ok:
                        found = matches[0]
                if found is None and len(matches) > 1:
                    # Prefer year/month subdir match when available.
                    prefer = (
                        year_month_subdir(row['year'], row['month']).as_posix().lower()
                    )
                    narrowed = [
                        p
                        for p in matches
                        if prefer in p.relative_to(cfg.files_dir).as_posix().lower()
                    ]
                    if len(narrowed) == 1:
                        found = narrowed[0]
                    else:
                        ambiguous += 1
                        continue

            if found is None:
                missing += 1
                continue

            try:
                if sniff_is_error_page(found):
                    bad += 1
                    continue
            except OSError:
                bad += 1
                continue

            pending_updates.append((row['remote_id'], str(found), None))
            linked += 1
            linked_bytes += size
            if len(pending_updates) >= 500:
                flush()
            if linked % 2000 == 0:
                logger.info(
                    'progress linked=%s (%.1f GB) missing=%s bad=%s ambiguous=%s',
                    linked,
                    linked_bytes / 1e9,
                    missing,
                    bad,
                    ambiguous,
                )
        flush()
    finally:
        try:
            flush()
        except Exception:
            logger.exception('final flush failed')
        db.close()

    logger.info(
        'Done linked=%s (%.2f GB) missing=%s bad=%s ambiguous=%s dry_run=%s',
        linked,
        linked_bytes / 1e9,
        missing,
        bad,
        ambiguous,
        args.dry_run,
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
