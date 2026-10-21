"""Print photo vs video backup progress (for health checks)."""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

DB = Path(os.environ.get('YANDEX_PHOTO_ROOT', Path.home() / 'YandexPhoto')) / (
    'yandex_photo_backup.sqlite'
)


def main() -> int:
    if not DB.exists():
        print(f'DB not found: {DB}', file=sys.stderr)
        print('Set YANDEX_PHOTO_ROOT or run collect first.', file=sys.stderr)
        return 1
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = list(
        conn.execute(
            """
            SELECT
              CASE
                WHEN COALESCE(media_type, '') LIKE 'video/%' THEN 'video'
                ELSE 'photo'
              END AS kind,
              SUM(CASE WHEN verify_status = 'verified' THEN 1 ELSE 0 END) AS verified,
              SUM(
                CASE WHEN download_status IN ('pending', 'failed') THEN 1 ELSE 0 END
              ) AS pending,
              SUM(
                CASE WHEN download_status = 'failed_permanent' THEN 1 ELSE 0 END
              ) AS perm_fail,
              COUNT(*) AS total,
              COALESCE(
                SUM(CASE WHEN verify_status = 'verified' THEN size ELSE 0 END), 0
              ) AS verified_bytes,
              COALESCE(
                SUM(
                  CASE
                    WHEN download_status IN ('pending', 'failed') THEN size
                    ELSE 0
                  END
                ),
                0
              ) AS pending_bytes
            FROM objects
            GROUP BY 1
            ORDER BY kind
            """
        )
    )
    for r in rows:
        gb = r['verified_bytes'] / (1024**3)
        pend_gb = r['pending_bytes'] / (1024**3)
        pct = 100.0 * r['verified'] / r['total'] if r['total'] else 0.0
        print(
            f"{r['kind']}: verified={r['verified']}/{r['total']} ({pct:.2f}%) "
            f"{gb:.2f}GB pending={r['pending']} ({pend_gb:.2f}GB) "
            f"perm_fail={r['perm_fail']}"
        )

    photo_md5 = conn.execute(
        """
        SELECT COUNT(*) FROM objects
        WHERE COALESCE(media_type, '') NOT LIKE 'video/%'
          AND download_status IN ('pending', 'failed')
          AND md5 IS NOT NULL AND md5 != ''
        """
    ).fetchone()[0]
    photo_no_md5 = conn.execute(
        """
        SELECT COUNT(*) FROM objects
        WHERE COALESCE(media_type, '') NOT LIKE 'video/%'
          AND download_status IN ('pending', 'failed')
          AND (md5 IS NULL OR md5 = '')
        """
    ).fetchone()[0]
    print(f'photo_pending_with_md5={photo_md5}')
    print(f'photo_pending_no_md5={photo_no_md5}')

    photo_pending = next((r['pending'] for r in rows if r['kind'] == 'photo'), 0)
    if photo_pending == 0:
        print('PHOTOS_DONE=1')
    else:
        print('PHOTOS_DONE=0')
    conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
