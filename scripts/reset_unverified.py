"""Reset non-verified objects back to pending for another download pass."""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from yandex_photo_backup.config import Config  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root', type=Path, default=None)
    ap.add_argument('--media', choices=('photos', 'videos', 'all'), default='all')
    args = ap.parse_args()
    cfg = Config.from_env(args.root)
    media = args.media
    if media == 'photos':
        where = "verify_status != 'verified' AND COALESCE(media_type, '') NOT LIKE 'video/%'"
    elif media == 'videos':
        where = "verify_status != 'verified' AND COALESCE(media_type, '') LIKE 'video/%'"
    else:
        where = "verify_status != 'verified'"

    db = sqlite3.connect(cfg.db_path, timeout=120)
    cur = db.execute(
        f"""
        UPDATE objects
        SET download_status = 'pending',
            verify_status = 'pending',
            attempts = 0,
            error = NULL,
            updated_at = ?
        WHERE {where}
        """,
        (time.time(),),
    )
    db.commit()
    pending = db.execute(
        "SELECT COUNT(*) FROM objects WHERE download_status IN ('pending', 'failed')"
    ).fetchone()[0]
    print(f'reset={cur.rowcount} pending_or_failed_total={pending}')
    db.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())