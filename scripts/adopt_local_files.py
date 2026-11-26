"""Mark manifest rows verified when a matching file exists on disk.

Searches backup tree and optional extra folders (Downloads, Yandex.Disk).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from yandex_photo_backup.config import Config  # noqa: E402
from yandex_photo_backup.db import ManifestDB  # noqa: E402
from yandex_photo_backup.downloader import local_path_for  # noqa: E402
from yandex_photo_backup.verify import md5_file, verify_local_file  # noqa: E402

logger = logging.getLogger('adopt_local_files')


def _index_names(roots: list[Path]) -> dict[str, list[Path]]:
    by_name: dict[str, list[Path]] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob('*'):
            if not path.is_file():
                continue
            try:
                if path.stat().st_size <= 0:
                    continue
            except OSError:
                continue
            by_name.setdefault(path.name.lower(), []).append(path)
    return by_name


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root', type=Path, default=None)
    ap.add_argument(
        '--scan',
        type=Path,
        action='append',
        default=None,
        help='Extra directories to search (repeatable)',
    )
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format='%(message)s')
    cfg = Config.from_env(args.root)
    extra = [Path(p) for p in (args.scan or [])]
    roots = [cfg.files_dir, *extra]
    by_name = _index_names(roots)

    db = ManifestDB(cfg.db_path)
    rows = list(
        db.conn.execute(
            """
            SELECT remote_id, name, size, md5, year, month
            FROM objects WHERE verify_status != 'verified'
            """
        )
    )

    linked = 0
    copied = 0
    for row in rows:
        expected = local_path_for(row, cfg.files_dir)
        for cand in [expected, *by_name.get(str(row['name']).lower(), [])]:
            if not cand.is_file():
                continue
            try:
                ok, reason, actual_md5 = verify_local_file(
                    cand,
                    expected_size=row['size'],
                    expected_md5=row['md5'],
                )
            except OSError as exc:
                logger.debug('skip unreadable %s: %s', cand, exc)
                continue
            if not ok:
                continue
            dest = expected
            if cand.resolve() != dest.resolve():
                if args.dry_run:
                    logger.info('DRY-RUN copy %s -> %s', cand, dest)
                    copied += 1
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if dest.exists():
                        dest.unlink(missing_ok=True)
                    dest.write_bytes(cand.read_bytes())
                    actual_md5 = md5_file(dest)
            if not args.dry_run:
                db.mark_verified(row['remote_id'], str(dest), local_md5=actual_md5)
            linked += 1
            logger.info('VERIFIED %s (%s)', row['name'], dest)
            break
    db.close()
    logger.info('Done verified=%s copied_to_backup=%s', linked, copied)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())